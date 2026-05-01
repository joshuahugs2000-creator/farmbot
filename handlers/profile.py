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
    DIPLOME_EMOJIS = {"bac": "📄", "licence": "🎓", "master": "🏅", "mba": "👑"}
    DOMAIN_EMOJIS  = {"finance": "📈", "informatique": "💻", "marketing": "📣",
                      "droit": "⚖️", "management": "🏢", "agriculture": "🌾", "securite": "🛡️"}
    diplomes_obtenus = [e for lvl, e in DIPLOME_EMOJIS.items() if getattr(u, f"diplome_{lvl}", False)]
    diplome_domain   = getattr(u, "diplome_domain", None)
    domain_str       = ""
    if diplome_domain:
        d_em = DOMAIN_EMOJIS.get(diplome_domain, "🎓")
        domain_str = f"  ·  {d_em} {diplome_domain.capitalize()}"
    diplome_line = f"🎓 <b>Diplômes</b> : {'  '.join(diplomes_obtenus) + domain_str if diplomes_obtenus else '—  (/diplome pour passer un examen)'}"

    lines = [
        f"╔══════════════════════════╗",
        f"  👤 <b>{update.effective_user.first_name}</b>{rank_badge}",
        f"  🏅 {title}",
        f"╚══════════════════════════╝",
        f"",
        f"🏠 <b>Famille</b>  : {fam_name or '— Sans famille'} ({size} membre(s))",
        f"📅 <b>Inscrit</b>  : {joined}",
        f"{color_dot} <b>Couleur</b>  : {color.capitalize()}",
        f"{diplome_line}",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"⭐ <b>KARMA</b>  : {karma:+d}  {level['emoji']} <i>{level['label']}</i>",
        f"   {bar}",
        f"   📈 Daily bonus : <b>{karma_sign}</b>  |  ⏱ Cooldown /work réduit de <b>{level['work_red']}%</b>",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"",
        f"💰 <b>Solde</b>   : {_fmt(coins)} {CURRENCY}",
    ]

    text = "\n".join(lines)
    if u and u.photo_file_id:
        await update.message.reply_photo(u.photo_file_id, caption=text, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def setpic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Réponds à une photo ou un sticker pour définir ta photo de profil."""
    reply = update.message.reply_to_message
    if not reply:
        return await update.message.reply_text("❗ Réponds à une photo ou un sticker.")

    file_id = None
    if reply.photo:
        file_id = reply.photo[-1].file_id
    elif reply.sticker:
        file_id = reply.sticker.file_id
    else:
        return await update.message.reply_text("❗ Le message doit contenir une photo ou un sticker.")

    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.user_id)
        if u:
            u.photo_file_id = file_id
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
