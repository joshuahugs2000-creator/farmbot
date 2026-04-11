"""
Job quotidien : vérifie les anniversaires de mariage et envoie des félicitations.
"""
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from database.db import AsyncSessionLocal, get_anniversaries_today, get_user
from utils.helpers import mention
from datetime import datetime


async def check_anniversaries(context: ContextTypes.DEFAULT_TYPE):
    """Appelé chaque jour à 08:00 UTC via JobQueue."""

    # Charger toutes les données nécessaires DANS la session
    notifications = []
    async with AsyncSessionLocal() as session:
        rels = await get_anniversaries_today(session)
        for rel in rels:
            u1 = await get_user(session, rel.user_id)
            u2 = await get_user(session, rel.related_user_id)
            if not u1 or not u2:
                continue
            notifications.append({
                "group_id":    rel.group_id,
                "years":       datetime.utcnow().year - rel.created_at.year,
                "name1":       mention(u1),
                "name2":       mention(u2),
                "family_name": u1.family_name or u1.first_name,
            })

    # Envoyer les notifications hors session (aucun accès ORM)
    for n in notifications:
        if not n["group_id"]:
            continue
        years = n["years"]
        text = (
            f"💍 <b>Anniversaire de mariage !</b>\n\n"
            f"Aujourd'hui, {n['name1']} et {n['name2']} fêtent "
            f"<b>{years} an{'s' if years > 1 else ''}</b> de mariage ! 🎉\n"
            f"Félicitations à la famille {n['family_name']} ! 🥂"
        )
        try:
            await context.bot.send_message(
                chat_id=n["group_id"],
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass  # Le bot a peut-être quitté le groupe
