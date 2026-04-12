"""
Système de paris avec mini-jeux interactifs.

/bet [mise]         → ouvre un jeu au groupe (45s pour rejoindre, 30s pour choisir)
/bet [mise] @user   → duel direct contre un joueur ciblé

Jeux disponibles :
  Courses   : chevaux, chameaux, tortues, voitures, fermiers
  Combats   : coqs, gladiateurs, mages, archers
  Hasard    : dés royaux, cartes, tir à la cible, récolte

Règles :
  - Mise minimum : 500 $
  - Tous les participants misent le même montant
  - Le concurrent gagnant est tiré au sort (pondéré par cote)
  - Plusieurs joueurs sur le même concurrent → ils partagent le gain
  - En duel : chacun choisit son concurrent, le meilleur gagne
"""

import random
import asyncio
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database.db import AsyncSessionLocal, get_user, add_coins
from utils.helpers import ensure_user, is_group, parse_target, mention

logger = logging.getLogger(__name__)

# ─── Définition des jeux ──────────────────────────────────────────────────────

GAMES = {
    "chevaux": {
        "name": "🏇 Course de Chevaux",
        "emoji": "🏇",
        "label": "chevaux",
        "contestants": [
            ("⚡ Éclair",    1.4),
            ("🌪️ Tempête",  2.0),
            ("🔥 Inferno",  3.0),
            ("🌙 Minuit",   4.5),
            ("💨 Fantôme",  7.0),
            ("🦴 Vieux Os", 10.0),
        ],
        "start_msg": "Les chevaux sont au départ...",
        "run_msgs": ["Les sabots tonnent !", "La foule hurle !", "Dernière ligne droite !"],
    },
    "chameaux": {
        "name": "🐪 Course de Chameaux",
        "emoji": "🐪",
        "label": "chameaux",
        "contestants": [
            ("🌵 Cactus",   1.5),
            ("🏜️ Sahara",   2.2),
            ("☀️ Soleil",   3.0),
            ("🌊 Mirage",   5.0),
            ("👑 Sultan",   8.0),
        ],
        "start_msg": "Les chameaux crachent et s'élancent...",
        "run_msgs": ["Le sable vole !", "Les cloches sonnent !", "Le désert tremble !"],
    },
    "tortues": {
        "name": "🐢 Course de Tortues",
        "emoji": "🐢",
        "label": "tortues",
        "contestants": [
            ("🐢 Rapido",   1.8),
            ("🐢 Turbo",    2.5),
            ("🐢 Flash",    3.5),
            ("🐢 Rocket",   6.0),
            ("🐢 Sensei",   9.0),
        ],
        "start_msg": "Les tortues... avancent. Lentement.",
        "run_msgs": ["Une tortue s'arrête pour dormir.", "Incroyable, elles bougent !", "C'est... presque fini."],
    },
    "voitures": {
        "name": "🏎️ Course de Voitures",
        "emoji": "🏎️",
        "label": "voitures",
        "contestants": [
            ("🔴 Ferrari",  1.3),
            ("🔵 Bugatti",  2.0),
            ("🟡 Lamborghini", 2.8),
            ("⚫ McLaren",  4.0),
            ("🟢 Koenigsegg", 6.5),
            ("🟠 Pagani",   9.0),
        ],
        "start_msg": "Les moteurs rugissent...",
        "run_msgs": ["Les pneus crissent !", "Dérapage contrôlé !", "Vitesse maximale !"],
    },
    "fermiers": {
        "name": "👨‍🌾 Course de Fermiers",
        "emoji": "👨‍🌾",
        "label": "fermiers",
        "contestants": [
            ("🌽 Maïs-Rapid",  1.6),
            ("🥕 Carotte-Man", 2.3),
            ("🥔 Potato-Run",  3.2),
            ("🍅 Tomate-Bolt", 5.0),
            ("🌻 Sunflower",   7.5),
        ],
        "start_msg": "Les fermiers lâchent leurs brouettes...",
        "run_msgs": ["Un fermier trébuche sur une patate !", "La foule de poules s'excite !", "Le champ tremble !"],
    },
    "coqs": {
        "name": "🐓 Combat de Coqs",
        "emoji": "🐓",
        "label": "coqs",
        "contestants": [
            ("🐓 Roi-Soleil",  1.5),
            ("🐓 Foudre",      2.2),
            ("🐓 Titan",       3.0),
            ("🐓 Diable",      5.5),
            ("🐓 Fantôme",     8.0),
        ],
        "start_msg": "Les coqs s'affrontent dans l'arène...",
        "run_msgs": ["Les plumes volent !", "Un coq chancelle !", "L'arène est en feu !"],
    },
    "gladiateurs": {
        "name": "⚔️ Combat de Gladiateurs",
        "emoji": "⚔️",
        "label": "gladiateurs",
        "contestants": [
            ("⚔️ Maximus",   1.4),
            ("🛡️ Spartacus", 2.0),
            ("🔱 Neptune",   3.0),
            ("🦁 Leo",       5.0),
            ("💀 Ombre",     8.5),
        ],
        "start_msg": "Le Colisée rugit...",
        "run_msgs": ["Le sang coule !", "La foule scande !", "Les épées s'entrechoquent !"],
    },
    "mages": {
        "name": "🧙 Duel de Mages",
        "emoji": "🧙",
        "label": "mages",
        "contestants": [
            ("🔥 Pyromancien", 1.6),
            ("❄️ Cryomancien", 2.0),
            ("⚡ Foudromage",  2.8),
            ("🌑 Nécromancien",5.0),
            ("✨ Archimage",    7.0),
        ],
        "start_msg": "Les sorts fusent dans les airs...",
        "run_msgs": ["Les éclairs illuminent l'arène !", "Un bouclier magique éclate !", "La magie déborde !"],
    },
    "archers": {
        "name": "🏹 Tournoi d'Archers",
        "emoji": "🏹",
        "label": "archers",
        "contestants": [
            ("🏹 Robin",     1.5),
            ("🏹 Legolas",   2.0),
            ("🏹 Yumi",      3.0),
            ("🏹 Sagittaire",5.0),
            ("🏹 L'Aveugle", 9.0),
        ],
        "start_msg": "Les arcs se tendent...",
        "run_msgs": ["Les flèches sifflent !", "Une flèche frôle la cible !", "La corde vibre !"],
    },
    "des": {
        "name": "🎲 Dés Royaux",
        "emoji": "🎲",
        "label": "des",
        "contestants": [
            ("🎲 Dé d'Or",    1.3),
            ("🎲 Dé d'Argent",2.0),
            ("🎲 Dé de Fer",  3.0),
            ("🎲 Dé Maudit",  5.0),
            ("🎲 Dé du Destin",8.0),
        ],
        "start_msg": "Les dés roulent sur la table royale...",
        "run_msgs": ["Le plateau tremble !", "Les dés s'entrechoquent !", "Dernier rebond !"],
    },
    "cartes": {
        "name": "🃏 Duel de Cartes",
        "emoji": "🃏",
        "label": "cartes",
        "contestants": [
            ("🃏 As de Pique",  1.4),
            ("🃏 Roi de Cœur", 2.0),
            ("🃏 Dame de Trèfle",3.0),
            ("🃏 Valet Fou",   5.0),
            ("🃏 Joker",       8.0),
        ],
        "start_msg": "Le jeu est distribué...",
        "run_msgs": ["Les cartes volent !", "Un bluff tendu !", "Dernière carte retournée !"],
    },
    "tir": {
        "name": "🎯 Tir à la Cible",
        "emoji": "🎯",
        "label": "tir",
        "contestants": [
            ("🎯 Œil-de-Faucon", 1.5),
            ("🎯 Tireur d'Élite",2.2),
            ("🎯 Sniper",        3.0),
            ("🎯 Le Borgne",     5.5),
            ("🎯 L'Aveugle",     9.0),
        ],
        "start_msg": "Les tireurs prennent position...",
        "run_msgs": ["Le vent se lève !", "Un tir manque de peu !", "La cible oscille !"],
    },
    "recolte": {
        "name": "🌱 Paris sur la Récolte",
        "emoji": "🌱",
        "label": "recolte",
        "contestants": [
            ("🌹 Rose",        1.5),
            ("🌻 Tournesol",   2.2),
            ("🍒 Cerise",      3.5),
            ("🍎 Pomme",       5.0),
            ("💎 Diamant",     9.0),
        ],
        "start_msg": "Les plantes s'élancent vers le soleil...",
        "run_msgs": ["La pluie tombe !", "Le soleil brille fort !", "Les racines s'agitent !"],
    },
}

# Ordre d'affichage du menu
GAME_KEYS = list(GAMES.keys())

# ─── État en mémoire des sessions de paris ────────────────────────────────────
# { session_id: { ... } }
active_sessions = {}

SESSION_JOIN_TIMEOUT  = 45
SESSION_PICK_TIMEOUT  = 30

def _session_id(chat_id: int, msg_id: int) -> str:
    return f"{chat_id}:{msg_id}"

def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")

# ─── Utilitaires ──────────────────────────────────────────────────────────────

def _game_menu_keyboard(session_id: str) -> InlineKeyboardMarkup:
    rows = []
    keys = GAME_KEYS
    for i in range(0, len(keys), 3):
        row = []
        for k in keys[i:i+3]:
            g = GAMES[k]
            row.append(InlineKeyboardButton(
                f"{g['emoji']} {g['label'].capitalize()}",
                callback_data=f"rb:game:{session_id}:{k}"
            ))
        rows.append(row)
    return InlineKeyboardMarkup(rows)

def _join_keyboard(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Rejoindre la course", callback_data=f"rb:join:{session_id}")
    ]])

def _pick_keyboard(session_id: str, game_key: str) -> InlineKeyboardMarkup:
    game = GAMES[game_key]
    rows = []
    for i, (name, cote) in enumerate(game["contestants"]):
        rows.append([InlineKeyboardButton(
            f"{name}  (x{cote})",
            callback_data=f"rb:pick:{session_id}:{i}"
        )])
    return InlineKeyboardMarkup(rows)

def _duel_accept_keyboard(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🤝 Accepter le duel", callback_data=f"rb:duel_accept:{session_id}")
    ]])

def _render_participants(session: dict) -> str:
    game = GAMES[session["game_key"]]
    lines = []
    for uid, info in session["participants"].items():
        pick = info.get("pick")
        if pick is not None:
            name, cote = game["contestants"][pick]
            lines.append(f"  • {info['name']} → {name} (x{cote})")
        else:
            lines.append(f"  • {info['name']} → en attente de choix...")
    return "\n".join(lines) if lines else "  (aucun participant)"

# ─── /bet ─────────────────────────────────────────────────────────────────────

async def bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return await update.message.reply_text("Cette commande est réservée aux groupes.")

    if not context.args:
        return await update.message.reply_text(
            "Usage :\n"
            "/bet [mise]        — ouvre une course au groupe\n"
            "/bet [mise] @user  — duel contre un joueur\n\n"
            "Mise minimum : 500 $."
        )

    try:
        mise = int(context.args[0].replace(",", "").replace(" ", ""))
        assert mise >= 500
    except (ValueError, AssertionError):
        return await update.message.reply_text("Mise minimum : 500 $.")

    proposer = await ensure_user(update.effective_user)
    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        u = await get_user(session, proposer.user_id)
        if not u or u.coins < mise:
            return await update.message.reply_text("Solde insuffisant !")

    # ── Mode duel ──
    duel_target = None
    if len(context.args) >= 2:
        target_tg = await parse_target(update, context)
        if target_tg:
            duel_target = await ensure_user(target_tg)
            if duel_target.user_id == proposer.user_id:
                return await update.message.reply_text("Tu ne peux pas te défier toi-même !")

    # ── Choisir le jeu : afficher le menu ──
    msg = await update.message.reply_text(
        f"{'⚔️ DUEL' if duel_target else '🎮 NOUVEAU PARI'} — {mention(proposer)}\n"
        f"Mise : {_fmt(mise)} ${'  •  Duel contre ' + mention(duel_target) if duel_target else ''}\n\n"
        "Choisis le jeu :",
        reply_markup=_game_menu_keyboard("__tmp__"),
        parse_mode=ParseMode.HTML,
    )

    sid = _session_id(group_id, msg.message_id)

    # Mettre à jour les boutons avec le vrai session_id
    await msg.edit_reply_markup(_game_menu_keyboard(sid))

    active_sessions[sid] = {
        "phase": "game_pick",
        "mise": mise,
        "group_id": group_id,
        "proposer_id": proposer.user_id,
        "proposer_name": proposer.first_name,
        "duel_target_id": duel_target.user_id if duel_target else None,
        "duel_target_name": duel_target.first_name if duel_target else None,
        "game_key": None,
        "participants": {},
        "msg_id": msg.message_id,
        "chat_id": group_id,
        "bot": context.bot,
    }

# ─── Callbacks ────────────────────────────────────────────────────────────────

async def race_bet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    action = parts[1]

    if action == "game":
        # rb:game:{sid}:{game_key}  — peut contenir ":" dans sid
        sid      = parts[2] + ":" + parts[3]
        game_key = parts[4]
        await _handle_game_pick(query, sid, game_key)

    elif action == "join":
        sid = parts[2] + ":" + parts[3]
        await _handle_join(query, context, sid)

    elif action == "duel_accept":
        sid = parts[2] + ":" + parts[3]
        await _handle_duel_accept(query, context, sid)

    elif action == "pick":
        sid        = parts[2] + ":" + parts[3]
        contestant = int(parts[4])
        await _handle_pick(query, context, sid, contestant)


async def _handle_game_pick(query, sid: str, game_key: str):
    sess = active_sessions.get(sid)
    if not sess or sess["phase"] != "game_pick":
        return await query.answer("Session expirée.", show_alert=True)

    if query.from_user.id != sess["proposer_id"]:
        return await query.answer("Seul le proposant choisit le jeu.", show_alert=True)

    game = GAMES[game_key]
    sess["game_key"] = game_key
    sess["phase"]    = "joining" if not sess["duel_target_id"] else "duel_waiting"

    if sess["duel_target_id"]:
        # Mode duel : on attend que la cible accepte
        sess["phase"] = "duel_waiting"
        contestants_txt = "\n".join(
            f"  {i+1}. {name}  (cote x{cote})"
            for i, (name, cote) in enumerate(game["contestants"])
        )
        await query.edit_message_text(
            f"⚔️ {game['name']}\n\n"
            f"{sess['proposer_name']} défie {sess['duel_target_name']} !\n"
            f"Mise : {_fmt(sess['mise'])} $ chacun\n\n"
            f"Concurrents :\n{contestants_txt}\n\n"
            f"⏳ {sess['duel_target_name']}, tu as {SESSION_JOIN_TIMEOUT}s pour accepter.",
            reply_markup=_duel_accept_keyboard(sid),
        )
        asyncio.create_task(_timeout_duel(sid, sess["bot"], sess["chat_id"], sess["msg_id"]))
    else:
        # Mode groupe : phase de recrutement
        contestants_txt = "\n".join(
            f"  {i+1}. {name}  (cote x{cote})"
            for i, (name, cote) in enumerate(game["contestants"])
        )
        await query.edit_message_text(
            f"🎮 {game['name']}\n\n"
            f"Proposé par {sess['proposer_name']}\n"
            f"Mise : {_fmt(sess['mise'])} $\n\n"
            f"Concurrents :\n{contestants_txt}\n\n"
            f"⏳ {SESSION_JOIN_TIMEOUT}s pour rejoindre la course !",
            reply_markup=_join_keyboard(sid),
        )
        asyncio.create_task(_timeout_join(sid, sess["bot"], sess["chat_id"], sess["msg_id"]))


async def _handle_join(query, context, sid: str):
    sess = active_sessions.get(sid)
    if not sess or sess["phase"] != "joining":
        return await query.answer("Inscription fermée.", show_alert=True)

    user = await ensure_user(query.from_user)
    uid  = user.user_id

    if uid in sess["participants"]:
        return await query.answer("Tu es déjà inscrit !", show_alert=True)

    async with AsyncSessionLocal() as session:
        u = await get_user(session, uid)
        if not u or u.coins < sess["mise"]:
            return await query.answer("Solde insuffisant !", show_alert=True)

    sess["participants"][uid] = {"name": user.first_name, "pick": None}
    await query.answer(f"Inscrit ! Tu as {SESSION_PICK_TIMEOUT}s pour choisir ton concurrent.")

    game = GAMES[sess["game_key"]]
    contestants_txt = "\n".join(
        f"  {i+1}. {name}  (x{cote})"
        for i, (name, cote) in enumerate(game["contestants"])
    )
    participants_txt = "\n".join(f"  • {v['name']}" for v in sess["participants"].values())

    try:
        await query.edit_message_text(
            f"🎮 {game['name']}\n\n"
            f"Mise : {_fmt(sess['mise'])} $\n\n"
            f"Concurrents :\n{contestants_txt}\n\n"
            f"Participants ({len(sess['participants'])}) :\n{participants_txt}\n\n"
            f"⏳ {SESSION_JOIN_TIMEOUT}s restantes pour rejoindre.",
            reply_markup=_join_keyboard(sid),
        )
    except Exception:
        pass


async def _handle_duel_accept(query, context, sid: str):
    sess = active_sessions.get(sid)
    if not sess or sess["phase"] != "duel_waiting":
        return await query.answer("Session expirée.", show_alert=True)

    if query.from_user.id != sess["duel_target_id"]:
        return await query.answer("Ce duel ne te concerne pas.", show_alert=True)

    # Vérifier solde des deux
    proposer_id = sess["proposer_id"]
    target_id   = sess["duel_target_id"]
    mise        = sess["mise"]

    async with AsyncSessionLocal() as session:
        p = await get_user(session, proposer_id)
        t = await get_user(session, target_id)
        if not p or p.coins < mise:
            await query.edit_message_text("Le proposant n'a plus assez de $ !")
            active_sessions.pop(sid, None)
            return
        if not t or t.coins < mise:
            return await query.answer("Tu n'as pas assez de $ !", show_alert=True)

    sess["participants"][proposer_id] = {"name": sess["proposer_name"], "pick": None}
    sess["participants"][target_id]   = {"name": sess["duel_target_name"], "pick": None}
    sess["phase"] = "picking"

    game = GAMES[sess["game_key"]]
    contestants_txt = "\n".join(
        f"  {i+1}. {name}  (x{cote})"
        for i, (name, cote) in enumerate(game["contestants"])
    )
    await query.edit_message_text(
        f"⚔️ DUEL ACCEPTÉ — {game['name']}\n\n"
        f"{sess['proposer_name']} vs {sess['duel_target_name']}\n"
        f"Mise : {_fmt(mise)} $ chacun\n\n"
        f"Concurrents :\n{contestants_txt}\n\n"
        f"⏳ {SESSION_PICK_TIMEOUT}s pour choisir votre concurrent !",
        reply_markup=_pick_keyboard(sid, sess["game_key"]),
    )
    asyncio.create_task(_timeout_pick(sid, sess["bot"], sess["chat_id"], sess["msg_id"]))


async def _handle_pick(query, context, sid: str, contestant_idx: int):
    sess = active_sessions.get(sid)
    if not sess or sess["phase"] not in ("picking", "joining"):
        return await query.answer("Phase de choix terminée.", show_alert=True)

    uid = query.from_user.id
    if uid not in sess["participants"]:
        return await query.answer("Tu n'es pas inscrit dans cette session.", show_alert=True)

    game = GAMES[sess["game_key"]]
    if contestant_idx >= len(game["contestants"]):
        return await query.answer("Choix invalide.", show_alert=True)

    sess["participants"][uid]["pick"] = contestant_idx
    name, cote = game["contestants"][contestant_idx]
    await query.answer(f"Tu mises sur {name} (x{cote}) !")

    # Passer en phase picking si on était encore en joining
    if sess["phase"] == "joining":
        sess["phase"] = "picking"
        asyncio.create_task(_timeout_pick(sid, sess["bot"], sess["chat_id"], sess["msg_id"]))

    # Vérifier si tout le monde a choisi
    all_picked = all(v["pick"] is not None for v in sess["participants"].values())

    try:
        await query.edit_message_text(
            f"🎮 {game['name']}\n\n"
            f"Choix en cours :\n{_render_participants(sess)}\n\n"
            + ("✅ Tous ont choisi ! La course va démarrer..." if all_picked else f"⏳ En attente des autres choix..."),
            reply_markup=None if all_picked else _pick_keyboard(sid, sess["game_key"]),
        )
    except Exception:
        pass

    if all_picked:
        await asyncio.sleep(2)
        await _run_race(sid)

# ─── Timeouts ─────────────────────────────────────────────────────────────────

async def _timeout_duel(sid: str, bot, chat_id: int, msg_id: int):
    await asyncio.sleep(SESSION_JOIN_TIMEOUT)
    sess = active_sessions.get(sid)
    if not sess or sess["phase"] != "duel_waiting":
        return
    active_sessions.pop(sid, None)
    try:
        await bot.edit_message_text(
            f"⏰ Duel expiré — {sess['duel_target_name']} n'a pas répondu dans les temps.",
            chat_id=chat_id, message_id=msg_id,
        )
    except Exception:
        pass


async def _timeout_join(sid: str, bot, chat_id: int, msg_id: int):
    await asyncio.sleep(SESSION_JOIN_TIMEOUT)
    sess = active_sessions.get(sid)
    if not sess or sess["phase"] != "joining":
        return

    if not sess["participants"]:
        active_sessions.pop(sid, None)
        try:
            await bot.edit_message_text(
                "⏰ Personne n'a rejoint la course. Pari annulé.",
                chat_id=chat_id, message_id=msg_id,
            )
        except Exception:
            pass
        return

    # Assez de monde → passer à la phase de choix
    sess["phase"] = "picking"
    game = GAMES[sess["game_key"]]
    contestants_txt = "\n".join(
        f"  {i+1}. {name}  (x{cote})"
        for i, (name, cote) in enumerate(game["contestants"])
    )
    participants_txt = "\n".join(f"  • {v['name']}" for v in sess["participants"].values())
    try:
        await bot.edit_message_text(
            f"🎮 {game['name']}\n\n"
            f"Mise : {_fmt(sess['mise'])} $\n\n"
            f"Concurrents :\n{contestants_txt}\n\n"
            f"Participants ({len(sess['participants'])}) :\n{participants_txt}\n\n"
            f"⏳ {SESSION_PICK_TIMEOUT}s pour choisir votre concurrent !",
            chat_id=chat_id, message_id=msg_id,
            reply_markup=_pick_keyboard(sid, sess["game_key"]),
        )
    except Exception:
        pass
    asyncio.create_task(_timeout_pick(sid, bot, chat_id, msg_id))


async def _timeout_pick(sid: str, bot, chat_id: int, msg_id: int):
    await asyncio.sleep(SESSION_PICK_TIMEOUT)
    sess = active_sessions.get(sid)
    if not sess or sess["phase"] != "picking":
        return

    # Assigner un concurrent aléatoire à ceux qui n'ont pas choisi
    game = GAMES[sess["game_key"]]
    for uid, info in sess["participants"].items():
        if info["pick"] is None:
            info["pick"] = random.randrange(len(game["contestants"]))

    await _run_race(sid)

# ─── Résolution de la course ──────────────────────────────────────────────────

async def _run_race(sid: str):
    sess = active_sessions.pop(sid, None)
    if not sess:
        return

    bot      = sess["bot"]
    chat_id  = sess["chat_id"]
    msg_id   = sess["msg_id"]
    game     = GAMES[sess["game_key"]]
    mise     = sess["mise"]
    parts    = sess["participants"]

    if not parts:
        return

    # Débiter toutes les mises
    async with AsyncSessionLocal() as session:
        for uid in parts:
            u = await get_user(session, uid)
            if u and u.coins >= mise:
                await add_coins(session, uid, -mise)

    # Animer la course
    try:
        await bot.edit_message_text(
            f"🏁 {game['name']}\n\n{game['start_msg']}",
            chat_id=chat_id, message_id=msg_id,
        )
    except Exception:
        pass

    for line in game["run_msgs"]:
        await asyncio.sleep(1.5)
        try:
            await bot.edit_message_text(
                f"🏁 {game['name']}\n\n{line}",
                chat_id=chat_id, message_id=msg_id,
            )
        except Exception:
            pass

    await asyncio.sleep(1.5)

    # Tirage pondéré du gagnant
    contestants  = game["contestants"]
    weights      = [1 / c[1] for c in contestants]
    winner_idx   = random.choices(range(len(contestants)), weights=weights, k=1)[0]
    winner_name, winner_cote = contestants[winner_idx]

    # Grouper par choix
    from collections import defaultdict
    picks = defaultdict(list)
    for uid, info in parts.items():
        picks[info["pick"]].append(uid)

    # Calculer la cagnotte totale
    total_pot = mise * len(parts)

    # Distribuer les gains
    result_lines = []
    winners_uids = picks.get(winner_idx, [])

    async with AsyncSessionLocal() as session:
        if winners_uids:
            gain_each = total_pot // len(winners_uids)
            for uid in winners_uids:
                await add_coins(session, uid, gain_each)
                profit = gain_each - mise
                name   = parts[uid]["name"]
                if len(winners_uids) > 1:
                    result_lines.append(f"  🏆 {name} +{_fmt(gain_each)} $ (cagnotte partagée)")
                else:
                    result_lines.append(f"  🏆 {name} +{_fmt(gain_each)} $")
        else:
            # Personne n'avait misé sur le gagnant → cagnotte brûlée
            result_lines.append("  💸 Personne n'avait choisi le bon — cagnotte brûlée !")

        # Perdants
        for uid, info in parts.items():
            if uid not in winners_uids:
                result_lines.append(f"  ❌ {info['name']} -{_fmt(mise)} $")

    # Construire le classement de la course
    race_board = []
    shuffled   = list(range(len(contestants)))
    random.shuffle(shuffled)
    for pos, idx in enumerate(shuffled):
        name, _ = contestants[idx]
        bar      = "▓" * (len(contestants) - pos) + "░" * pos
        marker   = " ← GAGNANT" if idx == winner_idx else ""
        race_board.append(f"{name}  {bar}{marker}")

    final_msg = (
        f"🏁 {game['name']} — RÉSULTATS\n\n"
        + "\n".join(race_board)
        + f"\n\n🥇 Vainqueur : {winner_name}\n\n"
        + "\n".join(result_lines)
    )

    try:
        await bot.edit_message_text(final_msg, chat_id=chat_id, message_id=msg_id)
    except Exception:
        try:
            await bot.send_message(chat_id=chat_id, text=final_msg)
        except Exception:
            pass
