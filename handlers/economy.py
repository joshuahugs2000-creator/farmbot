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
from handlers.journal import log_event
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database.db import (
    AsyncSessionLocal, get_user, get_richlist,
    add_coins, transfer_coins, claim_daily, claim_work,
    deduct_for_game, add_coins_smart, adjust_karma, log_action,
)
from sqlalchemy import text
from utils.helpers import ensure_user, parse_target, mention
from handlers.crime import _is_in_prison, _get_prison
from config import CURRENCY

logger = logging.getLogger(__name__)


def _fmt(n: int) -> str:
    """Formate un nombre avec des espaces : 1 234 567"""
    return f"{n:,}".replace(",", " ")


# ─── /acc ─────────────────────────────────────────────────────────────────────

async def acc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update.effective_user)

    # Cibler quelqu'un via reply ou @mention, sinon soi-même
    target_tg = await parse_target(update, context)
    if target_tg and target_tg.id != update.effective_user.id:
        target      = await ensure_user(target_tg)
        target_name = target_tg.first_name
        target_id   = target.user_id
        own_account = False
    else:
        me          = await ensure_user(update.effective_user)
        target_name = update.effective_user.first_name
        target_id   = me.user_id
        own_account = True

    async with AsyncSessionLocal() as session:
        u = await get_user(session, target_id)
        if not u:
            return await update.message.reply_text(
                f"❌ {target_name} n'a pas encore de compte. Il doit faire /start d'abord."
            )
        coins = u.coins

    footer = "\n📥 /daily  |  🔨 /work  |  💸 /pay" if own_account else ""

    await update.message.reply_text(
        f"💳 Compte de <b>{target_name}</b>\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"💰 Solde : <b>{_fmt(coins)} {CURRENCY}</b>\n"
        f"━━━━━━━━━━━━━━━━━"
        f"{footer}",
        parse_mode="HTML",
    )


# ─── /daily ───────────────────────────────────────────────────────────────────

async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        result = await claim_daily(session, user.user_id)

    if result["status"] == "already":
        await update.message.reply_text("Tu as deja pris ton bonus aujourd'hui. Reviens demain !")
    elif result["status"] == "ok":
        pct  = result.get("karma_bonus_pct", 0)
        sign = f"+{pct}%" if pct >= 0 else f"{pct}%"
        karma_line = f"\n⭐ Bonus karma ({result.get('karma_label','')}) : {sign}" if pct != 0 else ""
        await update.message.reply_text(
            f"🎁 Bonus quotidien recu !\n"
            f"💰 +{_fmt(result['amount'])} {CURRENCY}{karma_line}\n"
            f"Solde : {_fmt(result['balance'])} {CURRENCY}"
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
        cd_h = result.get("cooldown_h", 8.0)
        karma_line = f"\n⏱ Prochain /work dans {cd_h}h ({result.get('karma_label','')})" if cd_h < 8.0 else ""
        await update.message.reply_text(
            f"{job_desc}\n"
            f"💰 +{_fmt(result['amount'])} {CURRENCY}{karma_line}\n"
            f"Solde : {_fmt(result['balance'])} {CURRENCY}"
        )
    else:
        await update.message.reply_text("Erreur. Fais /start d'abord.")


# ─── /pay ─────────────────────────────────────────────────────────────────────

async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        return await update.message.reply_text("Usage : /pay @pseudo montant\nEx : /pay @Mark 50000")

    # Montant = dernier argument (extrait AVANT parse_target pour éviter
    # que le nombre soit confondu avec un user ID dans parse_target)
    try:
        amount = int(args[-1].replace(" ", "").replace(",", ""))
        assert amount > 0
    except (ValueError, AssertionError):
        return await update.message.reply_text("Montant invalide. Ex : /pay @Mark 50000")

    # On retire le montant des args avant parse_target
    context.args = args[:-1]
    target_tg = await parse_target(update, context)
    context.args = args  # restauration

    if not target_tg:
        return await update.message.reply_text(
            "Utilisateur introuvable. Il doit avoir déjà utilisé le bot."
        )

    if target_tg.id == update.effective_user.id:
        return await update.message.reply_text("Tu ne peux pas te payer toi-même !")

    sender = await ensure_user(update.effective_user)
    target = await ensure_user(target_tg)

    async with AsyncSessionLocal() as session:
        result = await transfer_coins(session, sender.user_id, target.user_id, amount)
        # 🎯 Karma : don généreux ≥ 10 000 $ = +1 karma
        karma_msg = ""
        if result == "ok" and amount >= 10_000:
            await adjust_karma(session, sender.user_id, +1)
            karma_msg = "\n⭐ +1 karma (don généreux) !"

    if result == "insufficient":
        await update.message.reply_text(f"Solde insuffisant ! Il te faut {_fmt(amount)} {CURRENCY}.")
    elif result == "not_found":
        await update.message.reply_text("Utilisateur introuvable.")
    else:
        group_id = update.effective_chat.id if update.effective_chat.type != "private" else None
        async with AsyncSessionLocal() as _ls:
            # Log côté envoyeur
            await log_action(
                _ls,
                user_id  = sender.user_id,
                username = sender.username,
                command  = "pay",
                args     = f"→ @{target.username or target.first_name}",
                amount   = -amount,
                result   = "ok",
                group_id = group_id,
            )
            # Log côté destinataire
            await log_action(
                _ls,
                user_id  = target.user_id,
                username = target.username,
                command  = "pay_reçu",
                args     = f"← @{sender.username or sender.first_name}",
                amount   = +amount,
                result   = "ok",
                group_id = group_id,
            )
        await update.message.reply_text(
            f"💸 {mention(sender)} a envoyé {_fmt(amount)} {CURRENCY} à {mention(target)} !{karma_msg}",
            parse_mode=ParseMode.HTML,
        )


# ─── /richlist ────────────────────────────────────────────────────────────────

TOP10_BADGES = {
    0: "👑", 1: "🥈", 2: "🥉", 3: "💎", 4: "💎",
    5: "⭐", 6: "⭐", 7: "🔥", 8: "🔥", 9: "🎖️",
}
TOP10_LABELS = {
    0: "Roi de la richesse", 1: "Vice-roi", 2: "Seigneur",
    3: "Élite Diamond", 4: "Élite Diamond",
    5: "Top Star", 6: "Top Star",
    7: "Flambeur", 8: "Flambeur", 9: "Top 10",
}

async def richlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncSessionLocal() as session:
        top = await get_richlist(session, 10)

    def fmt_short(n):
        if n >= 1_000_000_000: return f"{n/1_000_000_000:.2f}B"
        if n >= 1_000_000:     return f"{n/1_000_000:.0f}M"
        return _fmt(n)

    BADGES = {3:"💎",4:"💎",5:"⭐",6:"⭐",7:"🔥",8:"🔥",9:"🎖️"}
    PODIUM = [
        ("🥇", "𝗣𝗟𝗔𝗖𝗘 𝟭"),
        ("🥈", "𝗣𝗟𝗔𝗖𝗘 𝟮"),
        ("🥉", "𝗣𝗟𝗔𝗖𝗘 𝟯"),
    ]

    lines = ["💰 <b>Classement des plus riches</b>\n"]

    for i, u in enumerate(top):
        if i < 3:
            medal, place = PODIUM[i]
            lines.append(f"{medal} <b>{place} — {u.first_name}</b>")
            lines.append(f"      {_fmt(u.coins)} $")
        else:
            if i == 3:
                lines.append("──────────────")
            badge = BADGES.get(i, f"{i+1}")
            lines.append(f"{i+1} {badge} {u.first_name} — {fmt_short(u.coins)}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


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
        if await _is_in_prison(session, update.effective_user.id):
            prison = await _get_prison(session, update.effective_user.id)
            if prison:
                minutes_left = int((prison.released_at - __import__('datetime').datetime.utcnow()).total_seconds() / 60)
                return await update.message.reply_text(
                    f"🔒 Tu es en prison ! Impossible de jouer.\nLibération dans <b>{minutes_left} minutes</b>.",
                    parse_mode=ParseMode.HTML
                )

    player = [_card(), _card()]
    dealer = [_card(), _card()]
    pv     = _hand_value(player)
    dv     = _hand_value(dealer)

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
        if result == "push":
            source_msg = ""
            msg        = "Egalite — mise remboursee."
            new_bal    = (await get_user(session, user.user_id)).coins
        else:
            source = await deduct_for_game(session, user.user_id, mise)
            if source == "insufficient":
                return await update.message.reply_text("❌ Solde insuffisant (compte perso et compte commun) !")
            source_msg = "\n💑 Mise prise sur le compte commun." if source == "couple" else ""

            if result == "win":
                await add_coins_smart(session, user.user_id, mise * 2)
                msg = f"✅ Tu gagnes {_fmt(mise)} {CURRENCY} !"
            else:
                msg = f"❌ Tu perds {_fmt(mise)} {CURRENCY}."

            u2      = await get_user(session, user.user_id)
            new_bal = u2.coins if u2 else 0

    await update.message.reply_text(
        f"🃏 BLACKJACK\n"
        f"Toi    : {_show_hand(player)} = {pv}\n"
        f"Dealer : {_show_hand(dealer)} = {dv}\n\n"
        f"{msg}{source_msg}\n"
        f"Solde : {_fmt(new_bal)} {CURRENCY}"
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
        if await _is_in_prison(session, update.effective_user.id):
            prison = await _get_prison(session, update.effective_user.id)
            if prison:
                minutes_left = int((prison.released_at - __import__('datetime').datetime.utcnow()).total_seconds() / 60)
                return await update.message.reply_text(
                    f"🔒 Tu es en prison ! Impossible de jouer.\nLibération dans <b>{minutes_left} minutes</b>.",
                    parse_mode=ParseMode.HTML
                )

    numero = random.randint(0, 36)
    couleur = "rouge" if numero in REDS else ("noir" if numero in BLACKS else "vert")

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
        source = await deduct_for_game(session, user.user_id, mise)
        if source == "insufficient":
            return await update.message.reply_text("❌ Solde insuffisant (compte perso et compte commun) !")
        source_msg = "\n💑 Mise prise sur le compte commun." if source == "couple" else ""

        if win:
            gain = mise * (mult - 1)
            await add_coins_smart(session, user.user_id, gain + mise)
            msg  = f"✅ Gagne ! +{_fmt(gain)} {CURRENCY}"
        else:
            msg  = f"❌ Perdu. -{_fmt(mise)} {CURRENCY}"
        u2      = await get_user(session, user.user_id)
        new_bal = u2.coins if u2 else 0

    color_emoji = {"rouge":"🔴","noir":"⚫","vert":"🟢"}.get(couleur,"⚪")
    await update.message.reply_text(
        f"🎡 ROULETTE\n"
        f"Numero : {numero} {color_emoji}\n"
        f"Ton pari : {choix} ({_fmt(mise)} {CURRENCY})\n\n"
        f"{msg}{source_msg}\n"
        f"Solde : {_fmt(new_bal)} {CURRENCY}"
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
        if await _is_in_prison(session, update.effective_user.id):
            prison = await _get_prison(session, update.effective_user.id)
            if prison:
                minutes_left = int((prison.released_at - __import__('datetime').datetime.utcnow()).total_seconds() / 60)
                return await update.message.reply_text(
                    f"🔒 Tu es en prison ! Impossible de jouer.\nLibération dans <b>{minutes_left} minutes</b>.",
                    parse_mode=ParseMode.HTML
                )

    reels = random.choices(SYMBOLS, weights=WEIGHTS, k=3)

    if reels[0] == reels[1] == reels[2]:
        s    = reels[0]
        mult = {"💎": 50, "7️⃣": 30, "⭐": 15, "🍇": 8, "🍊": 5, "🍋": 3, "🍒": 2}.get(s, 2)
        gain = mise * mult
        msg  = f"🎰 JACKPOT ! x{mult} — +{_fmt(gain)} {CURRENCY} !"
        delta = gain
        if gain >= 50_000:  # log seulement les gros gains
            await log_event("casino_big_win", user=update.effective_user.first_name, amount=_fmt(gain))
    elif reels[0] == reels[1] or reels[1] == reels[2]:
        gain  = mise // 2
        msg   = f"Deux identiques ! +{_fmt(gain)} {CURRENCY}."
        delta = gain
    else:
        msg   = f"Rien. -{_fmt(mise)} {CURRENCY}."
        delta = -mise

    async with AsyncSessionLocal() as session:
        if delta < 0:
            source = await deduct_for_game(session, user.user_id, mise)
            if source == "insufficient":
                return await update.message.reply_text("❌ Solde insuffisant (compte perso et compte commun) !")
            source_msg = "\n💑 Mise prise sur le compte commun." if source == "couple" else ""
        else:
            source = await deduct_for_game(session, user.user_id, mise)
            if source == "insufficient":
                return await update.message.reply_text("❌ Solde insuffisant (compte perso et compte commun) !")
            source_msg = "\n💑 Mise prise sur le compte commun." if source == "couple" else ""
            await add_coins_smart(session, user.user_id, mise + delta)

        u2      = await get_user(session, user.user_id)
        new_bal = u2.coins if u2 else 0

    await update.message.reply_text(
        f"🎰 SLOTS\n"
        f"[ {reels[0]} | {reels[1]} | {reels[2]} ]\n\n"
        f"{msg}{source_msg}\n"
        f"Solde : {_fmt(new_bal)} {CURRENCY}"
    )


# ─── /des ─────────────────────────────────────────────────────────────────────

DES_MISE       = 50_000
DES_GAIN       = 10_000_000
DES_MAX_TRIES  = 10          # essais maximum par jour
DES_FACES      = {
    1: "1️⃣",  2: "2️⃣",  3: "3️⃣",  4: "4️⃣",  5: "5️⃣",
    6: "6️⃣",  7: "7️⃣",  8: "8️⃣",  9: "9️⃣",  10: "🔟",
    11: "1️⃣1️⃣", 12: "1️⃣2️⃣", 13: "1️⃣3️⃣", 14: "1️⃣4️⃣", 15: "1️⃣5️⃣",
    16: "1️⃣6️⃣", 17: "1️⃣7️⃣", 18: "1️⃣8️⃣", 19: "1️⃣9️⃣", 20: "2️⃣0️⃣",
    21: "2️⃣1️⃣",
}

# Compteur quotidien : { user_id: {"date": date, "count": int} }
_des_daily: dict = {}

async def des(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /des <1-21>  — Mise fixe de 50 000 $. Devine le nombre exact → gagne 10 000 000 $.
    """
    player_tg = update.effective_user
    await ensure_user(player_tg)

    # ── Limite quotidienne ─────────────────────────────────────────────────────
    from datetime import date as _date
    today = _date.today()
    rec   = _des_daily.get(player_tg.id)
    if rec and rec["date"] == today:
        tries_left = DES_MAX_TRIES - rec["count"]
    else:
        tries_left = DES_MAX_TRIES

    # Vérifier l'argument
    if not context.args:
        await update.message.reply_text(
            f"🎲 <b>JEU DU DÉ</b>\n\n"
            f"Mise fixe : <b>{_fmt(DES_MISE)} 💰</b>\n"
            f"Si tu trouves le bon nombre (1 à 21) → <b>{_fmt(DES_GAIN)} 💰</b> !\n\n"
            f"⚠️ Limite : <b>{DES_MAX_TRIES} essais par jour</b>\n"
            f"Essais restants aujourd'hui : <b>{tries_left}</b>\n\n"
            f"Usage : <code>/des 14</code>",
            parse_mode=ParseMode.HTML
        )
        return

    try:
        choix = int(context.args[0])
        assert 1 <= choix <= 21
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ Choisis un nombre entre <b>1</b> et <b>21</b>.\nEx: <code>/des 14</code>", parse_mode=ParseMode.HTML)
        return

    async with AsyncSessionLocal() as session:
        # Prison ?
        if await _is_in_prison(session, player_tg.id):
            prison = await _get_prison(session, player_tg.id)
            from datetime import datetime
            mins = max(0, int((prison.released_at - datetime.utcnow()).total_seconds() / 60))
            await update.message.reply_text(f"⛓️ Tu es en prison ! Libération dans <b>{mins} min</b>.", parse_mode=ParseMode.HTML)
            return

        player = await get_user(session, player_tg.id)
        if player.coins < DES_MISE:
            await update.message.reply_text(
                f"❌ Il te faut <b>{_fmt(DES_MISE)} 💰</b> pour jouer.\nTon solde : <b>{_fmt(player.coins)} 💰</b>",
                parse_mode=ParseMode.HTML
            )
            return

        # Vérifier la limite quotidienne
        if tries_left <= 0:
            await update.message.reply_text(
                f"⛔ Tu as atteint ta limite de <b>{DES_MAX_TRIES} essais</b> pour aujourd'hui !\n"
                f"Reviens demain pour rejouer. 🎲",
                parse_mode=ParseMode.HTML
            )
            return

        # Incrémenter le compteur
        from datetime import date as _date
        today2 = _date.today()
        if player_tg.id not in _des_daily or _des_daily[player_tg.id]["date"] != today2:
            _des_daily[player_tg.id] = {"date": today2, "count": 0}
        _des_daily[player_tg.id]["count"] += 1
        tries_after = DES_MAX_TRIES - _des_daily[player_tg.id]["count"]

        # Déduire la mise
        await session.execute(
            text("UPDATE users SET coins = CAST(coins AS BIGINT) - CAST(:amt AS BIGINT) WHERE user_id = :uid"),
            {"amt": DES_MISE, "uid": player_tg.id}
        )
        await session.commit()

        # Lancer le dé
        resultat = random.randint(1, 21)
        face_choix    = DES_FACES[choix]
        face_resultat = DES_FACES[resultat]

        if resultat == choix:
            # GAGNÉ
            await session.execute(
                text("UPDATE users SET coins = CAST(coins AS BIGINT) + CAST(:amt AS BIGINT) WHERE user_id = :uid"),
                {"amt": DES_GAIN, "uid": player_tg.id}
            )
            await session.commit()
            await update.message.reply_text(
                f"🎲 <b>JEU DU DÉ</b>\n\n"
                f"Ton choix   : {face_choix} <b>{choix}</b>\n"
                f"Résultat    : {face_resultat} <b>{resultat}</b>\n\n"
                f"🏆 <b>JACKPOT ! TU AS TROUVÉ !</b>\n"
                f"💰 <b>+{_fmt(DES_GAIN)} 💰</b> crédités !\n\n"
                f"🎲 Essais restants aujourd'hui : <b>{tries_after}/{DES_MAX_TRIES}</b>",
                parse_mode=ParseMode.HTML
            )
        else:
            await update.message.reply_text(
                f"🎲 <b>JEU DU DÉ</b>\n\n"
                f"Ton choix   : {face_choix} <b>{choix}</b>\n"
                f"Résultat    : {face_resultat} <b>{resultat}</b>\n\n"
                f"❌ <b>PERDU !</b> -{_fmt(DES_MISE)} 💰\n\n"
                f"🎲 Essais restants aujourd'hui : <b>{tries_after}/{DES_MAX_TRIES}</b>",
                parse_mode=ParseMode.HTML
            )
