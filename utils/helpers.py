from telegram import Update, User as TGUser
from telegram.ext import ContextTypes
import database.db as _db
from database.db import upsert_user, get_user, compute_title
from database.models import User

def mention(user: User) -> str:
    """Retourne un lien mention HTML."""
    name = user.first_name
    if user.family_name:
        name += f" {user.family_name}"
    return f'<a href="tg://user?id={user.user_id}">{name}</a>'

def mention_tg(tg_user: TGUser) -> str:
    return f'<a href="tg://user?id={tg_user.id}">{tg_user.first_name}</a>'

async def ensure_user(tg_user: TGUser) -> User:
    """Crée ou met à jour le user en base."""
    async with _db.AsyncSessionLocal() as session:
        return await upsert_user(
            session,
            tg_user.id,
            tg_user.username or "",
            tg_user.first_name,
        )

def is_group(update: Update) -> bool:
    return update.effective_chat.type in ("group", "supergroup")

def parse_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Retourne le TGUser cible depuis :
    - la réponse à un message
    - la mention (@username ou entité mention)
    """
    if update.message.reply_to_message:
        return update.message.reply_to_message.from_user
    if context.args:
        for entity in (update.message.entities or []):
            if entity.type in ("mention", "text_mention"):
                if entity.user:
                    return entity.user
    return None

def progress_bar(current: int, total: int, length: int = 10) -> str:
    filled = int(length * current / total) if total else 0
    return "█" * filled + "░" * (length - filled)
