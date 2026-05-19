import io
from telegram import Update
from telegram.ext import ContextTypes

from database.db import AsyncSessionLocal, get_user, get_relationships, compute_title
from database.models import RelationType
from utils.helpers import ensure_user


async def _get_photo_bytes(bot, user_id: int) -> bytes | None:
    """Récupère la photo de profil Telegram d'un utilisateur en bytes."""
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if not photos or photos.total_count == 0:
            return None
        file_id = photos.photos[0][-1].file_id
        tg_file = await bot.get_file(file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        buf.seek(0)
        return buf.read()
    except Exception:
        return None


async def tree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Génère et envoie une image de l'arbre généalogique avec vraies photos."""
    try:
        from utils.tree_renderer import render_tree
    except Exception as e:
        return await update.message.reply_text(f"❗ Arbre indisponible : {e}")

    tg_user = update.effective_user
    bot     = context.bot
    user    = await ensure_user(tg_user)

    await update.message.reply_text("⏳ Génération de l'arbre en cours...")

    async with AsyncSessionLocal() as session:
        rels = await get_relationships(session, user.user_id)
        u    = await get_user(session, user.user_id)

        async def make_node(uid, first_name, profile_color):
            photo = await _get_photo_bytes(bot, uid)
            return {
                "user_id": uid,
                "name":    first_name[:16],
                "color":   profile_color or "blue",
                "photo":   photo,
            }

        user_node = await make_node(
            user.user_id,
            u.first_name if u else tg_user.first_name,
            u.profile_color if u else "blue",
        )

        spouse_node  = None
        parent_nodes = []
        child_nodes  = []
        friend_nodes = []
        spouse_nodes = []  # liste pour gérer la polygamie

        for rel in rels:
            other_id = rel.related_user_id if rel.user_id == user.user_id else rel.user_id
            other    = await get_user(session, other_id)
            node     = await make_node(
                other_id,
                other.first_name if other else str(other_id),
                other.profile_color if other else "blue",
            )
            if rel.relation_type == RelationType.SPOUSE:
                spouse_nodes.append(node)
            elif rel.relation_type == RelationType.PARENT:
                if rel.user_id == user.user_id:
                    child_nodes.append(node)
                else:
                    parent_nodes.append(node)
            elif rel.relation_type == RelationType.FRIEND:
                friend_nodes.append(node)

        # Pour le rendu on prend le premier époux comme "principal"
        # les autres sont ajoutés après dans la rangée principale
        spouse_node = spouse_nodes[0] if spouse_nodes else None
        extra_spouses = spouse_nodes[1:] if len(spouse_nodes) > 1 else []

    members = {
        "user":          user_node,
        "spouse":        spouse_node,
        "extra_spouses": extra_spouses,
        "parents":       parent_nodes,
        "children":      child_nodes,
        "friends":       friend_nodes,
    }

    img_bytes = render_tree(members)
    await update.message.reply_photo(
        photo=io.BytesIO(img_bytes),
        caption=f"🌳 Arbre de {tg_user.first_name}",
    )
