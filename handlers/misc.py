from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database.db import AsyncSessionLocal, get_settings, get_leaderboard, compute_title, get_user
from utils.helpers import ensure_user, mention


HELP_TEXT = """
<b>FamTree Bot — Commandes</b>

<b>Famille</b>
/marry — Demander en mariage
/adopt — Adopter un membre
/friend — Ajouter un ami
/divorce — Divorcer
/disown — Desavouer un enfant
/unfriend — Retirer un ami
/setfamilyname nom — Nom de famille
/leave — Quitter (heritage transmis)

<b>Arbre</b>
/tree — Ton arbre genealogique
/bigtree — Arbre du groupe

<b>Jardin</b>
/garden — Voir ton jardin
/plant slot plante — Planter
/harvest [slot] — Recolter

<b>Profil</b>
/me — Ton profil
/setpic — Photo de profil
/customize — Couleur du profil
/titles — Liste des titres

<b>Economie</b>
/acc — Voir ton compte
/daily — Bonus quotidien
/work — Travailler (cooldown 8h)
/pay montant — Envoyer des $
/richlist — Top 10 des plus riches

<b>Casino</b>
/blackjack mise — Blackjack vs bot
/roulette mise choix — Roulette
/slots mise — Machine a sous
/bet mise description — Proposer un pari

<b>Loterie</b>
/createloto prix — Lancer une loterie privee (min 1 000 $)
/loto — Voir la loterie active du groupe
/ticket [nb] — Acheter des tickets
/tirage — Forcer le tirage (createur ou admin)
/tirageforce — Forcer le tirage bot (admin)
/cancelloto — Annuler la loterie et rembourser

<b>Banque</b>
/banks — Voir les banques disponibles
/bankopen — Ouvrir un compte bancaire
/bankdeposit montant — Deposer des coins
/bankwithdraw montant — Retirer des coins
/bankbalance — Voir son solde bancaire
/bankloan montant — Prendre un pret
/bankrepay montant — Rembourser un pret
/bankloans — Voir ses prets

<b>Investissements</b>
/market — Voir le marche
/buy actif quantite — Acheter des actions
/sell actif quantite — Vendre des actions
/portfolio — Voir son portefeuille

<b>Compte commun (couple)</b>
/couple create — Creer le compte commun
/couple balance — Voir le solde commun
/couple deposit montant — Deposer depuis ton compte perso
/couple withdraw montant — Retirer vers ton compte perso

<b>Evenements</b>
/open — Ouvrir un coffre mystere (si actif)
⭐ Heure Doree — gains casino x2 (annonce automatique)

<b>General</b>
/leaderboard — Top familles
/familyphoto — Photo de famille
/mode — Mode global/groupe
/toggle garden — Activer/desactiver le jardin
/help — Cette aide
"""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update.effective_user)
    await update.message.reply_text(
        f"Bienvenue dans <b>FamTree Bot</b>, {update.effective_user.first_name} !\n\n"
        "Cree ta famille virtuelle, gere ton jardin, joue au casino et batis ta dynastie !\n\n"
        "Tape /help pour voir toutes les commandes.",
        parse_mode=ParseMode.HTML,
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
