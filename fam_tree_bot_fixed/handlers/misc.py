from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database.db import AsyncSessionLocal, get_settings, get_leaderboard, compute_title, get_user
from utils.helpers import ensure_user, mention


HELP_TEXT = """
🌳 <b>FamTree Bot — Commandes</b>

<b>👨‍👩‍👧 Famille</b>
/marry — Demander quelqu'un en mariage
/adopt — Adopter un membre
/friend — Ajouter un ami
/divorce — Divorcer
/disown — Désavouer un enfant
/unfriend — Retirer un ami
/setfamilyname &lt;nom&gt; — Définir le nom de famille
/leave — Quitter (héritage transmis)

<b>🌳 Arbre</b>
/tree — Voir ton arbre généalogique (image)
/bigtree — Voir l'arbre du groupe

<b>🌱 Jardin</b>
/garden — Voir ton jardin
/plant &lt;slot&gt; &lt;plante&gt; — Planter
/harvest [slot] — Récolter

<b>✨ Waifu & Karma</b>
/waifu — Waifu du jour
/upvote — Donner +1 karma (répondre au message)
/downvote — Donner -1 karma (répondre au message)

<b>👤 Profil</b>
/me — Voir ton profil
/setpic — Définir ta photo (répondre à une photo)
/customize — Changer la couleur du profil
/titles — Liste des titres dynastiques

<b>📊 Général</b>
/leaderboard — Top familles
/familyphoto — Photo de famille composite
/mode — Changer le mode (global/groupe)
/toggle &lt;garden|waifu&gt; — Activer/désactiver une fonctionnalité
/help — Afficher cette aide
"""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update.effective_user)
    await update.message.reply_text(
        f"🌳 Bienvenue dans <b>FamTree Bot</b>, {update.effective_user.first_name} !\n\n"
        "Crée ta famille virtuelle, gère ton jardin, découvre ta waifu du jour et monte dans "
        "la hiérarchie dynastique.\n\n"
        "Tape /help pour voir toutes les commandes.",
        parse_mode=ParseMode.HTML,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncSessionLocal() as session:
        top = await get_leaderboard(session, 10)

    if not top:
        return await update.message.reply_text("📊 Aucune donnée disponible.")

    lines = ["🏆 <b>Classement — Plus grandes familles</b>\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, entry in enumerate(top):
        u     = entry["user"]
        size  = entry["size"]
        medal = medals[i] if i < 3 else f"{i+1}."
        async with AsyncSessionLocal() as session:
            title = await compute_title(session, u.user_id)
        lines.append(f"{medal} {u.first_name} — {size} membres  {title}  ⭐{u.karma}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Basculer entre mode global et mode groupe."""
    if update.effective_chat.type not in ("group", "supergroup"):
        return await update.message.reply_text("❗ Commande de groupe uniquement.")

    group_id = update.effective_chat.id
    async with AsyncSessionLocal() as session:
        s = await get_settings(session, group_id)
        s.mode = "group" if s.mode == "global" else "global"
        await session.commit()
        new_mode = s.mode

    await update.message.reply_text(
        f"🔄 Mode basculé sur <b>{new_mode}</b>.\n"
        f"{'Les relations sont maintenant partagées entre tous les groupes.' if new_mode == 'global' else 'Les relations sont maintenant spécifiques à ce groupe.'}",
        parse_mode=ParseMode.HTML,
    )


async def toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage : /toggle garden | waifu"""
    if update.effective_chat.type not in ("group", "supergroup"):
        return await update.message.reply_text("❗ Commande de groupe uniquement.")
    if not context.args:
        return await update.message.reply_text("Usage : /toggle <garden|waifu>")

    feature = context.args[0].lower()
    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        s = await get_settings(session, group_id)
        if feature == "garden":
            s.garden_enabled = not s.garden_enabled
            state = "activé" if s.garden_enabled else "désactivé"
        elif feature == "waifu":
            s.waifu_enabled = not s.waifu_enabled
            state = "activée" if s.waifu_enabled else "désactivée"
        else:
            return await update.message.reply_text("❗ Fonctionnalité inconnue. Choix : garden, waifu")
        await session.commit()

    await update.message.reply_text(f"✅ <b>{feature.capitalize()}</b> {state}.", parse_mode=ParseMode.HTML)
