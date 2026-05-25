"""
Système d'enchères — FarmBot

Commandes joueurs :
  /bid [montant]         — enchérir sur l'objet en cours dans le groupe
  /expertise             — révéler la vraie valeur de ton dernier objet gagné
  /myitems               — voir ton inventaire
  /sellitem [id] [prix]  — mettre un objet en vente entre joueurs
  /shopitems             — voir les objets en vente par d'autres joueurs
  /buyitem [id]          — acheter un objet d'un autre joueur

Fonctionnement automatique :
  - 2x/jour (8h et 20h UTC), une enchère démarre dans chaque groupe actif
  - Durée : 10 minutes
  - L'objet a une mise de départ, une vraie valeur cachée révélée par /expertise
  - Si quelqu'un surenchérit, l'ancien leader est remboursé automatiquement
"""

import random
import logging
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from sqlalchemy import text

from database.db import AsyncSessionLocal, get_user, add_coins
from utils.helpers import ensure_user
from config import CURRENCY

logger = logging.getLogger(__name__)

# ─── CATALOGUE D'OBJETS ───────────────────────────────────────────────────────

ITEMS = [
    # (item_id, nom, emoji, rareté, valeur_min, valeur_max, mise_départ)
    # ── Camelote ──
    ("chaussette_perdue",   "Chaussette Perdue",          "🧦", "camelote",    0,            500,           100),
    ("tasse_ebrechee",      "Tasse Ébréchée",             "🍵", "camelote",    0,            1_000,         200),
    ("carte_pokemon_fake",  "Carte Pokémon Fake",         "🃏", "camelote",    0,            2_000,         500),
    ("bougie_fondue",       "Bougie à Moitié Fondue",     "🕯️","camelote",    0,            800,           150),
    ("stylo_vide",          "Stylo Sans Encre",           "🖊️","camelote",    0,            300,           50),
    ("parapluie_casse",     "Parapluie Cassé",            "☂️","camelote",    0,            1_500,         300),
    # ── Commun ──
    ("pierre_rare",         "Pierre 'Rare'",              "🪨", "commun",      500,          10_000,        2_000),
    ("montre_cassee",       "Montre Cassée de Luxe",      "⌚", "commun",      1_000,        25_000,        5_000),
    ("tableau_douteux",     "Tableau Douteux",            "🖼️","commun",      5_000,        50_000,        10_000),
    ("livre_ancien",        "Livre Ancien Illisible",     "📖", "commun",      2_000,        30_000,        6_000),
    ("vase_fissure",        "Vase de Chine Fissuré",      "🏺", "commun",      3_000,        40_000,        8_000),
    ("boussole_rouilee",    "Boussole Rouillée",          "🧭", "commun",      1_500,        20_000,        4_000),
    # ── Rare ──
    ("violon_ancien",       "Vieux Violon",               "🎻", "rare",        20_000,       200_000,       40_000),
    ("bague_or",            "Bague en Or",                "💍", "rare",        50_000,       500_000,       80_000),
    ("crypto_cle",          "Clé USB Crypto Oubliée",     "🔑", "rare",        10_000,       1_000_000,     100_000),
    ("epee_medievale",      "Épée Médiévale",             "⚔️","rare",        30_000,       300_000,       60_000),
    ("parchemin_magique",   "Parchemin Mystérieux",       "📜", "rare",        25_000,       250_000,       50_000),
    ("telescope_antique",   "Télescope Antique",          "🔭", "rare",        40_000,       400_000,       70_000),
    ("masque_venitien",     "Masque Vénitien d'Époque",   "🎭", "rare",        15_000,       180_000,       35_000),
    # ── Épique ──
    ("diamant_brut",        "Diamant Brut",               "💎", "épique",      200_000,      5_000_000,     500_000),
    ("tableau_celebre",     "Tableau Célèbre Volé",       "🎨", "épique",      500_000,      20_000_000,    2_000_000),
    ("medaille_olympic",    "Médaille Olympique",         "🏅", "épique",      1_000_000,    50_000_000,    5_000_000),
    ("coffre_tresor",       "Coffre au Trésor Scellé",    "🪙", "épique",      800_000,      30_000_000,    3_000_000),
    ("drone_militaire",     "Drone Militaire Volé",       "🚁", "épique",      600_000,      25_000_000,    2_500_000),
    ("couronne_royale",     "Couronne Royale",            "👑", "épique",      1_500_000,    80_000_000,    8_000_000),
    # ── Légendaire ──
    ("artefact_alien",      "Artefact Alien",             "👽", "légendaire",  10_000_000,   500_000_000,   50_000_000),
    ("etoile_morte",        "Fragment d'Étoile Morte",    "⭐", "légendaire",  100_000_000,  2_000_000_000, 200_000_000),
    ("saint_graal",         "Le Saint Graal",             "🏆", "légendaire",  50_000_000,   1_000_000_000, 100_000_000),
    ("larme_dragon",        "Larme de Dragon",            "🐉", "légendaire",  30_000_000,   800_000_000,   80_000_000),
    ("source_code_ia",      "Code Source d'une IA Secrète","🤖","légendaire",  200_000_000,  5_000_000_000, 500_000_000),
]

RARITY_EMOJI = {
    "camelote":   "⚪",
    "commun":     "🟢",
    "rare":       "🔵",
    "épique":     "🟣",
    "légendaire": "🟡",
}

def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")

def _random_item():
    """Choisit un objet aléatoire avec pondération par rareté."""
    weights = {
        "camelote":   35,
        "commun":     30,
        "rare":       20,
        "épique":     10,
        "légendaire": 5,
    }
    pool = [(item, weights[item[3]]) for item in ITEMS]
    items, w = zip(*pool)
    return random.choices(items, weights=w, k=1)[0]

# ─── INIT TABLES ──────────────────────────────────────────────────────────────

async def init_auction_tables():
    # Chaque table dans sa propre transaction pour éviter les rollbacks silencieux
    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS auction_sessions (
                id           SERIAL PRIMARY KEY,
                group_id     BIGINT NOT NULL,
                item_id      VARCHAR(50) NOT NULL,
                item_name    VARCHAR(100) NOT NULL,
                item_emoji   VARCHAR(10) NOT NULL,
                rarity       VARCHAR(20) NOT NULL,
                true_value   BIGINT NOT NULL,
                start_price  BIGINT NOT NULL,
                current_bid  BIGINT NOT NULL,
                leader_id    BIGINT,
                leader_name  VARCHAR(255),
                message_id   BIGINT,
                status       VARCHAR(20) DEFAULT 'active',
                started_at   TIMESTAMP DEFAULT NOW(),
                ends_at      TIMESTAMP NOT NULL
            )
        """))
        await session.commit()

    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS auction_inventory (
                id           SERIAL PRIMARY KEY,
                user_id      BIGINT NOT NULL,
                item_id      VARCHAR(50) NOT NULL,
                item_name    VARCHAR(100) NOT NULL,
                item_emoji   VARCHAR(10) NOT NULL,
                rarity       VARCHAR(20) NOT NULL,
                true_value   BIGINT NOT NULL,
                acquired_at  TIMESTAMP DEFAULT NOW(),
                revealed     BOOLEAN DEFAULT FALSE,
                for_sale     BOOLEAN DEFAULT FALSE,
                sale_price   BIGINT,
                daily_delta  FLOAT DEFAULT 0.0
            )
        """))
        await session.commit()

    logger.info("Tables auction initialisées.")

# ─── LANCEMENT D'UNE ENCHÈRE ──────────────────────────────────────────────────

async def _launch_auction(context: ContextTypes.DEFAULT_TYPE, group_id: int):
    """Lance une nouvelle enchère dans un groupe."""
    async with AsyncSessionLocal() as session:
        # Vérifier qu'aucune enchère active n'est en cours
        existing = await session.execute(text(
            "SELECT id FROM auction_sessions WHERE group_id = :gid AND status = 'active'"
        ), {"gid": group_id})
        if existing.fetchone():
            return

        item = _random_item()
        item_id, item_name, item_emoji, rarity, val_min, val_max, start_price = item
        true_value = random.randint(val_min, val_max)
        ends_at = datetime.utcnow() + timedelta(minutes=10)

        res = await session.execute(text("""
            INSERT INTO auction_sessions
                (group_id, item_id, item_name, item_emoji, rarity, true_value,
                 start_price, current_bid, ends_at)
            VALUES (:gid, :iid, :iname, :iemoji, :rarity, :tv, :sp, :sp, :ends)
            RETURNING id
        """), {
            "gid": group_id, "iid": item_id, "iname": item_name,
            "iemoji": item_emoji, "rarity": rarity, "tv": true_value,
            "sp": start_price, "ends": ends_at
        })
        auction_id = res.fetchone()[0]
        await session.commit()

    rarity_icon = RARITY_EMOJI.get(rarity, "⚪")

    # Vérifier si c'est la 1ère enchère de ce groupe
    async with AsyncSessionLocal() as session:
        res_count = await session.execute(text(
            "SELECT COUNT(*) FROM auction_sessions WHERE group_id = :gid"
        ), {"gid": group_id})
        total_auctions = res_count.fetchone()[0]

    if total_auctions <= 1:
        intro = (
            "📢 <b>NOUVEAU — SYSTÈME D'ENCHÈRES !</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🎯 Comment ça marche :\n"
            "• Des objets apparaissent 2x/jour\n"
            "• Enchéris avec <b>/bid [montant]</b>\n"
            "• Le plus offrant remporte l'objet\n"
            "• L'ancien leader est toujours remboursé\n"
            "• Découvre la valeur avec <b>/expertise</b>\n"
            "• Revends tes objets avec <b>/sellitem</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        )
    else:
        intro = ""

    text_msg = (
        f"{intro}"
        f"🔨 <b>ENCHÈRE EN COURS !</b>\n\n"
        f"{item_emoji} <b>{item_name}</b>\n"
        f"{rarity_icon} Rareté : <b>{rarity.capitalize()}</b>\n\n"
        f"💰 Mise de départ : <b>{_fmt(start_price)} {CURRENCY}</b>\n"
        f"❓ Valeur réelle : <b>???</b> (révélée après acquisition)\n\n"
        f"⏳ Enchère ouverte <b>10 minutes</b> !\n"
        f"👇 Enchérir avec <b>/bid [montant]</b>"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 Infos", callback_data=f"auction:info:{auction_id}"),
        InlineKeyboardButton("⚡ Enchérir vite !", callback_data=f"auction:bid:{auction_id}"),
    ]])

    try:
        msg = await context.bot.send_message(
            chat_id=group_id,
            text=text_msg,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )
        async with AsyncSessionLocal() as session:
            await session.execute(text(
                "UPDATE auction_sessions SET message_id = :mid WHERE id = :aid"
            ), {"mid": msg.message_id, "aid": auction_id})
            await session.commit()

        # Programmer la clôture dans 10 minutes
        context.job_queue.run_once(
            _close_auction,
            when=timedelta(minutes=10),
            data={"group_id": group_id, "auction_id": auction_id},
            name=f"close_auction_{auction_id}"
        )
    except Exception as e:
        logger.error(f"Erreur lancement enchère groupe {group_id}: {e}")

# ─── CLÔTURE D'UNE ENCHÈRE ────────────────────────────────────────────────────

async def _close_auction(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    group_id = job_data["group_id"]
    auction_id = job_data["auction_id"]

    async with AsyncSessionLocal() as session:
        res = await session.execute(text(
            "SELECT * FROM auction_sessions WHERE id = :aid AND status = 'active'"
        ), {"aid": auction_id})
        auction = res.fetchone()
        if not auction:
            return

        await session.execute(text(
            "UPDATE auction_sessions SET status = 'closed' WHERE id = :aid"
        ), {"aid": auction_id})

        if auction.leader_id:
            # Ajouter l'objet à l'inventaire du gagnant
            daily_delta = random.uniform(-0.15, 0.15)
            await session.execute(text("""
                INSERT INTO auction_inventory
                    (user_id, item_id, item_name, item_emoji, rarity, true_value, daily_delta)
                VALUES (:uid, :iid, :iname, :iemoji, :rarity, :tv, :delta)
            """), {
                "uid": auction.leader_id, "iid": auction.item_id,
                "iname": auction.item_name, "iemoji": auction.item_emoji,
                "rarity": auction.rarity, "tv": auction.true_value,
                "delta": daily_delta
            })

        await session.commit()

    if auction.leader_id:
        msg = (
            f"🏆 <b>Enchère terminée !</b>\n\n"
            f"{auction.item_emoji} <b>{auction.item_name}</b>\n"
            f"🥇 Gagnant : <b>{auction.leader_name}</b>\n"
            f"💰 Prix payé : <b>{_fmt(auction.current_bid)} {CURRENCY}</b>\n\n"
            f"👉 Utilise <b>/expertise</b> pour révéler la vraie valeur !"
        )
    else:
        msg = (
            f"😔 <b>Enchère terminée — personne n'a enchéri !</b>\n\n"
            f"{auction.item_emoji} <b>{auction.item_name}</b> repart sans preneur."
        )

    try:
        await context.bot.send_message(chat_id=group_id, text=msg, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Erreur clôture enchère: {e}")

# ─── JOB AUTOMATIQUE ──────────────────────────────────────────────────────────

async def _auction_job(context: ContextTypes.DEFAULT_TYPE):
    """Déclenche des enchères dans tous les groupes enregistrés."""
    async with AsyncSessionLocal() as session:
        res = await session.execute(text(
            "SELECT DISTINCT group_id FROM group_settings"
        ))
        groups = [row[0] for row in res.fetchall()]

    for group_id in groups:
        try:
            await _launch_auction(context, group_id)
        except Exception as e:
            logger.error(f"Erreur auction job groupe {group_id}: {e}")

def setup_auction_jobs(app):
    from datetime import time as dtime
    app.job_queue.run_daily(
        _auction_job,
        time=dtime(hour=8, minute=0),
        name="auction_morning"
    )
    app.job_queue.run_daily(
        _auction_job,
        time=dtime(hour=20, minute=0),
        name="auction_evening"
    )

# ─── COMMANDE /bid ────────────────────────────────────────────────────────────

async def bid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("❌ Les enchères ont lieu dans les groupes !")
        return

    await ensure_user(user)

    if not context.args:
        await update.message.reply_text("Usage : <code>/bid [montant]</code>", parse_mode=ParseMode.HTML)
        return

    try:
        amount = int(context.args[0].replace(" ", "").replace("_", ""))
    except ValueError:
        await update.message.reply_text("❌ Montant invalide.")
        return

    async with AsyncSessionLocal() as session:
        # Enchère active dans ce groupe
        res = await session.execute(text(
            "SELECT * FROM auction_sessions WHERE group_id = :gid AND status = 'active'"
        ), {"gid": chat.id})
        auction = res.fetchone()

        if not auction:
            await update.message.reply_text("❌ Aucune enchère en cours dans ce groupe.")
            return

        if auction.ends_at < datetime.utcnow():
            await update.message.reply_text("⏰ L'enchère est terminée !")
            return

        min_bid = max(auction.current_bid + 1, int(auction.current_bid * 1.05))
        if amount < min_bid:
            await update.message.reply_text(
                f"❌ L'enchère minimale est de <b>{_fmt(min_bid)} {CURRENCY}</b> "
                f"(+5% sur la mise actuelle).",
                parse_mode=ParseMode.HTML
            )
            return

        # Vérifier le solde
        db_user = await get_user(session, user.id)
        if not db_user or db_user.coins < amount:
            await update.message.reply_text("❌ Pas assez de $ !")
            return

        # Rembourser l'ancien leader
        old_leader = auction.leader_id
        old_bid = auction.current_bid
        if old_leader and old_leader != user.id:
            await session.execute(text(
                "UPDATE users SET coins = coins + :amt WHERE user_id = :uid"
            ), {"amt": old_bid, "uid": old_leader})

        # Débiter le nouveau leader
        await session.execute(text(
            "UPDATE users SET coins = coins - :amt WHERE user_id = :uid"
        ), {"amt": amount, "uid": user.id})

        # Mettre à jour l'enchère
        name = user.first_name[:50]
        await session.execute(text("""
            UPDATE auction_sessions
            SET current_bid = :bid, leader_id = :uid, leader_name = :name
            WHERE id = :aid
        """), {"bid": amount, "uid": user.id, "name": name, "aid": auction.id})
        await session.commit()

    time_left = max(0, int((auction.ends_at - datetime.utcnow()).total_seconds() / 60))
    await update.message.reply_text(
        f"✅ <b>Enchère placée !</b>\n\n"
        f"{auction.item_emoji} <b>{auction.item_name}</b>\n"
        f"💰 Ton offre : <b>{_fmt(amount)} {CURRENCY}</b>\n"
        f"⏳ Temps restant : ~{time_left} minute(s)\n\n"
        f"{'🔔 Lancien leader a été remboursé.' if old_leader and old_leader != user.id else ''}",
        parse_mode=ParseMode.HTML
    )

# ─── CALLBACK BOUTONS ─────────────────────────────────────────────────────────

async def auction_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data  # auction:bid:ID ou auction:info:ID

    parts = data.split(":")
    action = parts[1]
    auction_id = int(parts[2])

    async with AsyncSessionLocal() as session:
        res = await session.execute(text(
            "SELECT * FROM auction_sessions WHERE id = :aid"
        ), {"aid": auction_id})
        auction = res.fetchone()

    if not auction:
        await query.answer("Enchère introuvable.", show_alert=True)
        return

    if action == "info":
        time_left = max(0, int((auction.ends_at - datetime.utcnow()).total_seconds() / 60))
        status = "🟢 En cours" if auction.status == "active" else "🔴 Terminée"
        rarity_icon = RARITY_EMOJI.get(auction.rarity, "⚪")
        msg = (
            f"{auction.item_emoji} <b>{auction.item_name}</b>\n"
            f"{rarity_icon} Rareté : <b>{auction.rarity.capitalize()}</b>\n"
            f"📊 Statut : {status}\n"
            f"💰 Mise actuelle : <b>{_fmt(auction.current_bid)} {CURRENCY}</b>\n"
            f"👑 Leader : <b>{auction.leader_name or 'Personne'}</b>\n"
            f"⏳ Temps restant : ~{time_left} min"
        )
        await query.answer(msg[:200], show_alert=True)

    elif action == "bid":
        await query.answer(
            f"Utilise /bid [montant] pour enchérir !\n"
            f"Mise actuelle : {_fmt(auction.current_bid)} {CURRENCY}",
            show_alert=True
        )

# ─── COMMANDE /expertise ─────────────────────────────────────────────────────

async def expertise(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user)

    async with AsyncSessionLocal() as session:
        res = await session.execute(text("""
            SELECT * FROM auction_inventory
            WHERE user_id = :uid AND revealed = FALSE
            ORDER BY acquired_at DESC LIMIT 1
        """), {"uid": user.id})
        item = res.fetchone()

        if not item:
            await update.message.reply_text(
                "❌ Tu n'as aucun objet non révélé dans ton inventaire.\n"
                "Utilise /myitems pour voir tes objets."
            )
            return

        await session.execute(text(
            "UPDATE auction_inventory SET revealed = TRUE WHERE id = :iid"
        ), {"iid": item.id})
        await session.commit()

    rarity_icon = RARITY_EMOJI.get(item.rarity, "⚪")
    verdict = ""
    if item.true_value == 0:
        verdict = "😭 C'est de la <b>camelote absolue</b>, ça ne vaut rien !"
    elif item.true_value < 10_000:
        verdict = "😐 Pas grand chose..."
    elif item.true_value < 100_000:
        verdict = "🙂 Pas mal du tout !"
    elif item.true_value < 1_000_000:
        verdict = "😲 Très bonne affaire !"
    elif item.true_value < 100_000_000:
        verdict = "🤑 <b>EXCELLENT</b> investissement !"
    else:
        verdict = "🏆 <b>LÉGENDAIRE !</b> Tu es riche !"

    await update.message.reply_text(
        f"🔍 <b>EXPERTISE</b>\n\n"
        f"{item.item_emoji} <b>{item.item_name}</b>\n"
        f"{rarity_icon} Rareté : <b>{item.rarity.capitalize()}</b>\n\n"
        f"💎 Valeur réelle : <b>{_fmt(item.true_value)} {CURRENCY}</b>\n\n"
        f"{verdict}\n\n"
        f"Tu peux maintenant le vendre avec <code>/sellitem {item.id} [prix]</code>",
        parse_mode=ParseMode.HTML
    )

async def expertise_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback pour sellnow/keep après expertise."""
    query = update.callback_query
    await query.answer()
    data = query.data  # auction:sellnow:ID ou auction:keep:ID
    parts = data.split(":")
    action = parts[1]
    item_id = int(parts[2])

    if action == "keep":
        await query.edit_message_text("✅ Objet conservé dans ton inventaire.")
        return

    # sellnow : vendre immédiatement à la valeur réelle
    async with AsyncSessionLocal() as session:
        res = await session.execute(text(
            "SELECT * FROM auction_inventory WHERE id = :iid AND user_id = :uid"
        ), {"iid": item_id, "uid": query.from_user.id})
        item = res.fetchone()
        if not item:
            await query.answer("Objet introuvable.", show_alert=True)
            return

        await add_coins(query.from_user.id, item.true_value)
        await session.execute(text(
            "DELETE FROM auction_inventory WHERE id = :iid"
        ), {"iid": item_id})
        await session.commit()

    await query.edit_message_text(
        f"💰 Vendu ! Tu as reçu <b>{_fmt(item.true_value)} {CURRENCY}</b>.",
        parse_mode=ParseMode.HTML
    )

# ─── COMMANDE /myitems ────────────────────────────────────────────────────────

async def myitems(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user)

    async with AsyncSessionLocal() as session:
        res = await session.execute(text("""
            SELECT * FROM auction_inventory
            WHERE user_id = :uid
            ORDER BY acquired_at DESC
            LIMIT 20
        """), {"uid": user.id})
        items = res.fetchall()

    if not items:
        await update.message.reply_text("🎒 Ton inventaire est vide. Participe aux enchères avec /bid !")
        return

    lines = [f"🎒 <b>Ton inventaire ({len(items)} objet(s)) :</b>\n"]
    for it in items:
        rarity_icon = RARITY_EMOJI.get(it.rarity, "⚪")
        value_str = _fmt(it.true_value) if it.revealed else "???"
        sale_str = f" | 🏷️ En vente : {_fmt(it.sale_price)}" if it.for_sale else ""
        lines.append(
            f"#{it.id} {it.item_emoji} <b>{it.item_name}</b> "
            f"{rarity_icon} | 💎 {value_str}{sale_str}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

# ─── COMMANDE /sellitem ───────────────────────────────────────────────────────

async def sellitem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user)

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage : <code>/sellitem [id_objet] [prix]</code>",
            parse_mode=ParseMode.HTML
        )
        return

    try:
        item_id = int(context.args[0])
        price = int(context.args[1].replace(" ", "").replace("_", ""))
    except ValueError:
        await update.message.reply_text("❌ Paramètres invalides.")
        return

    if price <= 0:
        await update.message.reply_text("❌ Le prix doit être positif.")
        return

    async with AsyncSessionLocal() as session:
        res = await session.execute(text(
            "SELECT * FROM auction_inventory WHERE id = :iid AND user_id = :uid"
        ), {"iid": item_id, "uid": user.id})
        item = res.fetchone()

        if not item:
            await update.message.reply_text("❌ Objet introuvable dans ton inventaire.")
            return

        await session.execute(text(
            "UPDATE auction_inventory SET for_sale = TRUE, sale_price = :price WHERE id = :iid"
        ), {"price": price, "iid": item_id})
        await session.commit()

    await update.message.reply_text(
        f"✅ {item.item_emoji} <b>{item.item_name}</b> mis en vente pour "
        f"<b>{_fmt(price)} {CURRENCY}</b> !\n"
        f"Les autres joueurs peuvent l'acheter avec <code>/buyitem {item_id}</code>",
        parse_mode=ParseMode.HTML
    )

# ─── COMMANDE /shopitems ─────────────────────────────────────────────────────

async def shopitems(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncSessionLocal() as session:
        res = await session.execute(text("""
            SELECT ai.*, u.first_name as seller_name
            FROM auction_inventory ai
            JOIN users u ON u.user_id = ai.user_id
            WHERE ai.for_sale = TRUE
            ORDER BY ai.sale_price ASC
            LIMIT 20
        """))
        items = res.fetchall()

    if not items:
        await update.message.reply_text("🏪 Aucun objet en vente pour le moment.")
        return

    lines = [f"🏪 <b>Boutique des joueurs ({len(items)} objet(s)) :</b>\n"]
    for it in items:
        rarity_icon = RARITY_EMOJI.get(it.rarity, "⚪")
        value_str = _fmt(it.true_value) if it.revealed else "???"
        lines.append(
            f"#{it.id} {it.item_emoji} <b>{it.item_name}</b> {rarity_icon}\n"
            f"   💎 Valeur : {value_str} | 🏷️ Prix : <b>{_fmt(it.sale_price)}</b> "
            f"| 👤 {it.seller_name}"
        )

    lines.append("\n👉 Acheter avec <code>/buyitem [id]</code>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

# ─── COMMANDE /buyitem ────────────────────────────────────────────────────────

async def buyitem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user)

    if not context.args:
        await update.message.reply_text(
            "Usage : <code>/buyitem [id_objet]</code>",
            parse_mode=ParseMode.HTML
        )
        return

    try:
        item_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID invalide.")
        return

    async with AsyncSessionLocal() as session:
        res = await session.execute(text(
            "SELECT * FROM auction_inventory WHERE id = :iid AND for_sale = TRUE"
        ), {"iid": item_id})
        item = res.fetchone()

        if not item:
            await update.message.reply_text("❌ Objet introuvable ou plus en vente.")
            return

        if item.user_id == user.id:
            await update.message.reply_text("❌ Tu ne peux pas acheter ton propre objet !")
            return

        buyer = await get_user(session, user.id)
        if not buyer or buyer.coins < item.sale_price:
            await update.message.reply_text(
                f"❌ Tu n'as pas assez de {CURRENCY} ! Il te faut <b>{_fmt(item.sale_price)}</b>.",
                parse_mode=ParseMode.HTML
            )
            return

        # Transaction
        await session.execute(text(
            "UPDATE users SET coins = coins - :amt WHERE user_id = :uid"
        ), {"amt": item.sale_price, "uid": user.id})
        await session.execute(text(
            "UPDATE users SET coins = coins + :amt WHERE user_id = :uid"
        ), {"amt": item.sale_price, "uid": item.user_id})
        await session.execute(text("""
            UPDATE auction_inventory
            SET user_id = :buyer, for_sale = FALSE, sale_price = NULL
            WHERE id = :iid
        """), {"buyer": user.id, "iid": item_id})
        await session.commit()

    await update.message.reply_text(
        f"✅ Tu as acheté {item.item_emoji} <b>{item.item_name}</b> pour "
        f"<b>{_fmt(item.sale_price)} {CURRENCY}</b> !",
        parse_mode=ParseMode.HTML
    )

    try:
        await context.bot.send_message(
            chat_id=item.user_id,
            text=f"💰 Ton objet {item.item_emoji} <b>{item.item_name}</b> a été vendu pour "
                 f"<b>{_fmt(item.sale_price)} {CURRENCY}</b> !",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass


# ─── LA SALLE VIP ─────────────────────────────────────────────────────────────

SALLE_ITEMS = [
    # (item_id, nom, emoji, rareté, valeur_min, valeur_max, mise_départ, desc)
    ("couronne_roi_soleil",  "Couronne du Roi Soleil",      "👑", "mythique",  5_000_000,   50_000_000,  1_000_000, "Forgée pour un monarque oublié, elle irradie encore."),
    ("epee_legendaire",      "Épée des Légendes",           "⚔️","mythique",  8_000_000,   80_000_000,  2_000_000, "Une lame qui n'a jamais connu la défaite."),
    ("cristal_obscur",       "Cristal des Ombres",          "🔮", "mythique",  6_000_000,   60_000_000,  1_500_000, "Renferme une dimension entière en son cœur."),
    ("grimoire_interdit",    "Grimoire Interdit",           "📕", "mythique",  4_000_000,   40_000_000,  800_000,   "Chaque page est un secret que le monde a voulu effacer."),
    ("trone_ancien",         "Trône des Anciens",           "🪑", "mythique",  10_000_000,  100_000_000, 3_000_000, "Celui qui s'y assoit voit tout ce qui fut."),
    ("masque_mort",          "Masque de la Mort",           "💀", "mythique",  7_000_000,   70_000_000,  1_800_000, "Porté lors des rituels les plus sombres."),
    ("dragon_empaille",      "Dragon Empaillé Authentique", "🐉", "mythique",  9_000_000,   90_000_000,  2_500_000, "Dernier spécimen connu de son espèce."),
    ("etoile_noire",         "Étoile Noire",                "⭐", "divin",     50_000_000,  500_000_000, 10_000_000, "Fragment de la première étoile morte de l'univers."),
    ("artefact_divin",       "Artefact des Dieux",          "✨", "divin",     80_000_000,  800_000_000, 15_000_000, "Sa seule existence défie les lois de la physique."),
    ("oeil_cosmos",          "Œil du Cosmos",               "👁️","divin",     100_000_000, 1_000_000_000,20_000_000,"Regarde dans cet œil et l'univers te regarde en retour."),
    ("clef_eternite",        "Clef de l'Éternité",          "🗝️","divin",     60_000_000,  600_000_000, 12_000_000, "Ouvre une porte vers un lieu où le temps n'existe pas."),
    ("couronne_cosmos",      "Couronne du Cosmos",          "💫", "divin",     120_000_000, 1_200_000_000,25_000_000,"Portée par l'être qui a créé les étoiles."),
]


async def init_salle_tables():
    """Crée les tables pour La Salle VIP si elles n'existent pas."""
    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS salle_auctions (
                id           SERIAL PRIMARY KEY,
                item_id      VARCHAR(50)  NOT NULL,
                item_name    VARCHAR(100) NOT NULL,
                item_emoji   VARCHAR(10)  NOT NULL,
                rarity       VARCHAR(20)  NOT NULL,
                true_value   BIGINT       NOT NULL,
                start_price  BIGINT       NOT NULL,
                current_bid  BIGINT       NOT NULL,
                leader_id    BIGINT,
                leader_name  VARCHAR(255),
                custom_desc  TEXT,
                status       VARCHAR(20)  DEFAULT 'active',
                started_at   TIMESTAMP    DEFAULT NOW(),
                ends_at      TIMESTAMP    NOT NULL
            )
        """))
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS salle_vip_access (
                user_id    BIGINT PRIMARY KEY,
                expires_at TIMESTAMP NOT NULL,
                paid_at    TIMESTAMP DEFAULT NOW()
            )
        """))
        await session.commit()
    logger.info("Tables La Salle VIP initialisées.")


async def _launch_salle_auction(context=None):
    """Lance une nouvelle enchère dans La Salle (1 objet / heure)."""
    import random as _r
    async with AsyncSessionLocal() as session:
        # Clôturer les enchères expirées
        await session.execute(text("""
            UPDATE salle_auctions
            SET status = 'closed'
            WHERE status = 'active' AND ends_at <= NOW()
        """))
        await session.commit()

        # Vérifier qu'aucune enchère active n'est en cours
        existing = await session.execute(text(
            "SELECT id FROM salle_auctions WHERE status = 'active'"
        ))
        if existing.fetchone():
            return  # Encore active, on attend

        # Choisir un objet (priorité divin 20%, mythique 80%)
        pool = SALLE_ITEMS
        weights = [20 if it[3] == "divin" else 80 for it in pool]
        item = _r.choices(pool, weights=weights, k=1)[0]
        item_id, item_name, item_emoji, rarity, val_min, val_max, start_price, desc = item
        true_value = _r.randint(val_min, val_max)
        ends_at = datetime.utcnow() + timedelta(hours=1)

        await session.execute(text("""
            INSERT INTO salle_auctions
                (item_id, item_name, item_emoji, rarity, true_value,
                 start_price, current_bid, custom_desc, ends_at)
            VALUES (:iid, :iname, :iemoji, :rarity, :tv, :sp, :sp, :desc, :ends)
        """), {
            "iid": item_id, "iname": item_name, "iemoji": item_emoji,
            "rarity": rarity, "tv": true_value, "sp": start_price,
            "desc": desc, "ends": ends_at
        })
        await session.commit()

    logger.info(f"La Salle — nouvel objet : {item_name} ({rarity})")


def setup_salle_jobs(app):
    """Programme le job toutes les heures pour La Salle VIP."""
    app.job_queue.run_repeating(
        _launch_salle_auction,
        interval=timedelta(hours=1),
        first=timedelta(seconds=30),
        name="salle_vip_hourly",
    )
    logger.info("Job La Salle VIP programme (toutes les heures).")
