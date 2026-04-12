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


# ─── CARTE ───────────────────────────────────────────────────────────────────

async def _send_relation_card(context, chat_id, user1, user2, relation, caption):
    try:
        from utils.card_generator import generate_relation_card

        async def _photo(uid):
            try:
                photos = await context.bot.get_user_profile_photos(uid, limit=1)
                if photos.total_count:
                    f    = await context.bot.get_file(photos.photos[0][0].file_id)
                    data = await f.download_as_bytearray()
                    return bytes(data)
            except Exception:
                pass
            return None

        p1 = await _photo(user1.user_id)
        p2 = await _photo(user2.user_id)

        card = generate_relation_card(user1.first_name, user2.first_name, relation, p1, p2)

        await context.bot.send_photo(chat_id=chat_id, photo=io.BytesIO(card),
                                     caption=caption, parse_mode="HTML")
        for uid in (user1.user_id, user2.user_id):
            try:
                await context.bot.send_photo(chat_id=uid, photo=io.BytesIO(card),
                                             caption=caption, parse_mode="HTML")
            except Exception:
                pass
    except Exception as e:
        logger.error(f"_send_relation_card: {e}")
        await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML")


def _req_keyboard(req_id, req_type):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accepter", callback_data=f"req:accept:{req_id}:{req_type}"),
        InlineKeyboardButton("❌ Refuser",  callback_data=f"req:decline:{req_id}:{req_type}"),
    ]])


async def _sync_family_name(session, user_id, family_ids):
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
        return await update.message.reply_text("Cette commande n'est disponible que dans un groupe.")

    target_tg = await parse_target(update, context)
    if not target_tg:
        return await update.message.reply_text(
            "Comment utiliser /marry :\n"
            "1. Reponds au message de la personne + /marry\n"
            "2. Ou ecris /marry @pseudo\n\n"
            "La personne doit d'abord envoyer /start."
        )
    if target_tg.id == update.effective_user.id:
        return await update.message.reply_text("Tu ne peux pas te marier avec toi-meme !")
    if target_tg.is_bot:
        return await update.message.reply_text("Tu ne peux pas epouser un bot !")

    sender   = await ensure_user(update.effective_user)
    target   = await ensure_user(target_tg)
    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        # Verifier si sender est deja marie
        s_rel = await get_spouse(session, sender.user_id)
        if s_rel:
            return await update.message.reply_text("Tu es deja marie(e) ! Divorce d'abord avec /divorce.")

        # Verifier si target est deja marie — afficher avec qui
        t_rel = await get_spouse(session, target.user_id)
        if t_rel:
            other_id  = t_rel.related_user_id if t_rel.user_id == target.user_id else t_rel.user_id
            other     = await get_user(session, other_id)
            other_name = other.first_name if other else "quelqu'un"
            return await update.message.reply_text(
                f"💔 {mention(target)} est deja marie(e) avec {other_name}.",
                parse_mode=ParseMode.HTML,
            )

        # Demande en attente de sender ?
        from sqlalchemy import select as _sel
        from database.models import PendingRequest as _PR
        _ex = await session.execute(
            _sel(_PR).where(_PR.from_user_id == sender.user_id, _PR.request_type == RequestType.MARRY)
        )
        if _ex.scalar_one_or_none():
            return await update.message.reply_text("Tu as deja une demande de mariage en attente !")

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
        return await update.message.reply_text("Commande de groupe uniquement.")
    target_tg = await parse_target(update, context)
    if not target_tg:
        return await update.message.reply_text(
            "Usage : Reponds au message de la personne + /adopt  ou  /adopt @pseudo"
        )
    if target_tg.id == update.effective_user.id:
        return await update.message.reply_text("Tu ne peux pas t'adopter toi-meme !")
    if target_tg.is_bot:
        return await update.message.reply_text("Tu ne peux pas adopter un bot !")

    sender   = await ensure_user(update.effective_user)
    target   = await ensure_user(target_tg)
    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        if await relationship_exists(session, sender.user_id, target.user_id, RelationType.PARENT):
            return await update.message.reply_text("Cette personne est deja dans ta famille.")
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
        return await update.message.reply_text("Commande de groupe uniquement.")
    target_tg = await parse_target(update, context)
    if not target_tg:
        return await update.message.reply_text(
            "Usage : Reponds au message de la personne + /friend  ou  /friend @pseudo"
        )
    if target_tg.id == update.effective_user.id:
        return await update.message.reply_text("Tu es deja ton propre ami !")
    if target_tg.is_bot:
        return await update.message.reply_text("Tu ne peux pas ajouter un bot en ami !")

    sender   = await ensure_user(update.effective_user)
    target   = await ensure_user(target_tg)
    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        if await relationship_exists(session, sender.user_id, target.user_id, RelationType.FRIEND):
            return await update.message.reply_text("Vous etes deja amis !")
        req = await create_request(session, sender.user_id, target.user_id, RequestType.FRIEND, group_id, 0)
        msg = await update.message.reply_text(
            f"🤝 {mention(sender)} veut etre ami(e) avec {mention(target)} !\n"
            f"{mention(target)}, acceptes-tu ?",
            reply_markup=_req_keyboard(req.id, "friend"),
            parse_mode=ParseMode.HTML,
        )
        req.message_id = msg.message_id
        await session.commit()


# ─── CALLBACK ────────────────────────────────────────────────────────────────

async def request_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, action, req_id_str, req_type_str = query.data.split(":")
    req_id = int(req_id_str)

    async with AsyncSessionLocal() as session:
        req = await get_request(session, req_id)
        if not req:
            return await query.edit_message_text("Cette demande a expire.")

        from datetime import datetime
        if datetime.utcnow() > req.expires_at:
            await delete_request(session, req_id)
            return await query.edit_message_text("Demande expiree.")

        if query.from_user.id != req.to_user_id:
            return await query.answer("Cette demande ne te concerne pas.", show_alert=True)

        if action == "decline":
            await delete_request(session, req_id)
            return await query.edit_message_text("Demande refusee.")

        sender = await get_user(session, req.from_user_id)
        target = await get_user(session, req.to_user_id)

        if req_type_str == "marry":
            if await get_spouse(session, req.from_user_id) or await get_spouse(session, req.to_user_id):
                await delete_request(session, req_id)
                return await query.edit_message_text("L'un de vous est deja marie(e).")
            await add_relationship(session, req.from_user_id, req.to_user_id, RelationType.SPOUSE, req.group_id)
            fam = await get_family_members(session, req.from_user_id)
            await _sync_family_name(session, req.from_user_id, fam)

            # Cadeau de mariage aleatoire (500 - 8000 coins chacun)
            gift = random.randint(500, 8_000)
            for uid in (req.from_user_id, req.to_user_id):
                u = await get_user(session, uid)
                if u:
                    u.coins += gift
            await session.commit()

            relation_type = "married"
            text = (f"💒 {mention(sender)} et {mention(target)} sont maintenant maries ! 🎉\n"
                    f"Felicitations a la famille {sender.family_name or ''} !\n"
                    f"💝 Cadeau de mariage : <b>{gift:,} $</b> chacun !")

        elif req_type_str == "adopt":
            await add_relationship(session, req.from_user_id, req.to_user_id, RelationType.PARENT, req.group_id)
            fam = await get_family_members(session, req.from_user_id)
            await _sync_family_name(session, req.from_user_id, fam)
            relation_type = "adopted"
            text = f"👨‍👦 {mention(sender)} a officiellement adopte {mention(target)} !"

        else:
            await add_relationship(session, req.from_user_id, req.to_user_id, RelationType.FRIEND, req.group_id)
            relation_type = "friends"
            text = f"🤝 {mention(sender)} et {mention(target)} sont maintenant amis !"

        await delete_request(session, req_id)
        await query.edit_message_text("Accepte ! Generation de la carte...", parse_mode=ParseMode.HTML)

    await _send_relation_card(context, req.group_id, sender, target, relation_type, text)


# ─── /divorce ────────────────────────────────────────────────────────────────

async def divorce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        rel = await get_spouse(session, user.user_id)
        if not rel:
            return await update.message.reply_text("Tu n'es pas marie(e).")
        spouse_id = rel.related_user_id if rel.user_id == user.user_id else rel.user_id
        spouse    = await get_user(session, spouse_id)
        await remove_relationship(session, user.user_id, spouse_id, RelationType.SPOUSE)
    await update.message.reply_text(
        f"💔 {mention(user)} et {mention(spouse)} ont divorce.",
        parse_mode=ParseMode.HTML,
    )


# ─── /disown ─────────────────────────────────────────────────────────────────

async def disown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_tg = await parse_target(update, context)
    if not target_tg:
        return await update.message.reply_text("Mentionne l'enfant a desavouer.")
    user   = await ensure_user(update.effective_user)
    target = await ensure_user(target_tg)
    async with AsyncSessionLocal() as session:
        if not await relationship_exists(session, user.user_id, target.user_id, RelationType.PARENT):
            return await update.message.reply_text("Cette personne n'est pas dans ta famille.")
        await remove_relationship(session, user.user_id, target.user_id, RelationType.PARENT)
    await update.message.reply_text(
        f"😔 {mention(user)} a desavoue {mention(target)}.",
        parse_mode=ParseMode.HTML,
    )


# ─── /unfriend ───────────────────────────────────────────────────────────────

async def unfriend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_tg = await parse_target(update, context)
    if not target_tg:
        return await update.message.reply_text("Mentionne l'ami a retirer.")
    user   = await ensure_user(update.effective_user)
    target = await ensure_user(target_tg)
    async with AsyncSessionLocal() as session:
        if not await relationship_exists(session, user.user_id, target.user_id, RelationType.FRIEND):
            return await update.message.reply_text("Vous n'etes pas amis.")
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
    await update.message.reply_text(f"Nom de famille : {name}")


# ─── /leave ──────────────────────────────────────────────────────────────────

async def leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update.effective_user)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirmer", callback_data=f"leave:confirm:{user.user_id}"),
        InlineKeyboardButton("❌ Annuler",   callback_data="leave:cancel"),
    ]])
    await update.message.reply_text(
        f"⚠️ {mention(user)}, es-tu sur(e) de vouloir quitter ?\n"
        "80% de tes $ seront transmis a ta famille.",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


async def leave_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    if parts[1] == "cancel":
        return await query.edit_message_text("Annule.")
    user_id = int(parts[2])
    if query.from_user.id != user_id:
        return await query.answer("Ce n'est pas ta demande.", show_alert=True)
    async with AsyncSessionLocal() as session:
        result = await process_inheritance(session, user_id)
    if not result:
        return await query.edit_message_text("Erreur lors du depart.")
    members_count = len(result.get("members", []))
    await query.edit_message_text(
        f"Adieu !\n"
        f"{result['coins_each']:,} $ transmis a chacun des {members_count} membres."
    )


# ─── /familyphoto ─────────────────────────────────────────────────────────────

async def familyphoto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from PIL import Image
    import io as sio

    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        family_ids = await get_family_members(session, user.user_id)
        all_ids    = [user.user_id] + family_ids
        names      = {}
        for uid in all_ids[:9]:
            u = await get_user(session, uid)
            if u:
                names[uid] = u.first_name

    await update.message.reply_text("Composition de la photo de famille...")

    THUMB = 120
    COLS  = 3
    ROWS  = (len(all_ids[:9]) + COLS - 1) // COLS
    img   = Image.new("RGB", (COLS * THUMB, ROWS * THUMB), (20, 20, 35))

    for idx, uid in enumerate(all_ids[:9]):
        col = idx % COLS
        row = idx // COLS
        x, y = col * THUMB, row * THUMB
        try:
            photos = await context.bot.get_user_profile_photos(uid, limit=1)
            if photos.total_count:
                f    = await context.bot.get_file(photos.photos[0][0].file_id)
                data = await f.download_as_bytearray()
                thumb = Image.open(sio.BytesIO(bytes(data))).resize((THUMB, THUMB))
                img.paste(thumb, (x, y))
            else:
                raise ValueError("no photo")
        except Exception:
            from PIL import ImageDraw
            d = ImageDraw.Draw(img)
            d.rectangle([x, y, x+THUMB, y+THUMB], fill=(40, 40, 60))
            d.text((x+THUMB//2, y+THUMB//2), names.get(uid, "?")[0].upper(),
                   fill=(200, 200, 255), anchor="mm")

    buf = sio.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    buf.seek(0)
    fam_name = f"Famille {user.family_name}" if user.family_name else "Photo de famille"
    await update.message.reply_photo(buf, caption=f"📸 {fam_name}")
