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
  /broadcast    — message à tous les users
"""

import logging
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from sqlalchemy import select

from database.db import AsyncSessionLocal, get_user, add_coins
from database.models import User, BankAccount, Loan, Investment
from utils.helpers import ensure_user, parse_target, mention

logger = logging.getLogger(__name__)

# ─── IDs des admins ───────────────────────────────────────────────────────────
# Ajoute ton ID Telegram ici. Tu peux en mettre plusieurs séparés par des virgules.
ADMIN_IDS: set[int] = {
    6227863810,   # ← remplace par ton vrai ID Telegram
}


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
        "<b>🛡 Panneau Admin</b>\n\n"
        "<b>💰 Gestion argent</b>\n"
        "/give @user montant — Donner des $\n"
        "/take @user montant — Retirer des $\n"
        "/setcoins @user montant — Définir le solde exact\n\n"
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
        "/broadcast [message] — Message à tous les utilisateurs\n"
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

        total_banked = sum(a.balance for a in accounts)
        total_debt   = sum(l.remaining for l in loans)

    banned_str = "  🚫 BANNI" if u.is_banned else ""
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
