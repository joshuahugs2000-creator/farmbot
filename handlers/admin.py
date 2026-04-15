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
  /broadcast    — message à tous les users
"""

import logging
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from sqlalchemy import select, text

from database.db import AsyncSessionLocal, get_user, add_coins
from database.models import User, BankAccount, Loan, Investment
from utils.helpers import ensure_user, parse_target, mention

logger = logging.getLogger(__name__)

# ─── IDs des admins ───────────────────────────────────────────────────────────
# Ajoute ton ID Telegram ici. Tu peux en mettre plusieurs séparés par des virgules.
ADMIN_IDS: set[int] = {
    6227863810,   # ← remplace par ton vrai ID Telegram
}


# ─── État global du bot ───────────────────────────────────────────────────────
BOT_PAUSED: bool = False


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


async def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def _deny(update: Update):
    await update.message.reply_text("⛔ Accès refusé. Commande réservée aux admins.")


# ─── /adminhelp ───────────────────────────────────────────────────────────────

async def adminhelp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    text = (
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
        "<b>⏸️ Contrôle du bot</b>\n"
        "/pause — Mettre le bot en pause\n"
        "/resume — Réactiver le bot\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


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
        u.coins = amount
        await session.commit()

    await update.message.reply_text(
        f"✅ Solde de {mention(target)} défini à <b>{_fmt(amount)} $</b>",
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

        # Vérifier si en prison
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
    """Libère immédiatement un prisonnier (God mode admin)."""
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
    """Liste tous les prisonniers actuels (dont la peine n'est pas encore expirée)."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    now = datetime.utcnow()

    async with AsyncSessionLocal() as session:
        # On ne récupère QUE les prisonniers encore actifs
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
    """Mettre quelqu'un en prison manuellement (God mode admin)."""
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
    bail_amount = duration * 100  # caution = 100$ par minute

    async with AsyncSessionLocal() as session:
        # Vérifier si déjà en prison
        r = await session.execute(
            text("SELECT * FROM crime_prison WHERE user_id = :uid"),
            {"uid": target.user_id}
        )
        if r.fetchone():
            # Mettre à jour la peine existante
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
        return await update.message.reply_text("Usage : /broadcast [message]")

    msg = " ".join(context.args)
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.is_banned == False))
        users  = result.scalars().all()

    sent, failed = 0, 0
    for u in users:
        try:
            await context.bot.send_message(
                chat_id=u.user_id,
                text=f"📢 <b>Message officiel</b>\n\n{msg}",
                parse_mode=ParseMode.HTML,
            )
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"📢 Broadcast terminé.\n✅ Envoyé : {sent}\n❌ Échec : {failed}"
    )

# ─── /pause  /resume ──────────────────────────────────────────────────────────

async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin : met le bot en pause (plus aucune commande utilisateur)."""
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
    """Admin : réactive le bot."""
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
