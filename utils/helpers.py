from telegram import Update, User as TGUser
from telegram.ext import ContextTypes
import database.db as _db
from database.db import upsert_user, get_user, get_user_by_username, compute_title
from database.models import User

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
    Renvoie None si introuvable.
    """
    # 1. Réponse à un message
    if update.message.reply_to_message:
        return update.message.reply_to_message.from_user

    # 2. Entités du message
    for entity in (update.message.entities or []):
        if entity.type == "text_mention" and entity.user:
            # Telegram fournit directement l'objet user
            return entity.user

        if entity.type == "mention":
            # @username : Telegram ne fournit PAS entity.user ici
            # On extrait le @username du texte et on cherche en DB
            text = update.message.text or ""
            username = text[entity.offset : entity.offset + entity.length]  # inclut le @
            async with _db.AsyncSessionLocal() as session:
                db_user = await get_user_by_username(session, username)
            if db_user:
                # Reconstituer un objet TGUser minimal compatible
                class _FakeUser:
                    def __init__(self, u):
                        self.id         = u.user_id
                        self.first_name = u.first_name
                        self.username   = u.username
                        self.is_bot     = False
                return _FakeUser(db_user)
            else:
                # L'utilisateur n'a jamais interagi avec le bot
                return None

    return None

def progress_bar(current: int, total: int, length: int = 10) -> str:
    filled = int(length * current / total) if total else 0
    return "█" * filled + "░" * (length - filled)
