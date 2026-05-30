"""
handlers/profile.py — Profil utilisateur amélioré avec karma visuel.
"""
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database.db import (
    AsyncSessionLocal, get_user, upsert_user, compute_title,
    get_family_members, get_karma_level, karma_bar, get_richlist,
)
from utils.helpers import ensure_user
from config import PROFILE_COLORS, TITLES, CURRENCY


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


async def me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche la carte de profil complète."""
    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        u     = await get_user(session, user.user_id)
        title = await compute_title(session, user.user_id)
        fam   = await get_family_members(session, user.user_id)
        size  = len(fam)

    karma   = u.karma if u else 0
    coins   = u.coins if u else 0
    color   = u.profile_color if u else "blue"
    fam_name = u.family_name if u else None
    joined  = u.created_at.strftime("%d/%m/%Y") if u and u.created_at else "—"

    # Badge top 10
    TOP10_BADGES = {0:"👑",1:"🥈",2:"🥉",3:"💎",4:"💎",5:"⭐",6:"⭐",7:"🔥",8:"🔥",9:"🎖️"}
    TOP10_LABELS = {0:"Roi de la richesse",1:"Vice-roi",2:"Seigneur",3:"Élite Diamond",4:"Élite Diamond",5:"Top Star",6:"Top Star",7:"Flambeur",8:"Flambeur",9:"Top 10"}
    async with AsyncSessionLocal() as session2:
        top10 = await get_richlist(session2, 10)
    rank = next((i for i, t in enumerate(top10) if t.user_id == user.user_id), None)
    rank_badge = f"\n  {TOP10_BADGES[rank]} <b>Top {rank+1} — {TOP10_LABELS[rank]}</b>" if rank is not None else ""

    color_dot = {
        "blue": "🔵", "green": "🟢", "red": "🔴", "purple": "🟣",
        "orange": "🟠", "pink": "🩷", "gold": "🟡", "teal": "🩵",
    }.get(color, "🔵")

    level    = get_karma_level(karma)
    bar      = karma_bar(karma)
    karma_pct = level["daily_pct"]
    karma_sign = f"+{karma_pct}%" if karma_pct >= 0 else f"{karma_pct}%"

    # ── Diplômes ──────────────────────────────────────────────────────────────
    DIPLOME_LABELS = {"bac": "BAC", "licence": "LICENCE", "master": "MASTER", "mba": "MBA"}
    DIPLOME_EMOJIS = {"bac": "📄", "licence": "🎓", "master": "🏅", "mba": "👑"}
    DOMAIN_EMOJIS  = {"finance": "📈", "informatique": "💻", "marketing": "📣",
                      "droit": "⚖️", "management": "🏢", "agriculture": "🌾", "securite": "🛡️"}
    diplomes_obtenus = [
        f"{DIPLOME_EMOJIS[lvl]} {DIPLOME_LABELS[lvl]}"
        for lvl in ("bac", "licence", "master", "mba")
        if getattr(u, f"diplome_{lvl}", False)
    ]
    diplome_domain = getattr(u, "diplome_domain", None)
    if diplome_domain:
        d_em = DOMAIN_EMOJIS.get(diplome_domain, "🎓")
        domain_tag = f" · {d_em} <b>{diplome_domain.upper()}</b>"
    else:
        domain_tag = ""
    if diplomes_obtenus:
        diplome_str = "  ".join(diplomes_obtenus) + domain_tag
    else:
        diplome_str = "<i>Aucun — /diplome pour s'inscrire</i>"

    # ── Poste entreprise ──────────────────────────────────────────────────────
    company_line = ""
    try:
        from handlers.company import _get_user_company, ROLE_EMOJI as _ROLE_EMOJI, SECTORS as _SECTORS
        async with AsyncSessionLocal() as _cs:
            _company, _emp = await _get_user_company(_cs, update.effective_user.id)
            if _company and _emp:
                _re = _ROLE_EMOJI.get(_emp.role, "👤")
                _se, _ = _SECTORS.get(_company.sector, ("🏢", ""))
                company_line = f"\n\n  🏢 <b>ENTREPRISE</b>\n  ╰┈➤  {_re} <b>{_emp.role.capitalize()}</b> chez <b>{_company.name}</b> {_se}"
    except Exception:
        pass

    # ── Niveau de richesse ────────────────────────────────────────────────────
    WEALTH_LEVELS = [
        (1_000_000_000, "💎", "Milliardaire"),
        (100_000_000,   "🏆", "Magnat"),
        (10_000_000,    "💰", "Fortuné"),
        (1_000_000,     "📈", "Aisé"),
        (0,             "🪙", "Débutant"),
    ]
    wealth_emoji, wealth_label = next(
        (e, l) for threshold, e, l in WEALTH_LEVELS if coins >= threshold
    )

    top_badge_line = f"\n     {TOP10_BADGES[rank]} <i>{TOP10_LABELS[rank]}</i> — Top {rank + 1}" if rank is not None else ""
    fam_display = f"<b>{fam_name}</b>" if fam_name else "<i>Sans famille</i>"
    fam_size_str = f"  ({size} membre{'s' if size > 1 else ''})"

    # ── Genre et type de mariage ──────────────────────────────────────────────
    gender = getattr(u, "gender", None)
    marriage_type = getattr(u, "marriage_type", "monogame") or "monogame"
    gender_emoji = "♂️" if gender == "homme" else ("♀️" if gender == "femme" else "❓")
    gender_label = gender.capitalize() if gender else "Non défini"
    marry_emoji  = "❤️" if marriage_type == "monogame" else "💞"

    lines = [
        f"",
        f"「 {color_dot} 」<b>{update.effective_user.first_name}</b>",
        f"✦ {wealth_emoji} <b>{wealth_label}</b>  ┊  🏅 <i>{title}</i>{top_badge_line}",
        f"✦ 📅 <i>Depuis le {joined}</i>",
        f"",
        f"◈━━━━━━━━━━━━━━━━━━━━━━━━◈",
        f"",
        f"  💰 <b>FORTUNE</b>",
        f"  ╰┈➤  <code>{_fmt(coins)} {CURRENCY}</code>",
        f"",
        f"  {gender_emoji} <b>GENRE</b>  ┊  {marry_emoji} <i>{marriage_type.capitalize()}</i>",
        f"  ╰┈➤  <b>{gender_label}</b>  <i>(/setsexe · /setmariage)</i>",
        f"",
        f"  🏠 <b>FAMILLE</b>",
        f"  ╰┈➤  {fam_display}{fam_size_str}",
        f"",
        f"  🎓 <b>DIPLÔMES</b>",
        f"  ╰┈➤  {diplome_str}",
        f"{company_line}",
        f"",
        f"◈━━━━━━━━━━━━━━━━━━━━━━━━◈",
        f"",
        f"  {level['emoji']} <b>KARMA</b>  <code>{karma:+d}</code>  ┊  <i>{level['label']}</i>",
        f"  ╰┈➤ {bar}",
        f"       📈 <b>{karma_sign}</b> daily  ·  ⚡ <b>-{level['work_red']}%</b> /work",
        f"",
        f"◈━━━━━━━━━━━━━━━━━━━━━━━━◈",
    ]

    text = "\n".join(lines)
    if u and u.photo_file_id:
        file_type = getattr(u, 'photo_file_type', 'photo') or 'photo'
        try:
            if file_type == "sticker":
                await update.message.reply_sticker(u.photo_file_id)
                await update.message.reply_text(text, parse_mode=ParseMode.HTML)
            else:
                await update.message.reply_photo(u.photo_file_id, caption=text, parse_mode=ParseMode.HTML)
        except Exception:
            # file_id invalide → fallback texte + reset photo
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
            async with AsyncSessionLocal() as _s:
                await _s.execute(
                    __import__('sqlalchemy').text(
                        "UPDATE users SET photo_file_id = NULL WHERE user_id = :uid"
                    ),
                    {"uid": user.user_id}
                )
                await _s.commit()
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def setpic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Réponds à une photo ou un sticker pour définir ta photo de profil."""
    reply = update.message.reply_to_message
    if not reply:
        return await update.message.reply_text("❗ Réponds à une photo ou un sticker.")

    file_id = None
    file_type = None
    if reply.photo:
        file_id = reply.photo[-1].file_id
        file_type = "photo"
    elif reply.sticker:
        file_id = reply.sticker.file_id
        file_type = "sticker"
    else:
        return await update.message.reply_text("❗ Le message doit contenir une photo ou un sticker.")

    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.user_id)
        if u:
            u.photo_file_id = file_id
            if hasattr(u, 'photo_file_type'):
                u.photo_file_type = file_type
            await session.commit()

    await update.message.reply_text("✅ Photo de profil mise à jour !")


async def customize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Choisir la couleur du profil via des boutons."""
    color_emojis = {
        "blue": "🔵", "green": "🟢", "red": "🔴", "purple": "🟣",
        "orange": "🟠", "pink": "🩷", "gold": "🟡", "teal": "🩵",
    }
    buttons = [
        InlineKeyboardButton(f"{color_emojis[c]} {c.capitalize()}", callback_data=f"color:{c}")
        for c in PROFILE_COLORS
    ]
    keyboard = InlineKeyboardMarkup([buttons[i:i+4] for i in range(0, len(buttons), 4)])
    await update.message.reply_text("🎨 Choisis ta couleur de profil :", reply_markup=keyboard)


async def color_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    color = query.data.split(":")[1]
    if color not in PROFILE_COLORS:
        return

    user = await ensure_user(query.from_user)
    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.user_id)
        if u:
            u.profile_color = color
            await session.commit()

    await query.edit_message_text(f"✅ Couleur définie sur <b>{color}</b> !", parse_mode=ParseMode.HTML)


async def titles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Liste tous les titres dynastiques et leurs conditions."""
    lines = ["👑 <b>Titres dynastiques</b>\n"]
    for min_size, min_karma, title in TITLES:
        lines.append(
            f"{title}\n"
            f"  → Famille ≥ {min_size} membres  |  Karma ≥ {min_karma}\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def karmainfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Explique le système karma avec tous les niveaux."""
    from database.db import KARMA_LEVELS
    lines = [
        "⭐ <b>SYSTÈME KARMA</b>",
        "",
        "Le karma reflète ta réputation dans le jeu.",
        "Il monte grâce à tes bonnes actions, et chute si tu joues de façon criminelle.",
        "",
        "📈 <b>Ce qui augmente le karma :</b>",
        "  • Payer la caution de quelqu'un <code>/bail</code> → +2",
        "  • Faire un don généreux <code>/pay</code> (≥ 10 000 $) → +1",
        "  • Récolter ton jardin 5 fois → +1",
        "  • Gagner un combat d'arène (30% de chance) → +1",
        "",
        "📉 <b>Ce qui baisse le karma :</b>",
        "  • Voler quelqu'un <code>/rob</code> ou <code>/cambrioler</code> → -1",
        "  • Être condamné par <code>/juge</code> → -2",
        "  • Aller en prison → -1",
        "",
        "🏆 <b>Niveaux de karma :</b>",
        "",
    ]
    for threshold, emoji, label, daily_pct, work_red in KARMA_LEVELS:
        sign = f"+{daily_pct}%" if daily_pct >= 0 else f"{daily_pct}%"
        work_str = f" | ⏱ -{work_red}% cooldown" if work_red > 0 else ""
        lines.append(f"  {emoji} <b>{label}</b> (karma ≥ {threshold}) → Daily {sign}{work_str}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
