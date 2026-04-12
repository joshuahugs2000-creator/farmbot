"""
Compte commun pour les couples mariés.

/couple create   — Créer le compte commun (seulement si marié)
/couple balance  — Voir le solde commun
/couple deposit  — Déposer depuis son compte perso
/couple withdraw — Retirer vers son compte perso
"""
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database.db import (
    AsyncSessionLocal, get_user, get_spouse,
    get_couple_account, create_couple_account,
    couple_deposit, couple_withdraw,
)
from utils.helpers import ensure_user, mention


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


async def couple(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update.effective_user)
    sub  = context.args[0].lower() if context.args else "help"

    async with AsyncSessionLocal() as session:

        # ── /couple create ────────────────────────────────────────────────────
        if sub == "create":
            rel = await get_spouse(session, user.user_id)
            if not rel:
                return await update.message.reply_text(
                    "💔 Tu dois être marié(e) pour créer un compte commun."
                )
            existing = await get_couple_account(session, user.user_id)
            if existing:
                return await update.message.reply_text(
                    f"💑 Vous avez déjà un compte commun !\n"
                    f"💰 Solde : {_fmt(existing.balance)} $"
                )
            spouse_id = rel.related_user_id if rel.user_id == user.user_id else rel.user_id
            account   = await create_couple_account(session, user.user_id, spouse_id)
            if not account:
                return await update.message.reply_text("Erreur lors de la création du compte.")
            spouse = await get_user(session, spouse_id)
            await update.message.reply_text(
                f"💑 Compte commun créé entre {mention(user)} et {mention(spouse)} !\n"
                f"💰 Solde de départ : 0 $\n\n"
                f"Utilisez :\n"
                f"• /couple deposit [montant] — Déposer\n"
                f"• /couple withdraw [montant] — Retirer\n"
                f"• /couple balance — Voir le solde",
                parse_mode=ParseMode.HTML,
            )

        # ── /couple balance ───────────────────────────────────────────────────
        elif sub == "balance":
            account = await get_couple_account(session, user.user_id)
            if not account:
                return await update.message.reply_text(
                    "❌ Pas de compte commun.\n"
                    "Crée-en un avec /couple create (tu dois être marié(e))."
                )
            spouse_id = account.user2_id if account.user1_id == user.user_id else account.user1_id
            spouse    = await get_user(session, spouse_id)
            u         = await get_user(session, user.user_id)
            await update.message.reply_text(
                f"💑 Compte commun\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"💰 Solde commun : {_fmt(account.balance)} $\n"
                f"👤 Ton solde perso : {_fmt(u.coins)} $\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"Conjoint(e) : {mention(spouse)}",
                parse_mode=ParseMode.HTML,
            )

        # ── /couple deposit ───────────────────────────────────────────────────
        elif sub == "deposit":
            if len(context.args) < 2:
                return await update.message.reply_text("Usage : /couple deposit [montant]")
            try:
                amount = int(context.args[1].replace(",", "").replace(" ", ""))
                assert amount >= 1
            except (ValueError, AssertionError):
                return await update.message.reply_text("Montant invalide.")

            result = await couple_deposit(session, user.user_id, amount)
            if result == "no_account":
                await update.message.reply_text(
                    "❌ Pas de compte commun. Crée-en un avec /couple create."
                )
            elif result == "insufficient":
                await update.message.reply_text(
                    f"❌ Solde insuffisant ! Tu n'as pas {_fmt(amount)} $ sur ton compte perso."
                )
            elif result == "not_found":
                await update.message.reply_text("Compte introuvable.")
            else:
                account = await get_couple_account(session, user.user_id)
                u       = await get_user(session, user.user_id)
                await update.message.reply_text(
                    f"✅ {_fmt(amount)} $ déposés sur le compte commun !\n"
                    f"💑 Solde commun : {_fmt(account.balance)} $\n"
                    f"👤 Ton solde perso : {_fmt(u.coins)} $"
                )

        # ── /couple withdraw ──────────────────────────────────────────────────
        elif sub == "withdraw":
            if len(context.args) < 2:
                return await update.message.reply_text("Usage : /couple withdraw [montant]")
            try:
                amount = int(context.args[1].replace(",", "").replace(" ", ""))
                assert amount >= 1
            except (ValueError, AssertionError):
                return await update.message.reply_text("Montant invalide.")

            result = await couple_withdraw(session, user.user_id, amount)
            if result == "no_account":
                await update.message.reply_text(
                    "❌ Pas de compte commun. Crée-en un avec /couple create."
                )
            elif result == "insufficient":
                await update.message.reply_text(
                    f"❌ Solde commun insuffisant ! Le compte commun n'a pas {_fmt(amount)} $."
                )
            elif result == "not_found":
                await update.message.reply_text("Compte introuvable.")
            else:
                account = await get_couple_account(session, user.user_id)
                u       = await get_user(session, user.user_id)
                await update.message.reply_text(
                    f"✅ {_fmt(amount)} $ retirés du compte commun !\n"
                    f"💑 Solde commun : {_fmt(account.balance)} $\n"
                    f"👤 Ton solde perso : {_fmt(u.coins)} $"
                )

        # ── aide ──────────────────────────────────────────────────────────────
        else:
            await update.message.reply_text(
                "💑 <b>Compte commun du couple</b>\n\n"
                "/couple create — Créer le compte commun\n"
                "/couple balance — Voir le solde\n"
                "/couple deposit [montant] — Déposer depuis ton compte perso\n"
                "/couple withdraw [montant] — Retirer vers ton compte perso\n\n"
                "ℹ️ En cas de divorce, le solde est partagé en deux automatiquement.",
                parse_mode=ParseMode.HTML,
            )
