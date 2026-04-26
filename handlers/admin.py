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

from database.db import AsyncSessionLocal, get_user, add_coins, set_coins
from database.models import User, BankAccount, Loan, Investment, GroupSettings
from utils.helpers import ensure_user, parse_target, mention

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
        "/adminlist — Liste des admins actuels\n\n"
        "<b>📢 Communication</b>\n"
        "/broadcast [message] — Message à tous les utilisateurs\n\n"
        "<b>🎭 Drames économiques</b>\n"
        "/drame scandale @user — Perte % coins\n"
        "/drame catastrophe @user — Détruit portfolio\n"
        "/drame fisc @user — Impôts forcés\n"
        "/drame crise @user — Double peine\n"
        "/drame info @user — Fortune complète\n"
        "/setdramesesuil [montant] — Changer le seuil\n\n"
        "📰 <b>Articles :</b>\n"
        "/article @user — Générer un article sur un joueur\n"
        "/article hasard — Article sur un joueur aléatoire\n\n"
        "<b>⏸️ Contrôle du bot</b>\n"
        "/pause — Mettre le bot en pause\n"
        "/resume — Réactiver le bot\n"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


# ─── /give ────────────────────────────────────────────────────────────────────

async def give(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    target_tg = await parse_target(update, context)
    if not target_tg or not context.args:
        return await update.message.reply_text("Usage : /give @user montant")

    try:
        amount = int(context.args[-1].replace(",", "").replace(" ", ""))
        assert amount > 0
    except (ValueError, AssertionError):
        return await update.message.reply_text("Montant invalide.")

    target = await ensure_user(target_tg)
    async with AsyncSessionLocal() as session:
        # add_coins utilise asyncpg natif — supporte les BIGINT
        new_bal = await add_coins(session, target.user_id, amount)

    await update.message.reply_text(
        f"✅ <b>+{_fmt(amount)} $</b> donnés à {mention(target)}\n"
        f"Nouveau solde : {_fmt(new_bal)} $",
        parse_mode=ParseMode.HTML,
    )


# ─── /take ────────────────────────────────────────────────────────────────────

async def take(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    target_tg = await parse_target(update, context)
    if not target_tg or not context.args:
        return await update.message.reply_text("Usage : /take @user montant")

    try:
        amount = int(context.args[-1].replace(",", "").replace(" ", ""))
        assert amount > 0
    except (ValueError, AssertionError):
        return await update.message.reply_text("Montant invalide.")

    target = await ensure_user(target_tg)
    async with AsyncSessionLocal() as session:
        new_bal = await add_coins(session, target.user_id, -amount)

    await update.message.reply_text(
        f"✅ <b>-{_fmt(amount)} $</b> retirés à {mention(target)}\n"
        f"Nouveau solde : {_fmt(new_bal)} $",
        parse_mode=ParseMode.HTML,
    )


# ─── /setcoins ────────────────────────────────────────────────────────────────

async def setcoins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    target_tg = await parse_target(update, context)
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
        f"✅ Solde de {mention(target)} défini à <b>{_fmt(new_bal)} $</b>",
        parse_mode=ParseMode.HTML,
    )


# ─── /userinfo ────────────────────────────────────────────────────────────────

async def userinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    target_tg = await parse_target(update, context)
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
            prison_str = f"\n🔒 EN PRISON — libération dans {dur} | caution : {_fmt(prison_row.bail_amount)} $"

    lines = [
        f"<b>👤 Infos — {u.first_name}{banned_str}</b>",
        "",
        f"🆔 ID Telegram  : <code>{u.user_id}</code>",
        f"📛 Username     : @{u.username or '—'}",
        f"💰 Solde wallet : {_fmt(u.coins)} $",
        f"🏦 En banque    : {_fmt(total_banked)} $",
        f"💳 Dettes       : {_fmt(total_debt)} $",
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

    target_tg = await parse_target(update, context)
    if not target_tg:
        return await update.message.reply_text("Usage : /ban @user")

    if await is_admin(target_tg.id):
        return await update.message.reply_text("Tu ne peux pas bannir un autre admin.")

    target = await ensure_user(target_tg)
    async with AsyncSessionLocal() as session:
        u = await get_user(session, target.user_id)
        if not u:
            return await update.message.reply_text("Utilisateur introuvable.")
        u.is_banned = True
        await session.commit()

    await update.message.reply_text(
        f"🚫 {mention(target)} est maintenant banni du bot.",
        parse_mode=ParseMode.HTML,
    )


async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    target_tg = await parse_target(update, context)
    if not target_tg:
        return await update.message.reply_text("Usage : /unban @user")

    target = await ensure_user(target_tg)
    async with AsyncSessionLocal() as session:
        u = await get_user(session, target.user_id)
        if not u:
            return await update.message.reply_text("Utilisateur introuvable.")
        u.is_banned = False
        await session.commit()

    await update.message.reply_text(
        f"✅ {mention(target)} est débanni.",
        parse_mode=ParseMode.HTML,
    )


# ─── /resetuser ───────────────────────────────────────────────────────────────

async def resetuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    target_tg = await parse_target(update, context)
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

    target_tg = await parse_target(update, context)
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

    target_tg = await parse_target(update, context)
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


# ─── /liberer ─────────────────────────────────────────────────────────────────

async def liberer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    target_tg = await parse_target(update, context)
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
                f"   💰 Vol : {_fmt(row.amount_stolen)} $  |  🔓 Caution : {_fmt(row.bail_amount)} $\n"
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

    target_tg = await parse_target(update, context)
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

    target_tg = await parse_target(update, context)
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
                lines.append(f"  <code>{kid}</code> — {a['name']} (~{_fmt(a['base_price'])} $)")
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ─── /useractivity ────────────────────────────────────────────────────────────

async def useractivity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    target_tg = await parse_target(update, context)
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
            events.append((row.sold_at, "💰", f"Vente /market : {row.asset_id} x{row.quantity} → {sign}{_fmt(profit)} $"))

        res = await session.execute(text(f"""
            SELECT lt.created_at, ls.ticket_price, ls.group_id
            FROM lottery_tickets lt
            JOIN lottery_sessions ls ON ls.id = lt.session_id
            WHERE lt.user_id=:uid AND lt.created_at >= {since}
            ORDER BY lt.created_at DESC
        """), {"uid": uid})
        for row in res.fetchall():
            events.append((row.created_at, "🎟️", f"Ticket loto acheté ({_fmt(row.ticket_price)} $) — groupe {row.group_id}"))

        res = await session.execute(text(f"""
            SELECT amount, description, created_at
            FROM user_bets
            WHERE proposer_id=:uid AND created_at >= {since}
            ORDER BY created_at DESC
        """), {"uid": uid})
        for row in res.fetchall():
            events.append((row.created_at, "🎲", f"Pari créé : {_fmt(row.amount)} $ — \"{row.description[:40]}\""))

        res = await session.execute(text(f"""
            SELECT amount, description, created_at
            FROM user_bets
            WHERE target_id=:uid AND status IN ('active','done') AND created_at >= {since}
            ORDER BY created_at DESC
        """), {"uid": uid})
        for row in res.fetchall():
            events.append((row.created_at, "🤝", f"Pari accepté : {_fmt(row.amount)} $ — \"{row.description[:40]}\""))

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
            events.append((row.created_at, "💳", f"Prêt contracté : {_fmt(row.amount)} $ à {row.interest_rate*100:.1f}% — {row.bank_id}"))

        try:
            res = await session.execute(text(f"""
                SELECT ab.amount, ab.placed_at, acs.item_name
                FROM auction_bids ab
                JOIN auction_sessions acs ON acs.id = ab.session_id
                WHERE ab.user_id=:uid AND ab.placed_at >= {since}
                ORDER BY ab.placed_at DESC
            """), {"uid": uid})
            for row in res.fetchall():
                events.append((row.placed_at, "🔨", f"Enchère : {_fmt(row.amount)} $ sur \"{row.item_name}\""))
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
                events.append((row.acquired_at, "🏆", f"Objet gagné : {row.item_name} ({row.rarity}) — payé {_fmt(row.paid_price)} $"))
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
                events.append((row.created_at, "🥷", f"Cambriolage {status} : {_fmt(row.amount)} $ sur user {row.target_id}"))
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
                events.append((row.imprisoned_at, "🔒", f"Emprisonné : {row.reason} (caution {_fmt(row.bail_amount)} $)"))
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
        f"👤 ID : <code>{uid}</code> | 💰 Solde actuel : {_fmt(u.coins)} $\n"
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
