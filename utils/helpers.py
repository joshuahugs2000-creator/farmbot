from telegram import Update, User as TGUser
from telegram.ext import ContextTypes
import database.db as _db
from database.db import upsert_user, get_user, get_user_by_username, compute_title
from database.models import User
import logging

logger = logging.getLogger(__name__)

def mention(user) -> str:
    """Retourne un lien mention HTML. Compatible DB User et Telegram User."""
    name = user.first_name or "Joueur"
    # DB User -> family_name ; Telegram User -> last_name
    if hasattr(user, "family_name") and user.family_name:
        name += f" {user.family_name}"
    elif hasattr(user, "last_name") and user.last_name:
        name += f" {user.last_name}"
    # DB User -> user_id ; Telegram User -> id
    uid = getattr(user, "user_id", None) or getattr(user, "id", 0)
    return f'<a href="tg://user?id={uid}">{name}</a>'

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

async def parse_target(update: Update, context: ContextTypes.DEFAULT_TYPE, allow_bot: bool = False):
    """
    Retourne un objet compatible TGUser depuis :
    - la réponse à un message
    - une entité text_mention
    - un @username résolu via la DB
    - un ID numérique brut (ex: /drame scandale 123456789)

    Si allow_bot=False (défaut), retourne None avec message d'erreur si la cible est un bot.
    Si allow_bot=True (admin), la cible bot est autorisée.
    """
    msg    = update.message
    bot_id = context.bot.id

    async def _check_bot(uid: int, is_bot: bool, label: str):
        """Envoie un message d'erreur et retourne True si c'est un bot non autorisé."""
        if (is_bot or uid == bot_id) and not allow_bot:
            logger.warning(f"[parse_target] {label} → cible est un bot ({uid}), rejeté.")
            await msg.reply_text("❌ Tu ne peux pas cibler le bot.")
            return True
        return False

    # 0. ID numérique brut dans les args
    args = context.args or []
    for arg in args:
        stripped = arg.strip()
        if stripped.lstrip("-").isdigit():
            uid = int(stripped)
            if await _check_bot(uid, False, "ID brut"):
                return None
            async with _db.AsyncSessionLocal() as session:
                db_user = await _db.get_user(session, uid)
            if db_user:
                class _FakeUserById:
                    def __init__(self, u):
                        self.id         = u.user_id
                        self.first_name = u.first_name
                        self.username   = u.username
                        self.is_bot     = False
                logger.info(f"[parse_target] ID brut → {uid}")
                return _FakeUserById(db_user)
            logger.info(f"[parse_target] ID {uid} introuvable en DB")
            return None

    # 1. Réponse à un message
    if msg.reply_to_message:
        tg_user = msg.reply_to_message.from_user
        logger.info(f"[parse_target] reply → from_user={tg_user}")
        if tg_user is None:
            logger.warning("[parse_target] reply_to_message.from_user est None (admin anonyme ?)")
            return None
        if await _check_bot(tg_user.id, getattr(tg_user, "is_bot", False), "reply"):
            return None
        return tg_user

    # 2. Entités texte
    text = msg.text or msg.caption or ""
    for entity in (msg.entities or []):
        # Mention avec objet user connu de Telegram
        if entity.type == "text_mention" and entity.user:
            logger.info(f"[parse_target] text_mention → {entity.user.id}")
            if await _check_bot(entity.user.id, getattr(entity.user, "is_bot", False), "text_mention"):
                return None
            return entity.user

        # @username classique → résolution via DB
        if entity.type == "mention":
            username = text[entity.offset: entity.offset + entity.length]
            logger.info(f"[parse_target] mention → {username}")
            async with _db.AsyncSessionLocal() as session:
                db_user = await get_user_by_username(session, username)
            if db_user:
                if await _check_bot(db_user.user_id, False, f"@mention {username}"):
                    return None
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
