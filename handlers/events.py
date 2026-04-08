"""
Job quotidien : vérifie les anniversaires de mariage et envoie des félicitations.
"""
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from database.db import AsyncSessionLocal, get_anniversaries_today, get_user
from database.models import RelationType
from utils.helpers import mention
from datetime import datetime


async def check_anniversaries(context: ContextTypes.DEFAULT_TYPE):
    """Appelé chaque jour à minuit via JobQueue."""
    async with AsyncSessionLocal() as session:
        rels = await get_anniversaries_today(session)

    for rel in rels:
        u1 = None
        u2 = None
        async with AsyncSessionLocal() as session:
            u1 = await get_user(session, rel.user_id)
            u2 = await get_user(session, rel.related_user_id)

        if not u1 or not u2:
            continue

        years = datetime.utcnow().year - rel.created_at.year
        text = (
            f"💍 <b>Anniversaire de mariage !</b>\n\n"
            f"Aujourd'hui, {mention(u1)} et {mention(u2)} fêtent "
            f"<b>{years} an{'s' if years > 1 else ''}</b> de mariage ! 🎉\n"
            f"Félicitations à la famille {u1.family_name or u1.first_name} ! 🥂"
        )

        # Envoyer dans le groupe où la relation a été créée
        if rel.group_id:
            try:
                await context.bot.send_message(
                    chat_id=rel.group_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass  # Le bot a peut-être quitté le groupe
