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
    Retourne le TGUser cible depuis :
    - la réponse à un message
    - une entité text_mention (user connu de Telegram)
    - un @username → résolu via la base de données
    """
    msg = update.message

    # 1. Réponse à un message
    if msg.reply_to_message:
        from_user = msg.reply_to_message.from_user
        logger.info(f"[parse_target] reply_to_message détecté → from_user={from_user}")
        return from_user

    # 2. Entités du message
    entities = msg.entities or []
    text = msg.text or msg.caption or ""
    logger.info(f"[parse_target] Pas de reply. text={repr(text)} entities={[(e.type, e.offset, e.length) for e in entities]}")

    for entity in entities:
        if entity.type == "text_mention" and entity.user:
            logger.info(f"[parse_target] text_mention → {entity.user}")
            return entity.user

        if entity.type == "mention":
            username = text[entity.offset : entity.offset + entity.length]
            logger.info(f"[parse_target] mention @username={username}")
            async with _db.AsyncSessionLocal() as session:
                db_user = await get_user_by_username(session, username)
            logger.info(f"[parse_target] db_user pour {username} = {db_user}")
            if db_user:
                class _FakeUser:
                    def __init__(self, u):
                        self.id         = u.user_id
                        self.first_name = u.first_name
                        self.username   = u.username
                        self.is_bot     = False
                return _FakeUser(db_user)
            else:
                return None

    logger.info("[parse_target] Aucune cible trouvée → None")
    return None

def progress_bar(current: int, total: int, length: int = 10) -> str:
    filled = int(length * current / total) if total else 0
    return "█" * filled + "░" * (length - filled)
