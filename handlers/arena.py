"""
handlers/arena.py — Arène de jeux PvP avec paris spectateurs

🐔 COMBAT DE COQS
  /cockfight <mise>   — lancer un combat (mise minimum 1 000 $)
  Spectateurs : boutons inline pour parier sur Coq 1 ou Coq 2

✂️ PIERRE PAPIER CISEAUX
  /ppc @joueur <mise> — défier un joueur (répondre ou mentionner)
  Spectateurs : boutons inline pour parier sur l'un des deux joueurs
"""

import asyncio
import random
import logging
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database.db import AsyncSessionLocal, get_user, deduct_for_game, add_coins_smart
from utils.helpers import ensure_user, parse_target, mention

import sqlalchemy as sa
from config import CURRENCY

logger = logging.getLogger(__name__)


def _fmt(n: int) -> str:
    return f"{int(n):,}".replace(",", " ")


async def _get_balance(session, user_id: int) -> int:
    user = await get_user(session, user_id)
    return int(user.coins) if user else 0


async def _add_coins(session, user_id: int, amount: int):
    await session.execute(
        sa.text("UPDATE users SET coins = coins + :a WHERE user_id = :uid"),
        {"a": amount, "uid": user_id},
    )
    await session.commit()


async def _deduct_coins(session, user_id: int, amount: int):
    await session.execute(
        sa.text("UPDATE users SET coins = coins - :a WHERE user_id = :uid"),
        {"a": amount, "uid": user_id},
    )
    await session.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# 🐔 COMBAT DE COQS
# ═══════════════════════════════════════════════════════════════════════════════

# Structure : cockfight_sessions[chat_id] = { ...données de la session... }
cockfight_sessions: dict = {}

COQS = [
    {"nom": "🐔 El Diablo",    "emoji": "🔴", "force": 75, "vitesse": 60, "chance": 50},
    {"nom": "🐔 Tonnerre Bleu", "emoji": "🔵", "force": 55, "vitesse": 85, "chance": 65},
    {"nom": "🐔 Maître Plume",  "emoji": "🟡", "force": 65, "vitesse": 65, "chance": 80},
    {"nom": "🐔 Le Borgne",     "emoji": "⚫", "force": 90, "vitesse": 45, "chance": 40},
]

LOBBY_DURATION = 25  # secondes de paris


def _coq_stats(coq: dict) -> str:
    return (
        f"⚔️ Force: {'█' * (coq['force'] // 20)}{'░' * (5 - coq['force'] // 20)} {coq['force']}\n"
        f"💨 Vitesse: {'█' * (coq['vitesse'] // 20)}{'░' * (5 - coq['vitesse'] // 20)} {coq['vitesse']}\n"
        f"🍀 Chance: {'█' * (coq['chance'] // 20)}{'░' * (5 - coq['chance'] // 20)} {coq['chance']}"
    )


def _cockfight_lobby_text(session: dict, seconds_left: int) -> str:
    coq1 = session["coq1"]
    coq2 = session["coq2"]
    mise = session["mise"]
    bets1 = session["bets1"]
    bets2 = session["bets2"]
    total1 = sum(bets1.values())
    total2 = sum(bets2.values())

    lines1 = [f"  • {n} — <b>{_fmt(m)} {CURRENCY}</b>" for n, m in bets1.items()] or ["  <i>Aucun parieur</i>"]
    lines2 = [f"  • {n} — <b>{_fmt(m)} {CURRENCY}</b>" for n, m in bets2.items()] or ["  <i>Aucun parieur</i>"]

    return (
        f"🐔 <b>COMBAT DE COQS — Phase de paris</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{coq1['emoji']} <b>{coq1['nom']}</b>\n{_coq_stats(coq1)}\n\n"
        f"{coq2['emoji']} <b>{coq2['nom']}</b>\n{_coq_stats(coq2)}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Mise du combat : <b>{_fmt(mise)} {CURRENCY}</b>\n\n"
        f"🏟️ <b>Parieurs sur {coq1['nom']} ({_fmt(total1)} {CURRENCY})</b>\n"
        + "\n".join(lines1) + "\n\n"
        f"🏟️ <b>Parieurs sur {coq2['nom']} ({_fmt(total2)} {CURRENCY})</b>\n"
        + "\n".join(lines2) + "\n\n"
        f"⏳ Combat dans <b>{seconds_left}s</b> — Place tes paris !"
    )


def _cockfight_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    s = cockfight_sessions.get(chat_id, {})
    coq1 = s.get("coq1", {})
    coq2 = s.get("coq2", {})
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"{coq1.get('emoji','🔴')} Parier sur {coq1.get('nom','Coq 1')}", callback_data="cf:bet:1"),
            InlineKeyboardButton(f"{coq2.get('emoji','🔵')} Parier sur {coq2.get('nom','Coq 2')}", callback_data="cf:bet:2"),
        ]
    ])


def _simulate_fight(coq1: dict, coq2: dict) -> tuple[dict, list[str]]:
    """Simule un combat en 3 rounds, retourne (gagnant, narration)."""
    hp1, hp2 = 100, 100
    narration = []

    ACTIONS = [
        ("attaque", "🗡️ {} frappe {} pour {} dégâts !"),
        ("esquive", "💨 {} esquive l'attaque de {} !"),
        ("contre",  "⚡ {} contre-attaque {} pour {} dégâts !"),
    ]

    for rnd in range(1, 4):
        narration.append(f"\n<b>— Round {rnd} —</b>")

        # Score d'attaque pondéré par les stats
        score1 = (coq1["force"] * random.random() + coq1["vitesse"] * random.random() + coq1["chance"] * random.random()) / 3
        score2 = (coq2["force"] * random.random() + coq2["vitesse"] * random.random() + coq2["chance"] * random.random()) / 3

        action_idx = random.randint(0, 2)
        action, template = ACTIONS[action_idx]

        if score1 > score2:
            dmg = random.randint(15, 40)
            hp2 -= dmg
            if action == "esquive":
                narration.append(template.format(coq1["nom"], coq2["nom"]))
            else:
                narration.append(template.format(coq1["nom"], coq2["nom"], dmg))
        else:
            dmg = random.randint(15, 40)
            hp1 -= dmg
            if action == "esquive":
                narration.append(template.format(coq2["nom"], coq1["nom"]))
            else:
                narration.append(template.format(coq2["nom"], coq1["nom"], dmg))

        hp1 = max(0, hp1)
        hp2 = max(0, hp2)
        narration.append(f"  ❤️ {coq1['nom']}: {hp1} PV  |  ❤️ {coq2['nom']}: {hp2} PV")

        if hp1 <= 0 or hp2 <= 0:
            break

    if hp1 > hp2:
        return coq1, narration
    elif hp2 > hp1:
        return coq2, narration
    else:
        # Égalité → chance décide
        winner = coq1 if random.random() < coq1["chance"] / (coq1["chance"] + coq2["chance"]) else coq2
        return winner, narration


async def cockfight_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    user = update.effective_user

    if update.effective_chat.type == "private":
        return await update.message.reply_text("🐔 Le combat de coqs se joue en groupe !")

    if chat_id in cockfight_sessions:
        return await update.message.reply_text("⚠️ Un combat est déjà en cours dans ce groupe !")

    # Vérifier la mise
    args = context.args
    if not args or not args[0].isdigit():
        return await update.message.reply_text(
            "🐔 <b>Combat de Coqs</b>\n\nUsage : <code>/cockfight &lt;mise&gt;</code>\nEx : <code>/cockfight 5000</code>",
            parse_mode=ParseMode.HTML,
        )

    mise = int(args[0])
    if mise < 1000:
        return await update.message.reply_text("❌ Mise minimum : <b>1 000 $</b>", parse_mode=ParseMode.HTML)

    await ensure_user(user)
    async with AsyncSessionLocal() as session:
        bal = await _get_balance(session, user.id)
        if bal < mise:
            return await update.message.reply_text(
                f"❌ Solde insuffisant ! Tu as <b>{_fmt(bal)} {CURRENCY}</b>", parse_mode=ParseMode.HTML
            )
        await _deduct_coins(session, user.id, mise)

    # Tirer 2 coqs au hasard (différents)
    deux_coqs = random.sample(COQS, 2)
    coq1, coq2 = deux_coqs[0], deux_coqs[1]

    cockfight_sessions[chat_id] = {
        "organisateur_id":   user.id,
        "organisateur_name": user.first_name,
        "mise":  mise,
        "coq1":  coq1,
        "coq2":  coq2,
        "bets1": {},   # {first_name: montant}
        "bets2": {},
        "bets1_ids": {},  # {user_id: montant}
        "bets2_ids": {},
        "msg_id": None,
    }

    msg = await update.message.reply_text(
        _cockfight_lobby_text(cockfight_sessions[chat_id], LOBBY_DURATION),
        parse_mode=ParseMode.HTML,
        reply_markup=_cockfight_keyboard(chat_id),
    )

    cockfight_sessions[chat_id]["msg_id"] = msg.message_id

    # Countdown et lancement
    asyncio.create_task(_cockfight_countdown(context, chat_id, msg.chat_id))


async def _cockfight_countdown(context, chat_id: int, tg_chat_id: int):
    for remaining in range(LOBBY_DURATION - 5, 0, -5):
        await asyncio.sleep(5)
        s = cockfight_sessions.get(chat_id)
        if not s:
            return
        try:
            await context.bot.edit_message_text(
                chat_id=tg_chat_id,
                message_id=s["msg_id"],
                text=_cockfight_lobby_text(s, remaining),
                parse_mode=ParseMode.HTML,
                reply_markup=_cockfight_keyboard(chat_id),
            )
        except Exception:
            pass

    await asyncio.sleep(5)
    await _cockfight_resolve(context, chat_id, tg_chat_id)


async def _cockfight_resolve(context, chat_id: int, tg_chat_id: int):
    s = cockfight_sessions.pop(chat_id, None)
    if not s:
        return

    coq1, coq2 = s["coq1"], s["coq2"]
    mise = s["mise"]

    # Simulation du combat
    winner, narration = _simulate_fight(coq1, coq2)
    loser = coq2 if winner == coq1 else coq1
    winner_num = 1 if winner == coq1 else 2

    # Remboursement organisateur si son coq gagne (x1.8)
    async with AsyncSessionLocal() as session:
        if winner_num == 1:
            gain_org = int(mise * 1.8)
            await _add_coins(session, s["organisateur_id"], gain_org)
        else:
            # Organisateur perd sa mise, déjà déduite

            pass

        # Payer les parieurs gagnants
        bets_winners = s["bets1_ids"] if winner_num == 1 else s["bets2_ids"]
        bets_losers  = s["bets2_ids"] if winner_num == 1 else s["bets1_ids"]

        total_losers = sum(bets_losers.values())
        total_winners = sum(bets_winners.values())

        for uid, montant in bets_winners.items():
            if total_winners > 0:
                share = montant / total_winners
                gain = int(montant + total_losers * share * 0.90)  # 90% du pot perdants
            else:
                gain = int(montant * 1.5)
            await _add_coins(session, uid, gain)

    # Construction du message résultat
    fight_log = "\n".join(narration)

    paris_result = ""
    if bets_winners:
        paris_result += f"\n\n🏆 <b>Parieurs gagnants :</b>\n"
        for name, m in (s["bets1"] if winner_num == 1 else s["bets2"]).items():
            paris_result += f"  ✅ {name} — mise {_fmt(m)} {CURRENCY}\n"
    if (s["bets2"] if winner_num == 1 else s["bets1"]):
        paris_result += f"\n💸 <b>Parieurs perdants :</b>\n"
        for name, m in (s["bets2"] if winner_num == 1 else s["bets1"]).items():
            paris_result += f"  ❌ {name} — {_fmt(m)} {CURRENCY} perdus\n"

    org_result = f"✅ +{_fmt(int(mise * 0.8))} {CURRENCY}" if winner_num == 1 else f"❌ -{_fmt(mise)} {CURRENCY}"

    result_text = (
        f"🐔 <b>COMBAT TERMINÉ !</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{fight_log}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏆 <b>VAINQUEUR : {winner['nom']}</b> {winner['emoji']}\n"
        f"💀 Perdant : {loser['nom']}\n\n"
        f"👑 Organisateur ({s['organisateur_name']}) : {org_result}"
        f"{paris_result}"
    )

    try:
        await context.bot.edit_message_text(
            chat_id=tg_chat_id,
            message_id=s["msg_id"],
            text=result_text,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"Cockfight resolve error: {e}")


async def cockfight_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    chat_id = query.message.chat_id
    data = query.data  # "cf:bet:1" ou "cf:bet:2"

    s = cockfight_sessions.get(chat_id)
    if not s:
        return await query.answer("❌ Plus de combat en cours.", show_alert=True)

    parts = data.split(":")
    coq_num = int(parts[2])

    # Vérifier si déjà parié
    already_bet = user.id in s["bets1_ids"] or user.id in s["bets2_ids"]
    if already_bet:
        return await query.answer("⚠️ Tu as déjà parié sur ce combat !", show_alert=True)

    # Mise fixe = 10% de la mise du combat, minimum 500
    bet_amount = max(500, s["mise"] // 10)

    await ensure_user(user)
    async with AsyncSessionLocal() as session:
        bal = await _get_balance(session, user.id)
        if bal < bet_amount:
            return await query.answer(f"❌ Il te faut au moins {_fmt(bet_amount)} {CURRENCY} pour parier !", show_alert=True)
        await _deduct_coins(session, user.id, bet_amount)

    coq_cible = s["coq1"] if coq_num == 1 else s["coq2"]

    if coq_num == 1:
        s["bets1"][user.first_name] = bet_amount
        s["bets1_ids"][user.id] = bet_amount
    else:
        s["bets2"][user.first_name] = bet_amount
        s["bets2_ids"][user.id] = bet_amount

    await query.answer(f"✅ Tu as parié {_fmt(bet_amount)} {CURRENCY} sur {coq_cible['nom']} !", show_alert=True)

    # Mise à jour du message
    try:
        remaining = 10  # approximatif
        await query.edit_message_text(
            text=_cockfight_lobby_text(s, remaining),
            parse_mode=ParseMode.HTML,
            reply_markup=_cockfight_keyboard(chat_id),
        )
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# ✂️ PIERRE PAPIER CISEAUX — PvP avec spectateurs
# ═══════════════════════════════════════════════════════════════════════════════

ppc_sessions: dict = {}  # chat_id -> session

PPC_CHOICES = {
    "pierre":  {"emoji": "🪨", "bat": "ciseaux"},
    "papier":  {"emoji": "📄", "bat": "pierre"},
    "ciseaux": {"emoji": "✂️", "bat": "papier"},
}

PPC_LOBBY = 20  # secondes de lobby/paris


def _ppc_lobby_text(s: dict, seconds_left: int) -> str:
    j1_name = s["j1_name"]
    j2_name = s["j2_name"]
    mise = s["mise"]
    bets1 = s["bets1"]
    bets2 = s["bets2"]
    total1 = sum(bets1.values())
    total2 = sum(bets2.values())

    j1_status = "✅ Prêt" if s["j1_choice"] else "⏳ En attente..."
    j2_status = "✅ Prêt" if s["j2_choice"] else "⏳ En attente..."

    lines1 = [f"  • {n} — {_fmt(m)} {CURRENCY}" for n, m in bets1.items()] or ["  <i>Aucun parieur</i>"]
    lines2 = [f"  • {n} — {_fmt(m)} {CURRENCY}" for n, m in bets2.items()] or ["  <i>Aucun parieur</i>"]

    return (
        f"✂️ <b>PIERRE PAPIER CISEAUX — Duel</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔴 <b>{j1_name}</b> — {j1_status}\n"
        f"🔵 <b>{j2_name}</b> — {j2_status}\n\n"
        f"💰 Mise : <b>{_fmt(mise)} {CURRENCY}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📣 <b>Parieurs sur {j1_name} ({_fmt(total1)} {CURRENCY})</b>\n"
        + "\n".join(lines1) + "\n\n"
        f"📣 <b>Parieurs sur {j2_name} ({_fmt(total2)} {CURRENCY})</b>\n"
        + "\n".join(lines2) + "\n\n"
        f"⏳ Révélation dans <b>{seconds_left}s</b>"
    )


def _ppc_keyboard(s: dict) -> InlineKeyboardMarkup:
    j1_id = s["j1_id"]
    j2_id = s["j2_id"]
    j1_name = s["j1_name"]
    j2_name = s["j2_name"]

    rows = [
        # Choix pour les joueurs
        [
            InlineKeyboardButton("🪨 Pierre",  callback_data="ppc:choice:pierre"),
            InlineKeyboardButton("📄 Papier",  callback_data="ppc:choice:papier"),
            InlineKeyboardButton("✂️ Ciseaux", callback_data="ppc:choice:ciseaux"),
        ],
        # Paris pour spectateurs
        [
            InlineKeyboardButton(f"🔴 Parier sur {j1_name}", callback_data="ppc:bet:1"),
            InlineKeyboardButton(f"🔵 Parier sur {j2_name}", callback_data="ppc:bet:2"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


async def ppc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    user = update.effective_user

    if update.effective_chat.type == "private":
        return await update.message.reply_text("✂️ Le PPC se joue en groupe !")

    if chat_id in ppc_sessions:
        return await update.message.reply_text("⚠️ Un duel PPC est déjà en cours dans ce groupe !")

    # Parse target et mise
    args = context.args
    if not args:
        return await update.message.reply_text(
            "✂️ <b>Pierre Papier Ciseaux</b>\n\nUsage : <code>/ppc @joueur &lt;mise&gt;</code>\nEx : <code>/ppc @Ahmed 5000</code>",
            parse_mode=ParseMode.HTML,
        )

    target_tg = await parse_target(update, context)
    if not target_tg:
        return await update.message.reply_text("❌ Mentionne un joueur à défier !")

    if target_tg.id == user.id:
        return await update.message.reply_text("❌ Tu ne peux pas te défier toi-même !")

    if target_tg.is_bot:
        return await update.message.reply_text("❌ Tu ne peux pas défier un bot !")

    # Chercher la mise (dernier argument numérique)
    mise = None
    for arg in reversed(args):
        if arg.isdigit():
            mise = int(arg)
            break

    if not mise:
        return await update.message.reply_text("❌ Indique une mise ! Ex : <code>/ppc @joueur 5000</code>", parse_mode=ParseMode.HTML)

    if mise < 1000:
        return await update.message.reply_text("❌ Mise minimum : <b>1 000 $</b>", parse_mode=ParseMode.HTML)

    # Vérifier soldes
    await ensure_user(user)
    await ensure_user(target_tg)

    async with AsyncSessionLocal() as session:
        bal1 = await _get_balance(session, user.id)
        bal2 = await _get_balance(session, target_tg.id)

        if bal1 < mise:
            return await update.message.reply_text(f"❌ {user.first_name} n'a pas assez de {CURRENCY} !")
        if bal2 < mise:
            return await update.message.reply_text(f"❌ {target_tg.first_name} n'a pas assez de {CURRENCY} !")

        await _deduct_coins(session, user.id, mise)
        await _deduct_coins(session, target_tg.id, mise)

    ppc_sessions[chat_id] = {
        "j1_id":   user.id,
        "j1_name": user.first_name,
        "j2_id":   target_tg.id,
        "j2_name": target_tg.first_name,
        "mise":    mise,
        "j1_choice": None,
        "j2_choice": None,
        "bets1": {},       # {name: amount}
        "bets2": {},
        "bets1_ids": {},   # {user_id: amount}
        "bets2_ids": {},
        "msg_id": None,
    }

    s = ppc_sessions[chat_id]

    msg = await update.message.reply_text(
        _ppc_lobby_text(s, PPC_LOBBY),
        parse_mode=ParseMode.HTML,
        reply_markup=_ppc_keyboard(s),
    )

    ppc_sessions[chat_id]["msg_id"] = msg.message_id

    asyncio.create_task(_ppc_countdown(context, chat_id, msg.chat_id))


async def _ppc_countdown(context, chat_id: int, tg_chat_id: int):
    for remaining in range(PPC_LOBBY - 5, 0, -5):
        await asyncio.sleep(5)
        s = ppc_sessions.get(chat_id)
        if not s:
            return
        try:
            await context.bot.edit_message_text(
                chat_id=tg_chat_id,
                message_id=s["msg_id"],
                text=_ppc_lobby_text(s, remaining),
                parse_mode=ParseMode.HTML,
                reply_markup=_ppc_keyboard(s),
            )
        except Exception:
            pass

    await asyncio.sleep(5)
    await _ppc_resolve(context, chat_id, tg_chat_id)


async def _ppc_resolve(context, chat_id: int, tg_chat_id: int):
    s = ppc_sessions.pop(chat_id, None)
    if not s:
        return

    j1_id   = s["j1_id"]
    j2_id   = s["j2_id"]
    j1_name = s["j1_name"]
    j2_name = s["j2_name"]
    mise    = s["mise"]

    # Si un joueur n'a pas choisi → random
    choices = list(PPC_CHOICES.keys())
    c1 = s["j1_choice"] or random.choice(choices)
    c2 = s["j2_choice"] or random.choice(choices)

    e1 = PPC_CHOICES[c1]["emoji"]
    e2 = PPC_CHOICES[c2]["emoji"]

    # Déterminer gagnant
    if c1 == c2:
        result = "draw"
    elif PPC_CHOICES[c1]["bat"] == c2:
        result = "j1"
    else:
        result = "j2"

    async with AsyncSessionLocal() as session:
        if result == "draw":
            # Remboursement
            await _add_coins(session, j1_id, mise)
            await _add_coins(session, j2_id, mise)
            # Remboursement parieurs
            for uid, m in s["bets1_ids"].items():
                await _add_coins(session, uid, m)
            for uid, m in s["bets2_ids"].items():
                await _add_coins(session, uid, m)
        else:
            winner_id   = j1_id   if result == "j1" else j2_id
            winner_name = j1_name if result == "j1" else j2_name
            loser_name  = j2_name if result == "j1" else j1_name

            # Gagnant prend les 2 mises
            await _add_coins(session, winner_id, mise * 2)

            # Parieurs gagnants
            bets_w_ids = s["bets1_ids"] if result == "j1" else s["bets2_ids"]
            bets_l_ids = s["bets2_ids"] if result == "j1" else s["bets1_ids"]
            bets_w     = s["bets1"]     if result == "j1" else s["bets2"]
            bets_l     = s["bets2"]     if result == "j1" else s["bets1"]

            total_w = sum(bets_w_ids.values())
            total_l = sum(bets_l_ids.values())

            for uid, m in bets_w_ids.items():
                share = m / total_w if total_w > 0 else 1
                gain = int(m + total_l * share * 0.90)
                await _add_coins(session, uid, gain)

    # Construction du message résultat
    if result == "draw":
        outcome = (
            f"🤝 <b>ÉGALITÉ !</b>\n\n"
            f"🔴 {j1_name} : {e1} <b>{c1.upper()}</b>\n"
            f"🔵 {j2_name} : {e2} <b>{c2.upper()}</b>\n\n"
            f"💸 Tout le monde est remboursé !"
        )
    else:
        w_name = j1_name if result == "j1" else j2_name
        l_name = j2_name if result == "j1" else j1_name
        w_choice = c1 if result == "j1" else c2
        l_choice = c2 if result == "j1" else c1
        w_e = e1 if result == "j1" else e2
        l_e = e2 if result == "j1" else e1

        bets_w = s["bets1"] if result == "j1" else s["bets2"]
        bets_l = s["bets2"] if result == "j1" else s["bets1"]

        paris_txt = ""
        if bets_w:
            paris_txt += "\n🏆 <b>Parieurs gagnants :</b>\n"
            for name, m in bets_w.items():
                paris_txt += f"  ✅ {name} — {_fmt(m)} {CURRENCY} misés\n"
        if bets_l:
            paris_txt += "\n💸 <b>Parieurs perdants :</b>\n"
            for name, m in bets_l.items():
                paris_txt += f"  ❌ {name} — {_fmt(m)} {CURRENCY} perdus\n"

        outcome = (
            f"🏆 <b>VICTOIRE DE {w_name.upper()} !</b>\n\n"
            f"🔴 {j1_name} : {e1} <b>{c1.upper()}</b>\n"
            f"🔵 {j2_name} : {e2} <b>{c2.upper()}</b>\n\n"
            f"⚔️ {w_choice.upper()} bat {l_choice.upper()} !\n"
            f"💰 {w_name} remporte <b>{_fmt(mise * 2)} {CURRENCY}</b> !"
            f"{paris_txt}"
        )

    try:
        await context.bot.edit_message_text(
            chat_id=tg_chat_id,
            message_id=s["msg_id"],
            text=f"✂️ <b>RÉSULTAT DU DUEL</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n{outcome}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"PPC resolve error: {e}")


async def ppc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    chat_id = query.message.chat_id
    data = query.data

    s = ppc_sessions.get(chat_id)
    if not s:
        return await query.answer("❌ Pas de duel en cours.", show_alert=True)

    parts = data.split(":")
    action = parts[1]

    # ── Choix du joueur ──────────────────────────────────────────────
    if action == "choice":
        choix = parts[2]

        if user.id not in (s["j1_id"], s["j2_id"]):
            return await query.answer("👁️ Tu es spectateur, tu ne peux que parier !", show_alert=True)

        if user.id == s["j1_id"]:
            if s["j1_choice"]:
                return await query.answer("✅ Tu as déjà choisi !", show_alert=True)
            s["j1_choice"] = choix
        else:
            if s["j2_choice"]:
                return await query.answer("✅ Tu as déjà choisi !", show_alert=True)
            s["j2_choice"] = choix

        await query.answer(f"✅ Tu as choisi {PPC_CHOICES[choix]['emoji']} {choix.upper()} !", show_alert=True)

        try:
            await query.edit_message_text(
                text=_ppc_lobby_text(s, 10),
                parse_mode=ParseMode.HTML,
                reply_markup=_ppc_keyboard(s),
            )
        except Exception:
            pass

        # Si les deux ont choisi → résoudre immédiatement
        if s["j1_choice"] and s["j2_choice"]:
            ppc_sessions.pop(chat_id, None)
            await asyncio.sleep(2)
            await _ppc_resolve_direct(context, chat_id, query.message.chat_id, s)

    # ── Pari spectateur ──────────────────────────────────────────────
    elif action == "bet":
        joueur_num = int(parts[2])

        if user.id in (s["j1_id"], s["j2_id"]):
            return await query.answer("⚔️ Tu joues ce duel, tu ne peux pas parier !", show_alert=True)

        if user.id in s["bets1_ids"] or user.id in s["bets2_ids"]:
            return await query.answer("⚠️ Tu as déjà parié !", show_alert=True)

        bet_amount = max(500, s["mise"] // 10)

        await ensure_user(user)
        async with AsyncSessionLocal() as session:
            bal = await _get_balance(session, user.id)
            if bal < bet_amount:
                return await query.answer(f"❌ Il te faut {_fmt(bet_amount)} {CURRENCY} pour parier !", show_alert=True)
            await _deduct_coins(session, user.id, bet_amount)

        cible_name = s["j1_name"] if joueur_num == 1 else s["j2_name"]

        if joueur_num == 1:
            s["bets1"][user.first_name] = bet_amount
            s["bets1_ids"][user.id] = bet_amount
        else:
            s["bets2"][user.first_name] = bet_amount
            s["bets2_ids"][user.id] = bet_amount

        await query.answer(f"✅ {_fmt(bet_amount)} {CURRENCY} pariés sur {cible_name} !", show_alert=True)

        try:
            await query.edit_message_text(
                text=_ppc_lobby_text(s, 10),
                parse_mode=ParseMode.HTML,
                reply_markup=_ppc_keyboard(s),
            )
        except Exception:
            pass


async def _ppc_resolve_direct(context, chat_id: int, tg_chat_id: int, s: dict):
    """Résoudre un duel PPC quand les deux joueurs ont choisi avant le timer."""
    j1_id   = s["j1_id"]
    j2_id   = s["j2_id"]
    j1_name = s["j1_name"]
    j2_name = s["j2_name"]
    mise    = s["mise"]
    c1      = s["j1_choice"]
    c2      = s["j2_choice"]

    e1 = PPC_CHOICES[c1]["emoji"]
    e2 = PPC_CHOICES[c2]["emoji"]

    if c1 == c2:
        result = "draw"
    elif PPC_CHOICES[c1]["bat"] == c2:
        result = "j1"
    else:
        result = "j2"

    async with AsyncSessionLocal() as session:
        if result == "draw":
            await _add_coins(session, j1_id, mise)
            await _add_coins(session, j2_id, mise)
            for uid, m in s["bets1_ids"].items():
                await _add_coins(session, uid, m)
            for uid, m in s["bets2_ids"].items():
                await _add_coins(session, uid, m)
        else:
            winner_id = j1_id if result == "j1" else j2_id
            await _add_coins(session, winner_id, mise * 2)

            bets_w_ids = s["bets1_ids"] if result == "j1" else s["bets2_ids"]
            bets_l_ids = s["bets2_ids"] if result == "j1" else s["bets1_ids"]
            total_w = sum(bets_w_ids.values())
            total_l = sum(bets_l_ids.values())

            for uid, m in bets_w_ids.items():
                share = m / total_w if total_w > 0 else 1
                await _add_coins(session, uid, int(m + total_l * share * 0.90))

    if result == "draw":
        outcome = (
            f"🤝 <b>ÉGALITÉ !</b>\n\n"
            f"🔴 {j1_name} : {e1} <b>{c1.upper()}</b>\n"
            f"🔵 {j2_name} : {e2} <b>{c2.upper()}</b>\n\n"
            f"💸 Tout le monde est remboursé !"
        )
    else:
        w_name   = j1_name if result == "j1" else j2_name
        w_choice = c1 if result == "j1" else c2
        l_choice = c2 if result == "j1" else c1
        w_e = e1 if result == "j1" else e2
        l_e = e2 if result == "j1" else e1

        bets_w = s["bets1"] if result == "j1" else s["bets2"]
        bets_l = s["bets2"] if result == "j1" else s["bets1"]
        paris_txt = ""
        if bets_w:
            paris_txt += "\n🏆 <b>Parieurs gagnants :</b>\n"
            for name, m in bets_w.items():
                paris_txt += f"  ✅ {name} — {_fmt(m)} {CURRENCY} misés\n"
        if bets_l:
            paris_txt += "\n💸 <b>Parieurs perdants :</b>\n"
            for name, m in bets_l.items():
                paris_txt += f"  ❌ {name} — {_fmt(m)} {CURRENCY} perdus\n"

        outcome = (
            f"🏆 <b>VICTOIRE DE {w_name.upper()} !</b>\n\n"
            f"🔴 {j1_name} : {e1} <b>{c1.upper()}</b>\n"
            f"🔵 {j2_name} : {e2} <b>{c2.upper()}</b>\n\n"
            f"⚔️ {w_choice.upper()} bat {l_choice.upper()} !\n"
            f"💰 {w_name} remporte <b>{_fmt(mise * 2)} {CURRENCY}</b> !"
            f"{paris_txt}"
        )

    try:
        await context.bot.edit_message_text(
            chat_id=tg_chat_id,
            message_id=s["msg_id"],
            text=f"✂️ <b>RÉSULTAT DU DUEL</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n{outcome}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"PPC direct resolve error: {e}")
