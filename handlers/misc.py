from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database.db import AsyncSessionLocal, get_settings, get_leaderboard, compute_title, get_user
from utils.helpers import ensure_user, mention


HELP_TEXT = """
<b>🌳 Family Bot — Commandes</b>

<b>👨‍👩‍👧 Famille</b>
/marry — Demander en mariage
/adopt — Adopter un membre
/friend — Ajouter un ami
/divorce — Divorcer
/disown — Desavouer un enfant
/unfriend — Retirer un ami
/setfamilyname nom — Nom de famille
/leave — Quitter (heritage transmis)

<b>🌲 Arbre</b>
/tree — Ton arbre genealogique

<b>🌱 Jardin</b>
/garden — Voir ton jardin
/plant slot plante — Planter
/harvest [slot] — Recolter

<b>👤 Profil</b>
/me — Ton profil
/setpic — Photo de profil
/customize — Couleur du profil
/titles — Liste des titres

<b>💰 Economie</b>
/acc — Voir ton compte
/daily — Bonus quotidien
/work — Travailler (cooldown 8h)
/pay montant — Envoyer des $
/richlist — Top 10 des plus riches

<b>🎲 Casino</b>
/blackjack mise — Blackjack vs bot
/roulette mise choix — Roulette
/slots mise — Machine a sous

<b>🎮 Jeux</b>
/crash mise — Multiplicateur jusqu'au crash !
/apple mise — Apple of Fortune (50K - 5M)
/roue mise — Roue de Fortune
/rebet mise — Quitte ou double

<b>💸 Braquage</b>
/cambrioler @joueur — Voler un joueur
/braquage [mise] — Braquage collectif (2-6 joueurs)

<b>🎟 Loterie</b>
/createloto prix — Lancer une loterie (min 1 000 $)
/loto — Voir la loterie active
/ticket [nb] — Acheter des tickets
/tirage — Forcer le tirage
/tirageforce — Forcer le tirage (admin)
/cancelloto — Annuler la loterie

<b>🏦 Banque</b>
/banks — Banques disponibles
/bankopen — Ouvrir un compte
/bankdeposit montant — Deposer
/bankwithdraw montant — Retirer
/bankbalance — Solde bancaire
/bankloan montant — Prendre un pret
/bankrepay montant — Rembourser
/bankloans — Prets actifs

<b>📈 Investissements</b>
/market — Voir le marche
/buy actif quantite — Acheter
/sell actif quantite — Vendre
/portfolio — Portefeuille

<b>💰 Impots</b>
/impots — Ton taux d'imposition

<b>⛓️ Criminalite</b>
/rob @pseudo — Voler un joueur
/police @pseudo — Signaler un voleur
/bail — Payer sa caution
/juge @pseudo — Porter plainte
/security — Protection contre les vols

<b>✨ Evenements</b>
/open — Ouvrir un coffre mystere
⭐ Heure Doree — gains casino x2

<b>📊 General</b>
/leaderboard — Top familles
/familyphoto — Photo de famille
/mode — Mode global/groupe
/toggle garden — Activer/desactiver le jardin
/help — Cette aide
"""


START_PHOTO = "https://i.imgur.com/placeholder.jpg"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update.effective_user)

    caption = (
        f"👋 <b>Bienvenue, {update.effective_user.first_name} !</b>\n\n"
        "💞 <b>Your Family Bot ❤️</b> — Construis ta famille virtuelle !\n\n"
        "👨‍👩‍👧 Marie-toi, adopte, crée ton arbre généalogique.\n"
        "🌱 Gère ton jardin et récolte tes plantes.\n"
        "🎲 Joue au casino et enrichis ta dynastie.\n"
        "🏆 Gravis les classements et bâtis ton empire !\n\n"
        "Tape /help pour voir toutes les commandes."
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Ajouter au groupe", url=f"https://t.me/{context.bot.username}?startgroup=start")],
        [
            InlineKeyboardButton("📖 Guide rapide", url="https://telegra.ph/FarmBot-Guide-des-commandes"),
            InlineKeyboardButton("📢 Canal officiel", url="https://t.me/familybot_channel"),
        ],
        [InlineKeyboardButton("🛠 Contacter le dev", url="https://t.me/yoshider")],
    ])

    with open("assets/start_banner.jpg", "rb") as photo:
        await update.message.reply_photo(
            photo=photo,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncSessionLocal() as session:
        top = await get_leaderboard(session, 10)
        if not top:
            return await update.message.reply_text("Aucune donnee disponible.")

        lines  = ["<b>Classement — Plus grandes familles</b>\n"]
        medals = ["1.", "2.", "3."]
        for i, entry in enumerate(top):
            u     = entry["user"]
            size  = entry["size"]
            medal = medals[i] if i < 3 else f"{i+1}."
            title = await compute_title(session, u.user_id)
            lines.append(f"{medal} {u.first_name} — {size} membres  {title}  Karma:{u.karma}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        return await update.message.reply_text("Commande de groupe uniquement.")
    group_id = update.effective_chat.id
    async with AsyncSessionLocal() as session:
        s      = await get_settings(session, group_id)
        s.mode = "group" if s.mode == "global" else "global"
        await session.commit()
        new_mode = s.mode
    await update.message.reply_text(f"Mode : <b>{new_mode}</b>", parse_mode=ParseMode.HTML)


async def toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        return await update.message.reply_text("Commande de groupe uniquement.")
    if not context.args:
        return await update.message.reply_text("Usage : /toggle garden")

    feature  = context.args[0].lower()
    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        s = await get_settings(session, group_id)
        if feature == "garden":
            s.garden_enabled = not s.garden_enabled
            state = "active" if s.garden_enabled else "desactive"
        else:
            return await update.message.reply_text("Fonctionnalite inconnue. Seul 'garden' est disponible.")
        await session.commit()

    await update.message.reply_text(f"{feature.capitalize()} : {state}.")
