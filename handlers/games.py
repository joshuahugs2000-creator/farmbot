"""
handlers/games.py — Jeux : Crash, Mines, Roue de Fortune
"""
import asyncio
import random
import math
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

async def _set_coins(session, user_id: int, amount: int):
    await session.execute(
        sa.text("UPDATE users SET coins = :a WHERE user_id = :uid"),
        {"a": amount, "uid": user_id}
    )
    await session.commit()

# ═══════════════════════════════════════════════════════════════════════════════
# CRASH
# ═══════════════════════════════════════════════════════════════════════════════

# crash_games[chat_id] = {user_id: {"mise": int, "cashed_out": bool, "multiplier": float, "msg_id": int}}
crash_games: dict = {}
crash_running: dict = {}  # chat_id -> True si une partie est en cours

def _gen_crash_point() -> float:
    """Génère le multiplicateur de crash avec house edge de ~5%."""
    r = random.random()
    if r < 0.05:
        return 1.0  # crash immédiat 5% du temps
    # Distribution exponentielle inversée
    crash = 0.99 / (1 - random.random() * 0.95)
    return round(min(crash, 100.0), 2)

async def crash_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/crash <mise> — Mise sur le prochain Crash"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    await ensure_user(user)

    if not context.args:
        return await update.message.reply_text(
            "💥 <b>Crash</b>\n\nMise sur un multiplicateur qui monte… jusqu'au crash !\n\n"
            "Usage : <code>/crash &lt;mise&gt;</code>\n"
            "Puis : <code>/cashout</code> pour encaisser avant le crash\n\n"
            "💡 Exemple : <code>/crash 50000</code>",
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
                f"❌ Solde insuffisant. Tu as <b>{_fmt(balance)} $</b>",
                parse_mode=ParseMode.HTML
            )

        # Initialiser le jeu si besoin
        if chat_id not in crash_games:
            crash_games[chat_id] = {}

        if user.id in crash_games[chat_id]:
            return await update.message.reply_text("⚠️ Tu as déjà une mise active ! Fais <code>/cashout</code>", parse_mode=ParseMode.HTML)

        # Déduire la mise
        await _add_coins(session, user.id, -mise)

        crash_games[chat_id][user.id] = {
            "mise": mise,
            "cashed_out": False,
            "username": user.first_name,
        }

    # Si pas de partie en cours, démarrer une dans 10s
    if not crash_running.get(chat_id):
        crash_running[chat_id] = True
        msg = await update.message.reply_text(
            f"💥 <b>CRASH — Phase de mise</b>\n\n"
            f"✅ {mention(user)} mise <b>{_fmt(mise)} $</b>\n\n"
            f"⏳ La partie démarre dans <b>15 secondes</b>...\n"
            f"Rejoins avec <code>/crash &lt;mise&gt;</code>\n"
            f"Cash out avec <code>/cashout</code> pendant la partie",
            parse_mode=ParseMode.HTML
        )
        asyncio.create_task(_run_crash(context, chat_id, msg))
    else:
        await update.message.reply_text(
            f"✅ {mention(user)} entre dans le crash avec <b>{_fmt(mise)} $</b> !",
            parse_mode=ParseMode.HTML
        )

async def _run_crash(context, chat_id: int, lobby_msg):
    """Logique principale du Crash"""
    await asyncio.sleep(15)

    players = crash_games.get(chat_id, {})
    if not players:
        crash_running[chat_id] = False
        return

    crash_point = _gen_crash_point()
    multiplier = 1.0
    step = 0.1
    interval = 1.5  # secondes entre chaque update

    # Message live
    try:
        live_msg = await context.bot.send_message(
            chat_id,
            f"🚀 <b>CRASH EN COURS</b>\n\n📈 Multiplicateur : <b>x{multiplier:.2f}</b>\n\n"
            f"👥 Joueurs : {len(players)}\n"
            f"⚡ <code>/cashout</code> pour encaisser !",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        crash_running[chat_id] = False
        crash_games.pop(chat_id, None)
        return

    crashed = False
    while multiplier < crash_point:
        await asyncio.sleep(interval)
        multiplier = round(multiplier + step + multiplier * 0.04, 2)
        multiplier = min(multiplier, crash_point)

        active = sum(1 for p in players.values() if not p["cashed_out"])
        cashed = sum(1 for p in players.values() if p["cashed_out"])

        try:
            await live_msg.edit_text(
                f"🚀 <b>CRASH EN COURS</b>\n\n"
                f"📈 Multiplicateur : <b>x{multiplier:.2f}</b>\n\n"
                f"✅ Cash out : {cashed}  |  ⏳ En jeu : {active}\n"
                f"⚡ <code>/cashout</code> pour encaisser !",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

        if multiplier >= crash_point:
            crashed = True
            break

    # Résultats
    results = []
    async with AsyncSessionLocal() as session:
        for uid, data in players.items():
            if data["cashed_out"]:
                gain = int(data["mise"] * data.get("cashout_mult", multiplier))
                results.append(f"✅ {data['username']} — cash out à x{data.get('cashout_mult', multiplier):.2f} → <b>+{_fmt(gain)} $</b>")
            else:
                # Perdu
                results.append(f"💀 {data['username']} — perdu <b>{_fmt(data['mise'])} $</b>")

    result_text = "\n".join(results) if results else "Aucun joueur"

    try:
        await live_msg.edit_text(
            f"💥 <b>CRASH ! x{crash_point:.2f}</b>\n\n"
            f"{result_text}",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        try:
            await context.bot.send_message(
                chat_id,
                f"💥 <b>CRASH ! x{crash_point:.2f}</b>\n\n{result_text}",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    crash_games.pop(chat_id, None)
    crash_running[chat_id] = False


async def cashout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cashout — Encaisser pendant un Crash"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    await ensure_user(user)

    players = crash_games.get(chat_id, {})
    if user.id not in players:
        return await update.message.reply_text("❌ Tu n'as pas de mise active dans ce Crash.")

    data = players[user.id]
    if data["cashed_out"]:
        return await update.message.reply_text("⚠️ Tu as déjà encaissé !")

    # Trouver le multiplicateur actuel approximatif
    # On ne peut pas le récupérer directement, donc on stocke le temps
    import time
    current_mult = data.get("current_mult", 1.0)

    # Marquer comme cash out
    data["cashed_out"] = True
    data["cashout_mult"] = current_mult

    gain = int(data["mise"] * current_mult)
    async with AsyncSessionLocal() as session:
        await _add_coins(session, user.id, gain)

    profit = gain - data["mise"]
    await update.message.reply_text(
        f"💰 <b>Cash Out !</b>\n\n"
        f"{mention(user)} encaisse à <b>x{current_mult:.2f}</b>\n"
        f"Mise : {_fmt(data['mise'])} $ → Gain : <b>{_fmt(gain)} $</b> (+{_fmt(profit)} $)",
        parse_mode=ParseMode.HTML
    )


# Version améliorée : cashout avec suivi du multiplicateur en temps réel
async def _run_crash_v2(context, chat_id: int, lobby_msg):
    """Logique Crash améliorée avec tracking du multiplicateur par joueur"""
    await asyncio.sleep(15)

    players = crash_games.get(chat_id, {})
    if not players:
        crash_running[chat_id] = False
        return

    crash_point = _gen_crash_point()
    multiplier = 1.0

    try:
        live_msg = await context.bot.send_message(
            chat_id,
            f"🚀 <b>CRASH EN COURS</b>\n\n📈 <b>x{multiplier:.2f}</b>\n⚡ /cashout pour encaisser !",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        crash_running[chat_id] = False
        crash_games.pop(chat_id, None)
        return

    while multiplier < crash_point:
        await asyncio.sleep(1.2)
        multiplier = round(multiplier + 0.1 + multiplier * 0.03, 2)
        multiplier = min(multiplier, crash_point)

        # Mettre à jour le multiplicateur courant pour cashout
        for uid in players:
            if not players[uid]["cashed_out"]:
                players[uid]["current_mult"] = multiplier

        active = sum(1 for p in players.values() if not p["cashed_out"])
        try:
            await live_msg.edit_text(
                f"🚀 <b>CRASH EN COURS</b>\n\n📈 <b>x{multiplier:.2f}</b>\n"
                f"⏳ {active} joueur(s) encore en jeu\n⚡ /cashout pour encaisser !",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    # Crash !
    results = []
    async with AsyncSessionLocal() as session:
        for uid, data in players.items():
            if data["cashed_out"]:
                mult = data.get("cashout_mult", 1.0)
                gain = int(data["mise"] * mult)
                profit = gain - data["mise"]
                results.append(f"✅ {data['username']} x{mult:.2f} → +{_fmt(profit)} $")
            else:
                results.append(f"💀 {data['username']} → -{_fmt(data['mise'])} $")

    result_text = "\n".join(results) or "—"
    try:
        await live_msg.edit_text(
            f"💥 <b>CRASH à x{crash_point:.2f} !</b>\n\n{result_text}",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        await context.bot.send_message(
            chat_id,
            f"💥 <b>CRASH à x{crash_point:.2f} !</b>\n\n{result_text}",
            parse_mode=ParseMode.HTML
        )

    crash_games.pop(chat_id, None)
    crash_running[chat_id] = False


# Remplacer _run_crash par _run_crash_v2
_run_crash = _run_crash_v2


# ═══════════════════════════════════════════════════════════════════════════════
# MINES
# ═══════════════════════════════════════════════════════════════════════════════

# mines_sessions[user_id] = {"mise": int, "grid": list[bool], "revealed": list[bool], "multiplier": float, "chat_id": int, "msg_id": int}
mines_sessions: dict = {}

MINES_GRID_SIZE = 25  # 5x5
MINES_COUNT = 5

MINES_MULT = [
    1.0, 1.12, 1.28, 1.47, 1.70,
    1.98, 2.32, 2.74, 3.27, 3.94,
    4.80, 5.92, 7.40, 9.40, 12.1,
    15.9, 21.4, 29.6, 42.8, 65.0,
]

def _build_mines_keyboard(session_data: dict) -> InlineKeyboardMarkup:
    revealed = session_data["revealed"]
    grid = session_data["grid"]
    rows = []
    for row in range(5):
        btn_row = []
        for col in range(5):
            idx = row * 5 + col
            if revealed[idx]:
                if grid[idx]:  # mine
                    btn_row.append(InlineKeyboardButton("💣", callback_data=f"mines:boom:{idx}"))
                else:
                    btn_row.append(InlineKeyboardButton("💎", callback_data=f"mines:safe:{idx}"))
            else:
                btn_row.append(InlineKeyboardButton("⬛", callback_data=f"mines:pick:{idx}"))
        rows.append(btn_row)
    # Bouton Cash Out
    mult = session_data["multiplier"]
    safe_count = sum(1 for i, r in enumerate(revealed) if r and not grid[i])
    if safe_count > 0:
        gain = int(session_data["mise"] * mult)
        rows.append([InlineKeyboardButton(f"💰 Cash Out x{mult:.2f} → {_fmt(gain)} $", callback_data="mines:cashout")])
    return InlineKeyboardMarkup(rows)

async def mines_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/mines <mise> — Jeu de mines"""
    user = update.effective_user
    await ensure_user(user)

    if not context.args:
        return await update.message.reply_text(
            "💣 <b>Mines</b>\n\nRetourne des cases sur une grille 5x5.\n"
            "Chaque case sûre augmente ton multiplicateur.\nUne mine = tout perdu !\n\n"
            f"Il y a <b>{MINES_COUNT} mines</b> cachées sur 25 cases.\n\n"
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
        return await update.message.reply_text(
            "⚠️ Tu as déjà une partie en cours ! Termine-la d'abord.",
            parse_mode=ParseMode.HTML
        )

    async with AsyncSessionLocal() as session:
        balance = await _get_balance(session, user.id)
        if balance < mise:
            return await update.message.reply_text(
                f"❌ Solde insuffisant. Tu as <b>{_fmt(balance)} $</b>",
                parse_mode=ParseMode.HTML
            )
        await _add_coins(session, user.id, -mise)

    # Créer la grille
    grid = [False] * MINES_GRID_SIZE
    mine_positions = random.sample(range(MINES_GRID_SIZE), MINES_COUNT)
    for pos in mine_positions:
        grid[pos] = True

    mines_sessions[user.id] = {
        "mise": mise,
        "grid": grid,
        "revealed": [False] * MINES_GRID_SIZE,
        "multiplier": 1.0,
        "safe_count": 0,
        "chat_id": update.effective_chat.id,
    }

    keyboard = _build_mines_keyboard(mines_sessions[user.id])
    await update.message.reply_text(
        f"💣 <b>MINES</b> — {mention(user)}\n\n"
        f"Mise : <b>{_fmt(mise)} $</b> | Mines : {MINES_COUNT}/25\n"
        f"Clique sur une case pour la révéler !\n"
        f"💰 Cash Out dès que tu veux pour encaisser.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )

async def mines_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    await query.answer()

    parts = query.data.split(":")
    action = parts[1]

    if user.id not in mines_sessions:
        await query.edit_message_reply_markup(reply_markup=None)
        return await query.answer("❌ Pas de partie active.", show_alert=True)

    data = mines_sessions[user.id]

    if action == "cashout":
        gain = int(data["mise"] * data["multiplier"])
        profit = gain - data["mise"]
        async with AsyncSessionLocal() as session:
            await _add_coins(session, user.id, gain)
        del mines_sessions[user.id]
        await query.edit_message_text(
            f"💰 <b>Cash Out !</b>\n\n"
            f"Multiplicateur : <b>x{data['multiplier']:.2f}</b>\n"
            f"Gain : <b>{_fmt(gain)} $</b> (+{_fmt(profit)} $)\n\n"
            f"Bravo, tu as survécu ! 🎉",
            parse_mode=ParseMode.HTML
        )
        return

    if action == "pick":
        idx = int(parts[2])
        if data["revealed"][idx]:
            return

        data["revealed"][idx] = True

        if data["grid"][idx]:
            # MINE !
            # Révéler toutes les mines
            for i, is_mine in enumerate(data["grid"]):
                if is_mine:
                    data["revealed"][i] = True
            keyboard = _build_mines_keyboard(data)
            del mines_sessions[user.id]
            await query.edit_message_text(
                f"💥 <b>BOOM ! Mine !</b>\n\n"
                f"Tu as perdu <b>{_fmt(data['mise'])} $</b> 💸\n\n"
                f"Retente ta chance avec /mines",
                parse_mode=ParseMode.HTML
            )
            return
        else:
            # Case sûre
            data["safe_count"] = data.get("safe_count", 0) + 1
            sc = data["safe_count"]
            if sc < len(MINES_MULT):
                data["multiplier"] = MINES_MULT[sc]
            else:
                data["multiplier"] = MINES_MULT[-1]

            keyboard = _build_mines_keyboard(data)
            gain = int(data["mise"] * data["multiplier"])
            await query.edit_message_text(
                f"💣 <b>MINES</b> — {user.first_name}\n\n"
                f"Mise : <b>{_fmt(data['mise'])} $</b> | ✅ Cases sûres : {sc}\n"
                f"📈 Multiplicateur : <b>x{data['multiplier']:.2f}</b>\n"
                f"💰 Potentiel : {_fmt(gain)} $",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard
            )

# ═══════════════════════════════════════════════════════════════════════════════
# ROUE DE FORTUNE
# ═══════════════════════════════════════════════════════════════════════════════

WHEEL_SEGMENTS = [
    ("💀 RUINE",       0.0,   4),   # poids 4
    ("😭 x0.1",        0.1,   8),
    ("😞 x0.3",        0.3,  10),
    ("😐 x0.5",        0.5,  12),
    ("🙂 x0.8",        0.8,  14),
    ("💰 x1.5",        1.5,  20),
    ("🤑 x2.0",        2.0,  15),
    ("🎯 x3.0",        3.0,  10),
    ("⭐ x5.0",        5.0,   5),
    ("🔥 x10.0",      10.0,   2),
]

def _spin_wheel() -> tuple:
    segments = WHEEL_SEGMENTS
    total_weight = sum(s[2] for s in segments)
    r = random.uniform(0, total_weight)
    cumulative = 0
    for name, mult, weight in segments:
        cumulative += weight
        if r <= cumulative:
            return name, mult
    return segments[-1][0], segments[-1][1]

async def roue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/roue <mise> — Roue de Fortune"""
    user = update.effective_user
    await ensure_user(user)

    if not context.args:
        wheel_display = "\n".join(
            f"  {name} (poids: {w})" for name, _, w in WHEEL_SEGMENTS
        )
        return await update.message.reply_text(
            f"🎡 <b>Roue de Fortune</b>\n\n"
            f"Tente ta chance ! La roue peut te ruiner… ou te rendre riche !\n\n"
            f"Segments :\n{wheel_display}\n\n"
            f"Usage : <code>/roue &lt;mise&gt;</code>",
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
                f"❌ Solde insuffisant. Tu as <b>{_fmt(balance)} $</b>",
                parse_mode=ParseMode.HTML
            )
        await _add_coins(session, user.id, -mise)

    # Animation
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
    gain = int(mise * mult)
    profit = gain - mise

    async with AsyncSessionLocal() as session:
        await _add_coins(session, user.id, gain)

    if mult == 0:
        result_text = (
            f"💀 <b>RUINE TOTALE !</b>\n\n"
            f"{mention(user)} a tout perdu !\n"
            f"Mise : <b>{_fmt(mise)} $</b> → Gain : <b>0 $</b>\n"
            f"Perte : <b>-{_fmt(mise)} $</b>\n\n"
            f"😭 La roue a parlé..."
        )
    elif mult < 1.0:
        result_text = (
            f"🎡 <b>{name}</b>\n\n"
            f"{mention(user)}\n"
            f"Mise : {_fmt(mise)} $ → Gain : <b>{_fmt(gain)} $</b>\n"
            f"Perte : <b>-{_fmt(mise - gain)} $</b>"
        )
    elif mult == 1.5:
        result_text = (
            f"🎡 <b>{name}</b>\n\n"
            f"{mention(user)}\n"
            f"Mise : {_fmt(mise)} $ → Gain : <b>{_fmt(gain)} $</b>\n"
            f"Profit : <b>+{_fmt(profit)} $</b> 🎉"
        )
    else:
        result_text = (
            f"🎡 <b>{name}</b>\n\n"
            f"{mention(user)}\n"
            f"Mise : {_fmt(mise)} $ → Gain : <b>{_fmt(gain)} $</b>\n"
            f"Profit : <b>+{_fmt(profit)} $</b> 🎉"
        )

    await msg.edit_text(result_text, parse_mode=ParseMode.HTML)
