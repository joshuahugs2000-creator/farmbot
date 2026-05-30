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

import asyncio
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
_BROADCAST_LOCK: bool = False


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
        "/setmood — Voir le mood actuel\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ <b>GOD MODE — Commandes avancées</b>\n\n"
        "<b>📊 Surveillance</b>\n"
        "/statsbot — Snapshot global du bot\n"
        "/checkuser @user — Fiche ultra-complète d'un joueur\n"
        "/topactifs — Top 10 joueurs les plus actifs (7j)\n\n"
        "<b>🧊 Contrôle joueurs</b>\n"
        "/freeze @user — Geler un compte (30 jours prison)\n"
        "/unfreeze @user — Dégeler un compte\n"
        "/resetcooldown @user — Remettre daily/work/diplôme à zéro\n"
        "/addkarma @user montant — Ajouter/retirer du karma\n"
        "/setkarma @user valeur — Définir karma exact\n"
        "/wipeinventory @user — Vider l'inventaire enchères\n"
        "/resetbanque @user — Vider les comptes bancaires\n"
        "/wipeloans @user — Effacer tous les prêts actifs\n\n"
        "<b>💹 Économie globale</b>\n"
        "/inflation pct — Appliquer une inflation/déflation à TOUS\n"
        "/purgeprison — Libérer TOUS les prisonniers\n"
        "/broadcastdm message — DM à tous les users\n\n"
        "<b>🏢 Entreprises</b>\n"
        "/kickboite @user — Expulser de force d'une entreprise\n"
        "/deletecompany nom — Dissoudre une entreprise de force\n"
        "/forcepdg @user nom — Nommer PDG de force\n"
        "/mutecompany nom — Suspendre une entreprise\n"
        "/unmutecompany nom — Réactiver une entreprise\n"
        "/setreputation nom valeur — Modifier la réputation\n"
        "/addvalue nom montant — Modifier la valeur d'une entreprise\n"
        "/adminboites — Vue complète de toutes les entreprises\n"
        "/adminboite nom — Fiche détaillée : employés, salaires, logs\n\n"
        "<b>📈 Utilisation du bot</b>\n"
        "/statsusers — Actifs par période, nouveaux inscrits, top commandes\n\n"
        "<b>🎓 Diplômes &amp; Examens</b>\n"
        "/admindiplome @user bac|licence|master|mba [domaine] [retirer] — Accorder ou retirer un diplôme\n"
        "/examinfo @user — Voir niveau, cooldown, ancienneté, domaine, solde\n"
        "/examreset @user — Supprimer le cooldown (peut repasser immédiatement)\n"
        "/examanciennete @user jours — Forcer l'ancienneté (débloquer le Master)\n"
        "/examcoins @user — Donner exactement les coins pour le prochain diplôme\n"
        "/examunlock @user — TOUT débloquer : cooldown + ancienneté + coins\n"
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

    # Détection ID brut ou @username
    raw_id = None
    if context.args and context.args[0].lstrip("-").isdigit():
        raw_id = int(context.args[0])

    target_tg = await parse_target(update, context, allow_bot=True)

    # Si l'ID est inconnu de la DB, on crée une entrée fantôme pour le bannir quand même
    if not target_tg and raw_id:
        from database.db import AsyncSessionLocal as _ASL
        from database.models import User as _User
        async with _ASL() as _s:
            from sqlalchemy import select as _sel
            _res = await _s.execute(_sel(_User).where(_User.user_id == raw_id))
            _u = _res.scalar_one_or_none()
            if not _u:
                _u = _User(user_id=raw_id, username=None, first_name=f"User_{raw_id}")
                _s.add(_u)
                await _s.commit()
        class _FakeById:
            id = raw_id
            first_name = f"User_{raw_id}"
            username = None
            is_bot = False
        target_tg = _FakeById()

    if not target_tg:
        return await update.message.reply_text(
            "Usage :\n"
            "<code>/ban @user [raison]</code>\n"
            "<code>/ban 123456789 [raison]</code> — par ID\n\n"
            "Astuce : l\'ID se trouve dans /userinfo ou /userlist",
            parse_mode=ParseMode.HTML,
        )

    if await is_admin(target_tg.id):
        return await update.message.reply_text("❌ Tu ne peux pas bannir un autre admin.")

    # Raison : tout ce qui suit le @user ou l'ID
    raison_parts = context.args[1:] if context.args else []
    raison = " ".join(raison_parts) if raison_parts else "activité suspecte détectée"

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

    # Alerter tous les groupes
    alert_lines = [
        "🚨 <b>ALERTE SECURITE - BAN EXECUTE</b>",
        "━" * 22,
        "",
        "<b>Joueur banni :</b> " + target_tg.first_name,
        "ID : <code>" + str(target_tg.id) + "</code>",
        "<b>Raison :</b> " + raison,
        "",
        "💰 Solde et comptes bancaires remis a 0.",
        "🔒 Acces au bot definitivement bloque.",
    ]
    alert_msg = "\n".join(alert_lines)
    groups = await get_all_groups(active_only=True)
    for g in groups:
        try:
            await update.get_bot().send_message(
                chat_id=g.group_id,
                text=alert_msg,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    await update.message.reply_text(
        "🚫 <b>" + target_tg.first_name + " a ete banni.</b>\n\n"
        "📋 Raison : <i>" + raison + "</i>\n"
        "📩 Notification envoyee en prive + dans tous les groupes.",
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

    global _BROADCAST_LOCK
    if _BROADCAST_LOCK:
        return await update.message.reply_text("⚠️ Un broadcast est déjà en cours, attends qu'il se termine.")

    _BROADCAST_LOCK = True
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

    try:
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
            await asyncio.sleep(0.1)  # max 10 msgs/sec

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
            await asyncio.sleep(0.1)
    finally:
        _BROADCAST_LOCK = False

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


# ─── /admindiplome ────────────────────────────────────────────────────────────

async def admindiplome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Accorde ou retire un diplôme à un joueur sans examen.
    Usage : /admindiplome @user <bac|licence|master|mba> [domaine] [--retirer]
    Exemples :
      /admindiplome @toto bac
      /admindiplome @toto licence informatique
      /admindiplome @toto master --retirer
    """
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    args = context.args or []
    if len(args) < 2:
        return await update.message.reply_text(
            "❌ Usage : <code>/admindiplome @user &lt;bac|licence|master|mba&gt; [domaine] [--retirer]</code>",
            parse_mode=ParseMode.HTML,
        )

    target_arg = args[0].lstrip("@")
    level      = args[1].lower()
    retirer    = any(a.lower() in ("--retirer", "retirer", "-retirer", "remove") for a in args)
    domaine    = None
    DOMAINS_VALID = {"finance", "informatique", "marketing", "droit", "management", "agriculture", "securite"}
    LEVELS_VALID  = {"bac", "licence", "master", "mba"}

    if level not in LEVELS_VALID:
        return await update.message.reply_text(
            f"❌ Niveau invalide. Choisir parmi : <code>bac, licence, master, mba</code>",
            parse_mode=ParseMode.HTML,
        )

    # Domaine optionnel (pour licence/master/mba)
    for a in args[2:]:
        if a.lower() in DOMAINS_VALID:
            domaine = a.lower()
            break

    async with AsyncSessionLocal() as session:
        from database.db import get_user_by_username, get_user
        # Chercher par username ou user_id
        if target_arg.isdigit():
            u = await get_user(session, int(target_arg))
        else:
            u = await get_user_by_username(session, target_arg)

        if not u:
            return await update.message.reply_text("❌ Joueur introuvable.")

        params = {"uid": u.user_id}
        sets   = []

        if retirer:
            # Cascade : retirer ce niveau ET tous les diplômes au-dessus
            CASCADE = {
                "bac":     ["bac", "licence", "master", "mba"],
                "licence": ["licence", "master", "mba"],
                "master":  ["master", "mba"],
                "mba":     ["mba"],
            }
            for lvl in CASCADE[level]:
                sets.append(f"diplome_{lvl} = FALSE")
            # Réinitialiser le domaine si on retire jusqu'à la licence ou en dessous
            if level in ("bac", "licence"):
                sets.append("diplome_domain = NULL")
        else:
            sets.append(f"diplome_{level} = TRUE")
            if domaine and level in ("licence", "master", "mba"):
                sets.append("diplome_domain = :dom")
                params["dom"] = domaine

        await session.execute(
            text(f"UPDATE users SET {', '.join(sets)} WHERE user_id = :uid"),
            params,
        )
        await session.commit()

    LEVEL_EMOJIS = {"bac": "📄", "licence": "🎓", "master": "🏅", "mba": "👑"}

    if retirer:
        CASCADE = {"bac": ["bac","licence","master","mba"], "licence": ["licence","master","mba"], "master": ["master","mba"], "mba": ["mba"]}
        retiré_str = " + ".join(f"{LEVEL_EMOJIS[l]} {l.upper()}" for l in CASCADE[level])
        await update.message.reply_text(
            f"❌ Diplômes retirés : <b>{retiré_str}</b>\n"
            f"Joueur : <b>@{u.username or u.first_name}</b>",
            parse_mode=ParseMode.HTML,
        )
    else:
        dom_str = f" · <b>{domaine.capitalize()}</b>" if domaine else ""
        await update.message.reply_text(
            f"✅ Diplôme {LEVEL_EMOJIS[level]} <b>{level.upper()}</b>{dom_str} accordé "
            f"à <b>@{u.username or u.first_name}</b>.",
            parse_mode=ParseMode.HTML,
        )




# ─── Commandes admin examens ──────────────────────────────────────────────────

async def adminexam_reset_cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /examreset @user — Supprime le cooldown d'examen d'un joueur (peut repasser immédiatement).
    """
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    args = context.args or []
    if not args:
        return await update.message.reply_text("❌ Usage : <code>/examreset @user</code>", parse_mode=ParseMode.HTML)

    target = args[0].lstrip("@")
    async with AsyncSessionLocal() as session:
        from database.db import get_user, get_user_by_username
        u = await get_user(session, int(target)) if target.isdigit() else await get_user_by_username(session, target)
        if not u:
            return await update.message.reply_text("❌ Joueur introuvable.")
        await session.execute(text("UPDATE users SET exam_cooldown = NULL WHERE user_id = :uid"), {"uid": u.user_id})
        await session.commit()

    await update.message.reply_text(
        f"✅ Cooldown d'examen supprimé pour <b>@{u.username or u.first_name}</b>. Il peut repasser immédiatement.",
        parse_mode=ParseMode.HTML,
    )


async def adminexam_set_anciennete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /examanciennete @user jours — Force l'ancienneté d'un joueur (pour débloquer le Master).
    Ex : /examanciennete @toto 20
    """
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    args = context.args or []
    if len(args) < 2:
        return await update.message.reply_text(
            "❌ Usage : <code>/examanciennete @user jours</code>\nEx : /examanciennete @toto 20",
            parse_mode=ParseMode.HTML,
        )

    target = args[0].lstrip("@")
    try:
        jours = int(args[1])
    except ValueError:
        return await update.message.reply_text("❌ Le nombre de jours doit être un entier.")

    async with AsyncSessionLocal() as session:
        from database.db import get_user, get_user_by_username
        from datetime import datetime, timedelta
        u = await get_user(session, int(target)) if target.isdigit() else await get_user_by_username(session, target)
        if not u:
            return await update.message.reply_text("❌ Joueur introuvable.")
        new_date = datetime.utcnow() - timedelta(days=jours)
        await session.execute(
            text("UPDATE users SET created_at = :d WHERE user_id = :uid"),
            {"d": new_date, "uid": u.user_id},
        )
        await session.commit()

    await update.message.reply_text(
        f"✅ Ancienneté de <b>@{u.username or u.first_name}</b> forcée à <b>{jours} jours</b>.\n"
        f"Il peut maintenant passer le Master.",
        parse_mode=ParseMode.HTML,
    )


async def adminexam_give_coins_exam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /examcoins @user — Donne exactement les coins nécessaires pour passer le prochain diplôme.
    """
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    args = context.args or []
    if not args:
        return await update.message.reply_text("❌ Usage : <code>/examcoins @user</code>", parse_mode=ParseMode.HTML)

    target = args[0].lstrip("@")

    EXAMS_COST = {"bac": 0, "licence": 500_000, "master": 5_000_000, "mba": 50_000_000}
    LEVEL_ORDER = ["none", "bac", "licence", "master", "mba"]

    async with AsyncSessionLocal() as session:
        from database.db import get_user, get_user_by_username
        u = await get_user(session, int(target)) if target.isdigit() else await get_user_by_username(session, target)
        if not u:
            return await update.message.reply_text("❌ Joueur introuvable.")

        # Déterminer le niveau actuel et suivant
        if getattr(u, "diplome_mba", False):     current = "mba"
        elif getattr(u, "diplome_master", False): current = "master"
        elif getattr(u, "diplome_licence", False): current = "licence"
        elif getattr(u, "diplome_bac", False):   current = "bac"
        else:                                      current = "none"

        idx = LEVEL_ORDER.index(current)
        if idx >= len(LEVEL_ORDER) - 1:
            return await update.message.reply_text(
                f"<b>@{u.username or u.first_name}</b> a déjà tous les diplômes 🏆",
                parse_mode=ParseMode.HTML,
            )

        next_lvl = LEVEL_ORDER[idx + 1]
        cost = EXAMS_COST[next_lvl]
        if cost == 0:
            return await update.message.reply_text(
                f"Le <b>{next_lvl.upper()}</b> est gratuit, pas besoin de coins.",
                parse_mode=ParseMode.HTML,
            )

        # Donner exactement le manquant si insuffisant
        manquant = max(0, cost - u.coins)
        if manquant == 0:
            return await update.message.reply_text(
                f"<b>@{u.username or u.first_name}</b> a déjà assez pour le <b>{next_lvl.upper()}</b> ({u.coins:,} 💰).",
                parse_mode=ParseMode.HTML,
            )

        await session.execute(
            text("UPDATE users SET coins = CAST(coins AS BIGINT) + :c WHERE user_id = :uid"),
            {"c": manquant, "uid": u.user_id},
        )
        await session.commit()

    await update.message.reply_text(
        f"💰 <b>{manquant:,} $</b> donnés à <b>@{u.username or u.first_name}</b>.\n"
        f"Il peut maintenant passer le <b>{next_lvl.upper()}</b> ({cost:,} $ requis).",
        parse_mode=ParseMode.HTML,
    )


async def adminexam_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /examinfo @user — Affiche toutes les infos d'examen d'un joueur (niveau, cooldown, ancienneté, domaine).
    """
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    args = context.args or []
    if not args:
        return await update.message.reply_text("❌ Usage : <code>/examinfo @user</code>", parse_mode=ParseMode.HTML)

    target = args[0].lstrip("@")
    async with AsyncSessionLocal() as session:
        from database.db import get_user, get_user_by_username
        from datetime import datetime
        u = await get_user(session, int(target)) if target.isdigit() else await get_user_by_username(session, target)
        if not u:
            return await update.message.reply_text("❌ Joueur introuvable.")

        if getattr(u, "diplome_mba", False):      current = "mba"
        elif getattr(u, "diplome_master", False):  current = "master"
        elif getattr(u, "diplome_licence", False): current = "licence"
        elif getattr(u, "diplome_bac", False):     current = "bac"
        else:                                       current = "aucun"

        cd = getattr(u, "exam_cooldown", None)
        if cd and cd > datetime.utcnow():
            delta = cd - datetime.utcnow()
            h = int(delta.total_seconds() // 3600)
            m = int((delta.total_seconds() % 3600) // 60)
            cd_str = f"⏳ {h}h{m:02d}m restants"
        else:
            cd_str = "✅ Aucun (peut passer)"

        anciennete = (datetime.utcnow() - u.created_at).days if u.created_at else "?"
        domain = getattr(u, "diplome_domain", None) or "—"

        diplomes = []
        for lvl, emoji in [("bac","📄"),("licence","🎓"),("master","🏅"),("mba","👑")]:
            diplomes.append(f"{'✅' if getattr(u, f'diplome_{lvl}', False) else '⬜'} {emoji} {lvl.upper()}")

        await update.message.reply_text(
            f"🔍 <b>Exam Info — @{u.username or u.first_name}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📚 Niveau actuel : <b>{current.upper()}</b>\n"
            f"🎯 Domaine : <b>{domain}</b>\n"
            f"📅 Ancienneté : <b>{anciennete} jours</b> (Master requis : 20j)\n"
            f"💰 Solde : <b>{u.coins:,} $</b>\n"
            f"⏱ Cooldown : <b>{cd_str}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(diplomes),
            parse_mode=ParseMode.HTML,
        )


async def adminexam_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /examunlock @user — Supprime TOUTES les restrictions pour un joueur :
    cooldown, ancienneté (force 30j), et donne les coins si manquants.
    """
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    args = context.args or []
    if not args:
        return await update.message.reply_text("❌ Usage : <code>/examunlock @user</code>", parse_mode=ParseMode.HTML)

    target = args[0].lstrip("@")
    EXAMS_COST = {"bac": 0, "licence": 500_000, "master": 5_000_000, "mba": 50_000_000}
    LEVEL_ORDER_L = ["none", "bac", "licence", "master", "mba"]

    async with AsyncSessionLocal() as session:
        from database.db import get_user, get_user_by_username
        from datetime import datetime, timedelta
        u = await get_user(session, int(target)) if target.isdigit() else await get_user_by_username(session, target)
        if not u:
            return await update.message.reply_text("❌ Joueur introuvable.")

        if getattr(u, "diplome_mba", False):      current = "mba"
        elif getattr(u, "diplome_master", False):  current = "master"
        elif getattr(u, "diplome_licence", False): current = "licence"
        elif getattr(u, "diplome_bac", False):     current = "bac"
        else:                                       current = "none"

        idx = LEVEL_ORDER_L.index(current)
        actions = []

        # 1. Reset cooldown
        updates = ["exam_cooldown = NULL"]
        actions.append("✅ Cooldown supprimé")

        # 2. Force ancienneté 30j
        new_date = datetime.utcnow() - timedelta(days=30)
        updates.append("created_at = :cdate")
        actions.append("✅ Ancienneté forcée à 30 jours")

        # 3. Coins pour prochain niveau
        if idx < len(LEVEL_ORDER_L) - 1:
            next_lvl = LEVEL_ORDER_L[idx + 1]
            cost = EXAMS_COST.get(next_lvl, 0)
            manquant = max(0, cost - u.coins)
            if manquant > 0:
                updates.append("coins = CAST(coins AS BIGINT) + :manquant")
                actions.append(f"✅ {manquant:,} $ ajoutés pour le {next_lvl.upper()}")
            else:
                actions.append(f"✅ Assez de coins pour le {next_lvl.upper()}")
        else:
            next_lvl = None
            manquant = 0
            actions.append("🏆 Déjà tous les diplômes")

        await session.execute(
            text(f"UPDATE users SET {', '.join(updates)} WHERE user_id = :uid"),
            {"uid": u.user_id, "cdate": new_date, "manquant": manquant},
        )
        await session.commit()

    await update.message.reply_text(
        f"🔓 <b>@{u.username or u.first_name}</b> débloqué !\n\n"
        + "\n".join(actions)
        + (f"\n\n➡️ Il peut maintenant passer le <b>{next_lvl.upper()}</b>." if next_lvl else ""),
        parse_mode=ParseMode.HTML,
    )

# ═══════════════════════════════════════════════════════════════════════════════
# ▓▓▓  GOD MODE — COMMANDES AVANCÉES  ▓▓▓
# ═══════════════════════════════════════════════════════════════════════════════

# ─── /statsbot — Statistiques globales du bot ────────────────────────────────

async def statsbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Snapshot complet : users, argent, entreprises, banques, prison."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    async with AsyncSessionLocal() as session:
        total_users    = (await session.execute(text("SELECT COUNT(*) FROM users"))).scalar()
        total_coins    = (await session.execute(text("SELECT COALESCE(SUM(coins),0) FROM users"))).scalar()
        total_banned   = (await session.execute(text("SELECT COUNT(*) FROM users WHERE is_banned=TRUE"))).scalar()
        total_bank     = (await session.execute(text("SELECT COALESCE(SUM(balance),0) FROM bank_accounts"))).scalar()
        total_loans    = (await session.execute(text("SELECT COALESCE(SUM(amount),0) FROM loans WHERE status='active'"))).scalar() if True else 0
        total_prison   = (await session.execute(text("SELECT COUNT(*) FROM crime_prison"))).scalar()
        total_companies= (await session.execute(text("SELECT COUNT(*) FROM companies WHERE is_active=TRUE"))).scalar()
        total_emps     = (await session.execute(text("SELECT COUNT(*) FROM company_employees WHERE left_at IS NULL"))).scalar()
        richest        = (await session.execute(text("SELECT first_name, coins FROM users ORDER BY coins DESC LIMIT 1"))).fetchone()
        active_today   = (await session.execute(text(
            "SELECT COUNT(DISTINCT user_id) FROM activity_logs WHERE created_at > NOW() - INTERVAL '24 hours'"
        ))).scalar()

    richest_str = f"{richest.first_name} ({_fmt(richest.coins)} $)" if richest else "N/A"

    await update.message.reply_text(
        f"📊 <b>STATS GLOBALES — God Mode</b>\n\n"
        f"👥 Utilisateurs total : <b>{total_users}</b>\n"
        f"🟢 Actifs (24h) : <b>{active_today}</b>\n"
        f"🚫 Bannis : <b>{total_banned}</b>\n"
        f"🔒 En prison : <b>{total_prison}</b>\n\n"
        f"💰 Coins en circulation : <b>{_fmt(total_coins)} $</b>\n"
        f"🏦 Dépôts bancaires : <b>{_fmt(total_bank)} $</b>\n"
        f"💳 Prêts actifs : <b>{_fmt(total_loans)} $</b>\n\n"
        f"🏢 Entreprises actives : <b>{total_companies}</b>\n"
        f"👷 Employés actifs : <b>{total_emps}</b>\n\n"
        f"👑 Joueur le plus riche : <b>{richest_str}</b>",
        parse_mode=ParseMode.HTML
    )


# ─── /resetcooldown @user — Remet tous les cooldowns à zéro ──────────────────

async def resetcooldown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remet à zéro : last_daily, last_work, exam_cooldown."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    if not context.args:
        return await update.message.reply_text("❌ Usage : <code>/resetcooldown @user</code>", parse_mode="HTML")

    async with AsyncSessionLocal() as session:
        target = await _resolve_user(session, context.args[0])
        if not target:
            return await update.message.reply_text("❌ Utilisateur introuvable.")
        await session.execute(
            text("UPDATE users SET last_daily=NULL, last_work=NULL, exam_cooldown=NULL WHERE user_id=:uid"),
            {"uid": target.user_id}
        )
        await session.commit()

    await update.message.reply_text(
        f"✅ Cooldowns de <b>{target.first_name}</b> remis à zéro.\n"
        f"(daily, work, examen diplôme)",
        parse_mode="HTML"
    )


# ─── /addkarma @user montant — Modifier le karma ─────────────────────────────

async def addkarma(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ajoute ou retire du karma à un joueur."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    if len(context.args) < 2:
        return await update.message.reply_text("❌ Usage : <code>/addkarma @user montant</code>", parse_mode="HTML")

    try:
        amount = int(context.args[1])
    except ValueError:
        return await update.message.reply_text("❌ Montant invalide.")

    async with AsyncSessionLocal() as session:
        target = await _resolve_user(session, context.args[0])
        if not target:
            return await update.message.reply_text("❌ Utilisateur introuvable.")
        target.karma = max(-999, min(999, target.karma + amount))
        new_karma = target.karma
        await session.commit()

    sign = "+" if amount >= 0 else ""
    await update.message.reply_text(
        f"⭐ Karma de <b>{target.first_name}</b> : {sign}{amount} → <b>{new_karma}</b>",
        parse_mode="HTML"
    )


# ─── /wipeinventory @user — Vider l'inventaire d'enchères ────────────────────

async def wipeinventory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Supprime tous les objets d'enchères d'un joueur."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    if not context.args:
        return await update.message.reply_text("❌ Usage : <code>/wipeinventory @user</code>", parse_mode="HTML")

    async with AsyncSessionLocal() as session:
        target = await _resolve_user(session, context.args[0])
        if not target:
            return await update.message.reply_text("❌ Utilisateur introuvable.")
        result = await session.execute(
            text("DELETE FROM auction_items WHERE owner_id=:uid RETURNING id"),
            {"uid": target.user_id}
        )
        deleted = len(result.fetchall())
        await session.commit()

    await update.message.reply_text(
        f"🗑️ <b>{deleted}</b> objet(s) supprimé(s) de l'inventaire de <b>{target.first_name}</b>.",
        parse_mode="HTML"
    )


# ─── /resetbanque @user — Vider les comptes bancaires d'un joueur ────────────

async def resetbanque(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remet à zéro tous les comptes bancaires d'un joueur."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    if not context.args:
        return await update.message.reply_text("❌ Usage : <code>/resetbanque @user</code>", parse_mode="HTML")

    async with AsyncSessionLocal() as session:
        target = await _resolve_user(session, context.args[0])
        if not target:
            return await update.message.reply_text("❌ Utilisateur introuvable.")
        await session.execute(
            text("UPDATE bank_accounts SET balance=0 WHERE user_id=:uid"),
            {"uid": target.user_id}
        )
        await session.commit()

    await update.message.reply_text(
        f"🏦 Comptes bancaires de <b>{target.first_name}</b> remis à zéro.",
        parse_mode="HTML"
    )


# ─── /kickboite @user — Virer de force de son entreprise ─────────────────────

async def kickboite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Force le départ d'un employé de son entreprise (sans cooldown)."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    if not context.args:
        return await update.message.reply_text("❌ Usage : <code>/kickboite @user</code>", parse_mode="HTML")

    async with AsyncSessionLocal() as session:
        target = await _resolve_user(session, context.args[0])
        if not target:
            return await update.message.reply_text("❌ Utilisateur introuvable.")

        from datetime import datetime, timedelta
        bypass = datetime.utcnow() - timedelta(days=8)
        result = await session.execute(
            text("UPDATE company_employees SET left_at=:bypass WHERE user_id=:uid AND left_at IS NULL RETURNING company_id"),
            {"bypass": bypass, "uid": target.user_id}
        )
        rows = result.fetchall()
        await session.commit()

    if rows:
        await update.message.reply_text(
            f"✅ <b>{target.first_name}</b> a été expulsé de son entreprise (sans cooldown).",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(f"❌ {target.first_name} n'est dans aucune entreprise.")


# ─── /deletecompany nom — Supprimer une entreprise joueur ────────────────────

async def deletecompany(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dissout de force une entreprise (joueur seulement)."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    if not context.args:
        return await update.message.reply_text("❌ Usage : <code>/deletecompany [nom entreprise]</code>", parse_mode="HTML")

    name = " ".join(context.args)
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            text("SELECT id, name, is_bot_company FROM companies WHERE name ILIKE :n AND is_active=TRUE"),
            {"n": name}
        )).fetchone()

        if not row:
            return await update.message.reply_text(f"❌ Entreprise <b>{name}</b> introuvable.", parse_mode="HTML")
        if row.is_bot_company:
            return await update.message.reply_text("❌ Impossible de supprimer une entreprise officielle.")

        from datetime import datetime, timedelta
        bypass = datetime.utcnow() - timedelta(days=8)
        cid = row.id

        await session.execute(text("UPDATE company_employees SET left_at=:bp WHERE company_id=:cid AND left_at IS NULL"), {"bp": bypass, "cid": cid})
        await session.execute(text("UPDATE companies SET is_active=FALSE, treasury=0 WHERE id=:cid"), {"cid": cid})
        await session.commit()

    await update.message.reply_text(
        f"🏚️ L'entreprise <b>{row.name}</b> a été dissoute par un admin.",
        parse_mode="HTML"
    )


# ─── /forcepdg @user nom_entreprise — Nommer PDG de force ────────────────────

async def forcepdg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nomme de force un joueur PDG d'une entreprise."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    if len(context.args) < 2:
        return await update.message.reply_text("❌ Usage : <code>/forcepdg @user [nom entreprise]</code>", parse_mode="HTML")

    async with AsyncSessionLocal() as session:
        target = await _resolve_user(session, context.args[0])
        if not target:
            return await update.message.reply_text("❌ Utilisateur introuvable.")

        company_name = " ".join(context.args[1:])
        company = (await session.execute(
            text("SELECT id, name, owner_id FROM companies WHERE name ILIKE :n AND is_active=TRUE"),
            {"n": company_name}
        )).fetchone()

        if not company:
            return await update.message.reply_text(f"❌ Entreprise <b>{company_name}</b> introuvable.", parse_mode="HTML")

        from datetime import datetime
        # Dégrader l'ancien PDG
        await session.execute(
            text("UPDATE company_employees SET role='directeur' WHERE company_id=:cid AND role='pdg' AND left_at IS NULL"),
            {"cid": company.id}
        )
        # Vérifier si le target est déjà dans l'entreprise
        emp = (await session.execute(
            text("SELECT id FROM company_employees WHERE company_id=:cid AND user_id=:uid AND left_at IS NULL"),
            {"cid": company.id, "uid": target.user_id}
        )).fetchone()

        if emp:
            await session.execute(
                text("UPDATE company_employees SET role='pdg' WHERE company_id=:cid AND user_id=:uid AND left_at IS NULL"),
                {"cid": company.id, "uid": target.user_id}
            )
        else:
            await session.execute(
                text("INSERT INTO company_employees (company_id, user_id, role, joined_at) VALUES (:cid, :uid, 'pdg', :now)"),
                {"cid": company.id, "uid": target.user_id, "now": datetime.utcnow()}
            )
        await session.execute(
            text("UPDATE companies SET owner_id=:uid WHERE id=:cid"),
            {"uid": target.user_id, "cid": company.id}
        )
        await session.commit()

    await update.message.reply_text(
        f"👑 <b>{target.first_name}</b> est maintenant PDG de <b>{company.name}</b>.",
        parse_mode="HTML"
    )
    try:
        await context.bot.send_message(
            chat_id=target.user_id,
            text=f"👑 <b>Un admin t'a nommé PDG de {company.name} !</b>",
            parse_mode="HTML"
        )
    except Exception:
        pass


# ─── /purgeprison — Libérer TOUS les prisonniers ─────────────────────────────

async def purgeprison(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Libère tous les prisonniers d'un coup."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    async with AsyncSessionLocal() as session:
        result = await session.execute(text("DELETE FROM crime_prison RETURNING user_id"))
        freed = len(result.fetchall())
        await session.commit()

    await update.message.reply_text(
        f"🔓 <b>Amnistie générale !</b> {freed} prisonnier(s) libéré(s).",
        parse_mode="HTML"
    )


# ─── /freeze @user — Bloquer toutes les actions d'un joueur ──────────────────

async def freeze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Gèle un compte : coins mis à 0, prison longue durée (720h).
    /freeze @user
    /unfreeze @user pour dégeler
    """
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    if not context.args:
        return await update.message.reply_text("❌ Usage : <code>/freeze @user</code>", parse_mode="HTML")

    async with AsyncSessionLocal() as session:
        target = await _resolve_user(session, context.args[0])
        if not target:
            return await update.message.reply_text("❌ Utilisateur introuvable.")

        from datetime import datetime, timedelta
        released_at = datetime.utcnow() + timedelta(hours=720)

        # Mettre en prison longue durée
        existing = (await session.execute(
            text("SELECT id FROM crime_prison WHERE user_id=:uid"), {"uid": target.user_id}
        )).fetchone()
        if existing:
            await session.execute(
                text("UPDATE crime_prison SET released_at=:r WHERE user_id=:uid"),
                {"r": released_at, "uid": target.user_id}
            )
        else:
            await session.execute(
                text("INSERT INTO crime_prison (user_id, reason, released_at, bail_amount) VALUES (:uid, 'FREEZE ADMIN', :r, 999999999)"),
                {"uid": target.user_id, "r": released_at}
            )
        await session.commit()

    await update.message.reply_text(
        f"🧊 <b>{target.first_name}</b> est gelé pour <b>30 jours</b>.\n"
        f"Utilise <code>/unfreeze @{target.username or target.first_name}</code> pour dégeler.",
        parse_mode="HTML"
    )
    try:
        await context.bot.send_message(
            chat_id=target.user_id,
            text="🧊 <b>Ton compte a été gelé par un administrateur.</b>\nContacte le support.",
            parse_mode="HTML"
        )
    except Exception:
        pass


async def unfreeze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dégèle un compte gelé par /freeze."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    if not context.args:
        return await update.message.reply_text("❌ Usage : <code>/unfreeze @user</code>", parse_mode="HTML")

    async with AsyncSessionLocal() as session:
        target = await _resolve_user(session, context.args[0])
        if not target:
            return await update.message.reply_text("❌ Utilisateur introuvable.")
        await session.execute(text("DELETE FROM crime_prison WHERE user_id=:uid"), {"uid": target.user_id})
        await session.commit()

    await update.message.reply_text(
        f"✅ <b>{target.first_name}</b> a été dégelé.",
        parse_mode="HTML"
    )


# ─── /setkarma @user valeur — Définir karma exact ────────────────────────────

async def setkarma(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    if len(context.args) < 2:
        return await update.message.reply_text("❌ Usage : <code>/setkarma @user valeur</code>", parse_mode="HTML")
    try:
        val = int(context.args[1])
    except ValueError:
        return await update.message.reply_text("❌ Valeur invalide.")

    async with AsyncSessionLocal() as session:
        target = await _resolve_user(session, context.args[0])
        if not target:
            return await update.message.reply_text("❌ Introuvable.")
        target.karma = max(-999, min(999, val))
        await session.commit()

    await update.message.reply_text(
        f"⭐ Karma de <b>{target.first_name}</b> → <b>{val}</b>",
        parse_mode="HTML"
    )


# ─── /inflation pct — Augmenter tous les soldes d'un % ───────────────────────

async def inflation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /inflation 10  → +10% sur tous les coins de tous les joueurs
    /inflation -20 → -20% (déflation)
    """
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    if not context.args:
        return await update.message.reply_text("❌ Usage : <code>/inflation [pourcentage]</code>\nEx: /inflation 10 ou /inflation -20", parse_mode="HTML")

    try:
        pct = float(context.args[0])
    except ValueError:
        return await update.message.reply_text("❌ Pourcentage invalide.")

    multiplier = 1 + (pct / 100)
    msg = await update.message.reply_text(f"⏳ Application de {pct:+.1f}% sur tous les soldes...")

    async with AsyncSessionLocal() as session:
        users_result = await session.execute(text("SELECT user_id, coins FROM users"))
        users = users_result.fetchall()
        for u in users:
            new_coins = max(1000, int(u.coins * multiplier))
            await session.execute(
                text("UPDATE users SET coins=:c WHERE user_id=:uid"),
                {"c": new_coins, "uid": u.user_id}
            )
        await session.commit()

    arrow = "📈" if pct >= 0 else "📉"
    await msg.edit_text(
        f"{arrow} <b>{'Inflation' if pct >= 0 else 'Déflation'} appliquée !</b>\n\n"
        f"Variation : <b>{pct:+.1f}%</b>\n"
        f"Joueurs affectés : <b>{len(users)}</b>\n"
        f"Solde minimum garanti : <b>1 000 $</b>",
        parse_mode="HTML"
    )


# ─── /checkuser @user — Fiche complète God Mode ──────────────────────────────

async def checkuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fiche ultra-complète : coins, banque, prison, entreprise, diplômes, famille."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    if not context.args:
        return await update.message.reply_text("❌ Usage : <code>/checkuser @user</code>", parse_mode="HTML")

    async with AsyncSessionLocal() as session:
        target = await _resolve_user(session, context.args[0])
        if not target:
            return await update.message.reply_text("❌ Utilisateur introuvable.")

        uid = target.user_id

        # Banque
        banks = (await session.execute(
            text("SELECT bank_id, balance FROM bank_accounts WHERE user_id=:uid"), {"uid": uid}
        )).fetchall()
        bank_str = " | ".join(f"{b.bank_id}: {_fmt(b.balance)}$" for b in banks) or "Aucun"

        # Prison
        prison = (await session.execute(
            text("SELECT released_at, reason FROM crime_prison WHERE user_id=:uid"), {"uid": uid}
        )).fetchone()
        prison_str = f"🔒 jusqu'au {prison.released_at.strftime('%d/%m %H:%M')}" if prison else "🟢 Libre"

        # Entreprise
        emp_row = (await session.execute(
            text("""SELECT ce.role, c.name FROM company_employees ce
                    JOIN companies c ON c.id=ce.company_id
                    WHERE ce.user_id=:uid AND ce.left_at IS NULL AND c.is_active=TRUE"""),
            {"uid": uid}
        )).fetchone()
        company_str = f"{emp_row.name} ({emp_row.role})" if emp_row else "Aucune"

        # Diplômes
        diplomes = []
        if target.diplome_bac:     diplomes.append("📄 Bac")
        if target.diplome_licence: diplomes.append(f"🎓 Licence {target.diplome_domain or ''}")
        if target.diplome_master:  diplomes.append("🏅 Master")
        if target.diplome_mba:     diplomes.append("👑 MBA")
        diplomes_str = " · ".join(diplomes) or "Aucun"

        # Activité récente
        last_cmd = (await session.execute(
            text("SELECT command, created_at FROM activity_logs WHERE user_id=:uid ORDER BY created_at DESC LIMIT 1"),
            {"uid": uid}
        )).fetchone()
        last_cmd_str = f"/{last_cmd.command} ({last_cmd.created_at.strftime('%d/%m %H:%M')})" if last_cmd else "N/A"

        # Loans
        loans = (await session.execute(
            text("SELECT COALESCE(SUM(amount),0) as total FROM loans WHERE user_id=:uid AND status='active'"),
            {"uid": uid}
        )).fetchone()
        loans_str = _fmt(loans.total) + " $" if loans else "0 $"

    banned_str = "🚫 OUI" if target.is_banned else "✅ NON"

    await update.message.reply_text(
        f"🔍 <b>FICHE GOD — {target.first_name}</b>\n"
        f"├ ID : <code>{uid}</code>\n"
        f"├ @{target.username or 'sans username'}\n"
        f"├ Banni : {banned_str}\n"
        f"├ Prison : {prison_str}\n\n"
        f"💰 <b>FORTUNE</b>\n"
        f"├ Coins : <b>{_fmt(target.coins)} $</b>\n"
        f"├ Banque : {bank_str}\n"
        f"├ Prêts actifs : {loans_str}\n\n"
        f"🏢 <b>ENTREPRISE</b> : {company_str}\n"
        f"🎓 <b>DIPLÔMES</b> : {diplomes_str}\n\n"
        f"⌨️ <b>DERNIÈRE COMMANDE</b> : {last_cmd_str}\n"
        f"📅 Inscrit le : {target.created_at.strftime('%d/%m/%Y') if target.created_at else 'N/A'}",
        parse_mode="HTML"
    )


# ─── /setreputation nom valeur — Modifier réputation entreprise ───────────────

async def setreputation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Modifier la réputation d'une entreprise."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    if len(context.args) < 2:
        return await update.message.reply_text("❌ Usage : <code>/setreputation [nom entreprise] [0.0-5.0]</code>", parse_mode="HTML")

    try:
        val = float(context.args[-1])
        val = max(0.0, min(5.0, val))
    except ValueError:
        return await update.message.reply_text("❌ Valeur invalide (0.0 à 5.0).")

    name = " ".join(context.args[:-1])
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("UPDATE companies SET reputation=:v WHERE name ILIKE :n AND is_active=TRUE RETURNING name"),
            {"v": val, "n": name}
        )
        updated = result.fetchone()
        await session.commit()

    if updated:
        await update.message.reply_text(f"⭐ Réputation de <b>{updated.name}</b> → <b>{val}/5.0</b>", parse_mode="HTML")
    else:
        await update.message.reply_text(f"❌ Entreprise <b>{name}</b> introuvable.", parse_mode="HTML")


# ─── /addvalue nom montant — Modifier valeur entreprise ──────────────────────

async def addvalue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ajouter ou retirer de la valeur à une entreprise."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    if len(context.args) < 2:
        return await update.message.reply_text("❌ Usage : <code>/addvalue [nom] [montant]</code>\nMontant négatif pour réduire.", parse_mode="HTML")

    try:
        amount = int(context.args[-1].replace("_", ""))
    except ValueError:
        return await update.message.reply_text("❌ Montant invalide.")

    name = " ".join(context.args[:-1])
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("UPDATE companies SET value=GREATEST(1000000, value+:a) WHERE name ILIKE :n AND is_active=TRUE RETURNING name, value"),
            {"a": amount, "n": name}
        )
        updated = result.fetchone()
        await session.commit()

    if updated:
        sign = "+" if amount >= 0 else ""
        await update.message.reply_text(
            f"💰 Valeur de <b>{updated.name}</b> : {sign}{_fmt(amount)} $ → <b>{_fmt(updated.value)} $</b>",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(f"❌ Entreprise <b>{name}</b> introuvable.", parse_mode="HTML")


# ─── /wipeloans @user — Effacer tous les prêts ───────────────────────────────

async def wipeloans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Efface tous les prêts actifs d'un joueur (marqués remboursés)."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    if not context.args:
        return await update.message.reply_text("❌ Usage : <code>/wipeloans @user</code>", parse_mode="HTML")

    async with AsyncSessionLocal() as session:
        target = await _resolve_user(session, context.args[0])
        if not target:
            return await update.message.reply_text("❌ Introuvable.")
        result = await session.execute(
            text("UPDATE loans SET status='repaid' WHERE user_id=:uid AND status='active' RETURNING id"),
            {"uid": target.user_id}
        )
        count = len(result.fetchall())
        await session.commit()

    await update.message.reply_text(
        f"✅ <b>{count}</b> prêt(s) effacé(s) pour <b>{target.first_name}</b>.",
        parse_mode="HTML"
    )


# ─── /broadcastdm message — DM à tous les utilisateurs ──────────────────────

async def broadcastdm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envoie un DM à TOUS les utilisateurs (avec compteur succès/échec)."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    if not context.args:
        return await update.message.reply_text("❌ Usage : <code>/broadcastdm [message]</code>", parse_mode="HTML")

    message_text = " ".join(context.args)
    msg = await update.message.reply_text("📨 Envoi DM en cours...")

    async with AsyncSessionLocal() as session:
        users_result = await session.execute(text("SELECT user_id FROM users WHERE is_banned=FALSE"))
        users = users_result.fetchall()

    success, fail = 0, 0
    for row in users:
        try:
            await context.bot.send_message(
                chat_id=row.user_id,
                text=f"📢 <b>Message de l'administration :</b>\n\n{message_text}",
                parse_mode="HTML"
            )
            success += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.3)  # max ~3 DMs/sec — évite le 429

    await msg.edit_text(
        f"📨 <b>Broadcast DM terminé</b>\n\n"
        f"✅ Envoyés : <b>{success}</b>\n"
        f"❌ Échecs : <b>{fail}</b>",
        parse_mode="HTML"
    )


# ─── /topactifs — Top 10 joueurs les plus actifs ─────────────────────────────

async def topactifs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Top 10 des joueurs les plus actifs (commandes 7 derniers jours)."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text(
            """SELECT u.first_name, u.username, COUNT(al.id) as cmd_count
               FROM activity_logs al
               JOIN users u ON u.user_id = al.user_id
               WHERE al.created_at > NOW() - INTERVAL '7 days'
               GROUP BY u.user_id, u.first_name, u.username
               ORDER BY cmd_count DESC LIMIT 10"""
        ))).fetchall()

    if not rows:
        return await update.message.reply_text("Aucune activité sur les 7 derniers jours.")

    lines = ["🏆 <b>Top Actifs — 7 derniers jours</b>\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(rows):
        medal = medals[i] if i < 3 else f"{i+1}."
        tag = f"@{r.username}" if r.username else r.first_name
        lines.append(f"{medal} {tag} — <b>{r.cmd_count}</b> commandes")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── /mutecompany nom — Bloquer les revenus d'une entreprise ─────────────────

async def mutecompany(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Désactive (is_active=FALSE) une entreprise temporairement."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    if not context.args:
        return await update.message.reply_text("❌ Usage : <code>/mutecompany [nom]</code>", parse_mode="HTML")

    name = " ".join(context.args)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("UPDATE companies SET is_active=FALSE WHERE name ILIKE :n AND is_active=TRUE RETURNING name"),
            {"n": name}
        )
        updated = result.fetchone()
        await session.commit()

    if updated:
        await update.message.reply_text(f"⛔ <b>{updated.name}</b> a été suspendue.", parse_mode="HTML")
    else:
        await update.message.reply_text(f"❌ Introuvable ou déjà inactive.", parse_mode="HTML")


async def unmutecompany(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Réactive une entreprise suspendue."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    if not context.args:
        return await update.message.reply_text("❌ Usage : <code>/unmutecompany [nom]</code>", parse_mode="HTML")

    name = " ".join(context.args)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("UPDATE companies SET is_active=TRUE WHERE name ILIKE :n AND is_active=FALSE RETURNING name"),
            {"n": name}
        )
        updated = result.fetchone()
        await session.commit()

    if updated:
        await update.message.reply_text(f"✅ <b>{updated.name}</b> a été réactivée.", parse_mode="HTML")
    else:
        await update.message.reply_text(f"❌ Introuvable ou déjà active.", parse_mode="HTML")


# ─── Helper interne : résoudre @user ou ID ───────────────────────────────────

async def _resolve_user(session, arg: str):
    """Retourne un User depuis @username ou ID numérique."""
    arg = arg.lstrip("@")
    if arg.isdigit():
        from database.db import get_user
        return await get_user(session, int(arg))
    else:
        from database.db import get_user_by_username
        return await get_user_by_username(session, arg)


# ─── /adminboites — Vue admin complète de toutes les entreprises ──────────────

async def adminboites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Liste toutes les entreprises avec valeur, tréso, nb employés, revenus."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text(
            """
            SELECT c.id, c.name, c.sector, c.level, c.value, c.treasury,
                   c.reputation, c.is_active, c.is_bot_company,
                   COUNT(e.user_id) AS nb_emp
            FROM companies c
            LEFT JOIN company_employees e ON e.company_id = c.id AND e.left_at IS NULL
            GROUP BY c.id
            ORDER BY c.value DESC
            """
        ))).fetchall()

    if not rows:
        return await update.message.reply_text("❌ Aucune entreprise trouvée.")

    LEVEL_NAMES = {1:"Startup",2:"PME",3:"Société",4:"Corporation",5:"Holding"}
    SECTOR_EMO  = {"tech":"💻","finance":"📈","commerce":"🛒","droit":"⚖️","agriculture":"🌾","securite":"🛡️","immobilier":"🏗️","sante":"🏥"}
    # Taux mensuel par niveau
    RATES = {1:0.04, 2:0.06, 3:0.08, 4:0.10, 5:0.12}

    lines = ["🏢 <b>ADMIN — Toutes les entreprises</b>\n"]
    for r in rows:
        daily = int(r.value * RATES.get(r.level, 0.04) / 30)
        em    = SECTOR_EMO.get(r.sector, "🏢")
        state = "🟢" if r.is_active else "🔴"
        bot   = "🤖" if r.is_bot_company else ""
        lines.append(
            f"{state}{bot} <b>{r.name}</b>  {em} niv.{r.level} {LEVEL_NAMES.get(r.level,'?')}\n"
            f"  💰 Valeur : <code>{_fmt(r.value)}</code>  |  🏦 Tréso : <code>{_fmt(r.treasury)}</code>\n"
            f"  📈 Rev/jour estimé : <code>{_fmt(daily)}</code>  |  👥 Employés : {r.nb_emp}  |  ⭐ {r.reputation:.1f}/5\n"
        )

    # Telegram limite à 4096 chars — envoyer par chunks
    msg = "\n".join(lines)
    for i in range(0, len(msg), 4000):
        await update.message.reply_text(msg[i:i+4000], parse_mode="HTML")


# ─── /adminboite nom — Détail admin d'UNE entreprise ─────────────────────────

async def adminboite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fiche complète : employés, salaires, gains/pertes estimés."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    if not context.args:
        return await update.message.reply_text("❌ Usage : <code>/adminboite NomEntreprise</code>", parse_mode="HTML")

    name = " ".join(context.args)

    async with AsyncSessionLocal() as session:
        comp = (await session.execute(
            text("SELECT * FROM companies WHERE LOWER(name)=LOWER(:n) LIMIT 1"), {"n": name}
        )).fetchone()
        if not comp:
            return await update.message.reply_text(f"❌ Entreprise <b>{name}</b> introuvable.", parse_mode="HTML")

        emps = (await session.execute(text(
            """
            SELECT e.role, e.joined_at, e.command_count,
                   u.user_id, u.username, u.first_name, u.coins
            FROM company_employees e
            JOIN users u ON u.user_id = e.user_id
            WHERE e.company_id = :cid AND e.left_at IS NULL
            ORDER BY e.role
            """
        ), {"cid": comp.id})).fetchall()

        logs = (await session.execute(text(
            """
            SELECT event_type, description, amount, created_at
            FROM company_logs
            WHERE company_id = :cid
            ORDER BY created_at DESC LIMIT 10
            """
        ), {"cid": comp.id})).fetchall()

    RATES = {1:0.04, 2:0.06, 3:0.08, 4:0.10, 5:0.12}
    ROLE_SHARE = {"stagiaire":0.0,"employe":0.10,"manager":0.20,"directeur":0.35,"pdg":0.0}
    daily_total = int(comp.value * RATES.get(comp.level, 0.04) / 30)

    lines = [
        f"🔍 <b>ADMIN — {comp.name}</b>",
        f"Secteur : {comp.sector}  |  Niveau : {comp.level}  |  ⭐ {comp.reputation:.1f}/5",
        f"💰 Valeur : <code>{_fmt(comp.value)}</code>  |  🏦 Tréso : <code>{_fmt(comp.treasury)}</code>",
        f"📈 Revenu journalier estimé : <code>{_fmt(daily_total)}</code>",
        f"État : {'🟢 Active' if comp.is_active else '🔴 Inactive'}  |  {'🤖 Bot' if comp.is_bot_company else '👤 Joueur'}",
        "",
        "👥 <b>EMPLOYÉS &amp; SALAIRES</b>",
    ]

    total_salaires = 0
    for e in emps:
        share  = ROLE_SHARE.get(e.role, 0)
        salary = int(daily_total * share)
        total_salaires += salary
        name_d = f"@{e.username}" if e.username else e.first_name
        uid_d  = f"(ID:{e.user_id})"
        joined = e.joined_at.strftime("%d/%m/%y") if e.joined_at else "—"
        lines.append(
            f"  • {name_d} {uid_d} — <b>{e.role.capitalize()}</b>\n"
            f"    💵 Salaire/jour : <code>{_fmt(salary)}</code>  |  📅 Depuis : {joined}  |  🖱 Cmds : {e.command_count}"
        )

    pdg_cut = daily_total - total_salaires
    lines += [
        "",
        f"💸 Total salaires employés/jour : <code>{_fmt(total_salaires)}</code>",
        f"👑 Part PDG (dividendes) : <code>{_fmt(pdg_cut)}</code>",
        "",
        "📋 <b>10 DERNIERS LOGS</b>",
    ]
    for l in logs:
        amt  = f"  [{_fmt(l.amount)}]" if l.amount else ""
        date = l.created_at.strftime("%d/%m %H:%M") if l.created_at else "—"
        lines.append(f"  [{date}] {l.event_type}{amt} — {l.description}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── /statsusers — Statistiques d'utilisation du bot ─────────────────────────

async def statsusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nombre d'utilisateurs actifs par période + total."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    async with AsyncSessionLocal() as session:
        total     = (await session.execute(text("SELECT COUNT(*) FROM users"))).scalar()
        bannis    = (await session.execute(text("SELECT COUNT(*) FROM users WHERE is_banned=TRUE"))).scalar()
        actifs_1h = (await session.execute(text(
            "SELECT COUNT(DISTINCT user_id) FROM activity_logs WHERE created_at > NOW() - INTERVAL '1 hour'"
        ))).scalar()
        actifs_24h = (await session.execute(text(
            "SELECT COUNT(DISTINCT user_id) FROM activity_logs WHERE created_at > NOW() - INTERVAL '24 hours'"
        ))).scalar()
        actifs_7j  = (await session.execute(text(
            "SELECT COUNT(DISTINCT user_id) FROM activity_logs WHERE created_at > NOW() - INTERVAL '7 days'"
        ))).scalar()
        actifs_30j = (await session.execute(text(
            "SELECT COUNT(DISTINCT user_id) FROM activity_logs WHERE created_at > NOW() - INTERVAL '30 days'"
        ))).scalar()
        nouveaux_7j = (await session.execute(text(
            "SELECT COUNT(*) FROM users WHERE created_at > NOW() - INTERVAL '7 days'"
        ))).scalar()
        nouveaux_30j = (await session.execute(text(
            "SELECT COUNT(*) FROM users WHERE created_at > NOW() - INTERVAL '30 days'"
        ))).scalar()
        cmds_24h = (await session.execute(text(
            "SELECT COUNT(*) FROM activity_logs WHERE created_at > NOW() - INTERVAL '24 hours'"
        ))).scalar()
        cmds_7j = (await session.execute(text(
            "SELECT COUNT(*) FROM activity_logs WHERE created_at > NOW() - INTERVAL '7 days'"
        ))).scalar()
        top_cmds = (await session.execute(text(
            """
            SELECT command, COUNT(*) as nb
            FROM activity_logs
            WHERE created_at > NOW() - INTERVAL '7 days' AND command IS NOT NULL
            GROUP BY command ORDER BY nb DESC LIMIT 5
            """
        ))).fetchall()

    taux = round((actifs_24h / total * 100), 1) if total else 0

    lines = [
        "📊 <b>STATS UTILISATEURS — Bot</b>",
        "",
        f"👥 Total inscrits : <b>{total}</b>",
        f"🚫 Bannis : <b>{bannis}</b>",
        f"✅ Actifs (non bannis) : <b>{total - bannis}</b>",
        "",
        "⚡ <b>ACTIVITÉ</b>",
        f"🟢 Actifs dernière heure : <b>{actifs_1h}</b>",
        f"🟡 Actifs 24h : <b>{actifs_24h}</b>  ({taux}% du total)",
        f"🔵 Actifs 7 jours : <b>{actifs_7j}</b>",
        f"⚪ Actifs 30 jours : <b>{actifs_30j}</b>",
        "",
        "🆕 <b>NOUVEAUX INSCRITS</b>",
        f"  Cette semaine : <b>{nouveaux_7j}</b>",
        f"  Ce mois : <b>{nouveaux_30j}</b>",
        "",
        "🖱 <b>COMMANDES</b>",
        f"  24h : <b>{cmds_24h}</b>",
        f"  7 jours : <b>{cmds_7j}</b>",
        "",
        "🏆 <b>TOP 5 COMMANDES (7j)</b>",
    ]
    for row in top_cmds:
        lines.append(f"  /{row.command} — {row.nb} fois")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── /adminparts — Gestion admin des parts d'une entreprise ──────────────────

async def adminparts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /adminparts <NomEntreprise>                       → voir les parts de tous les membres
    /adminparts <NomEntreprise> add @user <qty>       → ajouter des parts
    /adminparts <NomEntreprise> remove @user <qty>    → retirer des parts
    /adminparts <NomEntreprise> set @user <qty>       → définir un montant exact
    """
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    if not context.args:
        return await update.message.reply_text(
            "❌ Usage :\n"
            "<code>/adminparts NomEntreprise</code>\n"
            "<code>/adminparts NomEntreprise add @user quantite</code>\n"
            "<code>/adminparts NomEntreprise remove @user quantite</code>\n"
            "<code>/adminparts NomEntreprise set @user quantite</code>",
            parse_mode="HTML"
        )

    # ── Parser les args ──────────────────────────────────────────────────────
    # Détecter si c'est une action (add/remove/set) ou juste une vue
    # Format : /adminparts [action?] [company ...] [@user qty?]
    # On cherche le mot clé action en position 0
    ACTION_KEYWORDS = {"add", "remove", "set"}
    action = None
    target_mention = None
    qty = None
    company_name_parts = []

    args = context.args

    # Chercher l'action dans les args
    if len(args) >= 4 and args[-3].lower() in ACTION_KEYWORDS:
        action = args[-3].lower()
        target_mention = args[-2].lstrip("@")
        try:
            qty = int(args[-1])
        except ValueError:
            return await update.message.reply_text("❌ Quantité invalide.")
        company_name_parts = args[:-3]
    else:
        # Pas d'action → vue seule
        company_name_parts = args

    company_name = " ".join(company_name_parts)
    if not company_name:
        return await update.message.reply_text("❌ Nom d'entreprise manquant.")

    async with AsyncSessionLocal() as session:
        # Récupérer l'entreprise
        comp = (await session.execute(
            text("SELECT * FROM companies WHERE LOWER(name)=LOWER(:n) LIMIT 1"),
            {"n": company_name}
        )).fetchone()
        if not comp:
            return await update.message.reply_text(
                f"❌ Entreprise <b>{company_name}</b> introuvable.", parse_mode="HTML"
            )

        # ── VUE SEULE ────────────────────────────────────────────────────────
        if action is None:
            shares = (await session.execute(text(
                """
                SELECT cs.owner_id, cs.quantity, u.username, u.first_name
                FROM company_shares cs
                LEFT JOIN users u ON u.user_id = cs.owner_id
                WHERE cs.company_id = :cid AND cs.quantity > 0
                ORDER BY cs.quantity DESC
                """
            ), {"cid": comp.id})).fetchall()

            if not shares:
                return await update.message.reply_text(
                    f"📦 Aucune part enregistrée pour <b>{comp.name}</b>.", parse_mode="HTML"
                )

            price_per = comp.value // comp.total_shares if comp.total_shares else 0
            lines = [
                f"📦 <b>ADMIN — Parts de {comp.name}</b>",
                f"Total : <b>{comp.total_shares} parts</b>  |  💰 Prix/part : <b>{_fmt(price_per)} $</b>",
                f"PDG actuel : owner_shares=<b>{comp.owner_shares}</b>",
                "",
            ]
            for s in shares:
                name_d = f"@{s.username}" if s.username else (s.first_name or f"ID:{s.owner_id}")
                pct = (s.quantity / comp.total_shares * 100) if comp.total_shares else 0
                valeur = s.quantity * price_per
                lines.append(
                    f"• <b>{name_d}</b> (uid:{s.owner_id})\n"
                    f"  📦 {s.quantity} parts ({pct:.1f}%)  |  💰 Valeur : {_fmt(valeur)} $"
                )

            lines.append(
                f"\n💡 Modifier : <code>/adminparts {comp.name} add/remove/set @user quantite</code>"
            )
            return await update.message.reply_text("\n".join(lines), parse_mode="HTML")

        # ── ACTION (add / remove / set) ───────────────────────────────────────
        if qty is None or qty < 0:
            return await update.message.reply_text("❌ Quantité invalide (doit être ≥ 0).")

        # Résoudre l'utilisateur cible
        target_user = (await session.execute(
            text("SELECT * FROM users WHERE LOWER(username)=LOWER(:u) LIMIT 1"),
            {"u": target_mention}
        )).fetchone()
        if not target_user:
            return await update.message.reply_text(
                f"❌ Utilisateur @{target_mention} introuvable (doit avoir utilisé le bot)."
            )

        target_id = target_user.user_id
        name_d = f"@{target_user.username}" if target_user.username else target_user.first_name

        # Récupérer la ligne de parts actuelle
        share_row = (await session.execute(
            text("SELECT * FROM company_shares WHERE company_id=:cid AND owner_id=:uid LIMIT 1"),
            {"cid": comp.id, "uid": target_id}
        )).fetchone()
        current_qty = share_row.quantity if share_row else 0

        if action == "add":
            new_qty = current_qty + qty
        elif action == "remove":
            new_qty = max(0, current_qty - qty)
        elif action == "set":
            new_qty = qty
        else:
            return await update.message.reply_text("❌ Action invalide.")

        # Appliquer
        if share_row:
            await session.execute(
                text("UPDATE company_shares SET quantity=:q WHERE company_id=:cid AND owner_id=:uid"),
                {"q": new_qty, "cid": comp.id, "uid": target_id}
            )
        else:
            await session.execute(
                text("INSERT INTO company_shares (company_id, owner_id, quantity, acquired_at) VALUES (:cid, :uid, :q, NOW())"),
                {"cid": comp.id, "uid": target_id, "q": new_qty}
            )

        # Si c'est le PDG, mettre à jour owner_shares aussi
        if target_id == comp.owner_id:
            await session.execute(
                text("UPDATE companies SET owner_shares=:q WHERE id=:cid"),
                {"q": new_qty, "cid": comp.id}
            )

        # Log dans company_logs
        await session.execute(
            text(
                "INSERT INTO company_logs (company_id, event_type, description, created_at) "
                "VALUES (:cid, 'admin_parts', :desc, NOW())"
            ),
            {
                "cid": comp.id,
                "desc": f"[ADMIN] {action.upper()} parts {name_d} : {current_qty} → {new_qty}"
            }
        )

        await session.commit()

        price_per = comp.value // comp.total_shares if comp.total_shares else 0
        valeur_new = new_qty * price_per
        action_emoji = {"add": "➕", "remove": "➖", "set": "🔧"}[action]

        await update.message.reply_text(
            f"{action_emoji} <b>Parts mises à jour — {comp.name}</b>\n\n"
            f"👤 {name_d} (uid:{target_id})\n"
            f"📦 Avant : <b>{current_qty} parts</b>\n"
            f"📦 Après : <b>{new_qty} parts</b>\n"
            f"💰 Valeur portefeuille : <b>{_fmt(valeur_new)} $</b>\n\n"
            f"✅ Modifié et loggé.",
            parse_mode="HTML"
        )


# ─── /auditboite [nom] — Détecter le spam dépôt/retrait ──────────────────────

async def auditboite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Analyse les logs d'une entreprise et détecte le spam dépôt/retrait."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    if not context.args:
        return await update.message.reply_text(
            "❌ Usage : <code>/auditboite [nom entreprise]</code>",
            parse_mode="HTML"
        )

    name = " ".join(context.args)

    async with AsyncSessionLocal() as session:
        comp = (await session.execute(
            text("SELECT * FROM companies WHERE LOWER(name)=LOWER(:n) AND is_active=TRUE LIMIT 1"),
            {"n": name}
        )).fetchone()
        if not comp:
            return await update.message.reply_text(f"❌ Entreprise <b>{name}</b> introuvable.", parse_mode="HTML")

        logs = (await session.execute(text(
            """
            SELECT event_type, amount, created_at
            FROM company_logs
            WHERE company_id = :cid AND event_type IN ('depot', 'retrait')
            ORDER BY created_at ASC
            """
        ), {"cid": comp.id})).fetchall()

    if not logs:
        return await update.message.reply_text(
            f"✅ <b>{comp.name}</b> — Aucun log de dépôt/retrait trouvé.",
            parse_mode="HTML"
        )

    # Analyse
    total_depot   = sum(l.amount or 0 for l in logs if l.event_type == "depot")
    total_retrait = sum(l.amount or 0 for l in logs if l.event_type == "retrait")
    nb_depot      = sum(1 for l in logs if l.event_type == "depot")
    nb_retrait    = sum(1 for l in logs if l.event_type == "retrait")

    # Détecter les cycles rapides (dépôt suivi d'un retrait dans les 60s)
    cycles_suspects = 0
    gain_illégitime = 0
    for i in range(len(logs) - 1):
        a, b = logs[i], logs[i + 1]
        if a.event_type == "depot" and b.event_type == "retrait":
            delta = (b.created_at - a.created_at).total_seconds()
            if delta < 120:  # moins de 2 minutes = suspect
                cycles_suspects += 1
                gain_illégitime += (a.amount or 0)

    # Valeur légitime estimée = valeur actuelle - gain illégitime
    valeur_actuelle   = comp.value
    valeur_estimée    = max(50_000_000, valeur_actuelle - gain_illégitime)
    surplus           = valeur_actuelle - valeur_estimée

    verdict = "🚨 EXPLOIT DÉTECTÉ" if cycles_suspects >= 3 else ("⚠️ Activité suspecte" if cycles_suspects >= 1 else "✅ Aucune anomalie")

    lines = [
        f"🔍 <b>AUDIT — {comp.name}</b>",
        f"",
        f"📊 <b>Résumé des mouvements</b>",
        f"  💰 Dépôts   : <b>{nb_depot} opérations</b> → <code>{_fmt(total_depot)} $</code>",
        f"  💸 Retraits : <b>{nb_retrait} opérations</b> → <code>{_fmt(total_retrait)} $</code>",
        f"  🔁 Cycles suspects (&lt;2min) : <b>{cycles_suspects}</b>",
        f"",
        f"📈 <b>Impact sur la valeur</b>",
        f"  Valeur actuelle   : <code>{_fmt(valeur_actuelle)} $</code>",
        f"  Gain illégitime ≈ : <code>{_fmt(gain_illégitime)} $</code>",
        f"  Valeur corrigée ≈ : <code>{_fmt(valeur_estimée)} $</code>",
        f"",
        f"<b>Verdict : {verdict}</b>",
        f"",
        f"🛠 <b>Actions disponibles :</b>",
        f"  <code>/setvalue {comp.name} {valeur_estimée}</code> — corriger la valeur",
        f"  <code>/addvalue {comp.name} -{surplus}</code> — retirer le surplus",
        f"  <code>/resetboite {comp.name}</code> — reset complet au niveau initial",
        f"  <code>/deletecompany {comp.name}</code> — supprimer l'entreprise",
    ]

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── /setvalue [nom] [montant] — Fixer la valeur exacte ─────────────────────

async def setvalue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fixe la valeur d'une entreprise à un montant exact."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    if len(context.args) < 2:
        return await update.message.reply_text(
            "❌ Usage : <code>/setvalue [nom] [montant]</code>",
            parse_mode="HTML"
        )

    try:
        montant = int(context.args[-1].replace("_", "").replace(" ", ""))
    except ValueError:
        return await update.message.reply_text("❌ Montant invalide.")

    if montant < 1_000_000:
        return await update.message.reply_text("❌ Valeur minimum : 1 000 000 $")

    name = " ".join(context.args[:-1])

    async with AsyncSessionLocal() as session:
        result = (await session.execute(
            text("UPDATE companies SET value=:v WHERE LOWER(name)=LOWER(:n) AND is_active=TRUE RETURNING name, value"),
            {"v": montant, "n": name}
        )).fetchone()
        await session.commit()

    if result:
        await update.message.reply_text(
            f"🔧 <b>{result.name}</b> — Valeur fixée à <code>{_fmt(result.value)} $</code>",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(f"❌ Entreprise <b>{name}</b> introuvable.", parse_mode="HTML")


# ─── /resetboite [nom] — Reset valeur + trésorerie au niveau initial ─────────

LEVEL_BASE_VALUE = {1: 50_000_000, 2: 200_000_000, 3: 500_000_000, 4: 1_000_000_000, 5: 5_000_000_000}

async def resetboite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remet la valeur et la trésorerie d'une entreprise à l'état initial de son niveau."""
    if not await is_admin(update.effective_user.id):
        return await _deny(update)
    if not context.args:
        return await update.message.reply_text(
            "❌ Usage : <code>/resetboite [nom entreprise]</code>",
            parse_mode="HTML"
        )

    name = " ".join(context.args)

    async with AsyncSessionLocal() as session:
        comp = (await session.execute(
            text("SELECT * FROM companies WHERE LOWER(name)=LOWER(:n) AND is_active=TRUE LIMIT 1"),
            {"n": name}
        )).fetchone()
        if not comp:
            return await update.message.reply_text(f"❌ Entreprise <b>{name}</b> introuvable.", parse_mode="HTML")

        ancienne_valeur = comp.value
        ancienne_treso  = comp.treasury
        base_value      = LEVEL_BASE_VALUE.get(comp.level, 50_000_000)

        await session.execute(
            text("UPDATE companies SET value=:v, treasury=0 WHERE id=:cid"),
            {"v": base_value, "cid": comp.id}
        )
        await session.commit()

    await update.message.reply_text(
        f"🔄 <b>Reset — {comp.name}</b>\n\n"
        f"  Valeur avant    : <code>{_fmt(ancienne_valeur)} $</code>\n"
        f"  Valeur après    : <code>{_fmt(base_value)} $</code>\n"
        f"  Trésorerie avant: <code>{_fmt(ancienne_treso)} $</code>\n"
        f"  Trésorerie après: <code>0 $</code>\n\n"
        f"✅ Entreprise remise à l'état initial du niveau {comp.level}.",
        parse_mode="HTML"
    )


# ─── /admintransfert — Changer le propriétaire et redistribuer les parts ──────

async def admintransfert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Change le propriétaire (PDG) d'une entreprise et/ou redistribue librement
    les parts entre membres.

    Sous-commandes :
      /admintransfert info <NomEntreprise>
          → Affiche le propriétaire actuel + toutes les parts

      /admintransfert proprio <NomEntreprise> @newpdg
          → Change uniquement le PDG/PDG (propriétaire) sans toucher aux parts

      /admintransfert parts <NomEntreprise> @user1:qty1 @user2:qty2 ...
          → Redistribue les parts comme tu veux (total = nouveau total_shares)

      /admintransfert full <NomEntreprise> @newpdg @user1:qty1 @user2:qty2 ...
          → Change le PDG ET redistribue les parts en une seule commande
    """
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    if not context.args or len(context.args) < 2:
        return await update.message.reply_text(
            "❌ <b>Usage :</b>\n\n"
            "<code>/admintransfert info NomEntreprise</code>\n"
            "  → Voir proprio + parts actuels\n\n"
            "<code>/admintransfert proprio NomEntreprise @newpdg</code>\n"
            "  → Changer le PDG/PDG uniquement\n\n"
            "<code>/admintransfert parts NomEntreprise @user1:100 @user2:50</code>\n"
            "  → Redéfinir les parts librement\n\n"
            "<code>/admintransfert full NomEntreprise @newpdg @user1:100 @user2:50</code>\n"
            "  → Changer le PDG + redistribuer les parts\n\n"
            "💡 <i>Les @users doivent avoir utilisé le bot au moins une fois.</i>",
            parse_mode="HTML"
        )

    sub = context.args[0].lower()
    args = context.args[1:]

    # ── Helper : résoudre un username ou ID ────────────────────────────────
    async def resolve(session, mention: str):
        mention = mention.lstrip("@")
        if mention.isdigit():
            return (await session.execute(
                text("SELECT * FROM users WHERE user_id=:uid LIMIT 1"),
                {"uid": int(mention)}
            )).fetchone()
        return (await session.execute(
            text("SELECT * FROM users WHERE LOWER(username)=LOWER(:u) LIMIT 1"),
            {"u": mention}
        )).fetchone()

    # ── Helper : afficher l'état actuel ────────────────────────────────────
    async def show_info(session, comp, msg_prefix=""):
        shares = (await session.execute(text(
            """
            SELECT cs.owner_id, cs.quantity, u.username, u.first_name
            FROM company_shares cs
            LEFT JOIN users u ON u.user_id = cs.owner_id
            WHERE cs.company_id = :cid AND cs.quantity > 0
            ORDER BY cs.quantity DESC
            """
        ), {"cid": comp.id})).fetchall()

        owner_row = (await session.execute(
            text("SELECT username, first_name FROM users WHERE user_id=:uid LIMIT 1"),
            {"uid": comp.owner_id}
        )).fetchone()
        owner_label = f"@{owner_row.username}" if owner_row and owner_row.username else (
            owner_row.first_name if owner_row else f"ID:{comp.owner_id}"
        )

        price_per = comp.value // comp.total_shares if comp.total_shares else 0
        lines = [
            msg_prefix,
            f"🏢 <b>{comp.name}</b>",
            f"💎 Propriétaire (PDG) : <b>{owner_label}</b> (uid:{comp.owner_id})",
            f"📦 Total parts : <b>{comp.total_shares}</b>  |  💰 Prix/part : <b>{_fmt(price_per)} $</b>",
            "",
        ]
        for s in shares:
            name_d = f"@{s.username}" if s.username else (s.first_name or f"ID:{s.owner_id}")
            pct = (s.quantity / comp.total_shares * 100) if comp.total_shares else 0
            lines.append(f"• <b>{name_d}</b> — {s.quantity} parts ({pct:.1f}%)")

        lines.append(
            f"\n💡 <code>/admintransfert full {comp.name} @newpdg @user1:qty1 @user2:qty2</code>"
        )
        return "\n".join(l for l in lines if l is not None)

    # ══════════════════════════════════════════════════════════════════════════
    # Sous-commande : info
    # ══════════════════════════════════════════════════════════════════════════
    if sub == "info":
        company_name = " ".join(args)
        async with AsyncSessionLocal() as session:
            comp = (await session.execute(
                text("SELECT * FROM companies WHERE LOWER(name)=LOWER(:n) LIMIT 1"),
                {"n": company_name}
            )).fetchone()
            if not comp:
                return await update.message.reply_text(
                    f"❌ Entreprise <b>{company_name}</b> introuvable.", parse_mode="HTML"
                )
            msg = await show_info(session, comp)
        return await update.message.reply_text(msg, parse_mode="HTML")

    # ══════════════════════════════════════════════════════════════════════════
    # Sous-commande : proprio  → changer PDG uniquement
    # ══════════════════════════════════════════════════════════════════════════
    if sub == "proprio":
        # Format : proprio <NomEntreprise> @newpdg
        # Le dernier arg est le @user, le reste = nom entreprise
        if len(args) < 2:
            return await update.message.reply_text(
                "❌ Usage : <code>/admintransfert proprio NomEntreprise @newpdg</code>",
                parse_mode="HTML"
            )
        new_pdg_mention = args[-1]
        company_name = " ".join(args[:-1])

        async with AsyncSessionLocal() as session:
            comp = (await session.execute(
                text("SELECT * FROM companies WHERE LOWER(name)=LOWER(:n) LIMIT 1"),
                {"n": company_name}
            )).fetchone()
            if not comp:
                return await update.message.reply_text(
                    f"❌ Entreprise <b>{company_name}</b> introuvable.", parse_mode="HTML"
                )

            new_pdg = await resolve(session, new_pdg_mention)
            if not new_pdg:
                return await update.message.reply_text(
                    f"❌ Utilisateur <b>{new_pdg_mention}</b> introuvable.", parse_mode="HTML"
                )

            old_owner_id = comp.owner_id
            new_owner_id = new_pdg.user_id
            new_pdg_label = f"@{new_pdg.username}" if new_pdg.username else new_pdg.first_name

            # Rétrograder l'ancien PDG → directeur s'il est encore dans l'entreprise
            await session.execute(
                text(
                    "UPDATE company_employees SET role='directeur' "
                    "WHERE company_id=:cid AND user_id=:uid AND role IN ('pdg') AND left_at IS NULL"
                ),
                {"cid": comp.id, "uid": old_owner_id}
            )

            # Vérifier si le nouveau PDG est déjà dans l'entreprise
            emp_exists = (await session.execute(
                text("SELECT id FROM company_employees WHERE company_id=:cid AND user_id=:uid AND left_at IS NULL"),
                {"cid": comp.id, "uid": new_owner_id}
            )).fetchone()

            if emp_exists:
                await session.execute(
                    text("UPDATE company_employees SET role='pdg' WHERE company_id=:cid AND user_id=:uid AND left_at IS NULL"),
                    {"cid": comp.id, "uid": new_owner_id}
                )
            else:
                await session.execute(
                    text("INSERT INTO company_employees (company_id, user_id, role, joined_at) VALUES (:cid, :uid, 'pdg', NOW())"),
                    {"cid": comp.id, "uid": new_owner_id}
                )

            # Mettre à jour le propriétaire dans la table companies
            await session.execute(
                text("UPDATE companies SET owner_id=:uid WHERE id=:cid"),
                {"uid": new_owner_id, "cid": comp.id}
            )

            # Log
            await session.execute(
                text(
                    "INSERT INTO company_logs (company_id, event_type, description, created_at) "
                    "VALUES (:cid, 'admin_transfert', :desc, NOW())"
                ),
                {"cid": comp.id, "desc": f"[ADMIN] Nouveau PDG : {new_pdg_label} (uid:{new_owner_id}) — ancien propriétaire uid:{old_owner_id}"}
            )
            await session.commit()

        await update.message.reply_text(
            f"✅ <b>Propriétaire mis à jour — {comp.name}</b>\n\n"
            f"👤 Ancien PDG : uid:{old_owner_id} → rétrogradé Directeur\n"
            f"💎 Nouveau PDG : <b>{new_pdg_label}</b> (uid:{new_owner_id})\n\n"
            f"📦 Les parts n'ont <b>pas</b> été modifiées.\n"
            f"💡 Pour redistribuer : <code>/admintransfert parts {comp.name} @user:qty ...</code>",
            parse_mode="HTML"
        )

        # Notifier le nouveau PDG
        try:
            await context.bot.send_message(
                chat_id=new_owner_id,
                text=f"💎 <b>Un admin t'a nommé PDG de {comp.name} !</b>",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    # ══════════════════════════════════════════════════════════════════════════
    # Sous-commandes : parts et full
    # ══════════════════════════════════════════════════════════════════════════
    if sub not in ("parts", "full"):
        return await update.message.reply_text(
            "❌ Sous-commande invalide. Utilise : <code>info</code>, <code>proprio</code>, <code>parts</code>, <code>full</code>.",
            parse_mode="HTML"
        )

    # --- Parser les args pour 'parts' et 'full' ---
    # Format parts : parts NomEntreprise @u1:qty1 @u2:qty2 ...
    # Format full  : full  NomEntreprise @newpdg @u1:qty1 @u2:qty2 ...

    # Les attributions de parts ont le format @user:qty  (avec deux-points)
    share_assignments = [a for a in args if ":" in a]
    non_share_args    = [a for a in args if ":" not in a]

    if not share_assignments:
        return await update.message.reply_text(
            "❌ Aucune attribution de parts détectée.\n"
            "Format : <code>@user:quantite</code> (ex: <code>@alice:60 @bob:40</code>)",
            parse_mode="HTML"
        )

    # Pour 'full', le dernier arg non-share avant les assignments est le nouveau PDG
    new_pdg_mention = None
    if sub == "full":
        # Le @newpdg n'a pas de ':', c'est le dernier non_share_arg
        pdg_candidates = [a for a in non_share_args if a.startswith("@") or a.lstrip("@").isdigit()]
        if not pdg_candidates:
            return await update.message.reply_text(
                "❌ Pour <code>full</code>, indique le nouveau PDG : <code>@user</code> avant les attributions de parts.",
                parse_mode="HTML"
            )
        new_pdg_mention = pdg_candidates[-1]
        non_share_args  = [a for a in non_share_args if a != new_pdg_mention]

    company_name = " ".join(non_share_args)
    if not company_name:
        return await update.message.reply_text("❌ Nom d'entreprise manquant.", parse_mode="HTML")

    # Parser @user:qty
    parsed_shares = []  # list of (mention, qty)
    for entry in share_assignments:
        parts_entry = entry.split(":")
        if len(parts_entry) != 2:
            return await update.message.reply_text(
                f"❌ Format invalide : <code>{entry}</code>\nAttendu : <code>@user:quantite</code>",
                parse_mode="HTML"
            )
        mention_part = parts_entry[0]
        try:
            qty_part = int(parts_entry[1])
        except ValueError:
            return await update.message.reply_text(
                f"❌ Quantité invalide pour <code>{entry}</code>",
                parse_mode="HTML"
            )
        if qty_part < 0:
            return await update.message.reply_text(
                f"❌ La quantité ne peut pas être négative : <code>{entry}</code>",
                parse_mode="HTML"
            )
        parsed_shares.append((mention_part, qty_part))

    total_new_shares = sum(q for _, q in parsed_shares)
    if total_new_shares == 0:
        return await update.message.reply_text("❌ Le total des parts ne peut pas être 0.")

    async with AsyncSessionLocal() as session:
        comp = (await session.execute(
            text("SELECT * FROM companies WHERE LOWER(name)=LOWER(:n) LIMIT 1"),
            {"n": company_name}
        )).fetchone()
        if not comp:
            return await update.message.reply_text(
                f"❌ Entreprise <b>{company_name}</b> introuvable.", parse_mode="HTML"
            )

        # Résoudre tous les utilisateurs
        resolved = []
        for mention_part, qty_part in parsed_shares:
            u = await resolve(session, mention_part)
            if not u:
                return await update.message.reply_text(
                    f"❌ Utilisateur <b>{mention_part}</b> introuvable (doit avoir utilisé le bot).",
                    parse_mode="HTML"
                )
            resolved.append((u, qty_part))

        new_pdg = None
        if sub == "full":
            new_pdg = await resolve(session, new_pdg_mention)
            if not new_pdg:
                return await update.message.reply_text(
                    f"❌ Nouveau PDG <b>{new_pdg_mention}</b> introuvable.", parse_mode="HTML"
                )

        old_owner_id = comp.owner_id

        # ── Mise à jour des parts ──────────────────────────────────────────
        # 1. Mettre toutes les parts existantes à 0
        await session.execute(
            text("UPDATE company_shares SET quantity=0 WHERE company_id=:cid"),
            {"cid": comp.id}
        )

        # 2. Appliquer les nouvelles attributions
        lines_report = []
        new_owner_id = comp.owner_id  # par défaut inchangé

        for u, qty in resolved:
            u_label = f"@{u.username}" if u.username else u.first_name
            share_row = (await session.execute(
                text("SELECT id FROM company_shares WHERE company_id=:cid AND owner_id=:uid LIMIT 1"),
                {"cid": comp.id, "uid": u.user_id}
            )).fetchone()

            if share_row:
                await session.execute(
                    text("UPDATE company_shares SET quantity=:q WHERE company_id=:cid AND owner_id=:uid"),
                    {"q": qty, "cid": comp.id, "uid": u.user_id}
                )
            else:
                await session.execute(
                    text("INSERT INTO company_shares (company_id, owner_id, quantity, acquired_at) VALUES (:cid, :uid, :q, NOW())"),
                    {"cid": comp.id, "uid": u.user_id, "q": qty}
                )

            pct = (qty / total_new_shares * 100) if total_new_shares else 0
            lines_report.append(f"• <b>{u_label}</b> → {qty} parts ({pct:.1f}%)")

        # 3. Mettre à jour total_shares
        await session.execute(
            text("UPDATE companies SET total_shares=:t WHERE id=:cid"),
            {"t": total_new_shares, "cid": comp.id}
        )

        # ── Mise à jour PDG si 'full' ──────────────────────────────────────
        pdg_report = ""
        if sub == "full" and new_pdg:
            new_owner_id = new_pdg.user_id
            new_pdg_label = f"@{new_pdg.username}" if new_pdg.username else new_pdg.first_name

            # Rétrograder l'ancien PDG
            await session.execute(
                text(
                    "UPDATE company_employees SET role='directeur' "
                    "WHERE company_id=:cid AND user_id=:uid AND role IN ('pdg') AND left_at IS NULL"
                ),
                {"cid": comp.id, "uid": old_owner_id}
            )

            # Nommer le nouveau PDG
            emp_exists = (await session.execute(
                text("SELECT id FROM company_employees WHERE company_id=:cid AND user_id=:uid AND left_at IS NULL"),
                {"cid": comp.id, "uid": new_owner_id}
            )).fetchone()
            if emp_exists:
                await session.execute(
                    text("UPDATE company_employees SET role='pdg' WHERE company_id=:cid AND user_id=:uid AND left_at IS NULL"),
                    {"cid": comp.id, "uid": new_owner_id}
                )
            else:
                await session.execute(
                    text("INSERT INTO company_employees (company_id, user_id, role, joined_at) VALUES (:cid, :uid, 'pdg', NOW())"),
                    {"cid": comp.id, "uid": new_owner_id}
                )

            # Trouver les parts du nouveau PDG pour owner_shares
            new_owner_qty = next((q for u, q in resolved if u.user_id == new_owner_id), 0)

            await session.execute(
                text("UPDATE companies SET owner_id=:uid, owner_shares=:os WHERE id=:cid"),
                {"uid": new_owner_id, "os": new_owner_qty, "cid": comp.id}
            )

            pdg_report = f"\n💎 Nouveau PDG : <b>{new_pdg_label}</b> (uid:{new_owner_id})"

            # Notifier le nouveau PDG
            try:
                await context.bot.send_message(
                    chat_id=new_owner_id,
                    text=f"💎 <b>Un admin t'a nommé PDG de {comp.name} !</b>\n"
                         f"📦 Tu détiens <b>{new_owner_qty} parts</b>.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            # Juste les parts — mettre à jour owner_shares avec les parts du proprio actuel
            owner_qty = next((q for u, q in resolved if u.user_id == old_owner_id), None)
            if owner_qty is not None:
                await session.execute(
                    text("UPDATE companies SET owner_shares=:os WHERE id=:cid"),
                    {"os": owner_qty, "cid": comp.id}
                )

        # Log global
        log_desc = (
            f"[ADMIN] Redistribution parts ({total_new_shares} total) — "
            + ", ".join(f"uid:{u.user_id}→{q}" for u, q in resolved)
        )
        if sub == "full" and new_pdg:
            log_desc += f" | Nouveau PDG uid:{new_pdg.user_id}"

        await session.execute(
            text(
                "INSERT INTO company_logs (company_id, event_type, description, created_at) "
                "VALUES (:cid, 'admin_transfert', :desc, NOW())"
            ),
            {"cid": comp.id, "desc": log_desc}
        )
        await session.commit()

    action_label = "Redistribution parts + Changement PDG" if sub == "full" else "Redistribution parts"
    await update.message.reply_text(
        f"✅ <b>{action_label} — {comp.name}</b>{pdg_report}\n\n"
        f"📦 Nouveau total : <b>{total_new_shares} parts</b>\n\n"
        + "\n".join(lines_report) +
        f"\n\n🗂️ Tout a été loggé dans les logs de l'entreprise.",
        parse_mode="HTML"
    )
