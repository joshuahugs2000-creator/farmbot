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


async def bigtree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche tous les membres du groupe sous forme de texte."""
    if update.effective_chat.type not in ("group", "supergroup"):
        return await update.message.reply_text("❗ Commande de groupe uniquement.")

    async with AsyncSessionLocal() as session:
        from sqlalchemy import select
        from database.models import Relationship, User
        r    = await session.execute(select(Relationship).where(
            Relationship.group_id == update.effective_chat.id
        ))
        rels = list(r.scalars().all())

        seen  = set()
        lines = ["<b>Arbre du groupe</b>\n"]
        for rel in rels:
            pair = tuple(sorted([rel.user_id, rel.related_user_id]))
            if pair in seen:
                continue
            seen.add(pair)
            u1 = await get_user(session, rel.user_id)
            u2 = await get_user(session, rel.related_user_id)
            n1 = u1.first_name if u1 else str(rel.user_id)
            n2 = u2.first_name if u2 else str(rel.related_user_id)
            emoji = {"spouse": "💍", "parent": "👨‍👦", "friend": "🤝"}.get(rel.relation_type.value, "•")
            lines.append(f"{emoji} {n1} ↔ {n2}")

        if len(lines) == 1:
            lines.append("Aucune relation enregistrée dans ce groupe.")

    from telegram.constants import ParseMode
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
