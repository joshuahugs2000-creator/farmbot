from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database.db import AsyncSessionLocal, get_user, upsert_user, compute_title, get_family_members
from utils.helpers import ensure_user
from config import PROFILE_COLORS, TITLES


async def me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche la carte de profil complète."""
    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        u     = await get_user(session, user.user_id)
        title = await compute_title(session, user.user_id)
        fam   = await get_family_members(session, user.user_id)
        size  = len(fam)

    color_dot = {
        "blue": "🔵", "green": "🟢", "red": "🔴", "purple": "🟣",
        "orange": "🟠", "pink": "🩷", "gold": "🟡", "teal": "🩵",
    }.get(u.profile_color if u else "blue", "🔵")

    lines = [
        f"👤 <b>Profil de {update.effective_user.first_name}</b>",
        f"",
        f"🏅 Titre    : {title}",
        f"🏠 Famille  : {u.family_name or '—'}" if u else "",
        f"👨‍👩‍👧 Membres  : {size}",
        f"⭐ Karma    : {u.karma if u else 0}",
        f"💰 Coins    : {u.coins if u else 0}",
        f"{color_dot} Couleur   : {u.profile_color if u else 'blue'}",
    ]

    text = "\n".join(l for l in lines if l is not None)
    if u and u.photo_file_id:
        await update.message.reply_photo(u.photo_file_id, caption=text, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def setpic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Réponds à une photo ou un sticker pour définir ta photo de profil."""
    reply = update.message.reply_to_message
    if not reply:
        return await update.message.reply_text("❗ Réponds à une photo ou un sticker.")

    file_id = None
    if reply.photo:
        file_id = reply.photo[-1].file_id
    elif reply.sticker:
        file_id = reply.sticker.file_id
    else:
        return await update.message.reply_text("❗ Le message doit contenir une photo ou un sticker.")

    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.user_id)
        if u:
            u.photo_file_id = file_id
            await session.commit()

    await update.message.reply_text("✅ Photo de profil mise à jour !")


async def customize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Choisir la couleur du profil via des boutons."""
    color_emojis = {
        "blue": "🔵", "green": "🟢", "red": "🔴", "purple": "🟣",
        "orange": "🟠", "pink": "🩷", "gold": "🟡", "teal": "🩵",
    }
    buttons = [
        InlineKeyboardButton(f"{color_emojis[c]} {c.capitalize()}", callback_data=f"color:{c}")
        for c in PROFILE_COLORS
    ]
    keyboard = InlineKeyboardMarkup([buttons[i:i+4] for i in range(0, len(buttons), 4)])
    await update.message.reply_text("🎨 Choisis ta couleur de profil :", reply_markup=keyboard)


async def color_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    color = query.data.split(":")[1]
    if color not in PROFILE_COLORS:
        return

    user = await ensure_user(query.from_user)
    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.user_id)
        if u:
            u.profile_color = color
            await session.commit()

    await query.edit_message_text(f"✅ Couleur définie sur <b>{color}</b> !", parse_mode=ParseMode.HTML)


async def titles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Liste tous les titres dynastiques et leurs conditions."""
    lines = ["👑 <b>Titres dynastiques</b>\n"]
    for min_size, min_karma, title in TITLES:
        lines.append(
            f"{title}\n"
            f"  → Famille ≥ {min_size} membres  |  Karma ≥ {min_karma}\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
