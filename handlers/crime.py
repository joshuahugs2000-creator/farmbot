"""
Système criminalité :
/rob        — voler un joueur (reply ou @)
/rebet      — pari quitte ou double infini
/police     — appeler la police sur un voleur récent
/juge       — juger quelqu'un pour un crime récent
/security   — acheter une protection contre les vols
"""
import random
import logging
import asyncio
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from handlers.journal import log_event
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from sqlalchemy import select, text

from database.db import (
    AsyncSessionLocal, get_user, add_coins, transfer_coins,
)
from database.models import User
from utils.helpers import ensure_user, parse_target, mention

logger = logging.getLogger(__name__)

# ─── CONSTANTES ──────────────────────────────────────────────────────────────

SECURITY_AGENCIES = [
    {"id": "basic",   "name": "🔵 Garde Basique",   "protection": 0.20, "price": 50_000},
    {"id": "private", "name": "🟡 Agent Privé",      "protection": 0.40, "price": 500_000},
    {"id": "elite",   "name": "🟠 Équipe d'Élite",   "protection": 0.65, "price": 5_000_000},
    {"id": "total",   "name": "🔴 Blindage Total",   "protection": 0.85, "price": 25_000_000},
    {"id": "fort",    "name": "⚫ Forteresse",        "protection": 0.99, "price": 100_000_000},
]

# Durée maximale en prison selon la somme volée
def _prison_duration(amount: int) -> int:
    """Retourne la durée en prison (minutes) selon la somme volée."""
    if amount < 10_000:
        return random.randint(5, 15)
    elif amount < 100_000:
        return random.randint(15, 45)
    elif amount < 1_000_000:
        return random.randint(45, 120)
    elif amount < 10_000_000:
        return random.randint(120, 360)
    else:
        return random.randint(360, 720)

def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")

# ─── MIGRATION DB ─────────────────────────────────────────────────────────────

async def init_crime_tables():
    """Crée les tables nécessaires pour le système crime."""
    async with AsyncSessionLocal() as session:
        migrations = [
            # Historique des vols (pour police + juge)
            """CREATE TABLE IF NOT EXISTS crime_rob_log (
                id          SERIAL PRIMARY KEY,
                robber_id   BIGINT NOT NULL,
                victim_id   BIGINT NOT NULL,
                group_id    BIGINT NOT NULL,
                amount      BIGINT NOT NULL,
                success     BOOLEAN NOT NULL,
                created_at  TIMESTAMP DEFAULT NOW()
            )""",
            # Prison
            """CREATE TABLE IF NOT EXISTS crime_prison (
                id            SERIAL PRIMARY KEY,
                user_id       BIGINT NOT NULL UNIQUE,
                group_id      BIGINT NOT NULL,
                amount_stolen BIGINT NOT NULL,
                bail_amount   BIGINT NOT NULL,
                released_at   TIMESTAMP NOT NULL,
                created_at    TIMESTAMP DEFAULT NOW()
            )""",
            # Rebet (parties en cours)
            """CREATE TABLE IF NOT EXISTS crime_rebet (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT NOT NULL UNIQUE,
                group_id    BIGINT NOT NULL,
                message_id  BIGINT,
                pot         BIGINT NOT NULL,
                round       INTEGER DEFAULT 1,
                created_at  TIMESTAMP DEFAULT NOW()
            )""",
            # Sécurité
            """CREATE TABLE IF NOT EXISTS crime_security (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT NOT NULL UNIQUE,
                agency_id  VARCHAR(20) NOT NULL,
                bought_at  TIMESTAMP DEFAULT NOW()
            )""",
            # Jugements en attente
            """CREATE TABLE IF NOT EXISTS crime_judgment (
                id           SERIAL PRIMARY KEY,
                accused_id   BIGINT NOT NULL UNIQUE,
                accuser_id   BIGINT NOT NULL,
                group_id     BIGINT NOT NULL,
                crime_id     INTEGER,
                amount       BIGINT NOT NULL,
                message_id   BIGINT,
                created_at   TIMESTAMP DEFAULT NOW()
            )""",
        ]
        for sql in migrations:
            try:
                await session.execute(text(sql))
            except Exception:
                pass
        await session.commit()


# ─── HELPERS DB ───────────────────────────────────────────────────────────────

async def _get_security(session, user_id: int):
    r = await session.execute(
        text("SELECT agency_id FROM crime_security WHERE user_id = :uid"),
        {"uid": user_id}
    )
    return r.fetchone()


async def _get_prison(session, user_id: int):
    r = await session.execute(
        text("SELECT * FROM crime_prison WHERE user_id = :uid"),
        {"uid": user_id}
    )
    return r.fetchone()


async def _is_in_prison(session, user_id: int) -> bool:
    row = await _get_prison(session, user_id)
    if not row:
        return False
    if datetime.utcnow() >= row.released_at:
        # Peine terminée, on libère automatiquement
        await session.execute(
            text("DELETE FROM crime_prison WHERE user_id = :uid"),
            {"uid": user_id}
        )
        await session.commit()
        return False
    return True


async def _prison_block_message(update: Update, session, user_id: int) -> bool:
    """
    Vérifie si l'utilisateur est en prison et envoie un message de blocage.
    Retourne True si bloqué, False sinon.
    """
    if await _is_in_prison(session, user_id):
        prison = await _get_prison(session, user_id)
        minutes_left = max(0, int((prison.released_at - datetime.utcnow()).total_seconds() / 60))
        h = minutes_left // 60
        m = minutes_left % 60
        duration_str = f"{h}h{m:02d}m" if h > 0 else f"{m} minute(s)"
        await update.message.reply_text(
            f"🔒 <b>Tu es en prison !</b>\n\n"
            f"Tu ne peux utiliser aucune commande du bot tant que tu es incarcéré.\n"
            f"⏳ Libération dans : <b>{duration_str}</b>\n"
            f"💸 Caution : <b>{_fmt(prison.bail_amount)} 💰</b>\n\n"
            f"Demande à quelqu'un de payer ta caution avec <code>/bail @toi</code>",
            parse_mode=ParseMode.HTML
        )
        return True
    return False


async def _get_recent_rob(session, robber_id: int, group_id: int, minutes: int = 5):
    """Retourne le dernier vol réussi d'un joueur dans les N dernières minutes."""
    cutoff = datetime.utcnow() - timedelta(minutes=minutes)
    r = await session.execute(
        text("""SELECT * FROM crime_rob_log
                WHERE robber_id = :rid AND group_id = :gid
                AND success = TRUE AND created_at >= :cutoff
                ORDER BY created_at DESC LIMIT 1"""),
        {"rid": robber_id, "gid": group_id, "cutoff": cutoff}
    )
    return r.fetchone()


async def _get_crimes_last_7days(session, user_id: int):
    """Retourne tous les crimes d'un joueur dans les 7 derniers jours."""
    cutoff = datetime.utcnow() - timedelta(days=7)
    r = await session.execute(
        text("""SELECT * FROM crime_rob_log
                WHERE robber_id = :uid AND success = TRUE
                AND created_at >= :cutoff
                ORDER BY created_at DESC"""),
        {"uid": user_id, "cutoff": cutoff}
    )
    return r.fetchall()


# ─── /rob ─────────────────────────────────────────────────────────────────────

async def rob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("❌ Cette commande est réservée aux groupes.")
        return

    robber_tg = update.effective_user
    await ensure_user(robber_tg)

    async with AsyncSessionLocal() as session:
        # Vérifier si le voleur est en prison
        if await _prison_block_message(update, session, robber_tg.id):
            return

        # Trouver la cible
        target_tg = await parse_target(update, context)
        if not target_tg:
            await update.message.reply_text(
                "❌ Réponds au message de quelqu'un ou utilise <b>@pseudo</b> pour le voler.",
                parse_mode=ParseMode.HTML
            )
            return

        if target_tg.id == robber_tg.id:
            await update.message.reply_text("😂 Tu ne peux pas te voler toi-même !")
            return

        await ensure_user(target_tg)

        # Charger les deux users
        robber = await get_user(session, robber_tg.id)
        victim = await get_user(session, target_tg.id)

        if not victim:
            await update.message.reply_text("❌ Cette personne n'a pas de compte.")
            return

        if victim.coins <= 0:
            await update.message.reply_text(
                f"💸 {target_tg.first_name} n'a rien à voler !"
            )
            return

        # Vérifier la sécurité de la victime
        sec_row = await _get_security(session, target_tg.id)
        if sec_row:
            agency = next((a for a in SECURITY_AGENCIES if a["id"] == sec_row.agency_id), None)
            if agency and random.random() < agency["protection"]:
                await update.message.reply_text(
                    f"🛡️ La sécurité de {mention(victim)} a repoussé ton attaque !\n"
                    f"Tu t'es enfui en courant... 🏃",
                    parse_mode=ParseMode.HTML
                )
                return

        # Montant à voler : entre 5% et 30% des coins de la victime
        steal_pct = random.uniform(0.05, 0.30)
        amount = max(1, int(victim.coins * steal_pct))

        # Chance de succès : 45% (55% d'échec = plus de risque)
        success = random.random() < 0.45

        group_id = update.effective_chat.id

        if success:
            # ── Vol réussi — le voleur s'échappe avec le butin ──────────────
            # Retirer les coins de la victime
            await session.execute(
                text("UPDATE users SET coins = GREATEST(CAST(0 AS BIGINT), CAST(coins AS BIGINT) - CAST(:amt AS BIGINT)) WHERE user_id = :uid"),
                {"amt": amount, "uid": victim.user_id}
            )
            # Donner les coins au voleur
            await session.execute(
                text("UPDATE users SET coins = CAST(coins AS BIGINT) + CAST(:amt AS BIGINT) WHERE user_id = :uid"),
                {"amt": amount, "uid": robber.user_id}
            )

            # Logger le vol réussi
            await session.execute(
                text("""INSERT INTO crime_rob_log (robber_id, victim_id, group_id, amount, success)
                        VALUES (:rid, :vid, :gid, :amt, TRUE)"""),
                {"rid": robber_tg.id, "vid": target_tg.id, "gid": group_id, "amt": amount}
            )
            await session.commit()

            # Scénario aléatoire de fuite
            fuites = [
                "s'est éclipsé dans l'ombre avant que quiconque réagisse",
                "a disparu dans la foule introuvable",
                "a pris la fuite à toute vitesse",
                "a utilisé une sortie secrète",
                "s'est fondu dans la nature sans laisser de trace",
            ]
            import random as _r
            fuite = _r.choice(fuites)

            await update.message.reply_text(
                f"🦹 <b>VOL RÉUSSI !</b>\n\n"
                f"💰 <b>{robber_tg.first_name}</b> a volé <b>{_fmt(amount)} $</b> à {mention(victim)} !\n\n"
                f"🏃 Il {fuite}.\n\n"
                f"😤 {mention(victim)} : <i>-{_fmt(amount)} $</i>\n"
                f"😈 {robber_tg.first_name} : <i>+{_fmt(amount)} $</i>",
                parse_mode=ParseMode.HTML
            )
        else:
            # ── Vol raté ────────────────────────────────────────────────────
            bail = amount * 2
            released_at = datetime.utcnow() + timedelta(minutes=_prison_duration(amount))

            await session.execute(
                text("""INSERT INTO crime_rob_log (robber_id, victim_id, group_id, amount, success)
                        VALUES (:rid, :vid, :gid, :amt, FALSE)"""),
                {"rid": robber_tg.id, "vid": target_tg.id, "gid": group_id, "amt": amount}
            )

            # Emprisonner directement
            await session.execute(
                text("""INSERT INTO crime_prison (user_id, group_id, amount_stolen, bail_amount, released_at)
                        VALUES (:uid, :gid, :amt, :bail, :rel)
                        ON CONFLICT (user_id) DO UPDATE
                        SET group_id=:gid, amount_stolen=:amt, bail_amount=:bail, released_at=:rel"""),
                {"uid": robber_tg.id, "gid": group_id, "amt": amount,
                 "bail": bail, "rel": released_at}
            )
            await session.commit()

            minutes_prison = int((released_at - datetime.utcnow()).total_seconds() / 60)

            await update.message.reply_text(
                f"🚔 <b>{robber_tg.first_name}</b> s'est fait attraper !\n\n"
                f"Il tentait de voler <b>{_fmt(amount)} 💰</b> à {mention(victim)}.\n"
                f"🔒 En prison pour <b>{minutes_prison} minutes</b>.\n"
                f"💸 Caution : <b>{_fmt(bail)} 💰</b> (payable par quelqu'un d'autre)\n\n"
                f"⛔ Toutes les commandes du bot sont bloquées jusqu'à libération.",
                parse_mode=ParseMode.HTML
            )


# ─── /police ──────────────────────────────────────────────────────────────────

async def police(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("❌ Cette commande est réservée aux groupes.")
        return

    caller_tg = update.effective_user
    await ensure_user(caller_tg)

    async with AsyncSessionLocal() as session:
        # Bloquer si l'appelant est en prison
        if await _prison_block_message(update, session, caller_tg.id):
            return

    target_tg = await parse_target(update, context)
    if not target_tg:
        await update.message.reply_text(
            "❌ Réponds au message du suspect ou utilise <b>@pseudo</b>.",
            parse_mode=ParseMode.HTML
        )
        return

    if target_tg.id == caller_tg.id:
        await update.message.reply_text("😅 Tu ne peux pas appeler la police sur toi-même !")
        return

    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        await ensure_user(target_tg)
        suspect = await get_user(session, target_tg.id)
        if not suspect:
            await update.message.reply_text("❌ Joueur introuvable.")
            return

        # Vérifier si déjà en prison
        if await _is_in_prison(session, target_tg.id):
            await update.message.reply_text(
                f"🔒 {mention(suspect)} est déjà en prison !",
                parse_mode=ParseMode.HTML
            )
            return

        # Chercher un vol récent (5 dernières minutes)
        recent_rob = await _get_recent_rob(session, target_tg.id, group_id, minutes=5)
        if not recent_rob:
            await update.message.reply_text(
                f"🤷 Aucun vol signalé pour {mention(suspect)} dans les 5 dernières minutes.",
                parse_mode=ParseMode.HTML
            )
            return

        amount = recent_rob.amount
        victim_id = recent_rob.victim_id

        # Intervention de la police : 60% de chance d'attraper
        caught = random.random() < 0.60

        if caught:
            bail = amount * 2
            released_at = datetime.utcnow() + timedelta(minutes=_prison_duration(amount))

            # Rembourser la victime
            victim = await get_user(session, victim_id)
            refund_msg = ""
            if victim:
                await session.execute(
                    text("UPDATE users SET coins = GREATEST(CAST(0 AS BIGINT), CAST(coins AS BIGINT) - CAST(:amt AS BIGINT)) WHERE user_id = :uid"),
                    {"amt": amount, "uid": suspect.user_id}
                )
                await session.execute(
                    text("UPDATE users SET coins = CAST(coins AS BIGINT) + CAST(:amt AS BIGINT) WHERE user_id = :uid"),
                    {"amt": amount, "uid": victim_id}
                )
                await session.commit()
                refund_msg = f"💸 Les <b>{_fmt(amount)} 💰</b> volés ont été restitués à {mention(victim)}.\n"
            else:
                await session.commit()

            # Emprisonner
            await session.execute(
                text("""INSERT INTO crime_prison (user_id, group_id, amount_stolen, bail_amount, released_at)
                        VALUES (:uid, :gid, :amt, :bail, :rel)
                        ON CONFLICT (user_id) DO UPDATE
                        SET group_id=:gid, amount_stolen=:amt, bail_amount=:bail, released_at=:rel"""),
                {"uid": target_tg.id, "gid": group_id, "amt": amount,
                 "bail": bail, "rel": released_at}
            )
            await session.commit()

            minutes_prison = int((released_at - datetime.utcnow()).total_seconds() / 60)

            await update.message.reply_text(
                f"🚔 La police a intercepté {mention(suspect)} !\n\n"
                f"{refund_msg}"
                f"🔒 {mention(suspect)} est en prison pour <b>{minutes_prison} minutes</b>.\n"
                f"💸 Caution : <b>{_fmt(bail)} 💰</b> (payable par n'importe qui)\n\n"
                f"⛔ Toutes ses commandes sont bloquées jusqu'à libération.",
                parse_mode=ParseMode.HTML
            )
        else:
            await update.message.reply_text(
                f"💨 {mention(suspect)} a réussi à fuir la police !\n"
                f"Les agents n'ont pas pu l'attraper à temps...",
                parse_mode=ParseMode.HTML
            )


# ─── /bail (payer la caution de quelqu'un) ────────────────────────────────────

async def bail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("❌ Cette commande est réservée aux groupes.")
        return

    payer_tg = update.effective_user
    await ensure_user(payer_tg)

    target_tg = await parse_target(update, context)
    if not target_tg:
        await update.message.reply_text(
            "❌ Réponds au message du prisonnier ou utilise <b>@pseudo</b>.\nEx: <code>/bail @pseudo</code>",
            parse_mode=ParseMode.HTML
        )
        return

    if target_tg.id == payer_tg.id:
        await update.message.reply_text(
            "❌ Tu ne peux pas payer ta propre caution ! Demande à quelqu'un d'autre."
        )
        return

    async with AsyncSessionLocal() as session:
        # Le payeur peut être en prison lui-même, c'est OK (quelqu'un paie pour un autre)
        await ensure_user(target_tg)
        prisoner = await get_user(session, target_tg.id)
        payer = await get_user(session, payer_tg.id)

        prison_row = await _get_prison(session, target_tg.id)
        if not prison_row:
            await update.message.reply_text(
                f"✅ {target_tg.first_name} n'est pas en prison !"
            )
            return

        # Vérifier si la peine est déjà expirée
        if datetime.utcnow() >= prison_row.released_at:
            await session.execute(
                text("DELETE FROM crime_prison WHERE user_id = :uid"),
                {"uid": target_tg.id}
            )
            await session.commit()
            await update.message.reply_text(
                f"✅ {target_tg.first_name} vient d'être libéré automatiquement, sa peine était terminée !"
            )
            return

        bail_amount = prison_row.bail_amount

        if payer.coins < bail_amount:
            await update.message.reply_text(
                f"❌ Tu n'as pas assez d'argent !\n"
                f"Caution : <b>{_fmt(bail_amount)} 💰</b>\n"
                f"Ton solde : <b>{_fmt(payer.coins)} 💰</b>",
                parse_mode=ParseMode.HTML
            )
            return

        # Payer la caution
        await session.execute(
            text("UPDATE users SET coins = CAST(coins AS BIGINT) - CAST(:amt AS BIGINT) WHERE user_id = :uid"),
            {"amt": bail_amount, "uid": payer.user_id}
        )

        # Libérer le prisonnier
        await session.execute(
            text("DELETE FROM crime_prison WHERE user_id = :uid"),
            {"uid": target_tg.id}
        )
        await session.commit()

        await update.message.reply_text(
            f"🔓 {mention(payer)} a payé la caution de {mention(prisoner)} !\n"
            f"💸 <b>{_fmt(bail_amount)} 💰</b> dépensés.\n"
            f"🆓 {mention(prisoner)} est libre et peut à nouveau utiliser toutes les commandes !",
            parse_mode=ParseMode.HTML
        )


# ─── /juge ────────────────────────────────────────────────────────────────────

# Dict pour tracker les tâches de timeout de jugement {accused_id: asyncio.Task}
_judgment_timeouts: dict = {}


async def _auto_guilty(accused_id: int, chat_id: int, message_id: int, bot):
    """Déclare automatiquement l'accusé coupable après 60 secondes sans réponse."""
    await asyncio.sleep(60)

    async with AsyncSessionLocal() as session:
        r = await session.execute(
            text("SELECT * FROM crime_judgment WHERE accused_id = :uid"),
            {"uid": accused_id}
        )
        judgment = r.fetchone()
        if not judgment:
            return  # Déjà traité

        accused = await get_user(session, accused_id)
        amount = judgment.amount
        group_id = judgment.group_id

        # Supprimer le jugement
        await session.execute(
            text("DELETE FROM crime_judgment WHERE accused_id = :uid"),
            {"uid": accused_id}
        )

        # Peine normale (pas de réduction car silence = coupable)
        base_duration = _prison_duration(amount)
        released_at = datetime.utcnow() + timedelta(minutes=base_duration)
        bail = amount * 2

        await session.execute(
            text("""INSERT INTO crime_prison (user_id, group_id, amount_stolen, bail_amount, released_at)
                    VALUES (:uid, :gid, :amt, :bail, :rel)
                    ON CONFLICT (user_id) DO UPDATE
                    SET group_id=:gid, amount_stolen=:amt, bail_amount=:bail, released_at=:rel"""),
            {"uid": accused_id, "gid": group_id, "amt": amount, "bail": bail, "rel": released_at}
        )
        await session.commit()

        accused_mention = mention(accused) if accused else f"<a href='tg://user?id={accused_id}'>Accusé</a>"
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=(
                    f"⚖️ <b>VERDICT — SILENCE = COUPABLE</b>\n\n"
                    f"⏰ {accused_mention} n'a pas répondu dans les 60 secondes.\n\n"
                    f"🔒 Déclaré(e) <b>COUPABLE</b> par défaut.\n"
                    f"Prison : <b>{base_duration} minutes</b>\n"
                    f"💸 Caution : <b>{_fmt(bail)} 💰</b>\n\n"
                    f"⛔ Toutes ses commandes sont bloquées jusqu'à libération."
                ),
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    _judgment_timeouts.pop(accused_id, None)


async def juge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("❌ Cette commande est réservée aux groupes.")
        return

    accuser_tg = update.effective_user
    await ensure_user(accuser_tg)

    async with AsyncSessionLocal() as session:
        if await _prison_block_message(update, session, accuser_tg.id):
            return

    target_tg = await parse_target(update, context)
    if not target_tg:
        await update.message.reply_text(
            "❌ Réponds au message de l'accusé ou utilise <b>@pseudo</b>.\nEx: <code>/juge @pseudo</code>",
            parse_mode=ParseMode.HTML
        )
        return

    if target_tg.id == accuser_tg.id:
        await update.message.reply_text("⚖️ Tu ne peux pas te juger toi-même !")
        return

    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        await ensure_user(target_tg)
        accused = await get_user(session, target_tg.id)
        if not accused:
            await update.message.reply_text("❌ Joueur introuvable.")
            return

        # Vérifier si déjà en prison
        if await _is_in_prison(session, target_tg.id):
            await update.message.reply_text(
                f"🔒 {mention(accused)} est déjà en prison, il sera jugé à sa sortie.",
                parse_mode=ParseMode.HTML
            )
            return

        # Vérifier si un jugement est déjà en cours
        r = await session.execute(
            text("SELECT id FROM crime_judgment WHERE accused_id = :uid"),
            {"uid": target_tg.id}
        )
        if r.fetchone():
            await update.message.reply_text(
                f"⏳ Un jugement est déjà en cours pour {mention(accused)}.",
                parse_mode=ParseMode.HTML
            )
            return

        # Chercher les crimes des 7 derniers jours
        crimes = await _get_crimes_last_7days(session, target_tg.id)

        if not crimes:
            await update.message.reply_text(
                f"⚖️ Après vérification, {mention(accused)} n'a commis aucun crime "
                f"au cours des 7 derniers jours.\n\n✅ Dossier vide.",
                parse_mode=ParseMode.HTML
            )
            return

        # Prendre le crime le plus récent
        latest_crime = crimes[0]
        total_stolen = sum(c.amount for c in crimes)

        # Créer le jugement en attente
        await session.execute(
            text("""INSERT INTO crime_judgment
                    (accused_id, accuser_id, group_id, crime_id, amount)
                    VALUES (:acc, :acr, :gid, :cid, :amt)
                    ON CONFLICT (accused_id) DO UPDATE
                    SET accuser_id=:acr, group_id=:gid, crime_id=:cid, amount=:amt"""),
            {"acc": target_tg.id, "acr": accuser_tg.id, "gid": group_id,
             "cid": latest_crime.id, "amt": total_stolen}
        )
        await session.commit()

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Coupable", callback_data=f"juge:guilty:{target_tg.id}"),
                InlineKeyboardButton("❌ Non Coupable", callback_data=f"juge:notguilty:{target_tg.id}"),
            ]
        ])

        nb_crimes = len(crimes)
        msg = await update.message.reply_text(
            f"⚖️ <b>CONVOCATION AU TRIBUNAL</b>\n\n"
            f"L'accusé {mention(accused)} est convoqué devant le juge.\n\n"
            f"📋 <b>Dossier :</b> {nb_crimes} vol(s) au cours des 7 derniers jours\n"
            f"💰 <b>Total dérobé :</b> {_fmt(total_stolen)} 💰\n\n"
            f"Comment plaidez-vous ? ⏰ <b>60 secondes pour répondre</b>",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )

        # Annuler un timeout précédent s'il existe
        if target_tg.id in _judgment_timeouts:
            _judgment_timeouts[target_tg.id].cancel()

        # Lancer le timeout de 60 secondes
        task = asyncio.create_task(
            _auto_guilty(target_tg.id, update.effective_chat.id, msg.message_id, context.bot)
        )
        _judgment_timeouts[target_tg.id] = task


async def juge_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 3:
        return

    _, verdict, accused_id_str = parts
    accused_id = int(accused_id_str)

    # Seul l'accusé peut répondre
    if query.from_user.id != accused_id:
        await query.answer("⚖️ Seul l'accusé peut plaider !", show_alert=True)
        return

    # Annuler le timeout automatique puisque l'accusé a répondu
    if accused_id in _judgment_timeouts:
        _judgment_timeouts[accused_id].cancel()
        _judgment_timeouts.pop(accused_id, None)

    async with AsyncSessionLocal() as session:
        # Récupérer le jugement
        r = await session.execute(
            text("SELECT * FROM crime_judgment WHERE accused_id = :uid"),
            {"uid": accused_id}
        )
        judgment = r.fetchone()
        if not judgment:
            await query.edit_message_text("⚖️ Ce jugement n'est plus actif.")
            return

        accused = await get_user(session, accused_id)
        accuser = await get_user(session, judgment.accuser_id)
        amount = judgment.amount
        group_id = judgment.group_id

        # Supprimer le jugement en attente
        await session.execute(
            text("DELETE FROM crime_judgment WHERE accused_id = :uid"),
            {"uid": accused_id}
        )

        if verdict == "guilty":
            # Peine réduite : 50% de la durée normale
            base_duration = _prison_duration(amount)
            reduced = max(1, base_duration // 2)
            released_at = datetime.utcnow() + timedelta(minutes=reduced)
            bail = amount  # Caution normale (pas doublée car il a avoué)

            await session.execute(
                text("""INSERT INTO crime_prison (user_id, group_id, amount_stolen, bail_amount, released_at)
                        VALUES (:uid, :gid, :amt, :bail, :rel)
                        ON CONFLICT (user_id) DO UPDATE
                        SET group_id=:gid, amount_stolen=:amt, bail_amount=:bail, released_at=:rel"""),
                {"uid": accused_id, "gid": group_id, "amt": amount, "bail": bail, "rel": released_at}
            )
            await session.commit()

            accused_mention = mention(accused) if accused else f"<a href='tg://user?id={accused_id}'>Accusé</a>"
            await query.edit_message_text(
                f"⚖️ <b>VERDICT</b>\n\n"
                f"{accused_mention} a plaidé <b>COUPABLE</b>.\n\n"
                f"🔒 Peine réduite : <b>{reduced} minutes</b> de prison.\n"
                f"💸 Caution : <b>{_fmt(bail)} 💰</b>\n\n"
                f"⛔ Toutes ses commandes sont bloquées jusqu'à libération.",
                parse_mode=ParseMode.HTML
            )

        else:  # not guilty → jugement aléatoire
            roll = random.random()

            if roll < 0.40:
                # Acquitté
                await session.commit()
                accused_mention = mention(accused) if accused else f"<a href='tg://user?id={accused_id}'>Accusé</a>"
                await query.edit_message_text(
                    f"⚖️ <b>VERDICT</b>\n\n"
                    f"🎲 Le juge délibère...\n\n"
                    f"✅ {accused_mention} est <b>ACQUITTÉ(E)</b> !\n"
                    f"Faute de preuves suffisantes, l'accusé est libre.",
                    parse_mode=ParseMode.HTML
                )

            elif roll < 0.75:
                # Coupable, peine normale
                base_duration = _prison_duration(amount)
                released_at = datetime.utcnow() + timedelta(minutes=base_duration)
                bail = amount * 2

                await session.execute(
                    text("""INSERT INTO crime_prison (user_id, group_id, amount_stolen, bail_amount, released_at)
                            VALUES (:uid, :gid, :amt, :bail, :rel)
                            ON CONFLICT (user_id) DO UPDATE
                            SET group_id=:gid, amount_stolen=:amt, bail_amount=:bail, released_at=:rel"""),
                    {"uid": accused_id, "gid": group_id, "amt": amount, "bail": bail, "rel": released_at}
                )
                await session.commit()

                accused_mention = mention(accused) if accused else f"<a href='tg://user?id={accused_id}'>Accusé</a>"
                accuser_mention = mention(accuser) if accuser else "l'accusateur"

                await query.edit_message_text(
                    f"⚖️ <b>VERDICT</b>\n\n"
                    f"🎲 Le juge délibère...\n\n"
                    f"🔒 {accused_mention} est déclaré(e) <b>COUPABLE</b>.\n"
                    f"Prison : <b>{base_duration} minutes</b>\n"
                    f"💸 Caution : <b>{_fmt(bail)} 💰</b>\n\n"
                    f"_(La moitié de la caution ira à {accuser_mention} si elle est payée)_\n"
                    f"⛔ Toutes ses commandes sont bloquées jusqu'à libération.",
                    parse_mode=ParseMode.HTML
                )

            else:
                # Coupable, peine x2
                base_duration = _prison_duration(amount) * 2
                released_at = datetime.utcnow() + timedelta(minutes=base_duration)
                bail = amount * 2

                await session.execute(
                    text("""INSERT INTO crime_prison (user_id, group_id, amount_stolen, bail_amount, released_at)
                            VALUES (:uid, :gid, :amt, :bail, :rel)
                            ON CONFLICT (user_id) DO UPDATE
                            SET group_id=:gid, amount_stolen=:amt, bail_amount=:bail, released_at=:rel"""),
                    {"uid": accused_id, "gid": group_id, "amt": amount, "bail": bail, "rel": released_at}
                )
                await session.commit()

                accused_mention = mention(accused) if accused else f"<a href='tg://user?id={accused_id}'>Accusé</a>"
                accuser_mention = mention(accuser) if accuser else "l'accusateur"

                await query.edit_message_text(
                    f"⚖️ <b>VERDICT</b>\n\n"
                    f"🎲 Le juge délibère...\n\n"
                    f"😤 {accused_mention} a menti ! Peine <b>DOUBLÉE</b>.\n"
                    f"🔒 Prison : <b>{base_duration} minutes</b>\n"
                    f"💸 Caution : <b>{_fmt(bail)} 💰</b>\n\n"
                    f"_(La moitié de la caution ira à {accuser_mention} si elle est payée)_\n"
                    f"⛔ Toutes ses commandes sont bloquées jusqu'à libération.",
                    parse_mode=ParseMode.HTML
                )


# ─── /bail_from_judgment (payer caution avec split pour accusateur) ───────────

async def bail_judgment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Payer la caution d'un prisonnier jugé.
    La moitié va à l'accusateur si le jugement venait de /juge.
    Utilise /bailjuge @pseudo
    """
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("❌ Cette commande est réservée aux groupes.")
        return

    payer_tg = update.effective_user
    await ensure_user(payer_tg)

    target_tg = await parse_target(update, context)
    if not target_tg:
        await update.message.reply_text(
            "❌ Utilise <code>/bail @pseudo</code> pour payer la caution.",
            parse_mode=ParseMode.HTML
        )
        return

    if target_tg.id == payer_tg.id:
        await update.message.reply_text("❌ Tu ne peux pas payer ta propre caution !")
        return

    async with AsyncSessionLocal() as session:
        await ensure_user(target_tg)
        prisoner = await get_user(session, target_tg.id)
        payer = await get_user(session, payer_tg.id)

        prison_row = await _get_prison(session, target_tg.id)
        if not prison_row:
            await update.message.reply_text(f"✅ {target_tg.first_name} n'est pas en prison !")
            return

        # Vérifier si peine expirée
        if datetime.utcnow() >= prison_row.released_at:
            await session.execute(
                text("DELETE FROM crime_prison WHERE user_id = :uid"),
                {"uid": target_tg.id}
            )
            await session.commit()
            await update.message.reply_text(
                f"✅ {target_tg.first_name} vient d'être libéré automatiquement !"
            )
            return

        bail_amount = prison_row.bail_amount

        if payer.coins < bail_amount:
            await update.message.reply_text(
                f"❌ Fonds insuffisants !\n"
                f"Caution : <b>{_fmt(bail_amount)} 💰</b>\n"
                f"Ton solde : <b>{_fmt(payer.coins)} 💰</b>",
                parse_mode=ParseMode.HTML
            )
            return

        await session.execute(
            text("UPDATE users SET coins = CAST(coins AS BIGINT) - CAST(:amt AS BIGINT) WHERE user_id = :uid"),
            {"amt": bail_amount, "uid": payer.user_id}
        )

        # Libérer
        await session.execute(
            text("DELETE FROM crime_prison WHERE user_id = :uid"),
            {"uid": target_tg.id}
        )
        await session.commit()

        await update.message.reply_text(
            f"🔓 {mention(payer)} a payé la caution de {mention(prisoner)} !\n"
            f"💸 <b>{_fmt(bail_amount)} 💰</b> dépensés.\n"
            f"🆓 {mention(prisoner)} est libre et peut à nouveau utiliser toutes les commandes !",
            parse_mode=ParseMode.HTML
        )


# ─── /rebet ───────────────────────────────────────────────────────────────────

async def rebet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    player_tg = update.effective_user
    await ensure_user(player_tg)

    # Fonctionne en groupe ET en privé (group_id = 0 pour les chats privés)
    group_id = update.effective_chat.id if update.effective_chat.type in ("group", "supergroup") else 0

    async with AsyncSessionLocal() as session:
        # En prison ?
        if await _prison_block_message(update, session, player_tg.id):
            return

        # Partie déjà active ?
        r = await session.execute(
            text("SELECT * FROM crime_rebet WHERE user_id = :uid"),
            {"uid": player_tg.id}
        )
        if r.fetchone():
            await update.message.reply_text(
                "🎲 Tu as déjà une partie en cours ! Utilise les boutons pour continuer."
            )
            return

        # Lire la mise
        args = context.args
        if not args:
            await update.message.reply_text(
                "❌ Précise ta mise !\nEx: <code>/rebet 10000</code>",
                parse_mode=ParseMode.HTML
            )
            return

        try:
            bet_amount = int(args[0].replace(" ", "").replace("_", ""))
        except ValueError:
            await update.message.reply_text("❌ Montant invalide. Ex: <code>/rebet 5000</code>", parse_mode=ParseMode.HTML)
            return

        if bet_amount < 1:
            await update.message.reply_text("❌ La mise minimum est <b>1 💰</b>.", parse_mode=ParseMode.HTML)
            return

        player = await get_user(session, player_tg.id)
        if player.coins < bet_amount:
            await update.message.reply_text(
                f"❌ Tu n'as pas assez d'argent !\n"
                f"Mise : <b>{_fmt(bet_amount)} 💰</b>\n"
                f"Solde : <b>{_fmt(player.coins)} 💰</b>",
                parse_mode=ParseMode.HTML
            )
            return

        # Déduire la mise
        await session.execute(
            text("UPDATE users SET coins = CAST(coins AS BIGINT) - CAST(:amt AS BIGINT) WHERE user_id = :uid"),
            {"amt": bet_amount, "uid": player.user_id}
        )
        await session.commit()

        # Lancer le round 1
        risk = random.uniform(0.15, 0.40)
        lost = random.random() < risk

        if lost:
            await update.message.reply_text(
                f"🎲 <b>REBET — Round 1</b>\n\n"
                f"Mise : <b>{_fmt(bet_amount)} 💰</b>\n\n"
                f"💥 <b>PERDU !</b> La malchance frappe dès le départ...\n"
                f"Tu perds ta mise de <b>{_fmt(bet_amount)} 💰</b>.",
                parse_mode=ParseMode.HTML
            )
            return

        # Gagné — doubler le pot
        pot = bet_amount * 2

        # Sauvegarder la partie
        await session.execute(
            text("""INSERT INTO crime_rebet (user_id, group_id, pot, round)
                    VALUES (:uid, :gid, :pot, 1)"""),
            {"uid": player_tg.id, "gid": group_id, "pot": pot}
        )
        await session.commit()

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"💰 Récupérer {_fmt(pot)} 💰", callback_data=f"rebet:take:{player_tg.id}"),
                InlineKeyboardButton("🎲 Rebet", callback_data=f"rebet:play:{player_tg.id}"),
            ]
        ])

        msg = await update.message.reply_text(
            f"🎲 <b>REBET — Round 1</b>\n\n"
            f"Mise de départ : <b>{_fmt(bet_amount)} 💰</b>\n"
            f"✅ Gagné ! Cagnotte actuelle : <b>{_fmt(pot)} 💰</b>\n\n"
            f"Que fais-tu ?",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )

        # Sauvegarder le message_id
        await session.execute(
            text("UPDATE crime_rebet SET message_id = :mid WHERE user_id = :uid"),
            {"mid": msg.message_id, "uid": player_tg.id}
        )
        await session.commit()


async def rebet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 3:
        return

    _, action, player_id_str = parts
    player_id = int(player_id_str)

    if query.from_user.id != player_id:
        await query.answer("❌ Ce n'est pas ton jeu !", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        r = await session.execute(
            text("SELECT * FROM crime_rebet WHERE user_id = :uid"),
            {"uid": player_id}
        )
        game = r.fetchone()
        if not game:
            await query.edit_message_text("❌ Cette partie n'existe plus.")
            return

        pot = game.pot
        current_round = game.round
        player = await get_user(session, player_id)

        if action == "take":
            # Récupérer les gains
            await session.execute(
                text("UPDATE users SET coins = CAST(coins AS BIGINT) + CAST(:amt AS BIGINT) WHERE user_id = :uid"),
                {"amt": pot, "uid": player_id}
            )
            await session.execute(
                text("DELETE FROM crime_rebet WHERE user_id = :uid"),
                {"uid": player_id}
            )
            await session.commit()

            await query.edit_message_text(
                f"💰 <b>REBET — Round {current_round}</b>\n\n"
                f"Tu as encaissé <b>{_fmt(pot)} 💰</b> ! Sage décision 😏\n"
                f"Solde mis à jour.",
                parse_mode=ParseMode.HTML
            )

        elif action == "play":
            next_round = current_round + 1

            # Risque aléatoire croissant
            base_risk = min(0.15 + (current_round * 0.07), 0.70)
            risk = random.uniform(base_risk, min(base_risk + 0.20, 0.95))
            lost = random.random() < risk

            if lost:
                await session.execute(
                    text("DELETE FROM crime_rebet WHERE user_id = :uid"),
                    {"uid": player_id}
                )
                await session.commit()

                await query.edit_message_text(
                    f"🎲 <b>REBET — Round {next_round}</b>\n\n"
                    f"💥 <b>PERDU !</b>\n"
                    f"La chance t'a abandonné... Tu perds tout !\n"
                    f"Cagnotte envolée : <b>{_fmt(pot)} 💰</b> 😭",
                    parse_mode=ParseMode.HTML
                )
            else:
                new_pot = pot * 2

                await session.execute(
                    text("UPDATE crime_rebet SET pot = :pot, round = :rnd WHERE user_id = :uid"),
                    {"pot": new_pot, "rnd": next_round, "uid": player_id}
                )
                await session.commit()

                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(f"💰 Récupérer {_fmt(new_pot)} 💰", callback_data=f"rebet:take:{player_id}"),
                        InlineKeyboardButton("🎲 Rebet", callback_data=f"rebet:play:{player_id}"),
                    ]
                ])

                await query.edit_message_text(
                    f"🎲 <b>REBET — Round {next_round}</b>\n\n"
                    f"✅ Gagné encore ! Incroyable...\n"
                    f"💰 Cagnotte : <b>{_fmt(new_pot)} 💰</b>\n\n"
                    f"Que fais-tu ?",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
                )


# ─── /security ────────────────────────────────────────────────────────────────

async def security(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("❌ Cette commande est réservée aux groupes.")
        return

    player_tg = update.effective_user
    await ensure_user(player_tg)

    async with AsyncSessionLocal() as session:
        # En prison ?
        if await _prison_block_message(update, session, player_tg.id):
            return

        player = await get_user(session, player_tg.id)

        # Afficher les agences disponibles
        lines = ["🛡️ <b>AGENCES DE SÉCURITÉ</b>\n"]

        sec_row = await _get_security(session, player_tg.id)
        current_agency = sec_row.agency_id if sec_row else None

        for i, agency in enumerate(SECURITY_AGENCIES, 1):
            active = " ✅ <i>(actif)</i>" if agency["id"] == current_agency else ""
            affordable = "✔️" if player.coins >= agency["price"] else "❌"
            lines.append(
                f"{i}. {agency['name']}{active}\n"
                f"   Protection : <b>{int(agency['protection'] * 100)}%</b>\n"
                f"   Prix : <b>{_fmt(agency['price'])} 💰</b> {affordable}\n"
            )

        lines.append(f"\n💰 Ton solde : <b>{_fmt(player.coins)} 💰</b>")
        lines.append("\nUtilise <code>/security [numéro]</code> pour acheter une protection.")

        # Si un numéro est fourni
        if context.args:
            try:
                choice = int(context.args[0]) - 1
                if choice < 0 or choice >= len(SECURITY_AGENCIES):
                    raise ValueError
            except ValueError:
                await update.message.reply_text("❌ Numéro invalide. Ex: <code>/security 2</code>", parse_mode=ParseMode.HTML)
                return

            chosen = SECURITY_AGENCIES[choice]

            if player.coins < chosen["price"]:
                await update.message.reply_text(
                    f"❌ Tu n'as pas assez d'argent pour {chosen['name']} !\n"
                    f"Prix : <b>{_fmt(chosen['price'])} 💰</b>\n"
                    f"Solde : <b>{_fmt(player.coins)} 💰</b>",
                    parse_mode=ParseMode.HTML
                )
                return

            if current_agency == chosen["id"]:
                await update.message.reply_text(
                    f"✅ Tu as déjà {chosen['name']} comme protection !"
                )
                return

            # Confirmation si déjà une agence active
            if current_agency:
                old = next((a for a in SECURITY_AGENCIES if a["id"] == current_agency), None)
                old_name = old["name"] if old else current_agency

                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Confirmer", callback_data=f"sec:confirm:{player_tg.id}:{chosen['id']}"),
                        InlineKeyboardButton("❌ Annuler", callback_data=f"sec:cancel:{player_tg.id}"),
                    ]
                ])
                await update.message.reply_text(
                    f"⚠️ Tu as déjà <b>{old_name}</b> comme protection.\n"
                    f"Remplacer par <b>{chosen['name']}</b> pour <b>{_fmt(chosen['price'])} 💰</b> ?",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
                )
                return

            # Acheter directement
            await session.execute(
                text("UPDATE users SET coins = CAST(coins AS BIGINT) - CAST(:amt AS BIGINT) WHERE user_id = :uid"),
                {"amt": chosen["price"], "uid": player_tg.id}
            )
            await session.execute(
                text("""INSERT INTO crime_security (user_id, agency_id)
                        VALUES (:uid, :aid)
                        ON CONFLICT (user_id) DO UPDATE SET agency_id = :aid, bought_at = NOW()"""),
                {"uid": player_tg.id, "aid": chosen["id"]}
            )
            await session.commit()

            await update.message.reply_text(
                f"🛡️ Protection activée !\n\n"
                f"Agence : <b>{chosen['name']}</b>\n"
                f"Protection : <b>{int(chosen['protection'] * 100)}%</b>\n"
                f"💸 <b>{_fmt(chosen['price'])} 💰</b> débités.\n"
                f"Solde restant : <b>{_fmt(player.coins - chosen['price'])} 💰</b>",
                parse_mode=ParseMode.HTML
            )
        else:
            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def security_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    action = parts[1]
    player_id = int(parts[2])

    if query.from_user.id != player_id:
        await query.answer("❌ Ce n'est pas ton menu !", show_alert=True)
        return

    if action == "cancel":
        await query.edit_message_text("❌ Achat annulé.")
        return

    agency_id = parts[3]
    chosen = next((a for a in SECURITY_AGENCIES if a["id"] == agency_id), None)
    if not chosen:
        await query.edit_message_text("❌ Agence introuvable.")
        return

    async with AsyncSessionLocal() as session:
        player = await get_user(session, player_id)
        if player.coins < chosen["price"]:
            await query.edit_message_text(
                f"❌ Fonds insuffisants ! Prix : {_fmt(chosen['price'])} 💰"
            )
            return

        await session.execute(
            text("UPDATE users SET coins = CAST(coins AS BIGINT) - CAST(:amt AS BIGINT) WHERE user_id = :uid"),
            {"amt": chosen["price"], "uid": player_id}
        )
        await session.execute(
            text("""INSERT INTO crime_security (user_id, agency_id)
                    VALUES (:uid, :aid)
                    ON CONFLICT (user_id) DO UPDATE SET agency_id = :aid, bought_at = NOW()"""),
            {"uid": player_id, "aid": chosen["id"]}
        )
        await session.commit()

        await query.edit_message_text(
            f"🛡️ Protection mise à jour !\n\n"
            f"Agence : <b>{chosen['name']}</b>\n"
            f"Protection : <b>{int(chosen['protection'] * 100)}%</b>\n"
            f"💸 <b>{_fmt(chosen['price'])} 💰</b> débités.",
            parse_mode=ParseMode.HTML
        )
