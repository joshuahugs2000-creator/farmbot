"""
Système admin complet — accès réservé aux super-admins (IDs hardcodés uniquement).

Commandes :
  /adminhelp    — liste des commandes admin
  /give         — donner des $
  /take         — retirer des $
  /setcoins     — définir le solde exact
  /userinfo     — infos complètes sur un user
  /ban          — bannir
  /unban        — débannir
  /resetuser    — remettre le compte à zéro
  /adminadd     — ajouter un admin
  /adminremove  — retirer un admin
  /adminlist    — liste des admins
  /userlist     — liste de tous les utilisateurs enregistrés
  /liberer      — libérer un prisonnier (God mode)
  /emprisonner  — mettre quelqu'un en prison (God mode)
  /prisonlist   — voir tous les prisonniers actuels
  /broadcast    — message à tous les users ET groupes
"""

import logging
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from sqlalchemy import select, text

from database.db import AsyncSessionLocal, get_user, add_coins, set_coins, get_all_users, get_logs_for_user, get_suspicious_users, get_all_groups
from database.models import User, BankAccount, Loan, Investment, GroupSettings
from utils.helpers import ensure_user, parse_target, mention
from config import CURRENCY

logger = logging.getLogger(__name__)

# ─── IDs des admins ───────────────────────────────────────────────────────────
ADMIN_IDS: set[int] = {
    6227863810,
}

# ─── État global du bot ───────────────────────────────────────────────────────
BOT_PAUSED: bool = False


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


async def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def _deny(update: Update):
    await update.message.reply_text("⛔ Accès refusé. Commande réservée aux admins.")


# ─── Helper BIGINT natif asyncpg ─────────────────────────────────────────────

async def _set_coins_raw(session, user_id: int, amount: int) -> int:
    """
    Définit le solde exact via set_coins() (asyncpg natif, supporte BIGINT).
    """
    return await set_coins(user_id, amount)


# ─── /adminhelp ───────────────────────────────────────────────────────────────

async def adminhelp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    msg = (
        "<b>🛡 Panneau Admin — God Mode</b>\n\n"
        "<b>💰 Gestion argent</b>\n"
        "/give @user montant — Donner des $\n"
        "/take @user montant — Retirer des $\n"
        "/setcoins @user montant — Définir le solde exact\n\n"
        "<b>⛓️ Gestion prison</b>\n"
        "/emprisonner @user durée — Mettre quelqu'un en prison\n"
        "/liberer @user — Libérer quelqu'un de prison\n"
        "/prisonlist — Voir tous les prisonniers actuels\n\n"
        "<b>👤 Gestion utilisateurs</b>\n"
        "/userinfo @user — Infos complètes sur un user\n"
        "/ban @user — Bannir un utilisateur\n"
        "/unban @user — Débannir\n"
        "/resetuser @user — Remettre le compte à zéro\n\n"
        "<b>⚙️ Gestion admins</b>\n"
        "/adminadd @user — Ajouter un admin\n"
        "/adminremove @user — Retirer un admin\n"
        "/adminlist — Liste des admins actuels\n"
        "/userlist — Liste de tous les utilisateurs enregistrés\n"
        "/enquete @user — Rapport d'enquête complet (triche, fortune, activité)\n\n"
        "<b>📢 Communication</b>\n"
        "/broadcast [message] — Message à tous les utilisateurs\n\n"
        "<b>💥 Économie globale</b>\n"
        "/fin — Effondrement : -90% pour tous, -95% pour le Top 10\n"
        "/donate montant — Donner des $ à TOUS les joueurs\n"
        "/donate montant @user — Donner des $ à un joueur précis\n\n"
        "<b>🎭 Drames économiques</b>\n"
        "/drame scandale @user|ID — Perte % $\n"
        "/drame catastrophe @user|ID — Détruit portfolio\n"
        "/drame fisc @user|ID — Impôts forcés\n"
        "/drame crise @user|ID — Double peine\n"
        "/drame info @user|ID — Fortune complète\n"
        "/setdramesesuil [montant] — Changer le seuil\n\n"
        "<b>👑 Classements</b>\n"
        "/richlista — Top 10 riches avec @, ID et fortune\n\n"
        "📰 <b>Articles :</b>\n"
        "/article @user — Générer un article sur un joueur\n"
        "/article hasard — Article sur un joueur aléatoire\n\n"
        "<b>⏸️ Contrôle du bot</b>\n"
        "/pause — Mettre le bot en pause\n"
        "/resume — Réactiver le bot\n\n"
        "<b>🎡 Mood de la roue</b>\n"
        "/facile — Roue généreuse\n"
        "/normal — Roue neutre\n"
        "/difficile — Roue méchante\n"
        "/impitoyable — Roue DESTRUCTION TOTALE 💀\n"
        "/moodauto — Retour aléatoire\n"
        "/setmood — Voir le mood actuel\n"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


# ─── /give ────────────────────────────────────────────────────────────────────

async def give(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    args = context.args or []
    if not args:
        return await update.message.reply_text("Usage : /give @user montant")

    # Extraire le montant (dernier arg) AVANT parse_target
    # pour éviter que le nombre soit confondu avec un ID utilisateur
    try:
        amount = int(args[-1].replace(",", "").replace(" ", ""))
        assert amount > 0
    except (ValueError, AssertionError):
        return await update.message.reply_text("Montant invalide.")

    context.args = args[:-1]
    target_tg = await parse_target(update, context, allow_bot=True)
    context.args = args  # restauration

    if not target_tg:
        return await update.message.reply_text("Usage : /give @user montant")

    target = await ensure_user(target_tg)
    async with AsyncSessionLocal() as session:
        new_bal = await add_coins(session, target.user_id, amount)

    await update.message.reply_text(
        f"✅ <b>+{_fmt(amount)} {CURRENCY}</b> donnés à {mention(target)}\n"
        f"Nouveau solde : {_fmt(new_bal)} {CURRENCY}",
        parse_mode=ParseMode.HTML,
    )


# ─── /take ────────────────────────────────────────────────────────────────────

async def take(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    args = context.args or []
    if not args:
        return await update.message.reply_text("Usage : /take @user montant")

    try:
        amount = int(args[-1].replace(",", "").replace(" ", ""))
        assert amount > 0
    except (ValueError, AssertionError):
        return await update.message.reply_text("Montant invalide.")

    context.args = args[:-1]
    target_tg = await parse_target(update, context, allow_bot=True)
    context.args = args  # restauration

    if not target_tg:
        return await update.message.reply_text("Usage : /take @user montant")

    target = await ensure_user(target_tg)
    async with AsyncSessionLocal() as session:
        new_bal = await add_coins(session, target.user_id, -amount)

    await update.message.reply_text(
        f"✅ <b>-{_fmt(amount)} {CURRENCY}</b> retirés à {mention(target)}\n"
        f"Nouveau solde : {_fmt(new_bal)} {CURRENCY}",
        parse_mode=ParseMode.HTML,
    )


# ─── /setcoins ────────────────────────────────────────────────────────────────

async def setcoins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    target_tg = await parse_target(update, context, allow_bot=True)
    if not target_tg or not context.args:
        return await update.message.reply_text("Usage : /setcoins @user montant")

    try:
        amount = int(context.args[-1].replace(",", "").replace(" ", ""))
        assert amount >= 0
    except (ValueError, AssertionError):
        return await update.message.reply_text("Montant invalide.")

    target = await ensure_user(target_tg)
    async with AsyncSessionLocal() as session:
        u = await get_user(session, target.user_id)
        if not u:
            return await update.message.reply_text("Utilisateur introuvable.")
        # ── CORRECTION : bypass ORM → asyncpg natif pour supporter > 2 milliards
        new_bal = await _set_coins_raw(session, target.user_id, amount)

    await update.message.reply_text(
        f"✅ Solde de {mention(target)} défini à <b>{_fmt(new_bal)} {CURRENCY}</b>",
        parse_mode=ParseMode.HTML,
    )


# ─── /userinfo ────────────────────────────────────────────────────────────────

async def userinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    target_tg = await parse_target(update, context, allow_bot=True)
    if not target_tg:
        return await update.message.reply_text("Usage : /userinfo @user")

    target = await ensure_user(target_tg)
    async with AsyncSessionLocal() as session:
        u = await get_user(session, target.user_id)
        if not u:
            return await update.message.reply_text("Utilisateur introuvable.")

        bank_res = await session.execute(
            select(BankAccount).where(BankAccount.user_id == u.user_id)
        )
        accounts = bank_res.scalars().all()

        loan_res = await session.execute(
            select(Loan).where(Loan.user_id == u.user_id, Loan.status == "active")
        )
        loans = loan_res.scalars().all()

        inv_res = await session.execute(
            select(Investment).where(Investment.user_id == u.user_id, Investment.status == "active")
        )
        investments = inv_res.scalars().all()

        prison_res = await session.execute(
            text("SELECT * FROM crime_prison WHERE user_id = :uid"),
            {"uid": u.user_id}
        )
        prison_row = prison_res.fetchone()

        total_banked = sum(a.balance for a in accounts)
        total_debt   = sum(l.remaining for l in loans)

    banned_str = "  🚫 BANNI" if u.is_banned else ""
    prison_str = ""
    if prison_row:
        now = datetime.utcnow()
        if now < prison_row.released_at:
            mins = max(0, int((prison_row.released_at - now).total_seconds() / 60))
            h = mins // 60
            m = mins % 60
            dur = f"{h}h{m:02d}m" if h > 0 else f"{m}m"
            prison_str = f"\n🔒 EN PRISON — libération dans {dur} | caution : {_fmt(prison_row.bail_amount)} {CURRENCY}"

    lines = [
        f"<b>👤 Infos — {u.first_name}{banned_str}</b>",
        "",
        f"🆔 ID Telegram  : <code>{u.user_id}</code>",
        f"📛 Username     : @{u.username or '—'}",
        f"💰 Solde wallet : {_fmt(u.coins)} {CURRENCY}",
        f"🏦 En banque    : {_fmt(total_banked)} {CURRENCY}",
        f"💳 Dettes       : {_fmt(total_debt)} {CURRENCY}",
        f"📈 Investiss.   : {len(investments)} actifs",
        f"⭐ Karma        : {u.karma}",
        f"👨‍👩‍👧 Famille     : {u.family_name or '—'}",
        f"📅 Inscrit le   : {u.created_at.strftime('%d/%m/%Y') if u.created_at else '—'}",
    ]
    if prison_str:
        lines.append(prison_str)
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ─── /ban / /unban ────────────────────────────────────────────────────────────

async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    target_tg = await parse_target(update, context, allow_bot=True)
    if not target_tg:
        return await update.message.reply_text(
            "Usage : <code>/ban @user [raison]</code>\n"
            "Ex : <code>/ban @Ahmed activité suspecte détectée</code>",
            parse_mode=ParseMode.HTML,
        )

    if await is_admin(target_tg.id):
        return await update.message.reply_text("❌ Tu ne peux pas bannir un autre admin.")

    # Raison optionnelle (tout ce qui suit le @user)
    raison = " ".join(context.args[1:]) if context.args and len(context.args) > 1 else "activité suspecte détectée"

    target = await ensure_user(target_tg)
    async with AsyncSessionLocal() as session:
        u = await get_user(session, target.user_id)
        if not u:
            return await update.message.reply_text("Utilisateur introuvable.")
        if u.is_banned:
            return await update.message.reply_text(
                f"⚠️ {mention(target)} est déjà banni.",
                parse_mode=ParseMode.HTML,
            )
        u.is_banned = True
        u.coins = 0
        await session.execute(
            text("UPDATE bank_accounts SET balance = 0 WHERE user_id = :uid"),
            {"uid": target.user_id},
        )
        await session.execute(
            text("UPDATE couple_accounts SET balance = 0 WHERE user1_id = :uid OR user2_id = :uid"),
            {"uid": target.user_id},
        )
        await session.commit()

    # Notifier le banni en privé
    ban_msg = (
        f"🚨 <b>TU AS ÉTÉ BANNI DU BOT</b>\n\n"
        f"⚠️ <b>Raison :</b> {raison}\n\n"
        f"Tu ne peux plus utiliser aucune commande.\n"
        f"Si tu penses que c'est une erreur, contacte un administrateur."
    )
    try:
        await update.get_bot().send_message(
            chat_id=target_tg.id,
            text=ban_msg,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass  # Le joueur a peut-être bloqué le bot

    await update.message.reply_text(
        f"🚫 <b>{target_tg.first_name} a été banni.</b>\n\n"
        f"📋 Raison : <i>{raison}</i>\n"
        f"📩 Notification envoyée en privé.",
        parse_mode=ParseMode.HTML,
    )


async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    target_tg = await parse_target(update, context, allow_bot=True)
    if not target_tg:
        return await update.message.reply_text("Usage : <code>/unban @user</code>", parse_mode=ParseMode.HTML)

    target = await ensure_user(target_tg)
    async with AsyncSessionLocal() as session:
        u = await get_user(session, target.user_id)
        if not u:
            return await update.message.reply_text("Utilisateur introuvable.")
        if not u.is_banned:
            return await update.message.reply_text(
                f"⚠️ {mention(target)} n'est pas banni.",
                parse_mode=ParseMode.HTML,
            )
        u.is_banned = False
        await session.commit()

    # Notifier le débanni en privé
    unban_msg = (
        f"✅ <b>TON BAN A ÉTÉ LEVÉ</b>\n\n"
        f"Tu peux de nouveau utiliser toutes les commandes du bot.\n"
        f"Bonne continuation !"
    )
    try:
        await update.get_bot().send_message(
            chat_id=target_tg.id,
            text=unban_msg,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    await update.message.reply_text(
        f"✅ <b>{target_tg.first_name} a été débanni.</b>\n"
        f"📩 Notification envoyée en privé.",
        parse_mode=ParseMode.HTML,
    )


# ─── /resetuser ───────────────────────────────────────────────────────────────

async def resetuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    target_tg = await parse_target(update, context, allow_bot=True)
    if not target_tg:
        return await update.message.reply_text("Usage : /resetuser @user")

    target = await ensure_user(target_tg)
    async with AsyncSessionLocal() as session:
        u = await get_user(session, target.user_id)
        if not u:
            return await update.message.reply_text("Utilisateur introuvable.")
        u.coins      = 10_000
        u.karma      = 0
        u.is_banned  = False
        u.last_daily = None
        u.last_work  = None
        await session.commit()

    await update.message.reply_text(
        f"🔄 Compte de {mention(target)} remis à zéro (10 000 $).",
        parse_mode=ParseMode.HTML,
    )


# ─── /adminadd / /adminremove / /adminlist ────────────────────────────────────

async def adminadd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    target_tg = await parse_target(update, context, allow_bot=True)
    if not target_tg:
        return await update.message.reply_text("Usage : /adminadd @user")

    ADMIN_IDS.add(target_tg.id)
    await update.message.reply_text(
        f"✅ {target_tg.first_name} (<code>{target_tg.id}</code>) ajouté aux admins.\n"
        f"⚠️ Temporaire jusqu'au redémarrage du bot. Ajoute l'ID dans le code pour le rendre permanent.",
        parse_mode=ParseMode.HTML,
    )


async def adminremove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    target_tg = await parse_target(update, context, allow_bot=True)
    if not target_tg:
        return await update.message.reply_text("Usage : /adminremove @user")

    if target_tg.id == update.effective_user.id:
        return await update.message.reply_text("Tu ne peux pas te retirer toi-même.")

    ADMIN_IDS.discard(target_tg.id)
    await update.message.reply_text(f"✅ {target_tg.first_name} retiré des admins.")


async def adminlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    ids = "\n".join(f"• <code>{uid}</code>" for uid in ADMIN_IDS)
    await update.message.reply_text(
        f"<b>🛡 Admins actifs ({len(ADMIN_IDS)})</b>\n{ids}",
        parse_mode=ParseMode.HTML,
    )


# ─── /userlist ────────────────────────────────────────────────────────────────

async def userlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Liste tous les utilisateurs enregistrés dans la base (ayant utilisé /acc ou toute autre commande)."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    async with AsyncSessionLocal() as session:
        users = await get_all_users(session)

    if not users:
        return await update.message.reply_text("Aucun utilisateur enregistré.")

    # On construit la liste par blocs de 50 max pour ne pas dépasser la limite Telegram
    CHUNK = 50
    chunks = [users[i:i + CHUNK] for i in range(0, len(users), CHUNK)]

    for idx, chunk in enumerate(chunks):
        lines = []
        for u in chunk:
            if u.username:
                ref = f"@{u.username} ({u.first_name})"
            else:
                ref = f"<a href='tg://user?id={u.user_id}'>{u.first_name}</a>"
            banned = " 🚫" if u.is_banned else ""
            lines.append(f"• {ref} — <code>{u.user_id}</code>{banned}")

        header = (
            f"<b>👥 Utilisateurs enregistrés ({len(users)} total)</b>\n"
            if idx == 0
            else f"<b>👥 (suite {idx + 1}/{len(chunks)})</b>\n"
        )
        await update.message.reply_text(
            header + "\n".join(lines),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


# ─── /liberer ─────────────────────────────────────────────────────────────────

async def liberer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    target_tg = await parse_target(update, context, allow_bot=True)
    if not target_tg:
        return await update.message.reply_text("Usage : /liberer @user")

    target = await ensure_user(target_tg)

    async with AsyncSessionLocal() as session:
        r = await session.execute(
            text("SELECT * FROM crime_prison WHERE user_id = :uid"),
            {"uid": target.user_id}
        )
        prison_row = r.fetchone()

        if not prison_row:
            return await update.message.reply_text(
                f"✅ {target_tg.first_name} n'est pas en prison."
            )

        now = datetime.utcnow()
        minutes_left = max(0, int((prison_row.released_at - now).total_seconds() / 60))

        await session.execute(
            text("DELETE FROM crime_prison WHERE user_id = :uid"),
            {"uid": target.user_id}
        )
        await session.commit()

    await update.message.reply_text(
        f"🔓 <b>GRÂCE PRÉSIDENTIELLE</b>\n\n"
        f"{mention(target)} a été libéré(e) par l'admin !\n"
        f"⏱️ Il restait <b>{minutes_left} minute(s)</b> de peine.\n"
        f"🆓 Il/Elle est maintenant libre et peut utiliser toutes les commandes.",
        parse_mode=ParseMode.HTML,
    )


# ─── /prisonlist ──────────────────────────────────────────────────────────────

async def prisonlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    now = datetime.utcnow()

    async with AsyncSessionLocal() as session:
        r = await session.execute(
            text("SELECT * FROM crime_prison WHERE released_at > :now ORDER BY released_at ASC"),
            {"now": now}
        )
        rows = r.fetchall()

        if not rows:
            await update.message.reply_text("🏛️ Les prisons sont vides ! Personne n'est incarcéré.")
            return

        lines = ["<b>⛓️ LISTE DES PRISONNIERS</b>\n"]

        for row in rows:
            u = await get_user(session, row.user_id)
            name = u.first_name if u else f"ID {row.user_id}"
            minutes_left = max(0, int((row.released_at - now).total_seconds() / 60))
            h = minutes_left // 60
            m = minutes_left % 60
            duration_str = f"{h}h{m:02d}m" if h > 0 else f"{m}m"

            lines.append(
                f"👤 <b>{name}</b> (<code>{row.user_id}</code>)\n"
                f"   💰 Vol : {_fmt(row.amount_stolen)} {CURRENCY}  |  🔓 Caution : {_fmt(row.bail_amount)} {CURRENCY}\n"
                f"   ⏳ Libération dans : <b>{duration_str}</b>\n"
            )

        lines.append(f"\n📊 Total : <b>{len(rows)} prisonnier(s)</b>")
        lines.append("Utilise /liberer @user pour libérer quelqu'un.")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
        )


# ─── /emprisonner ─────────────────────────────────────────────────────────────

async def emprisonner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    target_tg = await parse_target(update, context, allow_bot=True)
    if not target_tg or not context.args:
        return await update.message.reply_text(
            "Usage : /emprisonner @user durée_minutes\nEx: /emprisonner @dupont 60"
        )

    try:
        duration = int(context.args[-1])
        assert duration > 0
    except (ValueError, AssertionError):
        return await update.message.reply_text("❌ Durée invalide. Ex: /emprisonner @user 60")

    target = await ensure_user(target_tg)

    from datetime import timedelta
    released_at = datetime.utcnow() + timedelta(minutes=duration)
    bail_amount = duration * 100

    async with AsyncSessionLocal() as session:
        r = await session.execute(
            text("SELECT * FROM crime_prison WHERE user_id = :uid"),
            {"uid": target.user_id}
        )
        if r.fetchone():
            await session.execute(
                text("""UPDATE crime_prison
                        SET released_at = :rel, bail_amount = :bail, amount_stolen = 0
                        WHERE user_id = :uid"""),
                {"rel": released_at, "bail": bail_amount, "uid": target.user_id}
            )
        else:
            await session.execute(
                text("""INSERT INTO crime_prison (user_id, group_id, amount_stolen, bail_amount, released_at)
                        VALUES (:uid, 0, 0, :bail, :rel)"""),
                {"uid": target.user_id, "bail": bail_amount, "rel": released_at}
            )
        await session.commit()

    h = duration // 60
    m = duration % 60
    dur_str = f"{h}h{m:02d}m" if h > 0 else f"{m}m"

    await update.message.reply_text(
        f"⛓️ <b>EMPRISONNEMENT ADMIN</b>\n\n"
        f"{mention(target)} a été mis(e) en prison !\n"
        f"⏳ Durée : <b>{dur_str}</b>\n"
        f"💸 Caution : <b>{_fmt(bail_amount)} 💰</b>",
        parse_mode=ParseMode.HTML,
    )


# ─── /broadcast ───────────────────────────────────────────────────────────────

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    if not context.args:
        return await update.message.reply_text(
            "Usage : /broadcast [message]\n"
            "Flags optionnels :\n"
            "  --users-only   → DM uniquement\n"
            "  --groups-only  → groupes uniquement"
        )

    raw_args = context.args
    users_only  = "--users-only"  in raw_args
    groups_only = "--groups-only" in raw_args
    msg_parts   = [a for a in raw_args if a not in ("--users-only", "--groups-only")]
    msg         = " ".join(msg_parts)

    if not msg:
        return await update.message.reply_text("❌ Le message est vide.")

    broadcast_text = f"📢 <b>Message officiel</b>\n\n{msg}"

    async with AsyncSessionLocal() as session:
        user_rows  = []
        group_rows = []

        if not groups_only:
            res        = await session.execute(select(User).where(User.is_banned == False))
            user_rows  = res.scalars().all()

        if not users_only:
            res        = await session.execute(select(GroupSettings))
            group_rows = res.scalars().all()

    sent_users, failed_users   = 0, 0
    sent_groups, failed_groups = 0, 0

    for u in user_rows:
        try:
            await context.bot.send_message(
                chat_id=u.user_id,
                text=broadcast_text,
                parse_mode=ParseMode.HTML,
            )
            sent_users += 1
        except Exception:
            failed_users += 1

    for g in group_rows:
        try:
            await context.bot.send_message(
                chat_id=g.group_id,
                text=broadcast_text,
                parse_mode=ParseMode.HTML,
            )
            sent_groups += 1
        except Exception:
            failed_groups += 1

    lines = ["📢 <b>Broadcast terminé.</b>"]
    if not groups_only:
        lines.append(f"👤 Users   → ✅ {sent_users}  ❌ {failed_users}")
    if not users_only:
        lines.append(f"👥 Groupes → ✅ {sent_groups}  ❌ {failed_groups}")
    lines.append(f"📊 Total envoyé : {sent_users + sent_groups}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ─── /pause  /resume ──────────────────────────────────────────────────────────

async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    import handlers.admin as _self
    if _self.BOT_PAUSED:
        await update.message.reply_text("⚠️ Le bot est <b>déjà en pause</b>.", parse_mode=ParseMode.HTML)
        return

    _self.BOT_PAUSED = True
    await update.message.reply_text(
        "⏸️ <b>Bot mis en pause.</b>\n\n"
        "Toutes les commandes utilisateur sont désactivées.\n"
        "Utilise <code>/resume</code> pour réactiver.",
        parse_mode=ParseMode.HTML,
    )


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    import handlers.admin as _self
    if not _self.BOT_PAUSED:
        await update.message.reply_text("✅ Le bot est <b>déjà actif</b>.", parse_mode=ParseMode.HTML)
        return

    _self.BOT_PAUSED = False
    await update.message.reply_text(
        "▶️ <b>Bot réactivé !</b>\n\nToutes les commandes sont à nouveau disponibles.",
        parse_mode=ParseMode.HTML,
    )


# ─── /giveportfolio ───────────────────────────────────────────────────────────

async def giveportfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    target_tg = await parse_target(update, context, allow_bot=True)
    if not target_tg or len(context.args) < 2:
        return await update.message.reply_text(
            "Usage : /giveportfolio @user [asset_id] [quantité]\n"
            "Ex : /giveportfolio @Jean gold_bar 3\n\n"
            "Voir /marketlist pour la liste des asset_id."
        )

    try:
        qty = int(context.args[-1])
        asset_id = context.args[-2].lower()
    except (ValueError, IndexError):
        asset_id = context.args[-1].lower()
        qty = 1

    qty = max(1, min(qty, 9999))

    from handlers.invest import ASSETS
    if asset_id not in ASSETS:
        asset_list = ", ".join(list(ASSETS.keys())[:10]) + "..."
        return await update.message.reply_text(
            f"Asset <code>{asset_id}</code> inconnu.\n"
            f"Exemples : {asset_list}\n"
            f"Voir /marketlist pour la liste complète.",
            parse_mode=ParseMode.HTML,
        )

    a = ASSETS[asset_id]
    target = await ensure_user(target_tg)

    async with AsyncSessionLocal() as session:
        inv = Investment(
            user_id   = target.user_id,
            asset_id  = asset_id,
            quantity  = qty,
            buy_price = 0,
        )
        session.add(inv)
        await session.commit()
        inv_id = inv.id

    await update.message.reply_text(
        f"✅ <b>Portfolio mis à jour !</b>\n\n"
        f"{a['emoji']} <b>{a['name']}</b> x{qty}\n"
        f"👤 Joueur : {mention(target)}\n"
        f"📋 ID position : <code>#{inv_id}</code>\n"
        f"💸 Prix d'achat enregistré : 0 $ (offert)",
        parse_mode=ParseMode.HTML,
    )


# ─── /takeportfolio ───────────────────────────────────────────────────────────

async def takeportfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    if not context.args:
        return await update.message.reply_text(
            "Usage : /takeportfolio [id_position]\n"
            "L'ID est visible dans /portfolio du joueur."
        )

    try:
        inv_id = int(context.args[0].lstrip("#"))
    except ValueError:
        return await update.message.reply_text("ID invalide.")

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Investment).where(Investment.id == inv_id, Investment.status == "active")
        )
        inv = result.scalar_one_or_none()

        if not inv:
            return await update.message.reply_text(
                f"Position #{inv_id} introuvable ou déjà vendue."
            )

        from handlers.invest import ASSETS
        a = ASSETS.get(inv.asset_id, {})
        owner_id = inv.user_id

        inv.status = "sold"
        inv.sell_price = 0
        inv.sold_at = datetime.utcnow()
        await session.commit()

    async with AsyncSessionLocal() as session:
        owner = await get_user(session, owner_id)
        owner_name = owner.first_name if owner else str(owner_id)

    await update.message.reply_text(
        f"🗑️ <b>Position supprimée</b>\n\n"
        f"{a.get('emoji','📊')} <b>{a.get('name', inv.asset_id)}</b> x{inv.quantity}\n"
        f"👤 Propriétaire : {owner_name}\n"
        f"📋 ID : <code>#{inv_id}</code>",
        parse_mode=ParseMode.HTML,
    )


# ─── /marketlist ─────────────────────────────────────────────────────────────

async def marketlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    from handlers.invest import ASSETS, CATEGORIES
    lines = ["<b>📋 Liste des asset_id disponibles</b>\n"]
    for cat in CATEGORIES:
        lines.append(f"<b>── {cat} ──</b>")
        for kid, a in ASSETS.items():
            if a["category"] == cat:
                lines.append(f"  <code>{kid}</code> — {a['name']} (~{_fmt(a['base_price'])} {CURRENCY})")
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ─── /useractivity ────────────────────────────────────────────────────────────

async def useractivity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    target_tg = await parse_target(update, context, allow_bot=True)
    if not target_tg:
        return await update.message.reply_text("Usage : /useractivity @user [heures]\nEx : /useractivity @Jean 4")

    hours = 4
    if context.args:
        try:
            hours = int(context.args[-1])
            hours = max(1, min(hours, 72))
        except ValueError:
            pass

    target = await ensure_user(target_tg)
    uid = target.user_id
    since = f"NOW() - INTERVAL '{hours} hours'"

    async with AsyncSessionLocal() as session:
        u = await get_user(session, uid)
        if not u:
            return await update.message.reply_text("Utilisateur introuvable.")

        events = []

        res = await session.execute(text(f"""
            SELECT asset_id, quantity, buy_price, bought_at
            FROM investments
            WHERE user_id=:uid AND bought_at >= {since}
            ORDER BY bought_at DESC
        """), {"uid": uid})
        for row in res.fetchall():
            events.append((row.bought_at, "📈", f"Achat /market : {row.asset_id} x{row.quantity} à {_fmt(row.buy_price)} $/u"))

        res = await session.execute(text(f"""
            SELECT asset_id, quantity, buy_price, sell_price, sold_at
            FROM investments
            WHERE user_id=:uid AND status='sold' AND sold_at >= {since}
            ORDER BY sold_at DESC
        """), {"uid": uid})
        for row in res.fetchall():
            profit = (row.sell_price - row.buy_price) * row.quantity
            sign = "+" if profit >= 0 else ""
            events.append((row.sold_at, "💰", f"Vente /market : {row.asset_id} x{row.quantity} → {sign}{_fmt(profit)} {CURRENCY}"))

        res = await session.execute(text(f"""
            SELECT lt.created_at, ls.ticket_price, ls.group_id
            FROM lottery_tickets lt
            JOIN lottery_sessions ls ON ls.id = lt.session_id
            WHERE lt.user_id=:uid AND lt.created_at >= {since}
            ORDER BY lt.created_at DESC
        """), {"uid": uid})
        for row in res.fetchall():
            events.append((row.created_at, "🎟️", f"Ticket loto acheté ({_fmt(row.ticket_price)} {CURRENCY}) — groupe {row.group_id}"))

        res = await session.execute(text(f"""
            SELECT amount, description, created_at
            FROM user_bets
            WHERE proposer_id=:uid AND created_at >= {since}
            ORDER BY created_at DESC
        """), {"uid": uid})
        for row in res.fetchall():
            events.append((row.created_at, "🎲", f"Pari créé : {_fmt(row.amount)} {CURRENCY} — \"{row.description[:40]}\""))

        res = await session.execute(text(f"""
            SELECT amount, description, created_at
            FROM user_bets
            WHERE target_id=:uid AND status IN ('active','done') AND created_at >= {since}
            ORDER BY created_at DESC
        """), {"uid": uid})
        for row in res.fetchall():
            events.append((row.created_at, "🤝", f"Pari accepté : {_fmt(row.amount)} {CURRENCY} — \"{row.description[:40]}\""))

        res = await session.execute(text(f"""
            SELECT bank_id, balance, opened_at
            FROM bank_accounts
            WHERE user_id=:uid AND opened_at >= {since}
            ORDER BY opened_at DESC
        """), {"uid": uid})
        for row in res.fetchall():
            events.append((row.opened_at, "🏦", f"Compte bancaire ouvert : {row.bank_id}"))

        res = await session.execute(text(f"""
            SELECT bank_id, amount, interest_rate, created_at
            FROM loans
            WHERE user_id=:uid AND created_at >= {since}
            ORDER BY created_at DESC
        """), {"uid": uid})
        for row in res.fetchall():
            events.append((row.created_at, "💳", f"Prêt contracté : {_fmt(row.amount)} {CURRENCY} à {row.interest_rate*100:.1f}% — {row.bank_id}"))

        try:
            res = await session.execute(text(f"""
                SELECT ab.amount, ab.placed_at, acs.item_name
                FROM auction_bids ab
                JOIN auction_sessions acs ON acs.id = ab.session_id
                WHERE ab.user_id=:uid AND ab.placed_at >= {since}
                ORDER BY ab.placed_at DESC
            """), {"uid": uid})
            for row in res.fetchall():
                events.append((row.placed_at, "🔨", f"Enchère : {_fmt(row.amount)} {CURRENCY} sur \"{row.item_name}\""))
        except Exception:
            pass

        try:
            res = await session.execute(text(f"""
                SELECT item_name, paid_price, rarity, acquired_at
                FROM auction_inventory
                WHERE user_id=:uid AND acquired_at >= {since}
                ORDER BY acquired_at DESC
            """), {"uid": uid})
            for row in res.fetchall():
                events.append((row.acquired_at, "🏆", f"Objet gagné : {row.item_name} ({row.rarity}) — payé {_fmt(row.paid_price)} {CURRENCY}"))
        except Exception:
            pass

        try:
            res = await session.execute(text(f"""
                SELECT target_id, amount, success, created_at
                FROM cambriolage_log
                WHERE attacker_id=:uid AND created_at >= {since}
                ORDER BY created_at DESC
            """), {"uid": uid})
            for row in res.fetchall():
                status = "✅ réussi" if row.success else "❌ échoué"
                events.append((row.created_at, "🥷", f"Cambriolage {status} : {_fmt(row.amount)} {CURRENCY} sur user {row.target_id}"))
        except Exception:
            pass

        try:
            res = await session.execute(text(f"""
                SELECT reason, bail_amount, imprisoned_at
                FROM crime_prison_log
                WHERE user_id=:uid AND imprisoned_at >= {since}
                ORDER BY imprisoned_at DESC
            """), {"uid": uid})
            for row in res.fetchall():
                events.append((row.imprisoned_at, "🔒", f"Emprisonné : {row.reason} (caution {_fmt(row.bail_amount)} {CURRENCY})"))
        except Exception:
            pass

    if u.last_work:
        import datetime as _dt
        cutoff = datetime.utcnow() - _dt.timedelta(hours=hours)
        if u.last_work >= cutoff:
            events.append((u.last_work, "💼", "/work effectué"))

    events.sort(key=lambda x: x[0], reverse=True)

    name = u.first_name or u.username or str(uid)
    header = (
        f"🔍 <b>Activité de {name}</b> — {hours}h\n"
        f"👤 ID : <code>{uid}</code> | 💰 Solde actuel : {_fmt(u.coins)} {CURRENCY}\n"
        f"─────────────────────────\n"
    )

    if not events:
        return await update.message.reply_text(
            header + "Aucune activité enregistrée sur cette période.",
            parse_mode=ParseMode.HTML,
        )

    lines = [header]
    for ts, emoji, desc in events[:40]:
        time_str = ts.strftime("%H:%M:%S") if ts else "?"
        lines.append(f"{emoji} <code>{time_str}</code> {desc}")

    if len(events) > 40:
        lines.append(f"\n<i>… et {len(events) - 40} autres événements.</i>")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ─── /enquete ────────────────────────────────────────────────────────────────

SEUIL_COINS_SUSPECT = 1_000_000_000  # 1 milliard


async def enquete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Rapport d'enquête complet sur un utilisateur.
    Usage : /enquete @username  OU en réponse à un message
    """
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    target_tg = await parse_target(update, context, allow_bot=True)
    if not target_tg:
        return await update.message.reply_text(
            "Usage : /enquete @username\nOu utilise la commande en réponse au message d'un utilisateur."
        )

    target = await ensure_user(target_tg)
    uid = target.user_id

    async with AsyncSessionLocal() as session:
        u = await get_user(session, uid)
        if not u:
            return await update.message.reply_text("❌ Utilisateur introuvable en base.")

        # ── Banque ────────────────────────────────────────────────────────────
        bank_res = await session.execute(
            select(BankAccount).where(BankAccount.user_id == uid)
        )
        accounts = bank_res.scalars().all()
        total_banked = sum(a.balance for a in accounts)

        # ── Prêts ─────────────────────────────────────────────────────────────
        loan_res = await session.execute(
            select(Loan).where(Loan.user_id == uid, Loan.status == "active")
        )
        loans = loan_res.scalars().all()
        total_debt = sum(l.remaining for l in loans)

        # ── Investissements ───────────────────────────────────────────────────
        inv_res = await session.execute(
            select(Investment).where(Investment.user_id == uid, Investment.status == "active")
        )
        investments = inv_res.scalars().all()
        total_invest = sum(
            i.buy_price * i.quantity for i in investments
        )

        # ── Compte commun ─────────────────────────────────────────────────────
        couple_row = await session.execute(
            text("SELECT balance FROM couple_accounts WHERE user1_id=:uid OR user2_id=:uid LIMIT 1"),
            {"uid": uid}
        )
        couple_bal = couple_row.fetchone()
        compte_commun = couple_bal[0] if couple_bal else 0

        # ── Relations ─────────────────────────────────────────────────────────
        from database.models import Relationship, RelationType
        rel_res = await session.execute(
            select(Relationship).where(
                (Relationship.user_id == uid) | (Relationship.related_user_id == uid)
            )
        )
        all_rels = rel_res.scalars().all()
        spouse_ids   = []
        children_ids = []
        friend_ids   = []
        for r in all_rels:
            other = r.related_user_id if r.user_id == uid else r.user_id
            if r.relation_type == RelationType.SPOUSE:
                spouse_ids.append(other)
            elif r.relation_type == RelationType.PARENT:
                children_ids.append(other)
            elif r.relation_type == RelationType.FRIEND:
                friend_ids.append(other)

        # ── Prison ────────────────────────────────────────────────────────────
        prison_res = await session.execute(
            text("SELECT * FROM crime_prison WHERE user_id=:uid"),
            {"uid": uid}
        )
        prison_row = prison_res.fetchone()

        # ── Bets ──────────────────────────────────────────────────────────────
        bets_prop = await session.execute(
            text("SELECT COUNT(*) FROM user_bets WHERE proposer_id=:uid"),
            {"uid": uid}
        )
        bets_acc = await session.execute(
            text("SELECT COUNT(*) FROM user_bets WHERE target_id=:uid AND status IN ('active','done')"),
            {"uid": uid}
        )
        bets_won = await session.execute(
            text("SELECT COUNT(*) FROM user_bets WHERE winner_id=:uid AND status='done'"),
            {"uid": uid}
        )
        nb_prop = bets_prop.scalar() or 0
        nb_acc  = bets_acc.scalar() or 0
        nb_won  = bets_won.scalar() or 0

        # Montant total gagné via bets
        bets_gain = await session.execute(
            text("""
                SELECT COALESCE(SUM(amount)*2, 0)
                FROM user_bets
                WHERE winner_id=:uid AND status='done'
            """),
            {"uid": uid}
        )
        total_bet_gain = bets_gain.scalar() or 0

        # ── Loterie ───────────────────────────────────────────────────────────
        loto_tickets = await session.execute(
            text("SELECT COUNT(*) FROM lottery_tickets WHERE user_id=:uid"),
            {"uid": uid}
        )
        loto_wins = await session.execute(
            text("SELECT COUNT(*) FROM lottery_sessions WHERE winner_id=:uid"),
            {"uid": uid}
        )
        nb_tickets = loto_tickets.scalar() or 0
        nb_loto_wins = loto_wins.scalar() or 0

        # ── Cambriolage ───────────────────────────────────────────────────────
        try:
            camb_res = await session.execute(
                text("""
                    SELECT COUNT(*) as total,
                           SUM(CASE WHEN success THEN 1 ELSE 0 END) as success,
                           COALESCE(SUM(amount), 0) as total_stolen
                    FROM cambriolage_log WHERE attacker_id=:uid
                """),
                {"uid": uid}
            )
            camb = camb_res.fetchone()
            nb_camb = camb[0] or 0
            nb_camb_ok = camb[1] or 0
            total_stolen = camb[2] or 0
        except Exception:
            nb_camb = nb_camb_ok = total_stolen = 0

        # ── Crimes (rob) ──────────────────────────────────────────────────────
        try:
            rob_res = await session.execute(
                text("""
                    SELECT COUNT(*) as total,
                           COALESCE(SUM(amount_stolen), 0) as stolen
                    FROM crime_prison_log WHERE user_id=:uid
                """),
                {"uid": uid}
            )
            rob = rob_res.fetchone()
            nb_rob = rob[0] or 0
            rob_total = rob[1] or 0
        except Exception:
            nb_rob = rob_total = 0

    # ─── Calcul du patrimoine total ───────────────────────────────────────────
    fortune_totale = u.coins + total_banked + total_invest + compte_commun

    # ─── Détection des signaux suspects ──────────────────────────────────────
    alertes = []

    if fortune_totale > SEUIL_COINS_SUSPECT:
        alertes.append(f"💰 Fortune totale anormalement élevée : {_fmt(fortune_totale)} {CURRENCY}")

    if nb_prop + nb_acc > 0:
        win_rate = int(nb_won / (nb_prop + nb_acc) * 100)
        if win_rate >= 80 and nb_won >= 3:
            alertes.append(f"🎲 Taux de victoire aux bets suspect : {win_rate}% ({nb_won}/{nb_prop + nb_acc})")

    if total_bet_gain > 500_000_000:
        alertes.append(f"🎲 Gains totaux bets : {_fmt(total_bet_gain)} {CURRENCY} (très élevé)")

    if nb_loto_wins >= 3:
        alertes.append(f"🎟️ A gagné la loterie {nb_loto_wins} fois")

    if nb_camb > 0 and nb_camb_ok / nb_camb >= 0.9 and nb_camb >= 5:
        alertes.append(f"🥷 Taux de réussite cambriolage suspect : {nb_camb_ok}/{nb_camb}")

    if total_stolen > 500_000_000:
        alertes.append(f"🥷 Total volé par cambriolage : {_fmt(total_stolen)} {CURRENCY}")

    # ─── Construction du rapport ──────────────────────────────────────────────
    prison_str = "Non"
    if prison_row:
        now = datetime.utcnow()
        if now < prison_row.released_at:
            mins = max(0, int((prison_row.released_at - now).total_seconds() / 60))
            prison_str = f"Oui — libération dans {mins} min"

    username_str = f"@{u.username}" if u.username else "—"
    inscrit_str  = u.created_at.strftime("%d/%m/%Y à %H:%M") if u.created_at else "—"
    daily_str    = u.last_daily or "jamais"
    work_str     = u.last_work.strftime("%d/%m/%Y %H:%M") if u.last_work else "jamais"
    banned_str   = "🚫 OUI" if u.is_banned else "Non"

    rapport = [
        f"🔍 <b>ENQUÊTE — {u.first_name} {username_str}</b>",
        f"🆔 ID : <code>{uid}</code>",
        f"📅 Inscription : {inscrit_str}",
        f"🚫 Banni : {banned_str}",
        f"🔒 En prison : {prison_str}",
        "",
        "<b>💰 PATRIMOINE</b>",
        f"  Wallet       : {_fmt(u.coins)} {CURRENCY}",
        f"  Banque       : {_fmt(total_banked)} {CURRENCY}",
        f"  Investiss.   : {_fmt(total_invest)} {CURRENCY}",
        f"  Compte commun: {_fmt(compte_commun)} {CURRENCY}",
        f"  Dettes        : -{_fmt(total_debt)} {CURRENCY}",
        f"  <b>TOTAL NET     : {_fmt(fortune_totale)} {CURRENCY}</b>",
        "",
        "<b>👨‍👩‍👧 RELATIONS</b>",
        f"  Conjoint(s)  : {len(spouse_ids)}",
        f"  Enfants/Parents: {len(children_ids)}",
        f"  Amis         : {len(friend_ids)}",
        f"  Nom de famille: {u.family_name or '—'}",
        "",
        "<b>🎲 PARIS (BETS)</b>",
        f"  Créés   : {nb_prop}  |  Acceptés : {nb_acc}",
        f"  Gagnés  : {nb_won}  |  Gains total : {_fmt(total_bet_gain)} {CURRENCY}",
        "",
        "<b>🎟️ LOTERIE</b>",
        f"  Tickets achetés : {nb_tickets}  |  Victoires : {nb_loto_wins}",
        "",
        "<b>🥷 CRIMINALITÉ</b>",
        f"  Cambriolages    : {nb_camb_ok}/{nb_camb} réussis — {_fmt(total_stolen)} {CURRENCY} volés",
        f"  Emprisonné (log): {nb_rob} fois",
        "",
        "<b>⏱️ DERNIÈRE ACTIVITÉ</b>",
        f"  /daily : {daily_str}",
        f"  /work  : {work_str}",
        f"  Karma  : {u.karma}",
    ]

    if alertes:
        rapport.append("")
        rapport.append("⚠️ <b>SIGNAUX SUSPECTS</b>")
        for a in alertes:
            rapport.append(f"  🔴 {a}")
    else:
        rapport.append("")
        rapport.append("✅ <b>Aucun signal suspect détecté.</b>")

    rapport.append("")
    rapport.append(f"<i>Rapport généré le {datetime.utcnow().strftime('%d/%m/%Y à %H:%M')} UTC</i>")

    await update.message.reply_text("\n".join(rapport), parse_mode=ParseMode.HTML)


# ─── /richlista ───────────────────────────────────────────────────────────────

async def richlista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Top 10 des plus riches (coins + banques) — vue admin avec @username et ID."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    async with AsyncSessionLocal() as session:
        # Récupérer tous les users avec leur fortune coins + banques en une seule requête
        result = await session.execute(
            text("""
                SELECT u.user_id, u.first_name, u.username, u.coins,
                       COALESCE(SUM(b.balance), 0) AS bank_total,
                       u.coins + COALESCE(SUM(b.balance), 0) AS fortune_totale
                FROM users u
                LEFT JOIN bank_accounts b ON b.user_id = u.user_id
                GROUP BY u.user_id, u.first_name, u.username, u.coins
                ORDER BY fortune_totale DESC
                LIMIT 10
            """)
        )
        top = result.fetchall()

    medals = ["🥇", "🥈", "🥉"]
    lines = ["👑 <b>TOP 10 — CLASSEMENT DES PLUS RICHES</b>\n"]

    for i, row in enumerate(top):
        medal = medals[i] if i < 3 else f"{i + 1}."
        username_str = f"@{row.username}" if row.username else "<i>sans @</i>"
        lines.append(
            f"{medal} <b>{row.first_name}</b>\n"
            f"   └ {username_str} | ID: <code>{row.user_id}</code>\n"
            f"   └ 💵 Liquide: {_fmt(row.coins)} {CURRENCY}\n"
            f"   └ 🏦 Banques: {_fmt(int(row.bank_total))} {CURRENCY}\n"
            f"   └ 💰 Total:   {_fmt(int(row.fortune_totale))} {CURRENCY}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ─── /logs ────────────────────────────────────────────────────────────────────

async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /logs @user [date]  ou  /logs ID [date]
    Affiche les logs d'un utilisateur pour aujourd'hui ou une date donnée.
    Réservé aux admins.
    """
    from datetime import datetime as _dt
    from sqlalchemy import text as _text

    if not await is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Accès refusé.")

    if not context.args:
        return await update.message.reply_text(
            "Usage : /logs @pseudo [YYYY-MM-DD]\n"
            "Ex : /logs @Mark\nEx : /logs @Mark 2024-01-15\nEx : /logs 123456789"
        )

    target_id   = None
    target_name = None
    msg = update.message

    # 1. Réponse à un message
    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        target_id   = u.id
        target_name = u.first_name or u.username or str(u.id)

    # 2. Entité Telegram (mention cliquable)
    if not target_id:
        for ent in (msg.entities or []):
            if ent.type == "text_mention" and ent.user:
                target_id   = ent.user.id
                target_name = ent.user.first_name or str(ent.user.id)
                break
            if ent.type == "mention":
                raw_username = msg.text[ent.offset: ent.offset + ent.length].lstrip("@")
                async with AsyncSessionLocal() as _s:
                    r = await _s.execute(
                        _text("SELECT user_id, first_name FROM users WHERE lower(username)=lower(:u) LIMIT 1"),
                        {"u": raw_username}
                    )
                    row = r.fetchone()
                if row:
                    target_id   = row[0]
                    target_name = row[1] or raw_username
                else:
                    return await msg.reply_text(f"❌ @{raw_username} introuvable en base.")
                break

    # 3. ID numérique brut dans les args
    if not target_id:
        for arg in context.args:
            if arg.lstrip("-").isdigit():
                uid = int(arg)
                async with AsyncSessionLocal() as _s:
                    r = await _s.execute(
                        _text("SELECT user_id, first_name FROM users WHERE user_id=:u LIMIT 1"),
                        {"u": uid}
                    )
                    row = r.fetchone()
                if row:
                    target_id   = row[0]
                    target_name = row[1] or str(uid)
                else:
                    return await msg.reply_text(f"❌ ID {uid} introuvable en base.")
                break

    # 4. @username texte brut sans entité Telegram
    if not target_id:
        for arg in context.args:
            if arg.startswith("@"):
                raw_username = arg.lstrip("@")
                async with AsyncSessionLocal() as _s:
                    r = await _s.execute(
                        _text("SELECT user_id, first_name FROM users WHERE lower(username)=lower(:u) LIMIT 1"),
                        {"u": raw_username}
                    )
                    row = r.fetchone()
                if row:
                    target_id   = row[0]
                    target_name = row[1] or raw_username
                else:
                    return await msg.reply_text(f"❌ @{raw_username} introuvable en base.")
                break

    if not target_id:
        return await msg.reply_text("❌ Utilisateur introuvable. Utilise @pseudo, un ID ou réponds à un message.")

    # Date optionnelle
    date_str = None
    for arg in context.args:
        if arg.startswith("20") and len(arg) == 10:
            date_str = arg
            break
    if not date_str:
        date_str = _dt.utcnow().strftime("%Y-%m-%d")

    async with AsyncSessionLocal() as session:
        logs = await get_logs_for_user(session, target_id, date_str, limit=80)

    if not logs:
        return await msg.reply_text(
            f"📋 Aucun log pour <b>{target_name}</b> le {date_str}.",
            parse_mode="HTML"
        )

    lines = [f"📋 <b>Logs de {target_name}</b> ({date_str}) — {len(logs)} action(s)\n"]
    for log in logs:
        row_map = log._mapping
        ts  = row_map["created_at"].strftime("%H:%M:%S")
        amt = f" [{row_map['amount']:+,} $]" if row_map.get("amount") else ""
        arg = f" {row_map['args']}"          if row_map.get("args")   else ""
        res = f" → {row_map['result']}"      if row_map.get("result") else ""
        lines.append(f"<code>{ts}</code> /{row_map['command']}{arg}{amt}{res}")

    txt = "\n".join(lines)
    if len(txt) > 3800:
        txt = txt[:3800] + "\n… (tronqué)"

    await msg.reply_text(txt, parse_mode="HTML")


async def suspicious_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /suspicious
    Liste les joueurs avec comportement anormal aujourd'hui.
    Réservé aux admins.
    """
    if not await is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Accès refusé.")

    async with AsyncSessionLocal() as session:
        suspects = await get_suspicious_users(session)

    if not suspects:
        return await update.message.reply_text(
            "✅ Aucun comportement suspect détecté aujourd'hui.",
        )

    lines = [f"🚨 <b>Comportements suspects — aujourd'hui</b>\n"]
    for s in suspects:
        uname = f"@{s['username']}" if s['username'] and not s['username'].isdigit() else f"ID:{s['user_id']}"
        lines.append(f"👤 <b>{uname}</b> ({s['cmd_count']} cmd)")
        for flag in s["flags"]:
            lines.append(f"   {flag}")
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── /grouplist ───────────────────────────────────────────────────────────────

async def grouplist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /grouplist         → liste tous les groupes actifs
    /grouplist all     → liste actifs + inactifs (bot kické)
    Réservé aux admins.
    """
    if not await is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Accès refusé.")

    show_all = bool(context.args and context.args[0].lower() == "all")
    groups   = await get_all_groups(active_only=not show_all)

    if not groups:
        return await update.message.reply_text("📭 Aucun groupe enregistré pour l'instant.")

    actifs   = [g for g in groups if g._mapping["is_active"]]
    inactifs = [g for g in groups if not g._mapping["is_active"]]

    lines = [f"🌐 <b>Groupes du bot</b> — {len(actifs)} actif(s)"
             + (f", {len(inactifs)} inactif(s)" if show_all and inactifs else "") + "\n"]

    def _fmt_group(g, idx: int) -> str:
        m        = g._mapping
        gid      = m["group_id"]
        title    = m["title"] or "Sans nom"
        gtype    = m["chat_type"] or "?"
        members  = m["member_count"]
        username = m["username"]
        link     = m["invite_link"]
        seen     = m["last_seen"]
        active   = m["is_active"]

        if username:
            access = f'<a href="https://t.me/{username.lstrip("@")}">@{username.lstrip("@")}</a>'
        elif link:
            access = f'<a href="{link}">Lien invite</a>'
        else:
            access = "🔒 Privé (pas de lien)"

        status      = "✅" if active else "❌ Inactif"
        members_str = f"{members:,}" if members else "?"
        seen_str    = seen.strftime("%d/%m %H:%M") if seen else "?"

        return (
            f"{idx}. {status} <b>{title}</b>\n"
            f"   🆔 <code>{gid}</code> | 👥 {members_str} membres | {gtype}\n"
            f"   🔗 {access}\n"
            f"   ⏱ Vu le {seen_str}"
        )

    for i, g in enumerate(actifs, 1):
        lines.append(_fmt_group(g, i))

    if show_all and inactifs:
        lines.append("\n<b>— Groupes inactifs (bot kické) —</b>")
        for i, g in enumerate(inactifs, len(actifs) + 1):
            lines.append(_fmt_group(g, i))

    full_text = "\n\n".join(lines)
    if len(full_text) <= 4000:
        await update.message.reply_text(full_text, parse_mode="HTML", disable_web_page_preview=True)
    else:
        chunk = lines[0] + "\n\n"
        for block in lines[1:]:
            if len(chunk) + len(block) + 2 > 4000:
                await update.message.reply_text(chunk.strip(), parse_mode="HTML", disable_web_page_preview=True)
                chunk = block + "\n\n"
            else:
                chunk += block + "\n\n"
        if chunk.strip():
            await update.message.reply_text(chunk.strip(), parse_mode="HTML", disable_web_page_preview=True)


# ─── /groupscan ───────────────────────────────────────────────────────────────

async def groupscan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /groupscan — Force la récupération du lien d'invitation pour tous les groupes
    où le bot est admin mais où le lien est manquant.
    Réservé aux admins.
    """
    if not await is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Accès refusé.")

    groups = await get_all_groups(active_only=True)
    if not groups:
        return await update.message.reply_text("📭 Aucun groupe enregistré.")

    msg = await update.message.reply_text("🔄 Scan en cours...")

    ok, skipped, failed = 0, 0, 0
    results = []

    for g in groups:
        m        = g._mapping
        gid      = m["group_id"]
        title    = m["title"] or str(gid)
        username = m["username"]
        link     = m["invite_link"]

        # Déjà un lien ou username public → skip
        if username or link:
            skipped += 1
            continue

        # Tentative de génération du lien
        try:
            new_link = await context.bot.export_chat_invite_link(gid)
            # Sauvegarder en base
            await upsert_group(
                group_id=gid,
                title=title,
                username=username,
                chat_type=m["chat_type"] or "supergroup",
                member_count=m["member_count"],
                invite_link=new_link,
            )
            results.append(f"✅ <b>{title}</b>\n   🔗 <a href='{new_link}'>Lien généré</a>")
            ok += 1
        except Exception as e:
            results.append(f"❌ <b>{title}</b> (<code>{gid}</code>)\n   ⚠️ {str(e)[:80]}")
            failed += 1

    summary = f"📊 Scan terminé — ✅ {ok} lien(s) générés | ⏭ {skipped} déjà connus | ❌ {failed} échec(s)\n\n"
    full = summary + "\n\n".join(results) if results else summary + "Rien à faire."

    if len(full) <= 4000:
        await msg.edit_text(full, parse_mode="HTML", disable_web_page_preview=True)
    else:
        await msg.edit_text(summary, parse_mode="HTML")
        chunk = ""
        for block in results:
            if len(chunk) + len(block) + 2 > 4000:
                await update.message.reply_text(chunk.strip(), parse_mode="HTML", disable_web_page_preview=True)
                chunk = block + "\n\n"
            else:
                chunk += block + "\n\n"
        if chunk.strip():
            await update.message.reply_text(chunk.strip(), parse_mode="HTML", disable_web_page_preview=True)


# ─── /fin — Crash économique global ──────────────────────────────────────────

async def fin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /fin
    Déclenche un effondrement économique global :
    - Tous les joueurs perdent 90% (coins + comptes bancaires)
    - Le TOP 10 (fortune totale) perd 95%
    Réservé aux super-admins.
    """
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    msg = await update.message.reply_text(
        "☠️ <b>L'EFFONDREMENT EST EN COURS...</b>\n"
        "⏳ Calcul des fortunes et application des pertes...",
        parse_mode=ParseMode.HTML
    )

    async with AsyncSessionLocal() as session:
        # Identifier le TOP 10 par fortune totale (coins + banque)
        top10_result = await session.execute(text("""
            SELECT u.user_id,
                   u.coins + COALESCE(SUM(b.balance), 0) AS fortune_totale
            FROM users u
            LEFT JOIN bank_accounts b ON b.user_id = u.user_id
            GROUP BY u.user_id, u.coins
            ORDER BY fortune_totale DESC
            LIMIT 10
        """))
        top10_ids = {row.user_id for row in top10_result.fetchall()}

        # Récupérer tous les users
        all_users_result = await session.execute(text("SELECT user_id, coins FROM users"))
        all_users = all_users_result.fetchall()

        users_affected = 0
        for row in all_users:
            uid = row.user_id
            loss_pct = 0.95 if uid in top10_ids else 0.90
            keep_pct = 1.0 - loss_pct

            # Réduire les coins (minimum 1 000)
            new_coins = max(1000, int(row.coins * keep_pct))
            await session.execute(
                text("UPDATE users SET coins = :coins WHERE user_id = :uid"),
                {"coins": new_coins, "uid": uid}
            )

            # Réduire les comptes bancaires (minimum 0)
            bank_result = await session.execute(
                text("SELECT id, balance FROM bank_accounts WHERE user_id = :uid"),
                {"uid": uid}
            )
            for bank_row in bank_result.fetchall():
                new_balance = max(0, int(bank_row.balance * keep_pct))
                await session.execute(
                    text("UPDATE bank_accounts SET balance = :bal WHERE id = :bid"),
                    {"bal": new_balance, "bid": bank_row.id}
                )

            users_affected += 1

        await session.commit()

    await msg.edit_text(
        f"💀 <b>C'EST LA FIN.</b>\n\n"
        f"📉 Joueurs normaux : <b>-90%</b> de toute leur fortune\n"
        f"👑 Top 10 les plus riches : <b>-95%</b> de toute leur fortune\n"
        f"🏦 Comptes bancaires inclus dans la purge\n"
        f"👥 Utilisateurs touchés : <b>{users_affected}</b>\n\n"
        f"💡 Minimum garanti : <b>1 000 {CURRENCY}</b> sur les coins.",
        parse_mode=ParseMode.HTML
    )


# ─── /donate — Don global ou ciblé ───────────────────────────────────────────

async def donate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /donate <montant>         → donne <montant> à TOUS les joueurs
    /donate <montant> @user   → donne <montant> à un joueur spécifique
    Réservé aux super-admins.
    """
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    args = context.args
    if not args:
        return await update.message.reply_text(
            "Usage :\n"
            "• <code>/donate 5000</code> — donne 5 000 à TOUS les joueurs\n"
            "• <code>/donate 5000 @user</code> — donne 5 000 à un joueur précis",
            parse_mode=ParseMode.HTML
        )

    try:
        amount = int(args[0].replace("_", "").replace(" ", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        return await update.message.reply_text("❌ Montant invalide. Exemple : /donate 5000")

    target_arg = args[1] if len(args) >= 2 else None

    # ── Don ciblé ──
    if target_arg:
        try:
            target_tg = await parse_target(update, context, target_arg)
        except Exception:
            return await update.message.reply_text("❌ Utilisateur introuvable.")

        async with AsyncSessionLocal() as session:
            await ensure_user(session, target_tg)
            new_bal = await add_coins(session, target_tg.id, amount)
            await session.commit()

        return await update.message.reply_text(
            f"✅ <b>Don effectué !</b>\n"
            f"👤 Bénéficiaire : <b>{target_tg.first_name}</b>\n"
            f"💰 Montant : <b>+{_fmt(amount)} {CURRENCY}</b>\n"
            f"💵 Nouveau solde coins : <b>{_fmt(new_bal)} {CURRENCY}</b>",
            parse_mode=ParseMode.HTML
        )

    # ── Don global ──
    msg = await update.message.reply_text(
        f"⏳ Don global de <b>{_fmt(amount)} {CURRENCY}</b> en cours...",
        parse_mode=ParseMode.HTML
    )

    async with AsyncSessionLocal() as session:
        all_users_result = await session.execute(text("SELECT user_id FROM users"))
        all_users = all_users_result.fetchall()

        count = 0
        for row in all_users:
            await session.execute(
                text("UPDATE users SET coins = coins + :amt WHERE user_id = :uid"),
                {"amt": amount, "uid": row.user_id}
            )
            count += 1

        await session.commit()

    await msg.edit_text(
        f"🎁 <b>Don global envoyé !</b>\n\n"
        f"💰 Montant par joueur : <b>+{_fmt(amount)} {CURRENCY}</b>\n"
        f"👥 Joueurs crédités : <b>{count}</b>\n"
        f"💸 Total distribué : <b>{_fmt(amount * count)} {CURRENCY}</b>",
        parse_mode=ParseMode.HTML
    )
