import random
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database.db import AsyncSessionLocal, get_or_set_waifu, get_family_members, get_user, vote_karma
from utils.helpers import ensure_user, is_group, parse_target, mention
from config import MOODS


async def waifu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Révèle la waifu du jour choisie parmi les membres de la famille."""
    if not is_group(update):
        return await update.message.reply_text("❗ Commande de groupe uniquement.")

    user     = await ensure_user(update.effective_user)
    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        family_ids = await get_family_members(session, user.user_id)
        if not family_ids:
            return await update.message.reply_text(
                "💔 Tu n'as pas encore de famille ! Utilise /marry, /adopt ou /friend."
            )
        waifu_id = await get_or_set_waifu(session, group_id, family_ids)
        waifu_user = await get_user(session, waifu_id)
        if not waifu_user:
            return await update.message.reply_text("❗ Erreur lors de la sélection.")

        mood     = random.choice(MOODS)
        text     = (
            f"✨ <b>Waifu du jour</b> ✨\n\n"
            f"💖 {mention(waifu_user)}\n"
            f"Humeur : {mood}\n\n"
            f"<i>Reviens demain pour une nouvelle waifu !</i>"
        )
        photo_id = waifu_user.photo_file_id   # lu DANS la session

    if photo_id:
        await update.message.reply_photo(
            photo=photo_id,
            caption=text,
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def upvote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Donne +1 karma à quelqu'un (1 fois par jour)."""
    target_tg = await parse_target(update, context)
    if not target_tg:
        return await update.message.reply_text(
            "❗ Réponds au message de la personne ou mentionne-la."
        )
    sender = await ensure_user(update.effective_user)
    target = await ensure_user(target_tg)
    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        result = await vote_karma(session, sender.user_id, target.user_id, group_id, +1)

    msgs = {
        "ok":      f"⬆️ {mention(sender)} a donné +1 karma à {mention(target)} !",
        "already": "❗ Tu as déjà voté pour cette personne aujourd'hui.",
        "self":    "❗ Tu ne peux pas voter pour toi-même.",
    }
    await update.message.reply_text(
        msgs.get(result, "❗ Erreur."), parse_mode=ParseMode.HTML
    )


async def downvote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Donne -1 karma à quelqu'un (1 fois par jour)."""
    target_tg = await parse_target(update, context)
    if not target_tg:
        return await update.message.reply_text(
            "❗ Réponds au message de la personne ou mentionne-la."
        )
    sender = await ensure_user(update.effective_user)
    target = await ensure_user(target_tg)
    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        result = await vote_karma(session, sender.user_id, target.user_id, group_id, -1)

    msgs = {
        "ok":      f"⬇️ {mention(sender)} a donné -1 karma à {mention(target)}.",
        "already": "❗ Tu as déjà voté pour cette personne aujourd'hui.",
        "self":    "❗ Tu ne peux pas voter pour toi-même.",
    }
    await update.message.reply_text(
        msgs.get(result, "❗ Erreur."), parse_mode=ParseMode.HTML
    )
