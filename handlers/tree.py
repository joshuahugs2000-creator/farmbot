import io
from telegram import Update
from telegram.ext import ContextTypes

from database.db import AsyncSessionLocal, get_user, get_relationships, get_family_members, compute_title
from database.models import RelationType
from utils.helpers import ensure_user


async def tree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Génère et envoie une image de l'arbre généalogique de l'utilisateur."""
    # Import lazy : si Pillow n'est pas dispo, seule cette commande échoue
    try:
        from utils.tree_renderer import render_tree
    except Exception as e:
        return await update.message.reply_text(f"❗ Arbre indisponible : {e}")

    tg_user = update.effective_user
    user    = await ensure_user(tg_user)

    async with AsyncSessionLocal() as session:
        rels = await get_relationships(session, user.user_id)
        u    = await get_user(session, user.user_id)

        user_node = {
            "name":  (u.first_name[:16] if u else tg_user.first_name[:16]),
            "title": await compute_title(session, user.user_id),
            "color": (u.profile_color if u else "blue"),
        }

        spouse_node  = None
        parent_nodes = []
        child_nodes  = []
        friend_nodes = []

        for rel in rels:
            other_id = rel.related_user_id if rel.user_id == user.user_id else rel.user_id
            other    = await get_user(session, other_id)
            title    = await compute_title(session, other_id)
            n = {
                "name":  (other.first_name[:16] if other else str(other_id)),
                "title": title,
                "color": (other.profile_color if other else "blue"),
            }
            if rel.relation_type == RelationType.SPOUSE:
                spouse_node = n
            elif rel.relation_type == RelationType.PARENT:
                if rel.user_id == user.user_id:
                    child_nodes.append(n)
                else:
                    parent_nodes.append(n)
            elif rel.relation_type == RelationType.FRIEND:
                friend_nodes.append(n)

    members = {
        "user":     user_node,
        "spouse":   spouse_node,
        "parents":  parent_nodes,
        "children": child_nodes,
        "friends":  friend_nodes,
    }

    await update.message.reply_text("Génération de l'arbre en cours...")
    img_bytes = render_tree(members)
    await update.message.reply_photo(
        photo=io.BytesIO(img_bytes),
        caption=f"Arbre de {tg_user.first_name}",
    )
