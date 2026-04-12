"""
Système économique complet :
/acc         — voir son compte
/daily       — bonus quotidien
/work        — travailler (cooldown 8h)
/pay         — transférer des $
/richlist    — classement des plus riches
/blackjack   — blackjack vs bot
/roulette    — roulette
/slots       — machine à sous
/race        — courses de chevaux
/bet         — proposer un pari à un autre user
/acceptbet   — accepter un pari
/resolvebet  — désigner le gagnant d'un pari
"""
import random
import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database.db import (
    AsyncSessionLocal, get_user, get_richlist,
    add_coins, transfer_coins, claim_daily, claim_work,
    create_bet, accept_bet, resolve_bet,
)
from utils.helpers import ensure_user, parse_target, mention

logger = logging.getLogger(__name__)


def _fmt(n: int) -> str:
    """Formate un nombre avec des espaces : 1 234 567"""
    return f"{n:,}".replace(",", " ")


# ─── /acc ─────────────────────────────────────────────────────────────────────

async def acc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.user_id)
        if not u:
            return await update.message.reply_text("Compte introuvable. Fais /start d'abord.")
        coins = u.coins

    await update.message.reply_text(
        f"💳 Compte de {update.effective_user.first_name}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"💰 Solde : {_fmt(coins)} $\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"📥 /daily  |  🔨 /work  |  💸 /pay"
    )


# ─── /daily ───────────────────────────────────────────────────────────────────

async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        result = await claim_daily(session, user.user_id)

    if result["status"] == "already":
        await update.message.reply_text("Tu as deja pris ton bonus aujourd'hui. Reviens demain !")
    elif result["status"] == "ok":
        await update.message.reply_text(
            f"🎁 Bonus quotidien recu !\n"
            f"💰 +{_fmt(result['amount'])} $\n"
            f"Solde : {_fmt(result['balance'])} $"
        )
    else:
        await update.message.reply_text("Erreur. Fais /start d'abord.")


# ─── /work ────────────────────────────────────────────────────────────────────

JOBS = [
    ("👨‍🌾 Tu as travaille aux champs", 10_000, 80_000),
    ("🚗 Tu as conduit un taxi", 15_000, 90_000),
    ("👨‍💻 Tu as code toute la nuit", 20_000, 150_000),
    ("🏗️ Tu as construit des maisons", 12_000, 100_000),
    ("🎤 Tu as anime une soiree", 25_000, 120_000),
    ("🍳 Tu as cuisine au restaurant", 10_000, 70_000),
    ("📦 Tu as livre des colis", 8_000, 60_000),
    ("🎨 Tu as vendu tes tableaux", 30_000, 200_000),
]


async def work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        result = await claim_work(session, user.user_id)

    if result["status"] == "cooldown":
        h = result["wait_min"] // 60
        m = result["wait_min"] % 60
        await update.message.reply_text(f"Tu es fatigue(e) ! Reviens dans {h}h{m:02d}m.")
    elif result["status"] == "ok":
        job_desc, _, _ = random.choice(JOBS)
        await update.message.reply_text(
            f"{job_desc}\n"
            f"💰 +{_fmt(result['amount'])} $\n"
            f"Solde : {_fmt(result['balance'])} $"
        )
    else:
        await update.message.reply_text("Erreur. Fais /start d'abord.")


# ─── /pay ─────────────────────────────────────────────────────────────────────

async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_tg = await parse_target(update, context)
    if not target_tg or not context.args:
        return await update.message.reply_text("Usage : /pay @pseudo montant\nEx : /pay @Mark 50000")

    # Montant = dernier argument
    try:
        amount = int(context.args[-1].replace(" ", "").replace(",", ""))
        assert amount > 0
    except (ValueError, AssertionError):
        return await update.message.reply_text("Montant invalide. Ex : /pay @Mark 50000")

    if target_tg.id == update.effective_user.id:
        return await update.message.reply_text("Tu ne peux pas te payer toi-meme !")

    sender = await ensure_user(update.effective_user)
    target = await ensure_user(target_tg)

    async with AsyncSessionLocal() as session:
        result = await transfer_coins(session, sender.user_id, target.user_id, amount)

    if result == "insufficient":
        await update.message.reply_text(f"Solde insuffisant ! Il te faut {_fmt(amount)} $.")
    elif result == "not_found":
        await update.message.reply_text("Utilisateur introuvable.")
    else:
        await update.message.reply_text(
            f"💸 {mention(sender)} a envoye {_fmt(amount)} $ a {mention(target)} !",
            parse_mode=ParseMode.HTML,
        )


# ─── /richlist ────────────────────────────────────────────────────────────────

async def richlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncSessionLocal() as session:
        top = await get_richlist(session, 10)

    medals = ["🥇", "🥈", "🥉"]
    lines  = ["💰 Classement des plus riches\n"]
    for i, u in enumerate(top):
        medal = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} {u.first_name} — {_fmt(u.coins)} $")

    await update.message.reply_text("\n".join(lines))


# ─── /blackjack ───────────────────────────────────────────────────────────────

def _card():
    vals = ["2","3","4","5","6","7","8","9","10","V","D","R","As"]
    suits = ["♠","♥","♦","♣"]
    return random.choice(vals), random.choice(suits)


def _hand_value(hand):
    total, aces = 0, 0
    for val, _ in hand:
        if val in ("V","D","R"):
            total += 10
        elif val == "As":
            total += 11
            aces  += 1
        else:
            total += int(val)
    while total > 21 and aces:
        total -= 10
        aces  -= 1
    return total


def _show_hand(hand, hide_second=False):
    if hide_second and len(hand) > 1:
        return f"{hand[0][0]}{hand[0][1]}  [?]"
    return "  ".join(f"{v}{s}" for v, s in hand)


async def blackjack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage : /blackjack [mise]\nEx : /blackjack 10000")
    try:
        mise = int(context.args[0].replace(" ","").replace(",",""))
        assert mise >= 1000
    except (ValueError, AssertionError):
        return await update.message.reply_text("Mise minimum : 1 000 $.")

    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.user_id)
        if not u or u.coins < mise:
            return await update.message.reply_text("Solde insuffisant !")

    player = [_card(), _card()]
    dealer = [_card(), _card()]
    pv     = _hand_value(player)
    dv     = _hand_value(dealer)

    # Bot tire jusqu'a 17
    while dv < 17:
        dealer.append(_card())
        dv = _hand_value(dealer)

    pv = _hand_value(player)

    if pv > 21:
        result = "lose"
    elif dv > 21 or pv > dv:
        result = "win"
    elif pv == dv:
        result = "push"
    else:
        result = "lose"

    async with AsyncSessionLocal() as session:
        if result == "win":
            gain = mise
            await add_coins(session, user.user_id, mise)
            msg  = f"✅ Tu gagnes {_fmt(mise)} $ !"
        elif result == "push":
            gain = 0
            msg  = "Egalite — mise remboursee."
        else:
            gain = -mise
            await add_coins(session, user.user_id, -mise)
            msg  = f"❌ Tu perds {_fmt(mise)} $."

        u2 = await get_user(session, user.user_id)
        new_bal = u2.coins if u2 else 0

    await update.message.reply_text(
        f"🃏 BLACKJACK\n"
        f"Toi    : {_show_hand(player)} = {pv}\n"
        f"Dealer : {_show_hand(dealer)} = {dv}\n\n"
        f"{msg}\n"
        f"Solde : {_fmt(new_bal)} $"
    )


# ─── /roulette ────────────────────────────────────────────────────────────────

REDS   = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
BLACKS = {2,4,6,8,10,11,13,15,17,20,22,24,26,28,29,31,33,35}


async def roulette(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        return await update.message.reply_text(
            "Usage : /roulette [mise] [choix]\n"
            "Choix : rouge, noir, pair, impair, 0-36\n"
            "Ex : /roulette 5000 rouge"
        )
    try:
        mise = int(context.args[0].replace(",",""))
        assert mise >= 1000
    except (ValueError, AssertionError):
        return await update.message.reply_text("Mise minimum 1 000 $.")

    choix = context.args[1].lower()
    user  = await ensure_user(update.effective_user)

    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.user_id)
        if not u or u.coins < mise:
            return await update.message.reply_text("Solde insuffisant !")

    numero = random.randint(0, 36)
    couleur = "rouge" if numero in REDS else ("noir" if numero in BLACKS else "vert")

    # Calcul gain
    if choix in ("rouge","noir"):
        win = (choix == couleur)
        mult = 2
    elif choix in ("pair","impair"):
        win = numero != 0 and ((numero % 2 == 0) == (choix == "pair"))
        mult = 2
    elif choix.isdigit() and 0 <= int(choix) <= 36:
        win  = (numero == int(choix))
        mult = 36
    else:
        return await update.message.reply_text("Choix invalide. Tape /roulette pour l'aide.")

    async with AsyncSessionLocal() as session:
        if win:
            gain = mise * (mult - 1)
            await add_coins(session, user.user_id, gain)
            msg  = f"✅ Gagne ! +{_fmt(gain)} $"
        else:
            await add_coins(session, user.user_id, -mise)
            msg  = f"❌ Perdu. -{_fmt(mise)} $"
        u2      = await get_user(session, user.user_id)
        new_bal = u2.coins if u2 else 0

    color_emoji = {"rouge":"🔴","noir":"⚫","vert":"🟢"}.get(couleur,"⚪")
    await update.message.reply_text(
        f"🎡 ROULETTE\n"
        f"Numero : {numero} {color_emoji}\n"
        f"Ton pari : {choix} ({_fmt(mise)} $)\n\n"
        f"{msg}\n"
        f"Solde : {_fmt(new_bal)} $"
    )


# ─── /slots ───────────────────────────────────────────────────────────────────

SYMBOLS = ["🍒","🍋","🍊","🍇","⭐","💎","7️⃣"]
WEIGHTS = [30,  25,  20,  15,   6,   3,   1]


async def slots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage : /slots [mise]\nEx : /slots 5000")
    try:
        mise = int(context.args[0].replace(",",""))
        assert mise >= 1000
    except (ValueError, AssertionError):
        return await update.message.reply_text("Mise minimum 1 000 $.")

    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.user_id)
        if not u or u.coins < mise:
            return await update.message.reply_text("Solde insuffisant !")

    reels = random.choices(SYMBOLS, weights=WEIGHTS, k=3)

    if reels[0] == reels[1] == reels[2]:
        s = reels[0]
        mult = {"💎": 50, "7️⃣": 30, "⭐": 15, "🍇": 8, "🍊": 5, "🍋": 3, "🍒": 2}.get(s, 2)
        gain = mise * mult
        msg  = f"🎰 JACKPOT ! x{mult} — +{_fmt(gain)} $ !"
        delta = gain
    elif reels[0] == reels[1] or reels[1] == reels[2]:
        gain = mise // 2
        msg  = f"Deux identiques ! +{_fmt(gain)} $."
        delta = gain
    else:
        msg  = f"Rien. -{_fmt(mise)} $."
        delta = -mise

    async with AsyncSessionLocal() as session:
        await add_coins(session, user.user_id, delta)
        u2      = await get_user(session, user.user_id)
        new_bal = u2.coins if u2 else 0

    await update.message.reply_text(
        f"🎰 SLOTS\n"
        f"[ {reels[0]} | {reels[1]} | {reels[2]} ]\n\n"
        f"{msg}\n"
        f"Solde : {_fmt(new_bal)} $"
    )


# ─── /race ────────────────────────────────────────────────────────────────────

HORSES = [
    ("⚡ Éclair",   1.5),
    ("🌪️ Tempete",  2.2),
    ("🔥 Inferno",  3.5),
    ("🌙 Minuit",   5.0),
    ("💨 Fantome",  8.0),
]


async def race(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        lines = ["🏇 COURSES DE CHEVAUX\n", "Chevaux disponibles :"]
        for i, (name, cote) in enumerate(HORSES, 1):
            lines.append(f"  {i}. {name}  (cote x{cote})")
        lines.append("\nUsage : /race [mise] [numero 1-5]")
        return await update.message.reply_text("\n".join(lines))

    try:
        mise    = int(context.args[0].replace(",",""))
        choix   = int(context.args[1]) - 1
        assert mise >= 1000
        assert 0 <= choix < len(HORSES)
    except (ValueError, AssertionError):
        return await update.message.reply_text("Usage : /race [mise] [1-5]")

    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.user_id)
        if not u or u.coins < mise:
            return await update.message.reply_text("Solde insuffisant !")

    # Tirage pondéré inversement aux cotes (plus la cote est basse, plus le cheval gagne souvent)
    weights = [1 / h[1] for h in HORSES]
    winner  = random.choices(range(len(HORSES)), weights=weights, k=1)[0]

    # Animation textuelle de la course
    positions = list(range(len(HORSES)))
    random.shuffle(positions)
    race_lines = []
    for pos, idx in enumerate(positions):
        bar   = "▓" * (len(HORSES) - pos) + "░" * pos
        race_lines.append(f"{HORSES[idx][0]}  {bar}")

    async with AsyncSessionLocal() as session:
        if winner == choix:
            cote  = HORSES[choix][1]
            gain  = int(mise * cote)
            delta = gain - mise
            await add_coins(session, user.user_id, delta)
            msg = f"✅ {HORSES[choix][0]} gagne ! +{_fmt(delta)} $ (x{cote})"
        else:
            await add_coins(session, user.user_id, -mise)
            msg = f"❌ {HORSES[winner][0]} gagne. Tu perds {_fmt(mise)} $."
        u2      = await get_user(session, user.user_id)
        new_bal = u2.coins if u2 else 0

    await update.message.reply_text(
        f"🏇 COURSE !\n\n"
        + "\n".join(race_lines) +
        f"\n\n{msg}\n"
        f"Solde : {_fmt(new_bal)} $"
    )


# ─── /bet ─────────────────────────────────────────────────────────────────────

async def bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage : /bet [montant] [description du pari]"""
    if not is_group(update):
        return await update.message.reply_text("Commande de groupe uniquement.")
    if not context.args or len(context.args) < 2:
        return await update.message.reply_text(
            "Usage : /bet [montant] [description]\n"
            "Ex : /bet 100000 Qui marquera le premier but ?"
        )
    try:
        amount = int(context.args[0].replace(",",""))
        assert amount >= 1000
    except (ValueError, AssertionError):
        return await update.message.reply_text("Mise minimum 1 000 $.")

    description = " ".join(context.args[1:])
    user        = await ensure_user(update.effective_user)
    group_id    = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        b = await create_bet(session, user.user_id, group_id, amount, description)
        if not b:
            return await update.message.reply_text("Solde insuffisant pour proposer ce pari !")

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"🤝 Accepter ({_fmt(amount)} $)", callback_data=f"bet:accept:{b.id}"),
        ]])
        await update.message.reply_text(
            f"🎲 PARI OUVERT !\n"
            f"Proposeur : {mention(user)}\n"
            f"Mise : {_fmt(amount)} $ chacun\n"
            f"Question : {description}\n\n"
            f"ID du pari : #{b.id}\n"
            f"Utilise le bouton pour accepter, ou /acceptbet {b.id}",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )


async def bet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, action, bet_id_str = query.data.split(":")
    bet_id = int(bet_id_str)

    acceptor = await ensure_user(query.from_user)
    async with AsyncSessionLocal() as session:
        result = await accept_bet(session, bet_id, acceptor.user_id)

    msgs = {
        "not_found":   "Pari introuvable.",
        "not_pending": "Ce pari n'est plus disponible.",
        "self":        "Tu ne peux pas accepter ton propre pari !",
        "expired":     "Ce pari a expire.",
        "insufficient":"Solde insuffisant pour ce pari !",
        "ok":          f"🤝 {mention(acceptor)} a accepte le pari #{bet_id} !\n"
                       f"Utilisez /resolvebet {bet_id} @gagnant quand c'est decide.",
    }
    await query.edit_message_text(msgs.get(result, "Erreur."), parse_mode=ParseMode.HTML)


async def acceptbet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage : /acceptbet [id]")
    try:
        bet_id = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("ID invalide.")

    acceptor = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        result = await accept_bet(session, bet_id, acceptor.user_id)

    msgs = {
        "not_found":   "Pari introuvable.",
        "not_pending": "Ce pari n'est plus disponible.",
        "self":        "Tu ne peux pas accepter ton propre pari !",
        "expired":     "Ce pari a expire.",
        "insufficient":"Solde insuffisant pour ce pari !",
        "ok":          f"Pari #{bet_id} accepte ! /resolvebet {bet_id} @gagnant quand c'est fini.",
    }
    await update.message.reply_text(msgs.get(result, "Erreur."), parse_mode=ParseMode.HTML)


async def resolvebet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        return await update.message.reply_text("Usage : /resolvebet [id] @gagnant")
    try:
        bet_id = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("ID invalide.")

    target_tg = await parse_target(update, context)
    if not target_tg:
        return await update.message.reply_text("Mentionne le gagnant avec @pseudo ou en repondant.")

    resolver = await ensure_user(update.effective_user)
    winner   = await ensure_user(target_tg)

    async with AsyncSessionLocal() as session:
        result = await resolve_bet(session, bet_id, winner.user_id, resolver.user_id)

    msgs = {
        "not_found":       "Pari introuvable.",
        "not_active":      "Ce pari n'est pas en cours.",
        "not_participant": "Seuls les participants peuvent resoudre le pari.",
        "invalid_winner":  "Le gagnant doit etre un des deux parieurs.",
    }
    if result == "ok":
        await update.message.reply_text(
            f"🏆 Pari #{bet_id} resolu !\n"
            f"{mention(winner)} remporte la mise !",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(msgs.get(result, "Erreur."))


def is_group(update: Update) -> bool:
    return update.effective_chat.type in ("group", "supergroup")
