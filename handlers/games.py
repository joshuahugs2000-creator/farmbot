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
        lines.append(f"  • {d['first_name']} — <b>{_fmt(d['mise'])} $</b>")
        total += d["mise"]
    body = "\n".join(lines) if lines else "  En attente de joueurs..."
    return (
        f"🚀 <b>CRASH — Phase de mise</b>\n\n"
        f"👥 <b>{len(players)} joueur(s)</b>  |  Pot : <b>{_fmt(total)} $</b>\n\n"
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
            lines.append(f"✅ {d['first_name']} — x{d['cashout_mult']:.2f}  →  <b>{sign} $</b>")
        else:
            potential = int(d["mise"] * multiplier)
            lines.append(f"⏳ {d['first_name']} — {_fmt(d['mise'])} $  →  <i>{_fmt(potential)} $</i>")
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
            winners.append(f"✅ {d['first_name']} — x{d['cashout_mult']:.2f}  →  <b>+{_fmt(profit)} $</b>")
        else:
            losers.append(f"💀 {d['first_name']}  →  <b>-{_fmt(d['mise'])} $</b>")
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
                f"❌ Solde insuffisant. Tu as <b>{_fmt(balance)} $</b>",
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
            f"✅ {mention(user)} rejoint le crash avec <b>{_fmt(mise)} $</b> !",
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
        f"💰 Cash Out à x{mult:.2f} ! Gain : {_fmt(gain)} $ ({sign} $)",
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
APPLE_MAX    = 5_000_000

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
            f"💰 Encaisser x{mult:.2f} → {_fmt(gain)} $",
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
        f"💵 Mise    <b>{_fmt(session['mise'])} $</b>",
        f"",
        f"Risque ligne  {bar}",
        f"🍎 Pommes rouges (pièges) : <b>{n_bombs}</b>",
        f"🍏 Pommes vertes (sûres)  : <b>{safe}</b>",
        f"",
        f"🎯 Multiplicateur si tu passes : <b>x{mult_next}</b>",
    ]
    if level > 1:
        lines.append(f"💰 Encaisser maintenant : <b>{_fmt(cashout_gain)} $</b>")
    lines.append(f"\n👇 <b>Choisis une pomme !</b>")
    return "\n".join(lines)


async def apple_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/apple <mise> — Apple of Fortune (50 000 $ – 5 000 000 $)"""
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
            f"<b>Mises :</b> {_fmt(APPLE_MIN)} $ – {_fmt(APPLE_MAX)} $\n\n"
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
            f"❌ Mise minimum : <b>{_fmt(APPLE_MIN)} $</b>", parse_mode=ParseMode.HTML
        )
    if mise > APPLE_MAX:
        return await update.message.reply_text(
            f"❌ Mise maximum : <b>{_fmt(APPLE_MAX)} $</b>", parse_mode=ParseMode.HTML
        )

    if user.id in apple_sessions:
        return await update.message.reply_text("⚠️ Tu as déjà une partie en cours !", parse_mode=ParseMode.HTML)

    async with AsyncSessionLocal() as session:
        balance = await _get_balance(session, user.id)
        if balance < mise:
            return await update.message.reply_text(
                f"❌ Solde insuffisant. Tu as <b>{_fmt(balance)} $</b>", parse_mode=ParseMode.HTML
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
            f"Gain : <b>{_fmt(gain)} $</b>  (<b>+{_fmt(profit)} $</b>)\n\n"
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
                    f"💸 Tu as perdu <b>{_fmt(sess['mise'])} $</b>\n\n"
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
                    f"x{mult:.2f}  →  <b>{_fmt(gain_now)} $</b> 🎉🍏",
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
    # label                    type      valeur       poids
    ("💀 Ruine totale",       "ruine",   0,             4),
    ("😭 x0.1",               "mult",    0.1,           6),
    ("😞 x0.3",               "mult",    0.3,           8),
    ("🔄 IDEM",               "idem",    0,            12),
    ("😐 x0.5",               "mult",    0.5,          10),
    ("💵 +100 000 $",         "fixed",   100_000,       8),
    ("🙂 x0.8",               "mult",    0.8,          10),
    ("💰 x1.5",               "mult",    1.5,          15),
    ("💵 +500 000 $",         "fixed",   500_000,       5),
    ("🤑 x2.0",               "mult",    2.0,          10),
    ("🎯 x3.0",               "mult",    3.0,           7),
    ("💵 +1 000 000 $",       "fixed",   1_000_000,     3),
    ("⭐ x5.0",               "mult",    5.0,           4),
    ("🔥 x10.0",              "mult",    10.0,          2),
    ("💎 JACKPOT x25.0",      "mult",    25.0,          1),
]


def _spin_wheel() -> tuple:
    """Retourne (label, type, valeur)."""
    total = sum(s[3] for s in WHEEL_SEGMENTS)
    r     = random.uniform(0, total)
    cum   = 0
    for label, kind, val, weight in WHEEL_SEGMENTS:
        cum += weight
        if r <= cum:
            return label, kind, val
    last = WHEEL_SEGMENTS[-1]
    return last[0], last[1], last[2]


def _wheel_result_text(user_mention: str, mise: int, label: str, kind: str, val) -> tuple[str, int]:
    """Calcule le gain et génère le texte de résultat. Retourne (texte, gain)."""
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
        gain   = int(val)
        profit = gain - mise
        emoji  = "💵"
        title  = f"{label.split(' ', 1)[1]} fixe !"
    else:  # mult
        gain   = int(mise * val)
        profit = gain - mise
        emoji  = "🎡"
        title  = f"{label} !"

    mise_str   = _fmt(mise)
    gain_str   = _fmt(gain)
    profit_str = (f"+{_fmt(profit)}" if profit >= 0 else _fmt(profit))

    text = (
        f"{emoji} <b>{title}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎡 Résultat : <b>{label}</b>\n\n"
        f"{user_mention}\n"
        f"💵 Mise     : <b>{mise_str} $</b>\n"
        f"🏆 Gain     : <b>{gain_str} $</b>\n"
        f"{'📈' if profit >= 0 else '📉'} Résultat   : <b>{profit_str} $</b>"
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
                f"❌ Solde insuffisant. Tu as <b>{_fmt(balance)} $</b>", parse_mode=ParseMode.HTML
            )
        await _add_coins(session, user.id, -mise)

    animation_frames = [
        "🎡 La roue tourne...",
        "🎡 ⠋ En cours...",
        "🎡 ⠙ Tourne encore...",
        "🎡 ⠹ Ralentit...",
        "🎡 ⠸ Ça ralentit...",
        "🎡 ⠼ Presque arrêtée...",
        "🎡 ⠴ Stop imminent...",
        "🎡 ⠦ Et c'est...",
    ]

    msg = await update.message.reply_text(animation_frames[0])
    for frame in animation_frames[1:]:
        await asyncio.sleep(0.5)
        try:
            await msg.edit_text(frame)
        except Exception:
            pass

    label, kind, val = _spin_wheel()
    result_text, gain = _wheel_result_text(mention(user), mise, label, kind, val)

    async with AsyncSessionLocal() as session:
        await _add_coins(session, user.id, gain)

    await msg.edit_text(result_text, parse_mode=ParseMode.HTML)


# ═══════════════════════════════════════════════════════════════════════════════
# REBET — Quitte ou double avec boutons Récupérer / Remiser
# ═══════════════════════════════════════════════════════════════════════════════

# Stockage en mémoire : rebet_sessions[chat_id][user_id] = {mise, gains, round}
rebet_sessions: dict = {}

MIN_REBET = 5000


def _rebet_keyboard(chat_id: int, user_id: int, gains: int) -> InlineKeyboardMarkup:
    next_win = gains * 2
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"💰 Récupérer {_fmt(gains)} $", callback_data=f"rebet:cash:{chat_id}:{user_id}"),
        InlineKeyboardButton(f"🎲 Remiser → {_fmt(next_win)} $", callback_data=f"rebet:double:{chat_id}:{user_id}"),
    ]])


def _rebet_text(first_name: str, mise_initiale: int, gains: int, round_num: int) -> str:
    multiplier = gains / mise_initiale if mise_initiale else 1
    next_win = gains * 2
    return (
        f"🎲 <b>REBET — {first_name}</b>\n\n"
        f"🪙 Mise de départ : <b>{_fmt(mise_initiale)} $</b>\n"
        f"📈 Multiplicateur : <b>x{multiplier:.1f}</b>\n"
        f"💵 Gains actuels : <b>{_fmt(gains)} $</b>\n"
        f"⚡ Prochain gain : <b>{_fmt(next_win)} $</b>\n"
        f"🔄 Tour n°{round_num}\n\n"
        f"Que veux-tu faire ?"
    )


async def rebet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user)

    if not context.args:
        return await update.message.reply_text(
            f"Usage : <code>/rebet &lt;mise&gt;</code>\n"
            f"Mise minimum : <b>{_fmt(MIN_REBET)} $</b>\n\n"
            "🎲 Quitte ou double — récupère ou remise !",
            parse_mode=ParseMode.HTML
        )

    try:
        mise = int(context.args[0].replace(" ", "").replace("_", ""))
    except ValueError:
        return await update.message.reply_text("❌ Mise invalide.")

    if mise < MIN_REBET:
        return await update.message.reply_text(
            f"❌ Mise minimum : <b>{_fmt(MIN_REBET)} $</b>", parse_mode=ParseMode.HTML
        )

    chat_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        result = await deduct_for_game(session, user.id, mise)

    if result == "insufficient":
        async with AsyncSessionLocal() as session:
            bal = await _get_balance(session, user.id)
        return await update.message.reply_text(
            f"❌ Solde insuffisant. Tu as <b>{_fmt(bal)} $</b>", parse_mode=ParseMode.HTML
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
            f"🪙 Mise de départ : <b>{_fmt(mise)} $</b>\n"
            f"✅ Gains encaissés : <b>{_fmt(gains)} $</b>\n"
            f"📊 Profit net : <b>{sign} $</b>\n"
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
                f"🪙 Mise de départ : <b>{_fmt(mise)} $</b>\n"
                f"❌ Tu perds tout !\n"
                f"🔄 Tours joués : <b>{rnd}</b>\n\n"
                f"<i>Trop gourmand... 😅</i>",
                parse_mode=ParseMode.HTML,
            )
