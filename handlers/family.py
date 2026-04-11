import io, random, logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database.db import (
    AsyncSessionLocal, upsert_user, get_user, get_spouse, get_relationships,
    get_family_members, add_relationship, remove_relationship, relationship_exists,
    create_request, get_request, delete_request, process_inheritance, compute_title,
)
from database.models import RelationType, RequestType
from utils.helpers import mention, mention_tg, is_group, parse_target, ensure_user
from config import MOODS

logger = logging.getLogger(__name__)


# ─── CARTE DE RELATION ───────────────────────────────────────────────────────

async def _send_relation_card(
    context,
    chat_id: int,
    user1,    # objet User (DB)
    user2,    # objet User (DB)
    relation: str,   # "married" | "adopted" | "friends"
    caption: str,
):
    """
    Génère la carte, l'envoie dans le groupe ET en DM aux deux utilisateurs.
    Les échecs DM sont ignorés silencieusement (l'utilisateur n'a pas lancé /start en privé).
    """
    from utils.card_generator import generate_relation_card

    # Télécharger les photos de profil
    async def _get_photo(uid: int) -> bytes | None:
        try:
            photos = await context.bot.get_user_profile_photos(uid, limit=1)
            if photos.total_count:
                file = await context.bot.get_file(photos.photos[0][0].file_id)
                data = await file.download_as_bytearray()
                return bytes(data)
        except Exception:
            pass
        return None

    photo1 = await _get_photo(user1.user_id)
    photo2 = await _get_photo(user2.user_id)

    card_bytes = generate_relation_card(
        name1=user1.first_name,
        name2=user2.first_name,
        relation=relation,
        photo1=photo1,
        photo2=photo2,
    )

    import io as _io
    # Envoi dans le groupe
    await context.bot.send_photo(
        chat_id=chat_id,
        photo=_io.BytesIO(card_bytes),
        caption=caption,
        parse_mode="HTML",
    )

    # Envoi en DM aux deux (silencieux si le bot n'a pas de conversation privée)
    for uid in (user1.user_id, user2.user_id):
        try:
            await context.bot.send_photo(
                chat_id=uid,
                photo=_io.BytesIO(card_bytes),
                caption=caption,
                parse_mode="HTML",
            )
        except Exception:
            pass  # L'utilisateur n'a pas démarré le bot en privé


# ─── HELPERS INTERNES ────────────────────────────────────────────────────────

def _req_keyboard(req_id: int, req_type: str):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accepter", callback_data=f"req:accept:{req_id}:{req_type}"),
        InlineKeyboardButton("❌ Refuser",  callback_data=f"req:decline:{req_id}:{req_type}"),
    ]])


async def _sync_family_name(session, user_id: int, family_ids: list):
    """Propage le nom de famille à tous les membres qui n'en ont pas."""
    user = await get_user(session, user_id)
    if not user or not user.family_name:
        return
    for fid in family_ids:
        m = await get_user(session, fid)
        if m and not m.family_name:
            m.family_name = user.family_name
    await session.commit()


# ─── /marry ──────────────────────────────────────────────────────────────────

async def marry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return await update.message.reply_text("❗ Cette commande n'est disponible que dans un groupe.")

    target_tg = await parse_target(update, context)

    if not target_tg:
        return await update.message.reply_text(
            "❗ Comment utiliser /marry :\n"
            "1️⃣ Réponds au message de la personne visée + /marry\n"
            "2️⃣ Ou écris /marry @pseudo\n\n"
            "💡 Si la personne n'a jamais utilisé le bot, elle doit d'abord envoyer /start."
        )
    if target_tg.id == update.effective_user.id:
        return await update.message.reply_text("😅 Tu ne peux pas te marier avec toi-même ! Réponds au message de l'autre personne.")
    if target_tg.is_bot:
        return await update.message.reply_text("🤖 Tu ne peux pas épouser un bot !")

    sender = await ensure_user(update.effective_user)
    target = await ensure_user(target_tg)
    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        if await get_spouse(session, sender.user_id):
            return await update.message.reply_text("💍 Tu es déjà marié(e) ! Divorce d'abord avec /divorce.")
        if await get_spouse(session, target.user_id):
            return await update.message.reply_text(f"💔 {mention(target)} est déjà marié(e).", parse_mode=ParseMode.HTML)

        # Bloquer si une demande de mariage est déjà en attente (envoyée par sender)
        from sqlalchemy import select as _sel
        from database.models import PendingRequest as _PR
        _ex = await session.execute(
            _sel(_PR).where(_PR.from_user_id == sender.user_id, _PR.request_type == RequestType.MARRY)
        )
        if _ex.scalar_one_or_none():
            return await update.message.reply_text("⏳ Tu as déjà une demande de mariage en attente !")

        req = await create_request(session, sender.user_id, target.user_id, RequestType.MARRY, group_id, 0)
        msg = await update.message.reply_text(
            f"💌 {mention(sender)} demande {mention(target)} en mariage !\n"
            f"{mention(target)}, acceptes-tu ?",
            reply_markup=_req_keyboard(req.id, "marry"),
            parse_mode=ParseMode.HTML,
        )
        req.message_id = msg.message_id
        await session.commit()


# ─── /adopt ──────────────────────────────────────────────────────────────────

async def adopt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return await update.message.reply_text("❗ Commande de groupe uniquement.")
    target_tg = await parse_target(update, context)
    if not target_tg:
        return await update.message.reply_text(
            "❗ Comment utiliser /adopt :\n"
            "1️⃣ Réponds au message de la personne visée + /adopt\n"
            "2️⃣ Ou écris /adopt @pseudo\n\n"
            "💡 La personne doit avoir envoyé /start au bot."
        )
    if target_tg.id == update.effective_user.id:
        return await update.message.reply_text("😅 Tu ne peux pas t'adopter toi-même !")
    if target_tg.is_bot:
        return await update.message.reply_text("🤖 Tu ne peux pas adopter un bot !")

    sender = await ensure_user(update.effective_user)
    target = await ensure_user(target_tg)
    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        if await relationship_exists(session, sender.user_id, target.user_id, RelationType.PARENT):
            return await update.message.reply_text("👨‍👧 Cette personne est déjà dans ta famille.")
        req = await create_request(session, sender.user_id, target.user_id, RequestType.ADOPT, group_id, 0)
        msg = await update.message.reply_text(
            f"👨‍👦 {mention(sender)} souhaite adopter {mention(target)} !\n"
            f"{mention(target)}, acceptes-tu ?",
            reply_markup=_req_keyboard(req.id, "adopt"),
            parse_mode=ParseMode.HTML,
        )
        req.message_id = msg.message_id
        await session.commit()


# ─── /friend ─────────────────────────────────────────────────────────────────

async def friend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return await update.message.reply_text("❗ Commande de groupe uniquement.")
    target_tg = await parse_target(update, context)
    if not target_tg:
        return await update.message.reply_text(
            "❗ Comment utiliser /friend :\n"
            "1️⃣ Réponds au message de la personne visée + /friend\n"
            "2️⃣ Ou écris /friend @pseudo\n\n"
            "💡 La personne doit avoir envoyé /start au bot."
        )
    if target_tg.id == update.effective_user.id:
        return await update.message.reply_text("😅 Tu es déjà ton propre ami !")
    if target_tg.is_bot:
        return await update.message.reply_text("🤖 Tu ne peux pas ajouter un bot en ami !")

    sender = await ensure_user(update.effective_user)
    target = await ensure_user(target_tg)
    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        if await relationship_exists(session, sender.user_id, target.user_id, RelationType.FRIEND):
            return await update.message.reply_text("🤝 Vous êtes déjà amis !")
        req = await create_request(session, sender.user_id, target.user_id, RequestType.FRIEND, group_id, 0)
        msg = await update.message.reply_text(
            f"🤝 {mention(sender)} veut être ami(e) avec {mention(target)} !\n"
            f"{mention(target)}, acceptes-tu ?",
            reply_markup=_req_keyboard(req.id, "friend"),
            parse_mode=ParseMode.HTML,
        )
        req.message_id = msg.message_id
        await session.commit()


# ─── CALLBACK : accepter / refuser ───────────────────────────────────────────

async def request_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, action, req_id_str, req_type_str = query.data.split(":")
    req_id = int(req_id_str)

    async with AsyncSessionLocal() as session:
        req = await get_request(session, req_id)
        if not req:
            return await query.edit_message_text("⏰ Cette demande a expiré.")

        from datetime import datetime
        if datetime.utcnow() > req.expires_at:
            await delete_request(session, req_id)
            return await query.edit_message_text("⏰ Demande expirée.")

        # Seule la cible peut répondre
        if query.from_user.id != req.to_user_id:
            return await query.answer("❗ Cette demande ne te concerne pas.", show_alert=True)

        if action == "decline":
            await delete_request(session, req_id)
            return await query.edit_message_text("❌ Demande refusée.")

        # ── Acceptation ──
        sender = await get_user(session, req.from_user_id)
        target = await get_user(session, req.to_user_id)

        if req_type_str == "marry":
            if await get_spouse(session, req.from_user_id) or await get_spouse(session, req.to_user_id):
                await delete_request(session, req_id)
                return await query.edit_message_text("💔 L'un de vous est déjà marié(e).")
            await add_relationship(session, req.from_user_id, req.to_user_id,
                                   RelationType.SPOUSE, req.group_id)
            fam = await get_family_members(session, req.from_user_id)
            await _sync_family_name(session, req.from_user_id, fam)
            relation_type = "married"
            text = (f"💒 {mention(sender)} et {mention(target)} sont maintenant mariés ! 🎉\n"
                    f"Félicitations à la famille {sender.family_name or ''} !")

        elif req_type_str == "adopt":
            await add_relationship(session, req.from_user_id, req.to_user_id,
                                   RelationType.PARENT, req.group_id)
            fam = await get_family_members(session, req.from_user_id)
            await _sync_family_name(session, req.from_user_id, fam)
            relation_type = "adopted"
            text = f"👨‍👦 {mention(sender)} a officiellement adopté {mention(target)} !"

        else:  # friend
            await add_relationship(session, req.from_user_id, req.to_user_id,
                                   RelationType.FRIEND, req.group_id)
            relation_type = "friends"
            text = f"🤝 {mention(sender)} et {mention(target)} sont maintenant amis !"

        await delete_request(session, req_id)
        await query.edit_message_text("✅ Accepté ! Génération de la carte...", parse_mode=ParseMode.HTML)

    # Envoi de la carte (hors du bloc session pour éviter les conflits async)
    await _send_relation_card(
        context=context,
        chat_id=req.group_id,
        user1=sender,
        user2=target,
        relation=relation_type,
        caption=text,
    )


# ─── /divorce ────────────────────────────────────────────────────────────────

async def divorce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        rel = await get_spouse(session, user.user_id)
        if not rel:
            return await update.message.reply_text("💔 Tu n'es pas marié(e).")
        spouse_id = rel.related_user_id if rel.user_id == user.user_id else rel.user_id
        spouse = await get_user(session, spouse_id)
        await remove_relationship(session, user.user_id, spouse_id, RelationType.SPOUSE)
        await update.message.reply_text(
            f"💔 {mention(user)} et {mention(spouse)} ont divorcé.",
            parse_mode=ParseMode.HTML,
        )


# ─── /disown ─────────────────────────────────────────────────────────────────

async def disown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_tg = await parse_target(update, context)
    if not target_tg:
        return await update.message.reply_text("❗ Mentionne l'enfant à désavouer.")
    user   = await ensure_user(update.effective_user)
    target = await ensure_user(target_tg)
    async with AsyncSessionLocal() as session:
        if not await relationship_exists(session, user.user_id, target.user_id, RelationType.PARENT):
            return await update.message.reply_text("❗ Cette personne n'est pas dans ta famille.")
        await remove_relationship(session, user.user_id, target.user_id, RelationType.PARENT)
        await update.message.reply_text(
            f"😔 {mention(user)} a désavoué {mention(target)}.",
            parse_mode=ParseMode.HTML,
        )


# ─── /unfriend ───────────────────────────────────────────────────────────────

async def unfriend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_tg = await parse_target(update, context)
    if not target_tg:
        return await update.message.reply_text("❗ Mentionne l'ami à retirer.")
    user   = await ensure_user(update.effective_user)
    target = await ensure_user(target_tg)
    async with AsyncSessionLocal() as session:
        if not await relationship_exists(session, user.user_id, target.user_id, RelationType.FRIEND):
            return await update.message.reply_text("❗ Vous n'êtes pas amis.")
        await remove_relationship(session, user.user_id, target.user_id, RelationType.FRIEND)
        await update.message.reply_text(
            f"😶 {mention(user)} et {mention(target)} ne sont plus amis.",
            parse_mode=ParseMode.HTML,
        )


# ─── /setfamilyname ──────────────────────────────────────────────────────────

async def setfamilyname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage : /setfamilyname NomDeFamille")
    name = " ".join(context.args)[:50]
    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.user_id)
        u.family_name = name
        fam = await get_family_members(session, user.user_id)
        await _sync_family_name(session, user.user_id, fam)
        await session.commit()
    await update.message.reply_text(f"🏠 Nom de famille défini : <b>{name}</b>", parse_mode=ParseMode.HTML)


# ─── /leave (héritage) ───────────────────────────────────────────────────────

async def leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update.effective_user)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirmer", callback_data=f"leave:confirm:{user.user_id}"),
        InlineKeyboardButton("❌ Annuler",   callback_data=f"leave:cancel"),
    ]])
    await update.message.reply_text(
        f"⚠️ {mention(user)}, es-tu sûr(e) de vouloir quitter ?\n"
        "80 % de tes coins seront transmis à ta famille et toutes tes relations seront dissoutes.",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


async def leave_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")

    if parts[1] == "cancel":
        return await query.edit_message_text("✅ Annulé.")

    user_id = int(parts[2])
    if query.from_user.id != user_id:
        return await query.answer("❗ Ce n'est pas ta demande.", show_alert=True)

    async with AsyncSessionLocal() as session:
        result = await process_inheritance(session, user_id)

    if not result:
        return await query.edit_message_text("❗ Erreur lors du départ.")

    oldest = result.get("oldest_child")
    title_msg = ""
    if oldest:
        async with AsyncSessionLocal() as session:
            heir = await get_user(session, oldest)
            if heir:
                title_msg = f"\n👑 {mention(heir)} hérite du titre dynastique !"

    members_count = len(result.get("members", []))
    await query.edit_message_text(
        f"🕊️ <b>Adieu !</b>\n"
        f"💰 {result['coins_each']} coins transmis à chacun des {members_count} membres de ta famille."
        + title_msg,
        parse_mode=ParseMode.HTML,
    )


# ─── /familyphoto ─────────────────────────────────────────────────────────────

async def familyphoto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Compose une grille des photos de profil de la famille."""
    from PIL import Image
    import io as sio

    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        family_ids = await get_family_members(session, user.user_id)
        all_ids = [user.user_id] + family_ids

        # Récupérer les noms
        names = {}
        for uid in all_ids[:9]:
            u = await get_user(session, uid)
            if u:
                names[uid] = u.first_name

    await update.message.reply_text("📸 Composition de la photo de famille...")

    THUMB = 120
    COLS  = 3
    ROWS  = (len(all_ids[:9]) + COLS - 1) // COLS
    img   = Image.new("RGB", (COLS * THUMB, ROWS * THUMB), (20, 20, 35))

    for idx, uid in enumerate(all_ids[:9]):
        col = idx % COLS
        row = idx // COLS
        x, y = col * THUMB, row * THUMB
        # Essayer de télécharger la photo
        try:
            photos = await context.bot.get_user_profile_photos(uid, limit=1)
            if photos.total_count:
                file = await context.bot.get_file(photos.photos[0][0].file_id)
                data = await file.download_as_bytearray()
                thumb = Image.open(sio.BytesIO(bytes(data))).resize((THUMB, THUMB))
                img.paste(thumb, (x, y))
            else:
                raise ValueError("no photo")
        except Exception:
            from PIL import ImageDraw
            from config import PROFILE_COLORS
            d = ImageDraw.Draw(img)
            d.rectangle([x, y, x+THUMB, y+THUMB], fill=(40, 40, 60))
            d.text((x+THUMB//2, y+THUMB//2), names.get(uid, "?")[0].upper(),
                   fill=(200, 200, 255), anchor="mm")

    buf = sio.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    buf.seek(0)
    family_name_str = f"Famille {user.family_name}" if user.family_name else "Photo de famille"
    await update.message.reply_photo(buf, caption=f"📸 {family_name_str}")
