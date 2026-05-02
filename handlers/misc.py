from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database.db import AsyncSessionLocal, get_settings, get_leaderboard, compute_title, get_user
from utils.helpers import ensure_user, mention
from config import CURRENCY


# ─── TEXTE D'AIDE COMPLET (utilisateurs) ─────────────────────────────────────

HELP_TEXT = """
<b>🌳 Your Family ❤️ — Toutes les commandes</b>

<b>👨‍👩‍👧 Famille</b>
/marry — Demander en mariage (60s pour répondre)
/adopt — Adopter un membre
/friend — Ajouter un ami
/divorce — Divorcer
/disown — Désavouer un enfant
/unfriend — Retirer un ami
/setfamilyname nom — Changer le nom de famille
/leave — Quitter la famille
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
/karmainfo — Infos sur le karma

<b>💰 Économie</b>
/acc — Voir ton compte
/daily — Bonus quotidien (5K–20K)
/work — Travailler (3K–30K, cooldown 8h)
/pay @joueur montant — Envoyer des $
/richlist — Top 10 des plus riches
/impots — Voir ton taux d'imposition

<b>🎓 Diplômes</b>
/diplome — Passer un diplôme (Bac, Licence, Master, MBA)

<b>🏢 Entreprises</b>
/listeboites — Voir toutes les entreprises
/infoboite nom — Détails d'une entreprise
/postuler nom — Postuler dans une entreprise
/rejoindre nom — Accepter une invitation
/demissionner — Quitter son entreprise
/monentreprise — Ta fiche employé & salaire
/salaireinfo — Voir ton salaire détaillé
/candidatures — Voir les candidatures (PDG/Dir.)
/accepter id — Accepter une candidature
/refuser id — Refuser une candidature
/recruter @pseudo [poste] — Inviter quelqu'un
/nommer @pseudo poste — Promouvoir un employé
/licencier @pseudo — Licencier un employé
/creerboite nom secteur — Créer ton entreprise (50M$)
/dissoudreboite — Dissoudre son entreprise
/depotboite montant — Déposer en trésorerie
/retraitboite montant — Retirer de la trésorerie
/logsboite — Historique de l'entreprise
/parts [nom] — Voir la répartition des parts
/acheterparts nb nom — Acheter des parts
/vendreparts nb prix — Vendre des parts

<b>🎮 Jeux</b>
/crash mise — Multiplicateur jusqu'au crash !
/apple mise — Apple of Fortune (max 10M)
/roue mise — Roue de Fortune (gain max 100M)
/rebet mise — Quitte ou double
/mines nb_mines mise — Démine la grille
/blackjack mise — Blackjack
/roulette mise — Roulette
/slots mise — Machine à sous

<b>🥊 Arène PvP</b>
/cockfight mise — Combat de coqs
/ppc @joueur mise — Pierre-Papier-Ciseaux
/lancer mise — Duel de dés vs bot
/lancer @joueur mise — Duel de dés PvP

<b>🏦 Banque</b>
/banks — Banques disponibles
/bankopen — Ouvrir un compte
/bankdeposit montant — Déposer (plafond 2B)
/bankwithdraw montant — Retirer
/bankbalance — Voir le solde bancaire
/bankloan montant — Prendre un prêt (max 5M)
/bankrepay montant — Rembourser un prêt
/bankloans — Voir tes prêts actifs

<b>📈 Investissements</b>
/market — Voir le marché
/buy actif quantité — Acheter un actif
/sell id — Vendre une position
/portfolio — Voir ton portefeuille

<b>💸 Crime & Braquage</b>
/cambrioler @joueur — Cambrioler un joueur
/police @joueur — Signaler un voleur
/bail @joueur — Payer la caution
/juge @joueur — Porter plainte
/security — Protection anti-vol

<b>🔨 Enchères</b>
/bid montant — Enchérir
/expertise — Valeur de ton dernier objet
/myitems — Ton inventaire
/sellitem id prix — Mettre en vente
/shopitems — Objets en vente
/buyitem id — Acheter un objet

<b>✨ Événements</b>
/open — Ouvrir un coffre mystère

<b>📊 Général</b>
/leaderboard — Top des plus grandes familles
/mode — Mode global/groupe
/toggle garden — Activer/désactiver le jardin
/help — Afficher cette aide
/nouveautes — Voir les dernières mises à jour
"""


# ─── TEXTE MISE À JOUR (/nouveautes) ─────────────────────────────────────────

NOUVEAUTES_TEXT = (
    "🆕 <b>Your Family ❤️ — Nouvelle maj !</b>\n\n"
    "💥 Deux gros trucs viennent d'arriver dans le jeu :\n\n"
    "🎓 <b>Les Diplômes</b> — Étudie, passe tes exams, monte en grade !\n"
    "🏢 <b>Les Entreprises</b> — Crée ta boîte ou rejoint une équipe pour te faire du blé chaque jour 💰\n\n"
    "Plus t'es diplômé, plus t'as accès à des postes relous bien payés 😈\n\n"
    "Clique en dessous pour tout comprendre 👇"
)

DIPLOME_DETAIL = (
    "🎓 <b>Les Diplômes — Comment ça marche ?</b>\n\n"
    "C'est simple : tu passes des examens pour monter de niveau. Plus t'es calé, mieux tu gagnes.\n\n"
    "⚡ <b>Les étapes :</b>\n"
    "1️⃣ Lance <code>/diplome</code> pour démarrer un exam\n"
    "2️⃣ Réponds vite, t'as un temps limité ⏱️\n"
    "3️⃣ Réussis → diplôme validé 🎉 Rate → retente !\n\n"
    "📊 <b>Les 4 niveaux :</b>\n"
    "📄 <b>Bac</b> — Le début, tout le monde peut l'avoir\n"
    "🎓 <b>Licence</b> — Accès aux postes Manager + tu choisis ton secteur\n"
    "🏅 <b>Master</b> — Directeur + entreprises de prestige\n"
    "👑 <b>MBA</b> — Le graal. Crée les plus grosses boîtes du jeu\n\n"
    "💡 <b>Pourquoi c'est important :</b>\n"
    "• Meilleur diplôme = meilleur poste = plus d'argent par jour 💸\n"
    "• Ta Licence détermine dans quel secteur tu peux créer ta boîte\n"
    "• Certaines entreprises refusent les gens sans Master/MBA\n\n"
    "🚀 Lance toi : <code>/diplome</code>"
)

ENTREPRISE_DETAIL = (
    "🏢 <b>Les Entreprises — Fais ta fortune !</b>\n\n"
    "Tu veux des revenus automatiques chaque jour ? C'est par ici 👇\n\n"
    "🤖 <b>Option 1 — Rejoindre une boîte du jeu</b>\n"
    "NexaTech, CapitalX, TradeHub... postulez et attendez d'être recruté !\n"
    "→ <code>/postuler NexaTech</code>\n"
    "⚠️ Le recrutement dépend de ton diplôme — sois préparé !\n\n"
    "💼 <b>Option 2 — Créer ta propre boîte</b>\n"
    "Coût : <b>50 000 000 $</b> + avoir une Licence minimum\n"
    "→ <code>/creerboite MonEntreprise tech</code>\n"
    "Secteurs dispo : tech | finance | commerce | droit | agriculture | securite | immobilier | sante\n\n"
    "💰 <b>Combien tu gagnes selon ton poste ?</b>\n"
    "👷 Stagiaire → 0$ (t'es là pour apprendre, pas te la couler 😂)\n"
    "👷 Employé → 10% des revenus journaliers\n"
    "💼 Manager → 20%\n"
    "🏦 Directeur → 35%\n"
    "👑 PDG → dividendes perso via <code>/retraitboite</code> 🤑\n\n"
    "📈 <b>Fais grandir ta boîte :</b>\n"
    "🏪 Startup → 🏢 PME → 🏬 Société → 🏦 Corporation → 👑 Holding (10 milliards !)\n\n"
    "🕹️ <b>Commandes utiles :</b>\n"
    "<code>/monentreprise</code> — Ta fiche perso\n"
    "<code>/salaireinfo</code> — Combien t'as gagné\n"
    "<code>/listeboites</code> — Toutes les boîtes dispo\n"
    "<code>/parts</code> — Qui possède quoi\n"
    "<code>/acheterparts nb nom</code> — OPA ! Rachète une boîte 😈"
)


# ─── CLAVIER MISE À JOUR ──────────────────────────────────────────────────────

def _nouveautes_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎓 En savoir plus : Diplômes", callback_data="info:diplomes"),
            InlineKeyboardButton("🏢 En savoir plus : Entreprises", callback_data="info:entreprises"),
        ]
    ])


START_PHOTO = "assets/start_banner.jpg"


# ─── COMMANDES ────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update.effective_user)

    caption = (
        f"👋 <b>Bienvenue, {update.effective_user.first_name} !</b>\n\n"
        "💞 <b>Your Family ❤️</b> — Construis ta famille virtuelle !\n\n"
        "👨‍👩‍👧 Marie-toi, adopte, crée ton arbre généalogique.\n"
        "🌱 Gère ton jardin et récolte tes plantes.\n"
        "🎲 Joue au casino et enrichis ta dynastie.\n"
        "🎓 Passe tes diplômes et bâtis ton empire entrepreneurial !\n\n"
        "Tape /help pour voir toutes les commandes.\n"
        "Tape /nouveautes pour les dernières nouveautés."
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


async def nouveautes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from handlers.admin import is_admin
    from database.models import User, GroupSettings
    from sqlalchemy import select

    if not await is_admin(update.effective_user.id):
        await update.message.reply_text(
            NOUVEAUTES_TEXT,
            parse_mode=ParseMode.HTML,
            reply_markup=_nouveautes_keyboard(),
        )
        return

    await update.message.reply_text("Envoi en cours a tous les membres et groupes...")

    sent_users, failed_users = 0, 0
    sent_groups, failed_groups = 0, 0

    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.is_banned == False))
        user_rows = res.scalars().all()
        res2 = await session.execute(select(GroupSettings))
        group_rows = res2.scalars().all()

    for u in user_rows:
        try:
            await context.bot.send_message(
                chat_id=u.user_id,
                text=NOUVEAUTES_TEXT,
                parse_mode=ParseMode.HTML,
                reply_markup=_nouveautes_keyboard(),
            )
            sent_users += 1
        except Exception:
            failed_users += 1

    for g in group_rows:
        try:
            await context.bot.send_message(
                chat_id=g.group_id,
                text=NOUVEAUTES_TEXT,
                parse_mode=ParseMode.HTML,
                reply_markup=_nouveautes_keyboard(),
            )
            sent_groups += 1
        except Exception:
            failed_groups += 1

    rapport = "Broadcast termine ! | Users -> " + str(sent_users) + " OK " + str(failed_users) + " echecs | Groupes -> " + str(sent_groups) + " OK " + str(failed_groups) + " echecs | Total : " + str(sent_users + sent_groups)
    await update.message.reply_text(rapport, parse_mode=ParseMode.HTML)


async def nouveautes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    if data == "info:diplomes":
        popup = "DIPLOMES\n\nLance /diplome pour passer un exam!\n\nBac - Le debut\nLicence - Manager + secteur\nMaster - Directeur\nMBA - Le graal\n\nMeilleur diplome = meilleur salaire"
    elif data == "info:entreprises":
        popup = "ENTREPRISES\n\nOption 1 - /postuler NexaTech\nOption 2 - /creerboite MonEntreprise tech (50M + Licence)\n\nSalaires:\nStagiaire 0% | Employe 10% | Manager 20% | Directeur 35% | PDG dividendes\n\nStartup -> PME -> Societe -> Corporation -> Holding"
    else:
        popup = "Section inconnue."
    await query.answer(text=popup, show_alert=True)


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
