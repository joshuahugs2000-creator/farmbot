from telegram import Update, User as TGUser
from telegram.ext import ContextTypes
import database.db as _db
from database.db import upsert_user, get_user, get_user_by_username, compute_title
from database.models import User
import logging

logger = logging.getLogger(__name__)

def mention(user: User) -> str:
    """Retourne un lien mention HTML."""
    name = user.first_name
    if user.family_name:
        name += f" {user.family_name}"
    return f'<a href="tg://user?id={user.user_id}">{name}</a>'

def mention_tg(tg_user: TGUser) -> str:
    return f'<a href="tg://user?id={tg_user.id}">{tg_user.first_name}</a>'

async def ensure_user(tg_user) -> User:
    """Crée ou met à jour le user en base. Accepte TGUser ou _FakeUser."""
    async with _db.AsyncSessionLocal() as session:
        return await upsert_user(
            session,
            tg_user.id,
            tg_user.username or "",
            tg_user.first_name,
        )

def is_group(update: Update) -> bool:
    return update.effective_chat.type in ("group", "supergroup")

async def parse_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Retourne un objet compatible TGUser depuis :
    - la réponse à un message
    - une entité text_mention
    - un @username résolu via la DB
    """
    msg = update.message

    # 1. Réponse à un message
    if msg.reply_to_message:
        tg_user = msg.reply_to_message.from_user
        logger.info(f"[parse_target] reply → from_user={tg_user}")
        if tg_user:
            return tg_user
        # from_user=None = admin anonyme ou channel → on ne peut pas identifier
        logger.warning("[parse_target] reply_to_message.from_user est None (admin anonyme ?)")
        return None

    # 2. Entités texte
    text = msg.text or msg.caption or ""
    for entity in (msg.entities or []):
        # Mention avec objet user connu de Telegram
        if entity.type == "text_mention" and entity.user:
            logger.info(f"[parse_target] text_mention → {entity.user.id}")
            return entity.user

        # @username classique → résolution via DB
        if entity.type == "mention":
            username = text[entity.offset: entity.offset + entity.length]
            logger.info(f"[parse_target] mention → {username}")
            async with _db.AsyncSessionLocal() as session:
                db_user = await get_user_by_username(session, username)
            if db_user:
                class _FakeUser:
                    def __init__(self, u):
                        self.id         = u.user_id
                        self.first_name = u.first_name
                        self.username   = u.username
                        self.is_bot     = False
                return _FakeUser(db_user)
            logger.info(f"[parse_target] {username} introuvable en DB")
            return None

    logger.info("[parse_target] Aucune cible → None")
    return None

def progress_bar(current: int, total: int, length: int = 10) -> str:
    filled = int(length * current / total) if total else 0
    return "█" * filled + "░" * (length - filled)
