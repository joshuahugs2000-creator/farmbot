import io, random, logging
from datetime import datetime
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from handlers.journal import log_event
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database.db import (
    AsyncSessionLocal, upsert_user, get_user, get_spouse, get_all_spouses, get_relationships,
    get_family_members, add_relationship, remove_relationship, relationship_exists,
    create_request, get_request, delete_request, process_inheritance, compute_title,
)
from database.models import RelationType, RequestType
from sqlalchemy import text
from utils.helpers import mention, mention_tg, is_group, parse_target, ensure_user
from config import MOODS, CURRENCY

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

    async with AsyncSessionLocal() as session:
        s_db = await get_user(session, sender.user_id)
        t_db = await get_user(session, target.user_id)

        # ── Vérification de compatibilité de genre ────────────────────────
        s_gender = getattr(s_db, "gender", None)
        t_gender = getattr(t_db, "gender", None)
        if s_gender and t_gender and s_gender == t_gender:
            label = "deux hommes" if s_gender == "homme" else "deux femmes"
            return await update.message.reply_text(
                f"❌ Le mariage entre {label} n'est pas autorisé sur ce bot.\n"
                f"Utilise /setsexe pour modifier ton genre."
            )

        # ── Vérification monogamie/polygamie ──────────────────────────────
        s_type = getattr(s_db, "marriage_type", "monogame") or "monogame"
        s_spouses = await get_all_spouses(session, sender.user_id)

        if s_spouses and s_type == "monogame":
            return await update.message.reply_text(
                "❌ Tu es déjà marié(e) et ton mode est <b>monogame</b>.\n"
                "Divorce d'abord avec /divorce, ou change de mode avec /setmariage polygame.",
                parse_mode=ParseMode.HTML,
            )

        # Vérifier si la cible est monogame et déjà mariée
        t_type = getattr(t_db, "marriage_type", "monogame") or "monogame"
        t_spouses = await get_all_spouses(session, target.user_id)
        if t_spouses and t_type == "monogame":
            other_id   = t_spouses[0].related_user_id if t_spouses[0].user_id == target.user_id else t_spouses[0].user_id
            other      = await get_user(session, other_id)
            other_name = other.first_name if other else "quelqu'un"
            return await update.message.reply_text(
                f"💔 {mention(target)} est déjà marié(e) avec {other_name} (mode monogame).",
                parse_mode=ParseMode.HTML,
            )

        # Vérifier demandes en attente
        from sqlalchemy import select as _sel
        from database.models import PendingRequest as _PR
        _ex = await session.execute(
            _sel(_PR).where(_PR.from_user_id == sender.user_id, _PR.request_type == RequestType.MARRY)
        )
        existing_sender = _ex.scalar_one_or_none()
        if existing_sender:
            if datetime.utcnow() < existing_sender.expires_at:
                return await update.message.reply_text("Tu as deja une demande de mariage en attente !")
            else:
                await session.delete(existing_sender)
                await session.commit()

        _ex2 = await session.execute(
            _sel(_PR).where(_PR.to_user_id == target.user_id, _PR.request_type == RequestType.MARRY)
        )
        existing_target = _ex2.scalar_one_or_none()
        if existing_target:
            if datetime.utcnow() < existing_target.expires_at:
                return await update.message.reply_text(
                    f"💔 {mention(target)} a déjà une demande de mariage en attente. Réessaie dans quelques instants.",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await session.delete(existing_target)
                await session.commit()

    # ── Demander le type de mariage avant d'envoyer la demande ───────────
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❤️ Monogame", callback_data=f"marry_type:mono:{target.user_id}:{update.effective_chat.id}"),
        InlineKeyboardButton("💞 Polygame", callback_data=f"marry_type:poly:{target.user_id}:{update.effective_chat.id}"),
    ]])
    await update.message.reply_text(
        f"💍 {mention(sender)}, quel type de mariage proposes-tu à {mention(target)} ?",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


async def marry_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback pour le choix monogame/polygame, crée ensuite la vraie demande."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    # marry_type : mono/poly : target_id : group_id
    _, m_type, target_id_str, group_id_str = parts
    target_id = int(target_id_str)
    group_id  = int(group_id_str)

    if query.from_user.id != query.from_user.id:  # sécurité: toujours l'émetteur
        return

    sender_tg = query.from_user
    sender    = await ensure_user(sender_tg)
    type_label = "Monogame ❤️" if m_type == "mono" else "Polygame 💞"
    marriage_type_str = "monogame" if m_type == "mono" else "polygame"

    async with AsyncSessionLocal() as session:
        target = await get_user(session, target_id)
        if not target:
            return await query.edit_message_text("❌ Cible introuvable.")

        req = await create_request(session, sender.user_id, target_id, RequestType.MARRY, group_id, 0)
        # Stocker le type de mariage dans extra
        req.extra = marriage_type_str
        await session.commit()

        req_keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Accepter", callback_data=f"req:accept:{req.id}:marry"),
            InlineKeyboardButton("❌ Refuser",  callback_data=f"req:decline:{req.id}:marry"),
        ]])
        msg = await query.edit_message_text(
            f"💌 {mention(sender)} demande {mention(target)} en mariage ! ({type_label})\n"
            f"{mention(target)}, acceptes-tu ?",
            reply_markup=req_keyboard,
            parse_mode=ParseMode.HTML,
        )
        req.message_id = msg.message_id if hasattr(msg, "message_id") else None
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
            # Récupérer le type de mariage stocké dans extra
            marriage_type = getattr(req, "extra", "monogame") or "monogame"

            sender_db = await get_user(session, req.from_user_id)
            target_db = await get_user(session, req.to_user_id)

            # Re-vérifier compatibilité genre au moment de l'acceptation
            s_gender = getattr(sender_db, "gender", None)
            t_gender = getattr(target_db, "gender", None)
            if s_gender and t_gender and s_gender == t_gender:
                await delete_request(session, req_id)
                return await query.edit_message_text("❌ Mariage impossible : même genre détecté.")

            # Vérif monogamie sender
            s_type = getattr(sender_db, "marriage_type", "monogame") or "monogame"
            s_spouses = await get_all_spouses(session, req.from_user_id)
            if s_spouses and s_type == "monogame":
                await delete_request(session, req_id)
                return await query.edit_message_text("❌ L'émetteur est déjà marié (monogame).")

            # Vérif monogamie target
            t_type = getattr(target_db, "marriage_type", "monogame") or "monogame"
            t_spouses = await get_all_spouses(session, req.to_user_id)
            if t_spouses and t_type == "monogame":
                await delete_request(session, req_id)
                return await query.edit_message_text("❌ Le/la destinataire est déjà marié(e) (monogame).")

            await add_relationship(session, req.from_user_id, req.to_user_id, RelationType.SPOUSE, req.group_id)
            fam = await get_family_members(session, req.from_user_id)
            await _sync_family_name(session, req.from_user_id, fam)

            # Cadeau de mariage aleatoire (500 - 8000 coins chacun)
            gift = random.randint(500, 8_000)
            for uid in (req.from_user_id, req.to_user_id):
                await session.execute(
                    text("UPDATE users SET coins = CAST(coins AS BIGINT) + CAST(:amt AS BIGINT) WHERE user_id = :uid"),
                    {"amt": gift, "uid": uid}
                )
            await session.commit()

            type_label = "polygame 💞" if marriage_type == "polygame" else "monogame ❤️"
            relation_type = "married"
            caption = (
                f"💒 {mention(sender)} et {mention(target)} sont maintenant mariés ! 🎉\n"
                f"Type : <b>{type_label}</b>\n"
                f"Félicitations à la famille {sender.family_name or ''} !\n"
                f"💝 Cadeau de mariage : <b>{gift:,} {CURRENCY}</b> chacun !"
            )
            await log_event("marriage", a=sender.first_name, b=target.first_name)

        elif req_type_str == "adopt":
            await add_relationship(session, req.from_user_id, req.to_user_id, RelationType.PARENT, req.group_id)
            fam = await get_family_members(session, req.from_user_id)
            await _sync_family_name(session, req.from_user_id, fam)
            relation_type = "adopted"
            caption = f"👨‍👦 {mention(sender)} a officiellement adopte {mention(target)} !"
            await log_event("adoption", a=sender.first_name, b=target.first_name)

        else:
            await add_relationship(session, req.from_user_id, req.to_user_id, RelationType.FRIEND, req.group_id)
            relation_type = "friends"
            caption = f"🤝 {mention(sender)} et {mention(target)} sont maintenant amis !"

        await delete_request(session, req_id)
        await query.edit_message_text("Accepte ! Generation de la carte...", parse_mode=ParseMode.HTML)

    await _send_relation_card(context, req.group_id, sender, target, relation_type, caption)


# ─── /divorce ────────────────────────────────────────────────────────────────

async def divorce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        spouses = await get_all_spouses(session, user.user_id)
        if not spouses:
            return await update.message.reply_text("Tu n'es pas marié(e).")

        if len(spouses) == 1:
            # Un seul conjoint : divorce direct
            rel = spouses[0]
            spouse_id = rel.related_user_id if rel.user_id == user.user_id else rel.user_id
            spouse    = await get_user(session, spouse_id)
            await remove_relationship(session, user.user_id, spouse_id, RelationType.SPOUSE)
            await log_event("divorce", a=user.first_name, b=spouse.first_name if spouse else "?")
            return await update.message.reply_text(
                f"💔 {mention(user)} et {mention(spouse)} ont divorcé.",
                parse_mode=ParseMode.HTML,
            )

        # Plusieurs conjoints : doit mentionner lequel
        target_tg = await parse_target(update, context)
        if not target_tg:
            # Afficher la liste des conjoints
            lines = ["💔 Tu as plusieurs conjoints. Mentionne celui dont tu veux divorcer :\n<code>/divorce @pseudo</code>\n"]
            for rel in spouses:
                sid = rel.related_user_id if rel.user_id == user.user_id else rel.user_id
                sp  = await get_user(session, sid)
                if sp:
                    lines.append(f"• {sp.first_name} (@{sp.username or sp.user_id})")
            return await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

        target = await get_user(session, target_tg.id)
        if not target:
            return await update.message.reply_text("❌ Utilisateur introuvable.")

        # Vérifier que c'est bien un conjoint
        is_spouse = any(
            (rel.user_id == user.user_id and rel.related_user_id == target_tg.id) or
            (rel.related_user_id == user.user_id and rel.user_id == target_tg.id)
            for rel in spouses
        )
        if not is_spouse:
            return await update.message.reply_text(
                f"❌ {mention(target)} n'est pas ton/ta conjoint(e).", parse_mode=ParseMode.HTML
            )

        await remove_relationship(session, user.user_id, target_tg.id, RelationType.SPOUSE)
        await log_event("divorce", a=user.first_name, b=target.first_name)
        return await update.message.reply_text(
            f"💔 {mention(user)} et {mention(target)} ont divorcé.",
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

FAMILY_NAME_COST = 50_000  # coût en coins

async def setfamilyname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import re
    if not context.args:
        return await update.message.reply_text(
            f"✏️ <b>Changer le nom de famille</b>\n\n"
            f"Usage : <code>/setfamilyname NomDeFamille</code>\n"
            f"💰 Coût : <b>{FAMILY_NAME_COST:,} {CURRENCY}</b>",
            parse_mode=ParseMode.HTML
        )

    name = " ".join(context.args)[:50].strip()
    if not re.match(r"^[\w\s\-']{2,50}$", name, re.UNICODE):
        return await update.message.reply_text(
            "❌ Nom invalide. Lettres, espaces et tirets uniquement (2 à 50 caractères)."
        )

    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.user_id)
        if not u:
            return await update.message.reply_text("❌ Compte introuvable.")
        if u.coins < FAMILY_NAME_COST:
            return await update.message.reply_text(
                f"❌ Pas assez de coins.\n"
                f"💰 Requis : <b>{FAMILY_NAME_COST:,} {CURRENCY}</b>\n"
                f"💵 Ton solde : <b>{u.coins:,} {CURRENCY}</b>",
                parse_mode=ParseMode.HTML
            )
        old_name = u.family_name or "—"
        u.coins -= FAMILY_NAME_COST
        u.family_name = name
        fam = await get_family_members(session, user.user_id)
        await _sync_family_name(session, user.user_id, fam)
        await session.commit()

    await update.message.reply_text(
        f"✅ <b>Nom de famille mis à jour !</b>\n\n"
        f"Ancien : <i>{old_name}</i>\n"
        f"Nouveau : <b>{name}</b>\n\n"
        f"💰 <b>{FAMILY_NAME_COST:,} {CURRENCY}</b> débités.",
        parse_mode=ParseMode.HTML
    )


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
        f"{result['coins_each']:,} {CURRENCY} transmis a chacun des {members_count} membres."
    )


# ─── /setsexe ────────────────────────────────────────────────────────────────

async def setsexe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Définit le genre de l'utilisateur (homme / femme)."""
    user = await ensure_user(update.effective_user)
    if not context.args:
        return await update.message.reply_text(
            "Usage : <code>/setsexe homme</code> ou <code>/setsexe femme</code>",
            parse_mode=ParseMode.HTML,
        )
    choice = context.args[0].lower()
    if choice not in ("homme", "femme"):
        return await update.message.reply_text("❌ Choix invalide. Utilise <code>homme</code> ou <code>femme</code>.", parse_mode=ParseMode.HTML)

    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.user_id)
        if not u:
            return
        u.gender = choice
        await session.commit()

    emoji = "♂️" if choice == "homme" else "♀️"
    await update.message.reply_text(f"{emoji} Genre défini : <b>{choice}</b>.", parse_mode=ParseMode.HTML)


# ─── /setmariage ─────────────────────────────────────────────────────────────

async def setmariage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Définit la préférence de mariage (monogame / polygame)."""
    user = await ensure_user(update.effective_user)
    if not context.args:
        return await update.message.reply_text(
            "Usage : <code>/setmariage monogame</code> ou <code>/setmariage polygame</code>",
            parse_mode=ParseMode.HTML,
        )
    choice = context.args[0].lower()
    if choice not in ("monogame", "polygame"):
        return await update.message.reply_text(
            "❌ Choix invalide. Utilise <code>monogame</code> ou <code>polygame</code>.",
            parse_mode=ParseMode.HTML,
        )

    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.user_id)
        if not u:
            return
        u.marriage_type = choice
        await session.commit()

    emoji = "❤️" if choice == "monogame" else "💞"
    await update.message.reply_text(
        f"{emoji} Mode de mariage défini : <b>{choice}</b>.\n"
        f"<i>Ce mode s'appliquera à ta prochaine demande de mariage.</i>",
        parse_mode=ParseMode.HTML,
    )


# ─── Callback choix type de mariage ─────────────────────────────────────────

async def marry_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback pour le choix monogame/polygame avant d'envoyer la demande."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    # marry_type:mono/poly:target_id:group_id
    _, m_type, target_id_str, group_id_str = parts
    target_id = int(target_id_str)
    group_id  = int(group_id_str)

    # Seul l'émetteur peut choisir
    sender_tg = query.from_user
    sender    = await ensure_user(sender_tg)
    type_label     = "Monogame ❤️" if m_type == "mono" else "Polygame 💞"
    marriage_type  = "monogame" if m_type == "mono" else "polygame"

    async with AsyncSessionLocal() as session:
        target = await get_user(session, target_id)
        if not target:
            return await query.edit_message_text("❌ Cible introuvable.")

        req = await create_request(session, sender.user_id, target_id, RequestType.MARRY, group_id, 0)
        req.extra = marriage_type
        await session.commit()

        req_keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Accepter", callback_data=f"req:accept:{req.id}:marry"),
            InlineKeyboardButton("❌ Refuser",  callback_data=f"req:decline:{req.id}:marry"),
        ]])
        await query.edit_message_text(
            f"💌 {mention(sender)} demande {mention(target)} en mariage ! ({type_label})\n"
            f"{mention(target)}, acceptes-tu ?",
            reply_markup=req_keyboard,
            parse_mode=ParseMode.HTML,
        )

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
