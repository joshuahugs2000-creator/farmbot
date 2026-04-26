from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database.db import AsyncSessionLocal, get_settings, get_leaderboard, compute_title, get_user
from utils.helpers import ensure_user, mention
from config import CURRENCY


HELP_TEXT = """
<b>🌳 FarmBot — Toutes les commandes</b>

<b>👨‍👩‍👧 Famille</b>
/marry — Demander en mariage
/adopt — Adopter un membre
/friend — Ajouter un ami
/divorce — Divorcer
/disown — Désavouer un enfant
/unfriend — Retirer un ami
/setfamilyname nom — Changer le nom de famille
/leave — Quitter (héritage transmis)
/familyphoto — Photo de famille

<b>🌲 Arbre</b>
/tree — Ton arbre généalogique

<b>🌱 Jardin</b>
/garden — Voir ton jardin
/plant slot plante — Planter
/harvest [slot] — Récolter

<b>👤 Profil</b>
/me — Ton profil complet
/setpic — Changer ta photo de profil
/customize — Couleur du profil
/titles — Liste des titres disponibles

<b>💰 Économie</b>
/acc — Voir ton compte
/daily — Bonus quotidien
/work — Travailler (cooldown 8h)
/pay @joueur montant — Envoyer des $
/richlist — Top 10 des plus riches

<b>🎲 Casino</b>
/blackjack mise — Blackjack contre le bot
/roulette mise choix — Roulette
/slots mise — Machine à sous

<b>🎮 Jeux</b>
/crash mise — Multiplicateur jusqu'au crash !
/apple mise — Apple of Fortune
/roue mise — Roue de Fortune
/rebet mise — Quitte ou double

<b>🥊 Arène PvP</b>
/cockfight mise — Combat de coqs
/ppc mise — Pierre-Papier-Ciseaux

<b>🏦 Banque</b>
/banks — Banques disponibles
/bankopen — Ouvrir un compte
/bankdeposit montant — Déposer
/bankwithdraw montant — Retirer
/bankbalance — Voir le solde bancaire
/bankloan montant — Prendre un prêt
/bankrepay montant — Rembourser un prêt
/bankloans — Voir tes prêts actifs

<b>📈 Investissements</b>
/market — Voir le marché (paginé)
/buy actif quantité — Acheter un actif
/sell id — Vendre une position
/portfolio — Voir ton portefeuille

<b>🎟 Loterie</b>
/createloto prix — Lancer une loterie
/loto — Voir la loterie active
/ticket [nb] — Acheter des tickets
/tirage — Lancer le tirage
/cancelloto — Annuler la loterie

<b>🏛️ Impôts</b>
/impots — Voir ton taux d'imposition

<b>💸 Braquage & Crime</b>
/cambrioler @joueur — Cambrioler un joueur
/braquage [mise] — Braquage collectif
/annulerbraquage — Annuler un braquage en cours
/rob @joueur — Voler un joueur
/police @joueur — Signaler un voleur
/bail @joueur — Payer la caution de quelqu'un
/juge @joueur — Porter plainte
/security — Activer la protection anti-vol

<b>🔨 Enchères</b>
/bid montant — Enchérir sur l'objet en cours
/expertise — Révéler la valeur de ton dernier objet
/myitems — Voir ton inventaire d'objets
/sellitem id prix — Mettre un objet en vente
/shopitems — Voir les objets en vente
/buyitem id — Acheter l'objet d'un autre joueur

<b>✨ Événements</b>
/open — Ouvrir un coffre mystère
⭐ Heure Dorée — gains casino x2 (aléatoire)

<b>📊 Général</b>
/leaderboard — Top des plus grandes familles
/mode — Basculer mode global/groupe
/toggle garden — Activer/désactiver le jardin
/help — Afficher cette aide
"""


START_PHOTO = "assets/start_banner.jpg"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update.effective_user)

    caption = (
        f"👋 <b>Bienvenue, {update.effective_user.first_name} !</b>\n\n"
        "💞 <b>FarmBot ❤️</b> — Construis ta famille virtuelle !\n\n"
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

    try:
        with open("assets/start_banner.jpg", "rb") as photo:
            await update.message.reply_photo(
                photo=photo,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
    except Exception:
        await update.message.reply_text(
            caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncSessionLocal() as session:
        top = await get_leaderboard(session, 10)
        if not top:
            return await update.message.reply_text("Aucune donnée disponible.")

        lines  = ["<b>🏆 Classement — Plus grandes familles</b>\n"]
        medals = ["🥇", "🥈", "🥉"]
        for i, entry in enumerate(top):
            u     = entry["user"]
            size  = entry["size"]
            medal = medals[i] if i < 3 else f"{i+1}."
            title = await compute_title(session, u.user_id)
            lines.append(f"{medal} {u.first_name} — {size} membres  {title}  Karma: {u.karma}")

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
            state = "activé ✅" if s.garden_enabled else "désactivé ❌"
        else:
            return await update.message.reply_text("Fonctionnalité inconnue. Seul 'garden' est disponible.")
        await session.commit()

    await update.message.reply_text(f"{feature.capitalize()} : {state}.")
