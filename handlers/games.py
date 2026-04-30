"""
handlers/games.py — Jeux : Crash (multijoueur inline), Apple of Fortune, Roue de Fortune
"""
import asyncio
import random
from datetime import date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database.db import AsyncSessionLocal, get_user, deduct_for_game, add_coins_smart
from utils.helpers import ensure_user, mention

import sqlalchemy as sa

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _fmt(n: int) -> str:
    return f"{int(n):,}".replace(",", " ")

async def _get_balance(session, user_id: int) -> int:
    user = await get_user(session, user_id)
    return int(user.coins) if user else 0

async def _add_coins(session, user_id: int, amount: int):
    await session.execute(
        sa.text("UPDATE users SET coins = coins + :a WHERE user_id = :uid"),
        {"a": amount, "uid": user_id}
    )
    await session.commit()

# ═══════════════════════════════════════════════════════════════════════════════
# CRASH — Multijoueur avec bouton inline Cash Out
# ═══════════════════════════════════════════════════════════════════════════════

crash_games:     dict = {}
crash_phase:     dict = {}
crash_lobby_msg: dict = {}
crash_live_msg:  dict = {}

LOBBY_SECONDS = 20
TICK_INTERVAL = 1.5


def _gen_crash_point() -> float:
    if random.random() < 0.05:
        return 1.0
    crash = 0.99 / (1 - random.random() * 0.95)
    return round(min(crash, 100.0), 2)


def _lobby_text(chat_id: int, seconds_left: int) -> str:
    players = crash_games.get(chat_id, {})
    lines = []
    total = 0
    for d in players.values():
        lines.append(f"  • {d['first_name']} — <b>{_fmt(d['mise'])} {CURRENCY}</b>")
        total += d["mise"]
    body = "\n".join(lines) if lines else "  En attente de joueurs..."
    return (
        f"🚀 <b>CRASH — Phase de mise</b>\n\n"
        f"👥 <b>{len(players)} joueur(s)</b>  |  Pot : <b>{_fmt(total)} {CURRENCY}</b>\n\n"
        f"{body}\n\n"
        f"⏳ Démarrage dans <b>{seconds_left}s</b>\n"
        f"Rejoins : <code>/crash &lt;mise&gt;</code>"
    )


def _running_text(chat_id: int, multiplier: float) -> str:
    players = crash_games.get(chat_id, {})
    lines = []
    for d in players.values():
        if d["cashed_out"]:
            gain   = int(d["mise"] * d["cashout_mult"])
            profit = gain - d["mise"]
            sign   = f"+{_fmt(profit)}" if profit >= 0 else _fmt(profit)
            lines.append(f"✅ {d['first_name']} — x{d['cashout_mult']:.2f}  →  <b>{sign} {CURRENCY}</b>")
        else:
            potential = int(d["mise"] * multiplier)
            lines.append(f"⏳ {d['first_name']} — {_fmt(d['mise'])} {CURRENCY}  →  <i>{_fmt(potential)} {CURRENCY}</i>")
    body = "\n".join(lines) if lines else "—"
    return (
        f"🚀 <b>CRASH EN COURS</b>\n\n"
        f"📈 <b>x{multiplier:.2f}</b>\n\n"
        f"{body}\n\n"
        f"⚡ Appuie sur <b>Cash Out</b> pour encaisser !"
    )


def _running_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("💰 Cash Out !", callback_data="crash:cashout")
    ]])


def _result_text(chat_id: int, crash_point: float) -> str:
    players = crash_games.get(chat_id, {})
    winners, losers = [], []
    for d in players.values():
        if d["cashed_out"]:
            gain   = int(d["mise"] * d["cashout_mult"])
            profit = gain - d["mise"]
            winners.append(f"✅ {d['first_name']} — x{d['cashout_mult']:.2f}  →  <b>+{_fmt(profit)} {CURRENCY}</b>")
        else:
            losers.append(f"💀 {d['first_name']}  →  <b>-{_fmt(d['mise'])} {CURRENCY}</b>")
    body = "\n".join(winners + losers) or "—"
    return (
        f"💥 <b>CRASH à x{crash_point:.2f} !</b>\n\n"
        f"{body}\n\n"
        f"Nouvelle partie : <code>/crash &lt;mise&gt;</code>"
    )


async def crash_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/crash <mise> — Rejoindre le prochain Crash"""
    user    = update.effective_user
    chat_id = update.effective_chat.id
    await ensure_user(user)

    if not context.args:
        return await update.message.reply_text(
            "🚀 <b>Crash</b>\n\n"
            "Le multiplicateur monte… jusqu'au crash !\n"
            "Encaisse avant l'explosion pour gagner.\n\n"
            "Usage : <code>/crash &lt;mise&gt;</code>\n"
            "Exemple : <code>/crash 50000</code>",
            parse_mode=ParseMode.HTML
        )

    try:
        mise = int(context.args[0].replace(" ", "").replace("_", ""))
    except ValueError:
        return await update.message.reply_text("❌ Mise invalide.")

    if mise < 1000:
        return await update.message.reply_text(
            "❌ Mise minimum : <b>1 000 $</b>", parse_mode=ParseMode.HTML
        )

    if crash_phase.get(chat_id) == "running":
        return await update.message.reply_text(
            "⚠️ Une partie est en cours, attends la prochaine !",
            parse_mode=ParseMode.HTML
        )

    if chat_id not in crash_games:
        crash_games[chat_id] = {}

    if user.id in crash_games[chat_id]:
        return await update.message.reply_text(
            "⚠️ Tu es déjà inscrit pour cette partie !", parse_mode=ParseMode.HTML
        )

    async with AsyncSessionLocal() as session:
        balance = await _get_balance(session, user.id)
        if balance < mise:
            return await update.message.reply_text(
                f"❌ Solde insuffisant. Tu as <b>{_fmt(balance)} {CURRENCY}</b>",
                parse_mode=ParseMode.HTML
            )
        await _add_coins(session, user.id, -mise)

    crash_games[chat_id][user.id] = {
        "mise":         mise,
        "cashed_out":   False,
        "cashout_mult": None,
        "current_mult": 1.0,
        "first_name":   user.first_name,
        "mention":      mention(user),
        "lock":         asyncio.Lock(),
    }

    if crash_phase.get(chat_id) != "lobby":
        crash_phase[chat_id] = "lobby"
        msg = await update.message.reply_text(
            _lobby_text(chat_id, LOBBY_SECONDS),
            parse_mode=ParseMode.HTML
        )
        crash_lobby_msg[chat_id] = msg
        asyncio.create_task(_run_lobby(context, chat_id))
    else:
        try:
            await crash_lobby_msg[chat_id].edit_text(
                _lobby_text(chat_id, -1),
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
        await update.message.reply_text(
            f"✅ {mention(user)} rejoint le crash avec <b>{_fmt(mise)} {CURRENCY}</b> !",
            parse_mode=ParseMode.HTML
        )


async def _run_lobby(context, chat_id: int):
    for remaining in range(LOBBY_SECONDS, 0, -5):
        await asyncio.sleep(5)
        if crash_phase.get(chat_id) != "lobby":
            return
        try:
            await crash_lobby_msg[chat_id].edit_text(
                _lobby_text(chat_id, max(remaining - 5, 0)),
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    if not crash_games.get(chat_id):
        crash_phase.pop(chat_id, None)
        return

    crash_phase[chat_id] = "running"
    asyncio.create_task(_run_crash(context, chat_id))


async def _run_crash(context, chat_id: int):
    crash_point = _gen_crash_point()
    multiplier  = 1.0

    try:
        live_msg = await context.bot.send_message(
            chat_id,
            _running_text(chat_id, multiplier),
            parse_mode=ParseMode.HTML,
            reply_markup=_running_keyboard()
        )
        crash_live_msg[chat_id] = live_msg
    except Exception:
        _cleanup_crash(chat_id)
        return

    while multiplier < crash_point:
        await asyncio.sleep(TICK_INTERVAL)
        multiplier = round(multiplier + 0.06 + multiplier * 0.04, 2)
        multiplier = min(multiplier, crash_point)

        for uid, d in crash_games[chat_id].items():
            if not d["cashed_out"]:
                d["current_mult"] = multiplier

        try:
            await live_msg.edit_text(
                _running_text(chat_id, multiplier),
                parse_mode=ParseMode.HTML,
                reply_markup=_running_keyboard()
            )
        except Exception:
            pass

    try:
        await live_msg.edit_text(
            _result_text(chat_id, crash_point),
            parse_mode=ParseMode.HTML,
            reply_markup=None
        )
    except Exception:
        try:
            await context.bot.send_message(
                chat_id,
                _result_text(chat_id, crash_point),
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    _cleanup_crash(chat_id)


def _cleanup_crash(chat_id: int):
    crash_games.pop(chat_id, None)
    crash_phase.pop(chat_id, None)
    crash_lobby_msg.pop(chat_id, None)
    crash_live_msg.pop(chat_id, None)


async def crash_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user    = query.from_user
    chat_id = query.message.chat_id

    await query.answer()

    players = crash_games.get(chat_id, {})

    if user.id not in players:
        return await query.answer("❌ Tu n'as pas de mise active !", show_alert=True)

    d = players[user.id]

    if crash_phase.get(chat_id) != "running":
        return await query.answer("⚠️ La partie n'est pas encore lancée.", show_alert=True)

    async with d["lock"]:
        if d["cashed_out"]:
            return await query.answer("⚠️ Tu as déjà encaissé !", show_alert=True)

        d["cashed_out"]   = True
        d["cashout_mult"] = d["current_mult"]

    mult   = d["cashout_mult"]
    gain   = int(d["mise"] * mult)
    profit = gain - d["mise"]

    async with AsyncSessionLocal() as session:
        await _add_coins(session, user.id, gain)

    sign = f"+{_fmt(profit)}" if profit >= 0 else _fmt(profit)
    await query.answer(
        f"💰 Cash Out à x{mult:.2f} ! Gain : {_fmt(gain)} {CURRENCY} ({sign} {CURRENCY})",
        show_alert=True
    )

    try:
        await crash_live_msg[chat_id].edit_text(
            _running_text(chat_id, mult),
            parse_mode=ParseMode.HTML,
            reply_markup=_running_keyboard()
        )
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# APPLE OF FORTUNE — 5 colonnes × 10 niveaux
# 🍏 = pomme verte (bonne) | 🍎 = pomme rouge (bombe/perdu)
# Mises : 50 000 $ minimum — 5 000 000 $ maximum
# ═══════════════════════════════════════════════════════════════════════════════

apple_sessions: dict = {}

APPLE_COLS   = 5
APPLE_LEVELS = 10
APPLE_MIN    = 50_000
APPLE_MAX    = 10_000_000                 # Mise max 10M

# Multiplicateurs ajustés pour la difficulté augmentée
# Niveaux 1-2 : 3/5 sûres (60%) | Niveaux 3-5 : 2/5 sûres (40%) | Niveaux 6-10 : 1/5 sûre (20%)
APPLE_MULTS = {
    1:  1.50,
    2:  2.10,
    3:  3.20,
    4:  4.80,
    5:  7.00,
    6:  12.00,
    7:  22.00,
    8:  45.00,
    9:  100.00,
    10: 500.00,
}

def _apple_bombs(level: int) -> int:
    if level <= 2:  return 2   # 3 sûres / 5
    if level <= 5:  return 3   # 2 sûres / 5
    if level <= 8:  return 4   # 1 sûre  / 5
    return 4                   # Niveaux 9-10 → 4 bombes / 5 (1 seule issue de sortie !)

def _apple_gen_row(level: int) -> list:
    n_bombs = _apple_bombs(level)
    row     = [False] * APPLE_COLS
    for pos in random.sample(range(APPLE_COLS), n_bombs):
        row[pos] = True
    return row

def _apple_keyboard(session: dict) -> InlineKeyboardMarkup:
    level    = session["level"]
    revealed = session["row_revealed"]
    row      = session["row_bombs"]

    buttons = []
    for i in range(APPLE_COLS):
        if revealed[i]:
            # Pomme rouge = bombe, pomme verte = bonne
            label = "🍎" if row[i] else "🍏"
            buttons.append(InlineKeyboardButton(label, callback_data=f"apple:done:{level}:{i}"))
        else:
            buttons.append(InlineKeyboardButton("🍏", callback_data=f"apple:pick:{level}:{i}"))

    rows = [buttons]

    if session["level"] > 1 or session.get("passed_one"):
        mult = APPLE_MULTS.get(session["level"] - 1, 1.0)
        gain = int(session["mise"] * mult)
        rows.append([InlineKeyboardButton(
            f"💰 Encaisser x{mult:.2f} → {_fmt(gain)} {CURRENCY}",
            callback_data="apple:cashout"
        )])

    return InlineKeyboardMarkup(rows)

def _apple_danger_emoji(level: int) -> str:
    n_bombs = _apple_bombs(level)
    safe    = APPLE_COLS - n_bombs
    return "🟢" * safe + "🔴" * n_bombs

def _apple_status(session: dict) -> str:
    level     = session["level"]
    n_bombs   = _apple_bombs(level)
    safe      = APPLE_COLS - n_bombs
    mult_next = APPLE_MULTS.get(level, "?")
    prev_mult = APPLE_MULTS.get(level - 1, 1.0) if level > 1 else 1.0
    cashout_gain = int(session["mise"] * prev_mult) if level > 1 else 0

    bar = _apple_danger_emoji(level)

    lines = [
        f"🍏 <b>APPLE OF FORTUNE</b>",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📊 Niveau  <b>{level} / {APPLE_LEVELS}</b>",
        f"💵 Mise    <b>{_fmt(session['mise'])} {CURRENCY}</b>",
        f"",
        f"Risque ligne  {bar}",
        f"🍎 Pommes rouges (pièges) : <b>{n_bombs}</b>",
        f"🍏 Pommes vertes (sûres)  : <b>{safe}</b>",
        f"",
        f"🎯 Multiplicateur si tu passes : <b>x{mult_next}</b>",
    ]
    if level > 1:
        lines.append(f"💰 Encaisser maintenant : <b>{_fmt(cashout_gain)} {CURRENCY}</b>")
    lines.append(f"\n👇 <b>Choisis une pomme !</b>")
    return "\n".join(lines)


async def apple_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/apple <mise> — Apple of Fortune (mise libre)"""
    user = update.effective_user
    await ensure_user(user)

    if not context.args:
        table = "\n".join(
            f"  Niveau {lvl:2d} — <b>x{mult:.2f}</b>  ({APPLE_COLS - _apple_bombs(lvl)}/5 sûres  |  {_apple_bombs(lvl)} pièges)"
            for lvl, mult in APPLE_MULTS.items()
        )
        return await update.message.reply_text(
            "🍏 <b>Apple of Fortune</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Gravis 10 niveaux en choisissant une pomme parmi 5.\n"
            "🍏 Pomme verte = tu passes au niveau suivant\n"
            "🍎 Pomme rouge = BOOM, tu perds tout !\n"
            "Les pièges augmentent à chaque palier.\n\n"
            f"<b>Mise minimum :</b> {_fmt(APPLE_MIN)} {CURRENCY}\n\n"
            f"<b>Table des gains :</b>\n{table}\n\n"
            "Usage : <code>/apple &lt;mise&gt;</code>\n"
            "Ex : <code>/apple 100000</code>",
            parse_mode=ParseMode.HTML
        )

    try:
        mise = int(context.args[0].replace(" ", "").replace("_", ""))
    except ValueError:
        return await update.message.reply_text("❌ Mise invalide.")

    if mise < APPLE_MIN:
        return await update.message.reply_text(
            f"❌ Mise minimum : <b>{_fmt(APPLE_MIN)} {CURRENCY}</b>", parse_mode=ParseMode.HTML
        )

    if user.id in apple_sessions:
        return await update.message.reply_text("⚠️ Tu as déjà une partie en cours !", parse_mode=ParseMode.HTML)

    async with AsyncSessionLocal() as session:
        balance = await _get_balance(session, user.id)
        if balance < mise:
            return await update.message.reply_text(
                f"❌ Solde insuffisant. Tu as <b>{_fmt(balance)} {CURRENCY}</b>", parse_mode=ParseMode.HTML
            )
        await _add_coins(session, user.id, -mise)

    apple_sessions[user.id] = {
        "mise":         mise,
        "level":        1,
        "row_bombs":    _apple_gen_row(1),
        "row_revealed": [False] * APPLE_COLS,
        "row_picked":   False,
        "passed_one":   False,
        "chat_id":      update.effective_chat.id,
        "lock":         asyncio.Lock(),
    }

    sess     = apple_sessions[user.id]
    keyboard = _apple_keyboard(sess)
    await update.message.reply_text(
        _apple_status(sess),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )


async def apple_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user  = query.from_user
    await query.answer()

    parts  = query.data.split(":")
    action = parts[1]

    if user.id not in apple_sessions:
        await query.edit_message_reply_markup(reply_markup=None)
        return

    sess = apple_sessions[user.id]

    # ── Cash Out ──────────────────────────────────────────────────────────────
    if action == "cashout":
        if sess["level"] <= 1 and not sess["passed_one"]:
            return await query.answer("❌ Passe au moins un niveau d'abord !", show_alert=True)
        mult   = APPLE_MULTS.get(sess["level"] - 1, 1.0)
        gain   = int(sess["mise"] * mult)
        profit = gain - sess["mise"]
        async with AsyncSessionLocal() as session:
            await _add_coins(session, user.id, gain)
        del apple_sessions[user.id]
        await query.edit_message_text(
            f"💰 <b>Encaissé !</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Niveau atteint : <b>{sess['level'] - 1} / {APPLE_LEVELS}</b>\n"
            f"Multiplicateur : <b>x{mult:.2f}</b>\n"
            f"Gain : <b>{_fmt(gain)} {CURRENCY}</b>  (<b>+{_fmt(profit)} {CURRENCY}</b>)\n\n"
            "Bien joué, tu as su t'arrêter ! 🍏",
            parse_mode=ParseMode.HTML
        )
        return

    if action == "done":
        return

    # ── Choisir une pomme ────────────────────────────────────────────────────
    if action == "pick":
        btn_level = int(parts[2])
        idx       = int(parts[3])

        if btn_level != sess["level"]:
            return

        if sess["row_picked"]:
            return

        async with sess["lock"]:
            if sess["row_picked"]:
                return

            sess["row_picked"]        = True
            sess["row_revealed"][idx] = True

            # 🍎 POMME ROUGE = BOMBE
            if sess["row_bombs"][idx]:
                for i, is_bomb in enumerate(sess["row_bombs"]):
                    if is_bomb:
                        sess["row_revealed"][i] = True
                keyboard = _apple_keyboard(sess)
                del apple_sessions[user.id]
                await query.edit_message_text(
                    f"🍎 <b>POMME EMPOISONNÉE !</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Niveau {sess['level']} — Case {idx + 1} était un piège !\n\n"
                    f"💸 Tu as perdu <b>{_fmt(sess['mise'])} {CURRENCY}</b>\n\n"
                    "Retente ta chance avec /apple 🍏",
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard
                )
                return

            # 🍏 POMME VERTE = NIVEAU PASSÉ
            current_level = sess["level"]
            mult          = APPLE_MULTS[current_level]
            gain_now      = int(sess["mise"] * mult)

            if current_level == APPLE_LEVELS:
                async with AsyncSessionLocal() as session:
                    await _add_coins(session, user.id, gain_now)
                del apple_sessions[user.id]
                await query.edit_message_text(
                    f"🏆 <b>VICTOIRE ABSOLUE !</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Tu as gravi les <b>{APPLE_LEVELS} niveaux</b> !\n"
                    f"x{mult:.2f}  →  <b>{_fmt(gain_now)} {CURRENCY}</b> 🎉🍏",
                    parse_mode=ParseMode.HTML
                )
                return

            next_level = current_level + 1
            sess["level"]        = next_level
            sess["row_bombs"]    = _apple_gen_row(next_level)
            sess["row_revealed"] = [False] * APPLE_COLS
            sess["row_picked"]   = False
            sess["passed_one"]   = True

        keyboard = _apple_keyboard(sess)
        await query.edit_message_text(
            f"🍏 <b>Bonne pomme !</b>  Niveau <b>{current_level}</b> passé — x{mult:.2f}\n\n"
            + _apple_status(sess),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )


# ═══════════════════════════════════════════════════════════════════════════════
# ROUE DE FORTUNE — Segments variés : argent fixe, idem, multiplicateurs
# ═══════════════════════════════════════════════════════════════════════════════
#
# Chaque segment : (label, type, valeur, poids)
#   type "mult"  → gain = mise × valeur
#   type "fixed" → gain = valeur (montant fixe indépendant de la mise)
#   type "idem"  → gain = mise (remboursement exact)
#   type "ruine" → gain = 0
# ──────────────────────────────────────────────────────────────────────────────

WHEEL_SEGMENTS = [
    # label                         type      valeur       poids
    ("💀 Ruine totale",            "ruine",   0,            6),   # +2
    ("☠️ Malédiction x0.1",       "mult",    0.1,          10),  # +4
    ("😭 x0.2",                    "mult",    0.2,           9),  # nouveau
    ("😞 x0.3",                    "mult",    0.3,          10),  # +2
    ("💸 x0.4",                    "mult",    0.4,           9),  # nouveau
    ("😐 x0.5",                    "mult",    0.5,           9),  # -1
    ("🔄 IDEM",                    "idem",    0,            10),  # -2
    ("🙂 x0.8",                    "mult",    0.8,           9),
    ("💵 +50 000 $",               "fixed",   50_000,        6),  # nouveau
    ("💰 x1.2",                    "mult",    1.2,           8),  # nouveau
    ("💵 +200 000 $",              "fixed",   200_000,       6),
    ("💰 x1.5",                    "mult",    1.5,          10),
    ("🎁 +500 000 $",              "fixed",   500_000,       4),
    ("🤑 x2.0",                    "mult",    2.0,           7),
    ("🎯 x3.0",                    "mult",    3.0,           5),
    ("💵 +1 000 000 $",            "fixed",   1_000_000,     3),
    ("⭐ x5.0",                    "mult",    5.0,           3),
    ("🔥 x10.0",                   "mult",    10.0,          2),
    ("🌟 MÉGA CHANCE x15.0",       "mult",    15.0,          1),
    ("💎 JACKPOT x25.0",           "mult",    25.0,          1),
]


# ─── SYSTÈME DE MOOD ─────────────────────────────────────────────────────────
# Le mood change à chaque heure. La seed est basée sur la date+heure
# + un salt aléatoire fixé au démarrage du process → imprévisible mais stable
# sur toute la durée d'une heure.

import os as _os
from config import CURRENCY
_MOOD_SALT = int.from_bytes(_os.urandom(4), "big")

MOODS = {
    # mood            : (multiplicateur_malchance, multiplicateur_chance, label_affichage)
    "impitoyable"  : (8.0, 0.05, "💀 Mode IMPITOYABLE — La roue veut ta ruine."),
    "tres_mechant" : (3.5, 0.2,  "😈 La roue est TRÈS MÉCHANTE ce soir..."),
    "mechant"      : (2.0, 0.5,  "😤 La roue est de mauvaise humeur."),
    "normal"       : (1.0, 1.0,  "😐 La roue est neutre."),
    "facile"       : (0.5, 2.0,  "😊 La roue est généreuse !"),
    "tres_facile"  : (0.2, 3.5,  "🤑 La roue est EN FEU ce soir !"),
}

# Probabilités d'apparition de chaque mood par heure
MOOD_WEIGHTS = {
    "impitoyable"  : 0,   # jamais aléatoire — admin only
    "tres_mechant" : 20,
    "mechant"      : 25,
    "normal"       : 30,
    "facile"       : 15,
    "tres_facile"  : 10,
}


_MOOD_OVERRIDE: str | None = None  # None = aléatoire, sinon clé forcée par admin


def _current_mood() -> tuple[str, tuple]:
    """Retourne (mood_key, mood_data) pour l'heure courante."""
    if _MOOD_OVERRIDE and _MOOD_OVERRIDE in MOODS:
        return _MOOD_OVERRIDE, MOODS[_MOOD_OVERRIDE]
    from datetime import datetime
    now   = datetime.utcnow()
    seed  = now.year * 1000000 + now.month * 10000 + now.day * 100 + now.hour
    seed  = (seed ^ _MOOD_SALT) & 0xFFFFFFFF
    rng   = random.Random(seed)
    keys  = list(MOOD_WEIGHTS.keys())
    weights = [MOOD_WEIGHTS[k] for k in keys]
    mood_key = rng.choices(keys, weights=weights, k=1)[0]
    return mood_key, MOODS[mood_key]


async def _set_mood_direct(update: Update, mood_key):
    import handlers.games as _self
    from handlers.admin import is_admin
    if not await is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Réservé aux admins.")
    _self._MOOD_OVERRIDE = mood_key
    if mood_key is None:
        await update.message.reply_text("🎲 Roue remise en mode <b>aléatoire</b>.", parse_mode="HTML")
    else:
        _, (_, _, label) = _current_mood()
        await update.message.reply_text(f"✅ {label}", parse_mode="HTML")

async def mood_facile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_mood_direct(update, "facile")

async def mood_normal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_mood_direct(update, "normal")

async def mood_difficile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_mood_direct(update, "mechant")

async def mood_impitoyable_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_mood_direct(update, "impitoyable")

async def mood_auto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_mood_direct(update, None)

async def setmood_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import handlers.games as _self
    from handlers.admin import is_admin
    if not await is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Réservé aux admins.")
    _, (_, _, label) = _current_mood()
    override_info = f"🔒 Forcé : <b>{_self._MOOD_OVERRIDE}</b>" if _self._MOOD_OVERRIDE else "🎲 Aléatoire"
    await update.message.reply_text(
        f"🎡 <b>Mood actuel :</b> {label}\n{override_info}\n\n"
        f"Commandes : /facile · /normal · /difficile · /impitoyable · /moodauto",
        parse_mode="HTML"
    )


def _spin_wheel() -> tuple:
    """Retourne (label, type, valeur) en tenant compte du mood actuel."""
    _, (bad_mult, good_mult, _) = _current_mood()

    # Recalculer les poids selon le mood
    adjusted = []
    for label, kind, val, weight in WHEEL_SEGMENTS:
        if kind == "ruine" or (kind == "mult" and isinstance(val, float) and val < 1.0):
            # Case de malchance → amplifiée si méchant
            new_w = max(1, int(weight * bad_mult))
        elif kind in ("mult", "fixed") and (
            (kind == "fixed") or (isinstance(val, float) and val >= 1.5)
        ):
            # Case de chance → amplifiée si facile
            new_w = max(1, int(weight * good_mult))
        else:
            new_w = weight
        adjusted.append((label, kind, val, new_w))

    total = sum(s[3] for s in adjusted)
    r     = random.uniform(0, total)
    cum   = 0
    for label, kind, val, w in adjusted:
        cum += w
        if r <= cum:
            return label, kind, val
    last = adjusted[-1]
    return last[0], last[1], last[2]


def _wheel_result_text(user_mention: str, mise: int, label: str, kind: str, val) -> tuple[str, int]:
    """Calcule le gain et génère le texte de résultat. Retourne (texte, gain)."""
    ROUE_GAIN_CAP = 100_000_000  # Plafond gain roue : 100M
    if kind == "ruine":
        gain   = 0
        profit = -mise
        emoji  = "💀"
        title  = "RUINE TOTALE !"
    elif kind == "idem":
        gain   = mise
        profit = 0
        emoji  = "🔄"
        title  = "IDEM — Remboursé !"
    elif kind == "fixed":
        gain   = min(int(val), ROUE_GAIN_CAP)
        profit = gain - mise
        emoji  = "💵"
        title  = f"{label.split(' ', 1)[1]} fixe !"
    else:  # mult
        gain   = min(int(mise * val), ROUE_GAIN_CAP)
        profit = gain - mise
        emoji  = "🎡"
        title  = f"{label} !"

    mise_str   = _fmt(mise)
    gain_str   = _fmt(gain)
    profit_str = (f"+{_fmt(profit)}" if profit >= 0 else _fmt(profit))

    profit_icon = "📈" if profit >= 0 else "📉"
    text = (
        f"{emoji} <b>{title}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎡 Vous êtes tombé sur : <b>{label}</b>\n\n"
        f"👤 {user_mention}\n\n"
        f"💵 Mise de départ : <b>{mise_str} {CURRENCY}</b>\n"
        f"🏆 Vos revenus sont : <b>{gain_str} {CURRENCY}</b>\n"
        f"{profit_icon} Bilan net : <b>{profit_str} {CURRENCY}</b>"
    )
    return text, gain


async def roue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/roue <mise>"""
    user = update.effective_user
    await ensure_user(user)

    if not context.args:
        segments_display = "\n".join(
            f"  {label}  <i>(poids {w})</i>"
            for label, _, _, w in WHEEL_SEGMENTS
        )
        return await update.message.reply_text(
            "🎡 <b>Roue de Fortune</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Tourne la roue et tente ta chance !\n"
            "Multiplicateurs, gains fixes, IDEM, JACKPOT… et la Ruine.\n\n"
            f"<b>Segments :</b>\n{segments_display}\n\n"
            "Usage : <code>/roue &lt;mise&gt;</code>",
            parse_mode=ParseMode.HTML
        )

    try:
        mise = int(context.args[0].replace(" ", "").replace("_", ""))
    except ValueError:
        return await update.message.reply_text("❌ Mise invalide.")

    if mise < 1000:
        return await update.message.reply_text("❌ Mise minimum : <b>1 000 $</b>", parse_mode=ParseMode.HTML)

    async with AsyncSessionLocal() as session:
        balance = await _get_balance(session, user.id)
        if balance < mise:
            return await update.message.reply_text(
                f"❌ Solde insuffisant. Tu as <b>{_fmt(balance)} {CURRENCY}</b>", parse_mode=ParseMode.HTML
            )
        await _add_coins(session, user.id, -mise)

    # Mood actuel
    mood_key, (_, _, mood_label) = _current_mood()

    # Calculer le résultat AVANT l'animation pour éviter tout blocage
    label, kind, val = _spin_wheel()
    result_text, gain = _wheel_result_text(mention(user), mise, label, kind, val)

    animation_frames = [
        f"🎡 <b>La roue est lancée...</b>\n<i>{mood_label}</i>",
        "🎡 ⠋ <i>Elle tourne à pleine vitesse !</i>",
        "🎡 ⠙ <i>Ça s'emballe...</i>",
        "🎡 ⠹ <i>La roue ralentit...</i>",
        "🎡 ⠸ <i>Encore un tour...</i>",
        "🎡 ⠼ <i>Presque là...</i>",
        "🎡 ⠴ <i>Elle s'arrête...</i>",
    ]

    msg = await update.message.reply_text(animation_frames[0], parse_mode=ParseMode.HTML)
    for frame in animation_frames[1:]:
        await asyncio.sleep(0.6)
        try:
            await msg.edit_text(frame, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    await asyncio.sleep(0.8)

    # Créditer les gains AVANT d'afficher le verdict
    async with AsyncSessionLocal() as session:
        await _add_coins(session, user.id, gain)

    # Afficher le verdict — toujours en reply_text pour éviter le blocage
    await asyncio.sleep(0.4)
    await update.message.reply_text(result_text, parse_mode=ParseMode.HTML)

    # Supprimer le message d'animation
    try:
        await msg.delete()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# REBET — Quitte ou double avec boutons Récupérer / Remiser
# ═══════════════════════════════════════════════════════════════════════════════

# Stockage en mémoire : rebet_sessions[chat_id][user_id] = {mise, gains, round}
rebet_sessions: dict = {}

MIN_REBET = 5000


def _rebet_keyboard(chat_id: int, user_id: int, gains: int) -> InlineKeyboardMarkup:
    next_win = gains * 2
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"💰 Récupérer {_fmt(gains)} {CURRENCY}", callback_data=f"rebet:cash:{chat_id}:{user_id}"),
        InlineKeyboardButton(f"🎲 Remiser → {_fmt(next_win)} {CURRENCY}", callback_data=f"rebet:double:{chat_id}:{user_id}"),
    ]])


def _rebet_text(first_name: str, mise_initiale: int, gains: int, round_num: int) -> str:
    multiplier = gains / mise_initiale if mise_initiale else 1
    next_win = gains * 2
    return (
        f"🎲 <b>REBET — {first_name}</b>\n\n"
        f"🪙 Mise de départ : <b>{_fmt(mise_initiale)} {CURRENCY}</b>\n"
        f"📈 Multiplicateur : <b>x{multiplier:.1f}</b>\n"
        f"💵 Gains actuels : <b>{_fmt(gains)} {CURRENCY}</b>\n"
        f"⚡ Prochain gain : <b>{_fmt(next_win)} {CURRENCY}</b>\n"
        f"🔄 Tour n°{round_num}\n\n"
        f"Que veux-tu faire ?"
    )


async def rebet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user)

    if not context.args:
        return await update.message.reply_text(
            f"Usage : <code>/rebet &lt;mise&gt;</code>\n"
            f"Mise minimum : <b>{_fmt(MIN_REBET)} {CURRENCY}</b>\n\n"
            "🎲 Quitte ou double — récupère ou remise !",
            parse_mode=ParseMode.HTML
        )

    try:
        mise = int(context.args[0].replace(" ", "").replace("_", ""))
    except ValueError:
        return await update.message.reply_text("❌ Mise invalide.")

    if mise < MIN_REBET:
        return await update.message.reply_text(
            f"❌ Mise minimum : <b>{_fmt(MIN_REBET)} {CURRENCY}</b>", parse_mode=ParseMode.HTML
        )

    chat_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        result = await deduct_for_game(session, user.id, mise)

    if result == "insufficient":
        async with AsyncSessionLocal() as session:
            bal = await _get_balance(session, user.id)
        return await update.message.reply_text(
            f"❌ Solde insuffisant. Tu as <b>{_fmt(bal)} {CURRENCY}</b>", parse_mode=ParseMode.HTML
        )

    # Initialiser la session
    if chat_id not in rebet_sessions:
        rebet_sessions[chat_id] = {}

    rebet_sessions[chat_id][user.id] = {
        "mise": mise,
        "gains": mise,
        "round": 1,
        "first_name": user.first_name,
    }

    await update.message.reply_text(
        _rebet_text(user.first_name, mise, mise, 1),
        parse_mode=ParseMode.HTML,
        reply_markup=_rebet_keyboard(chat_id, user.id, mise),
    )


async def rebet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    # format : rebet:action:chat_id:user_id
    if len(parts) != 4:
        return

    _, action, chat_id_str, user_id_str = parts
    chat_id = int(chat_id_str)
    user_id = int(user_id_str)

    # Vérifier que c'est bien le bon joueur
    if query.from_user.id != user_id:
        return await query.answer("❌ Ce n'est pas ton jeu !", show_alert=True)

    session_data = rebet_sessions.get(chat_id, {}).get(user_id)
    if not session_data:
        return await query.edit_message_text("⌛ Session expirée. Relance avec /rebet.")

    if action == "cash":
        # Le joueur récupère ses gains
        gains = session_data["gains"]
        mise  = session_data["mise"]
        rnd   = session_data["round"]
        del rebet_sessions[chat_id][user_id]

        async with AsyncSessionLocal() as session:
            await add_coins_smart(session, user_id, gains)

        profit = gains - mise
        sign   = f"+{_fmt(profit)}" if profit >= 0 else _fmt(profit)
        await query.edit_message_text(
            f"💰 <b>Gains récupérés !</b>\n\n"
            f"🪙 Mise de départ : <b>{_fmt(mise)} {CURRENCY}</b>\n"
            f"✅ Gains encaissés : <b>{_fmt(gains)} {CURRENCY}</b>\n"
            f"📊 Profit net : <b>{sign} {CURRENCY}</b>\n"
            f"🔄 Tours joués : <b>{rnd}</b>",
            parse_mode=ParseMode.HTML,
        )

    elif action == "double":
        # Quitte ou double !
        win = random.random() < 0.5

        if win:
            session_data["gains"] *= 2
            session_data["round"] += 1
            mise  = session_data["mise"]
            gains = session_data["gains"]
            rnd   = session_data["round"]

            await query.edit_message_text(
                f"✅ <b>Gagné !</b> Tes gains doublent !\n\n"
                + _rebet_text(session_data["first_name"], mise, gains, rnd),
                parse_mode=ParseMode.HTML,
                reply_markup=_rebet_keyboard(chat_id, user_id, gains),
            )
        else:
            # Perdu — tout est perdu
            mise = session_data["mise"]
            rnd  = session_data["round"]
            del rebet_sessions[chat_id][user_id]

            await query.edit_message_text(
                f"💥 <b>PERDU !</b>\n\n"
                f"🪙 Mise de départ : <b>{_fmt(mise)} {CURRENCY}</b>\n"
                f"❌ Tu perds tout !\n"
                f"🔄 Tours joués : <b>{rnd}</b>\n\n"
                f"<i>Trop gourmand... 😅</i>",
                parse_mode=ParseMode.HTML,
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 💣 /mines — Jeu de mines style Stake
# ═══════════════════════════════════════════════════════════════════════════════

import math as _math

mines_sessions: dict = {}  # user_id -> session

MINES_GRID = 25  # 5x5
MINES_MISE_MAX = 50_000_000
MINES_MISE_MIN = 100


def _mines_multiplier(revealed: int, nb_mines: int) -> float:
    """Calcule le multiplicateur actuel basé sur les cases révélées et les mines."""
    safe = MINES_GRID - nb_mines
    if revealed >= safe or safe <= 0:
        return 0.0
    # Multiplicateur : monte progressivement selon la probabilité de survie
    # Basé sur la combinatoire réelle
    try:
        prob = 1.0
        for i in range(revealed):
            prob *= (safe - i) / (MINES_GRID - i)
        if prob <= 0:
            return 0.0
        return round((0.97 / prob), 2)  # 97% RTP
    except Exception:
        return 1.0


def _mines_keyboard(session: dict) -> InlineKeyboardMarkup:
    grid = session["grid"]       # liste 25 éléments: "safe" ou "mine"
    revealed = session["revealed"]  # set d'index révélés
    rows = []
    for r in range(5):
        row = []
        for c in range(5):
            idx = r * 5 + c
            if idx in revealed:
                emoji = "💎" if grid[idx] == "safe" else "💣"
                btn = InlineKeyboardButton(emoji, callback_data=f"mines:noop:{idx}")
            else:
                btn = InlineKeyboardButton("⬜", callback_data=f"mines:reveal:{idx}")
            row.append(btn)
        rows.append(row)

    current_mult = _mines_multiplier(len(revealed), session["nb_mines"])
    current_gain = int(session["mise"] * current_mult)

    rows.append([
        InlineKeyboardButton(
            f"💰 Encaisser {_fmt(current_gain)} {CURRENCY} (×{current_mult})",
            callback_data="mines:cashout:0"
        )
    ])
    return InlineKeyboardMarkup(rows)


def _mines_status(session: dict) -> str:
    revealed_count = len(session["revealed"])
    nb_mines = session["nb_mines"]
    current_mult = _mines_multiplier(revealed_count, nb_mines)
    current_gain = int(session["mise"] * current_mult)
    safe_left = MINES_GRID - nb_mines - revealed_count

    return (
        f"💣 <b>MINES</b> — {nb_mines} mine{'s' if nb_mines > 1 else ''} cachée{'s' if nb_mines > 1 else ''}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🪙 Mise : <b>{_fmt(session['mise'])} {CURRENCY}</b>\n"
        f"💎 Cases sûres révélées : <b>{revealed_count}</b>\n"
        f"🎯 Cases sûres restantes : <b>{safe_left}</b>\n"
        f"📈 Multiplicateur actuel : <b>×{current_mult}</b>\n"
        f"💰 Gain potentiel : <b>{_fmt(current_gain)} {CURRENCY}</b>\n\n"
        f"<i>Révèle des cases ou encaisse !</i>"
    )


async def mines_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user = update.effective_user
    args = context.args

    if not args or len(args) < 2:
        return await update.message.reply_text(
            "💣 <b>Mines</b>\n\n"
            "Usage : <code>/mines <mines> <mise></code>\n"
            "• <b>mines</b> : nombre de mines (1–24)\n"
            "• <b>mise</b> : montant à miser\n\n"
            "Ex : <code>/mines 3 5000</code> — 3 mines, mise 5 000\n\n"
            "Plus il y a de mines, plus les gains montent vite !\n"
            "Encaisse avant de tomber sur une bombe 💣",
            parse_mode=ParseMode.HTML,
        )

    try:
        nb_mines = int(args[0])
        mise = int(args[1])
    except ValueError:
        return await update.message.reply_text("❌ Usage : <code>/mines <mines> <mise></code>", parse_mode=ParseMode.HTML)

    if nb_mines < 1 or nb_mines > 24:
        return await update.message.reply_text("❌ Nombre de mines : entre <b>1</b> et <b>24</b>", parse_mode=ParseMode.HTML)

    if mise < MINES_MISE_MIN:
        return await update.message.reply_text(f"❌ Mise minimum : <b>{_fmt(MINES_MISE_MIN)} {CURRENCY}</b>", parse_mode=ParseMode.HTML)

    if mise > MINES_MISE_MAX:
        return await update.message.reply_text(f"❌ Mise maximum : <b>{_fmt(MINES_MISE_MAX)} {CURRENCY}</b>", parse_mode=ParseMode.HTML)

    if user.id in mines_sessions:
        return await update.message.reply_text("⚠️ Tu as déjà une partie de mines en cours ! Encaisse ou termine-la d'abord.")

    await ensure_user(user)

    async with AsyncSessionLocal() as session:
        bal = await _get_balance(session, user.id)
        if bal < mise:
            return await update.message.reply_text(
                f"❌ Solde insuffisant ! Tu as <b>{_fmt(bal)} {CURRENCY}</b>", parse_mode=ParseMode.HTML
            )
        await session.execute(
            sa.text("UPDATE users SET coins = coins - :a WHERE user_id = :uid"),
            {"a": mise, "uid": user.id},
        )
        await session.commit()

    # Générer la grille
    import random as _random
    grid = ["safe"] * MINES_GRID
    mine_positions = _random.sample(range(MINES_GRID), nb_mines)
    for pos in mine_positions:
        grid[pos] = "mine"

    mines_sessions[user.id] = {
        "user_id":   user.id,
        "chat_id":   update.effective_chat.id,
        "mise":      mise,
        "nb_mines":  nb_mines,
        "grid":      grid,
        "revealed":  set(),
        "msg_id":    None,
    }

    s = mines_sessions[user.id]
    msg = await update.message.reply_text(
        _mines_status(s),
        parse_mode=ParseMode.HTML,
        reply_markup=_mines_keyboard(s),
    )
    mines_sessions[user.id]["msg_id"] = msg.message_id


async def mines_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    parts = query.data.split(":")
    action = parts[1]

    s = mines_sessions.get(user.id)
    if not s:
        return await query.answer("❌ Aucune partie en cours.", show_alert=True)

    if action == "noop":
        return  # Case déjà révélée, rien à faire

    if action == "cashout":
        nb_revealed = len(s["revealed"])
        mult = _mines_multiplier(nb_revealed, s["nb_mines"])
        gain = int(s["mise"] * mult)

        del mines_sessions[user.id]

        async with AsyncSessionLocal() as session:
            if gain > 0:
                await _add_coins(session, user.id, gain)

        profit = gain - s["mise"]
        sign = f"+{_fmt(profit)}" if profit >= 0 else _fmt(profit)

        return await query.edit_message_text(
            f"💰 <b>Gains encaissés !</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🪙 Mise : <b>{_fmt(s['mise'])} {CURRENCY}</b>\n"
            f"💎 Cases révélées : <b>{nb_revealed}</b>\n"
            f"📈 Multiplicateur : <b>×{mult}</b>\n"
            f"💰 Gain : <b>{_fmt(gain)} {CURRENCY}</b>\n"
            f"📊 Profit net : <b>{sign} {CURRENCY}</b>",
            parse_mode=ParseMode.HTML,
        )

    if action == "reveal":
        idx = int(parts[2])
        if idx in s["revealed"]:
            return

        s["revealed"].add(idx)

        if s["grid"][idx] == "mine":
            # BOOM
            del mines_sessions[user.id]

            # Afficher toutes les mines
            grid = s["grid"]
            revealed = s["revealed"]
            rows = []
            for r in range(5):
                row = []
                for c in range(5):
                    i = r * 5 + c
                    if grid[i] == "mine":
                        row.append(InlineKeyboardButton("💣", callback_data="mines:noop:0"))
                    elif i in revealed:
                        row.append(InlineKeyboardButton("💎", callback_data="mines:noop:0"))
                    else:
                        row.append(InlineKeyboardButton("⬜", callback_data="mines:noop:0"))
                rows.append(row)

            return await query.edit_message_text(
                f"💥 <b>BOOM ! Tu as trouvé une mine !</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🪙 Mise perdue : <b>{_fmt(s['mise'])} {CURRENCY}</b>\n"
                f"💎 Cases révélées avant : <b>{len(s['revealed']) - 1}</b>\n\n"
                f"<i>Trop gourmand... 😅</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(rows),
            )

        # Case sûre — vérifier si toutes les cases sûres sont révélées
        safe_count = MINES_GRID - s["nb_mines"]
        if len(s["revealed"]) >= safe_count:
            # Gagné max !
            mult = _mines_multiplier(len(s["revealed"]), s["nb_mines"])
            gain = int(s["mise"] * mult)
            del mines_sessions[user.id]
            async with AsyncSessionLocal() as session:
                await _add_coins(session, user.id, gain)
            return await query.edit_message_text(
                f"🎊 <b>INCROYABLE ! Toutes les cases sûres révélées !</b>\n\n"
                f"📈 Multiplicateur : <b>×{mult}</b>\n"
                f"💰 Gain : <b>{_fmt(gain)} {CURRENCY}</b>",
                parse_mode=ParseMode.HTML,
            )

        # Continuer
        try:
            await query.edit_message_text(
                _mines_status(s),
                parse_mode=ParseMode.HTML,
                reply_markup=_mines_keyboard(s),
            )
        except Exception:
            pass
