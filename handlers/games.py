"""
handlers/games.py — Jeux : Crash (multijoueur inline), Mines, Apple of Fortune, Roue de Fortune
"""
import asyncio
import random
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database.db import AsyncSessionLocal, get_user
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
#
# crash_games[chat_id] = {
#     user_id: {
#         "mise": int,
#         "cashed_out": bool,
#         "cashout_mult": float | None,
#         "current_mult": float,      ← mis à jour chaque tick
#         "first_name": str,
#         "mention": str,             ← HTML mention
#     }
# }
# crash_phase[chat_id]    = "lobby" | "running" | absent
# crash_lobby_msg[chat_id] = Message  ← message lobby éditable
# crash_live_msg[chat_id]  = Message  ← message live éditable
# ──────────────────────────────────────────────────────────────────────────────

crash_games:     dict = {}
crash_phase:     dict = {}
crash_lobby_msg: dict = {}
crash_live_msg:  dict = {}

LOBBY_SECONDS = 20   # durée de la phase de mise
TICK_INTERVAL = 1.5  # secondes entre chaque update du multiplicateur


def _gen_crash_point() -> float:
    """Génère le multiplicateur de crash avec house edge ~5%."""
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

    # Refuser si partie déjà en cours (phase running)
    if crash_phase.get(chat_id) == "running":
        return await update.message.reply_text(
            "⚠️ Une partie est en cours, attends la prochaine !",
            parse_mode=ParseMode.HTML
        )

    # Initialiser le dict du chat si besoin
    if chat_id not in crash_games:
        crash_games[chat_id] = {}

    # Refuser si joueur déjà inscrit
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

    # Premier joueur → lancer le lobby
    if crash_phase.get(chat_id) != "lobby":
        crash_phase[chat_id] = "lobby"
        msg = await update.message.reply_text(
            _lobby_text(chat_id, LOBBY_SECONDS),
            parse_mode=ParseMode.HTML
        )
        crash_lobby_msg[chat_id] = msg
        asyncio.create_task(_run_lobby(context, chat_id))
    else:
        # Mettre à jour le message lobby avec le nouveau joueur
        try:
            await crash_lobby_msg[chat_id].edit_text(
                _lobby_text(chat_id, -1),   # -1 = on n'affiche pas le timer ici
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
        await update.message.reply_text(
            f"✅ {mention(user)} rejoint le crash avec <b>{_fmt(mise)} $</b> !",
            parse_mode=ParseMode.HTML
        )


async def _run_lobby(context, chat_id: int):
    """Phase lobby : countdown de LOBBY_SECONDS secondes, mise à jour toutes les 5s."""
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

    # Lancer la partie si au moins 1 joueur
    if not crash_games.get(chat_id):
        crash_phase.pop(chat_id, None)
        return

    crash_phase[chat_id] = "running"
    asyncio.create_task(_run_crash(context, chat_id))


async def _run_crash(context, chat_id: int):
    """Partie en cours : multiplicateur monte jusqu'au crash."""
    crash_point = _gen_crash_point()
    multiplier  = 1.0

    # Envoyer le message live
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

    # Boucle de montée du multiplicateur
    while multiplier < crash_point:
        await asyncio.sleep(TICK_INTERVAL)
        multiplier = round(multiplier + 0.06 + multiplier * 0.04, 2)
        multiplier = min(multiplier, crash_point)

        # Mettre à jour current_mult pour chaque joueur encore en jeu
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

    # CRASH — créditer les perdants (gains déjà crédités au cashout)
    # Rien à créditer pour les perdants, la mise a déjà été débitée
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
    """Callback du bouton inline Cash Out pendant un Crash."""
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

    # Verrou par joueur — empêche le double cash out en cas de clics simultanés
    async with d["lock"]:
        if d["cashed_out"]:
            return await query.answer("⚠️ Tu as déjà encaissé !", show_alert=True)

        # Marquer ET capturer le multiplicateur AVANT tout await
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

    # Mettre à jour le message live immédiatement
    try:
        await crash_live_msg[chat_id].edit_text(
            _running_text(chat_id, mult),
            parse_mode=ParseMode.HTML,
            reply_markup=_running_keyboard()
        )
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# MINES
# ═══════════════════════════════════════════════════════════════════════════════

mines_sessions: dict = {}

MINES_GRID_SIZE = 25  # 5x5
MINES_COUNT     = 5

MINES_MULT = [
    1.0,  1.12, 1.28, 1.47, 1.70,
    1.98, 2.32, 2.74, 3.27, 3.94,
    4.80, 5.92, 7.40, 9.40, 12.1,
    15.9, 21.4, 29.6, 42.8, 65.0,
]


def _build_mines_keyboard(session_data: dict) -> InlineKeyboardMarkup:
    revealed = session_data["revealed"]
    grid     = session_data["grid"]
    rows     = []
    for row in range(5):
        btn_row = []
        for col in range(5):
            idx = row * 5 + col
            if revealed[idx]:
                btn_row.append(InlineKeyboardButton(
                    "💣" if grid[idx] else "💎",
                    callback_data=f"mines:done:{idx}"
                ))
            else:
                btn_row.append(InlineKeyboardButton("⬛", callback_data=f"mines:pick:{idx}"))
        rows.append(btn_row)
    mult       = session_data["multiplier"]
    safe_count = sum(1 for i, r in enumerate(revealed) if r and not grid[i])
    if safe_count > 0:
        gain = int(session_data["mise"] * mult)
        rows.append([InlineKeyboardButton(
            f"💰 Cash Out x{mult:.2f} → {_fmt(gain)} $",
            callback_data="mines:cashout"
        )])
    return InlineKeyboardMarkup(rows)


async def mines_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/mines <mise>"""
    user = update.effective_user
    await ensure_user(user)

    if not context.args:
        return await update.message.reply_text(
            f"💣 <b>Mines</b>\n\nGrille 5×5 — {MINES_COUNT} mines cachées.\n"
            "Chaque case sûre augmente ton multiplicateur.\nUne mine = tout perdu !\n\n"
            "Usage : <code>/mines &lt;mise&gt;</code>",
            parse_mode=ParseMode.HTML
        )

    try:
        mise = int(context.args[0].replace(" ", "").replace("_", ""))
    except ValueError:
        return await update.message.reply_text("❌ Mise invalide.")

    if mise < 1000:
        return await update.message.reply_text("❌ Mise minimum : <b>1 000 $</b>", parse_mode=ParseMode.HTML)

    if user.id in mines_sessions:
        return await update.message.reply_text("⚠️ Tu as déjà une partie en cours !", parse_mode=ParseMode.HTML)

    async with AsyncSessionLocal() as session:
        balance = await _get_balance(session, user.id)
        if balance < mise:
            return await update.message.reply_text(
                f"❌ Solde insuffisant. Tu as <b>{_fmt(balance)} $</b>", parse_mode=ParseMode.HTML
            )
        await _add_coins(session, user.id, -mise)

    grid           = [False] * MINES_GRID_SIZE
    mine_positions = random.sample(range(MINES_GRID_SIZE), MINES_COUNT)
    for pos in mine_positions:
        grid[pos] = True

    mines_sessions[user.id] = {
        "mise":       mise,
        "grid":       grid,
        "revealed":   [False] * MINES_GRID_SIZE,
        "multiplier": 1.0,
        "safe_count": 0,
        "chat_id":    update.effective_chat.id,
    }

    keyboard = _build_mines_keyboard(mines_sessions[user.id])
    await update.message.reply_text(
        f"💣 <b>MINES</b> — {mention(user)}\n\n"
        f"Mise : <b>{_fmt(mise)} $</b>  |  Mines : {MINES_COUNT}/25\n"
        "Clique une case pour la révéler !\n"
        "💰 Cash Out dès que tu veux.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )


async def mines_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    user   = query.from_user
    await query.answer()

    parts  = query.data.split(":")
    action = parts[1]

    if user.id not in mines_sessions:
        await query.edit_message_reply_markup(reply_markup=None)
        return

    data = mines_sessions[user.id]

    if action == "cashout":
        gain   = int(data["mise"] * data["multiplier"])
        profit = gain - data["mise"]
        async with AsyncSessionLocal() as session:
            await _add_coins(session, user.id, gain)
        del mines_sessions[user.id]
        await query.edit_message_text(
            f"💰 <b>Cash Out !</b>\n\n"
            f"Multiplicateur : <b>x{data['multiplier']:.2f}</b>\n"
            f"Gain : <b>{_fmt(gain)} $</b>  (+{_fmt(profit)} $)\n\n"
            "Bravo, tu as survécu ! 🎉",
            parse_mode=ParseMode.HTML
        )
        return

    if action == "pick":
        idx = int(parts[2])
        if data["revealed"][idx]:
            return
        data["revealed"][idx] = True

        if data["grid"][idx]:
            for i, is_mine in enumerate(data["grid"]):
                if is_mine:
                    data["revealed"][i] = True
            del mines_sessions[user.id]
            await query.edit_message_text(
                f"💥 <b>BOOM ! Mine !</b>\n\n"
                f"Tu as perdu <b>{_fmt(data['mise'])} $</b> 💸\n\n"
                "Retente ta chance avec /mines",
                parse_mode=ParseMode.HTML
            )
        else:
            data["safe_count"] += 1
            sc = data["safe_count"]
            data["multiplier"] = MINES_MULT[sc] if sc < len(MINES_MULT) else MINES_MULT[-1]
            gain     = int(data["mise"] * data["multiplier"])
            keyboard = _build_mines_keyboard(data)
            await query.edit_message_text(
                f"💣 <b>MINES</b> — {user.first_name}\n\n"
                f"Mise : <b>{_fmt(data['mise'])} $</b>  |  ✅ Cases sûres : {sc}\n"
                f"📈 Multiplicateur : <b>x{data['multiplier']:.2f}</b>\n"
                f"💰 Potentiel : {_fmt(gain)} $",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard
            )


# ═══════════════════════════════════════════════════════════════════════════════
# APPLE OF FORTUNE — 5 colonnes × 10 niveaux (style 1xBet officiel)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Bombes par niveau :
#   Niveaux 1-3  → 1 bombe / 5 cases  (prob. 4/5 = 80%)
#   Niveaux 4-6  → 2 bombes / 5 cases (prob. 3/5 = 60%)
#   Niveaux 7-9  → 3 bombes / 5 cases (prob. 2/5 = 40%)
#   Niveau  10   → 4 bombes / 5 cases (prob. 1/5 = 20%)
#
# Multiplicateurs officiels 1xBet :
#   Niveau 1  → x1.23   Niveau 6  → x4.50
#   Niveau 2  → x1.52   Niveau 7  → x6.80
#   Niveau 3  → x1.90   Niveau 8  → x11.00
#   Niveau 4  → x2.50   Niveau 9  → x18.50
#   Niveau 5  → x3.35   Niveau 10 → x349.68
# ──────────────────────────────────────────────────────────────────────────────

apple_sessions: dict = {}  # user_id → session

APPLE_COLS     = 5
APPLE_LEVELS   = 10

# Multiplicateur cumulatif après avoir passé le niveau N (index 1-10)
APPLE_MULTS = {
    1:  1.23,
    2:  1.52,
    3:  1.90,
    4:  2.50,
    5:  3.35,
    6:  4.50,
    7:  6.80,
    8:  11.00,
    9:  18.50,
    10: 349.68,
}

def _apple_bombs(level: int) -> int:
    if level <= 3:  return 1
    if level <= 6:  return 2
    if level <= 9:  return 3
    return 4


def _apple_gen_row(level: int) -> list:
    """Génère une rangée : liste de 5 bools (True = bombe)."""
    n_bombs  = _apple_bombs(level)
    row      = [False] * APPLE_COLS
    for pos in random.sample(range(APPLE_COLS), n_bombs):
        row[pos] = True
    return row


def _apple_keyboard(session: dict) -> InlineKeyboardMarkup:
    """Clavier de la rangée courante."""
    level    = session["level"]
    revealed = session["row_revealed"]
    row      = session["row_bombs"]
    n_bombs  = _apple_bombs(level)
    safe     = APPLE_COLS - n_bombs

    # Si une case a déjà été jouée sur cette ligne, bloquer toutes les autres
    line_played = any(revealed)

    buttons = []
    for i in range(APPLE_COLS):
        if revealed[i]:
            label = "💣" if row[i] else "🍎"
            buttons.append(InlineKeyboardButton(label, callback_data=f"apple:done:{i}"))
        elif line_played:
            # Case non révélée mais ligne déjà jouée → inactive (⬛ non cliquable)
            buttons.append(InlineKeyboardButton("⬛", callback_data=f"apple:done:{i}"))
        else:
            buttons.append(InlineKeyboardButton("🍏", callback_data=f"apple:pick:{i}"))

    rows = [buttons]

    # Bouton Cash Out si au moins 1 niveau passé
    if session["level"] > 1 or session.get("passed_one"):
        mult = APPLE_MULTS.get(session["level"] - 1, 1.0)
        gain = int(session["mise"] * mult)
        rows.append([InlineKeyboardButton(
            f"💰 Cash Out x{mult:.2f} → {_fmt(gain)} $",
            callback_data="apple:cashout"
        )])

    return InlineKeyboardMarkup(rows)


def _apple_status(session: dict) -> str:
    level   = session["level"]
    n_bombs = _apple_bombs(level)
    safe    = APPLE_COLS - n_bombs
    mult    = APPLE_MULTS.get(level, "?")
    prev_mult = APPLE_MULTS.get(level - 1, 1.0) if level > 1 else 1.0
    gain_if_cashout = int(session["mise"] * prev_mult) if level > 1 else 0

    danger_bar = "🔴" * n_bombs + "🟢" * safe

    text = (
        f"🍎 <b>APPLE OF FORTUNE</b>\n\n"
        f"📊 Niveau <b>{level}</b> / {APPLE_LEVELS}  |  Mise : <b>{_fmt(session['mise'])} $</b>\n"
        f"💣 Bombes : <b>{n_bombs}/5</b>  {danger_bar}\n"
        f"🎯 Si tu passes : <b>x{mult}</b>\n"
    )
    if level > 1:
        text += f"💰 Cash out maintenant : <b>{_fmt(gain_if_cashout)} $</b>\n"
    text += "\nChoisis une case 👇"
    return text


async def apple_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/apple <mise> — Apple of Fortune"""
    user = update.effective_user
    await ensure_user(user)

    if not context.args:
        table = "\n".join(
            f"  Niveau {lvl:2d} — x{mult:.2f}  ({APPLE_COLS - _apple_bombs(lvl)}/5 sûres)"
            for lvl, mult in APPLE_MULTS.items()
        )
        return await update.message.reply_text(
            "🍎 <b>Apple of Fortune</b>\n\n"
            "Gravis 10 niveaux en choisissant une case parmi 5.\n"
            "Les bombes augmentent à chaque palier — encaisse quand tu veux !\n\n"
            f"<b>Table des gains :</b>\n{table}\n\n"
            "Usage : <code>/apple &lt;mise&gt;</code>",
            parse_mode=ParseMode.HTML
        )

    try:
        mise = int(context.args[0].replace(" ", "").replace("_", ""))
    except ValueError:
        return await update.message.reply_text("❌ Mise invalide.")

    if mise < 1000:
        return await update.message.reply_text("❌ Mise minimum : <b>1 000 $</b>", parse_mode=ParseMode.HTML)

    if user.id in apple_sessions:
        return await update.message.reply_text("⚠️ Tu as déjà une partie en cours !", parse_mode=ParseMode.HTML)

    async with AsyncSessionLocal() as session:
        balance = await _get_balance(session, user.id)
        if balance < mise:
            return await update.message.reply_text(
                f"❌ Solde insuffisant. Tu as <b>{_fmt(balance)} $</b>", parse_mode=ParseMode.HTML
            )
        await _add_coins(session, user.id, -mise)

    # Initialiser la session — niveau 1
    apple_sessions[user.id] = {
        "mise":        mise,
        "level":       1,
        "row_bombs":   _apple_gen_row(1),
        "row_revealed": [False] * APPLE_COLS,
        "passed_one":  False,
        "chat_id":     update.effective_chat.id,
        "lock":        asyncio.Lock(),
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
            f"💰 <b>Cash Out !</b>\n\n"
            f"Niveau atteint : <b>{sess['level'] - 1}</b>  |  x{mult:.2f}\n"
            f"Gain : <b>{_fmt(gain)} $</b>  (+{_fmt(profit)} $)\n\n"
            "Bien joué ! 🍎",
            parse_mode=ParseMode.HTML
        )
        return

    # ── Case déjà révélée (tap accidentel) ───────────────────────────────────
    if action == "done":
        return

    # ── Choisir une case ─────────────────────────────────────────────────────
    if action == "pick":
        idx = int(parts[2])

        # Verrou par session — empêche de cliquer plusieurs cases simultanément
        async with sess["lock"]:
            if sess["row_revealed"][idx]:
                return  # case déjà traitée (double clic ou clic parallèle)

            # Vérifier qu'une case n'a pas déjà été résolue sur cette ligne
            # (si le niveau a changé entre le clic et le traitement)
            if any(sess["row_revealed"]):
                # Une case a déjà été jouée sur cette rangée, on ignore
                return

            sess["row_revealed"][idx] = True

            # 💣 BOMBE
            if sess["row_bombs"][idx]:
                # Révéler toutes les bombes de la rangée
                for i, is_bomb in enumerate(sess["row_bombs"]):
                    if is_bomb:
                        sess["row_revealed"][i] = True
                # Afficher la rangée finale avec bombes visibles
                keyboard = _apple_keyboard(sess)
                del apple_sessions[user.id]
                await query.edit_message_text(
                    f"💥 <b>BOOM ! Pomme empoisonnée !</b>\n\n"
                    f"Niveau {sess['level']} — Tu as perdu <b>{_fmt(sess['mise'])} $</b> 💸\n\n"
                    "Retente ta chance avec /apple",
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard
                )
                return

            # 🍎 BONNE POMME — niveau passé
            current_level = sess["level"]
            mult          = APPLE_MULTS[current_level]
            gain_now      = int(sess["mise"] * mult)

            # Niveau MAX atteint → victoire automatique
            if current_level == APPLE_LEVELS:
                async with AsyncSessionLocal() as session:
                    await _add_coins(session, user.id, gain_now)
                del apple_sessions[user.id]
                await query.edit_message_text(
                    f"🏆 <b>VICTOIRE TOTALE !</b>\n\n"
                    f"Tu as gravi les {APPLE_LEVELS} niveaux !\n"
                    f"x{mult:.2f}  →  <b>{_fmt(gain_now)} $</b> 🎉",
                    parse_mode=ParseMode.HTML
                )
                return

            # Passer au niveau suivant
            next_level = current_level + 1
            sess["level"]        = next_level
            sess["row_bombs"]    = _apple_gen_row(next_level)
            sess["row_revealed"] = [False] * APPLE_COLS
            sess["passed_one"]   = True

        # Edit du message EN DEHORS du lock (await ne bloque plus la session)
        keyboard = _apple_keyboard(sess)
        await query.edit_message_text(
            f"✅ <b>Bonne pomme !</b>  Niveau {current_level} passé — x{mult:.2f}\n\n"
            + _apple_status(sess),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )


# ═══════════════════════════════════════════════════════════════════════════════
# ROUE DE FORTUNE
# ═══════════════════════════════════════════════════════════════════════════════

WHEEL_SEGMENTS = [
    ("💀 RUINE",   0.0,   4),
    ("😭 x0.1",    0.1,   8),
    ("😞 x0.3",    0.3,  10),
    ("😐 x0.5",    0.5,  12),
    ("🙂 x0.8",    0.8,  14),
    ("💰 x1.5",    1.5,  20),
    ("🤑 x2.0",    2.0,  15),
    ("🎯 x3.0",    3.0,  10),
    ("⭐ x5.0",    5.0,   5),
    ("🔥 x10.0",  10.0,   2),
]


def _spin_wheel() -> tuple:
    total = sum(s[2] for s in WHEEL_SEGMENTS)
    r     = random.uniform(0, total)
    cum   = 0
    for name, mult, weight in WHEEL_SEGMENTS:
        cum += weight
        if r <= cum:
            return name, mult
    return WHEEL_SEGMENTS[-1][0], WHEEL_SEGMENTS[-1][1]


async def roue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/roue <mise>"""
    user = update.effective_user
    await ensure_user(user)

    if not context.args:
        wheel_display = "\n".join(f"  {name}" for name, _, _ in WHEEL_SEGMENTS)
        return await update.message.reply_text(
            f"🎡 <b>Roue de Fortune</b>\n\nSegments :\n{wheel_display}\n\n"
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
        "🎡 ⠙ En cours...",
        "🎡 ⠹ Ralentit...",
        "🎡 ⠸ Ralentit...",
        "🎡 ⠼ Presque...",
        "🎡 ⠴ Presque...",
        "🎡 ⠦ Stop !",
    ]

    msg = await update.message.reply_text(animation_frames[0])
    for frame in animation_frames[1:]:
        await asyncio.sleep(0.5)
        try:
            await msg.edit_text(frame)
        except Exception:
            pass

    name, mult = _spin_wheel()
    gain       = int(mise * mult)
    profit     = gain - mise

    async with AsyncSessionLocal() as session:
        await _add_coins(session, user.id, gain)

    if mult == 0:
        result = (
            f"💀 <b>RUINE TOTALE !</b>\n\n"
            f"{mention(user)} a tout perdu !\n"
            f"Mise : <b>{_fmt(mise)} $</b>  →  Gain : <b>0 $</b>"
        )
    elif profit < 0:
        result = (
            f"🎡 <b>{name}</b>\n\n"
            f"{mention(user)}\n"
            f"Mise : {_fmt(mise)} $  →  Gain : <b>{_fmt(gain)} $</b>\n"
            f"Perte : <b>-{_fmt(mise - gain)} $</b>"
        )
    else:
        result = (
            f"🎡 <b>{name}</b>\n\n"
            f"{mention(user)}\n"
            f"Mise : {_fmt(mise)} $  →  Gain : <b>{_fmt(gain)} $</b>\n"
            f"Profit : <b>+{_fmt(profit)} $</b> 🎉"
        )

    await msg.edit_text(result, parse_mode=ParseMode.HTML)
