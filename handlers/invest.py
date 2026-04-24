"""
Système d'investissement — version paginée avec assets premium.

Commandes :
  /market       — voir les assets (paginé par catégorie)
  /buy [asset] [quantité] — acheter
  /sell [id]    — vendre une position
  /portfolio    — voir ses investissements actifs
"""

import random
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from sqlalchemy import select

from database.db import AsyncSessionLocal, get_user, add_coins
from database.models import Investment
from utils.helpers import ensure_user

logger = logging.getLogger(__name__)

PAGE_SIZE = 5  # assets par page


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


# ─── Définition des assets ────────────────────────────────────────────────────

ASSETS = {
    # ── Actions ───────────────────────────────────────────────────────────────
    "farmcorp": {
        "name": "📊 FarmCorp Action",
        "category": "Action",
        "emoji": "📊",
        "base_price": 5_000,
        "volatility": 0.15,
        "risk": "safe",
        "desc": "Action stable d'une grande entreprise agricole",
    },
    "techbot": {
        "name": "🤖 TechBot Inc.",
        "category": "Action",
        "emoji": "🤖",
        "base_price": 12_000,
        "volatility": 0.25,
        "risk": "medium",
        "desc": "Action d'une startup IA prometteuse",
    },
    "energyx": {
        "name": "⚡ EnergyX Corp",
        "category": "Action",
        "emoji": "⚡",
        "base_price": 8_000,
        "volatility": 0.20,
        "risk": "medium",
        "desc": "Leader de l'énergie renouvelable",
    },
    "luxuria": {
        "name": "💍 Luxuria Group",
        "category": "Action",
        "emoji": "💍",
        "base_price": 50_000,
        "volatility": 0.30,
        "risk": "high",
        "desc": "Conglomérat du luxe mondial — très volatile",
    },
    "memestock": {
        "name": "🎭 MemeStock",
        "category": "Action",
        "emoji": "🎭",
        "base_price": 1_000,
        "volatility": 0.80,
        "risk": "extreme",
        "desc": "Action mème — x10 ou banqueroute",
    },
    "globalbank": {
        "name": "🏦 GlobalBank SA",
        "category": "Action",
        "emoji": "🏦",
        "base_price": 2_500_000,
        "volatility": 0.18,
        "risk": "medium",
        "desc": "Titre d'une banque internationale majeure",
    },
    "megacorp": {
        "name": "🏢 MegaCorp Industries",
        "category": "Action",
        "emoji": "🏢",
        "base_price": 10_000_000,
        "volatility": 0.22,
        "risk": "high",
        "desc": "Conglomérat industriel coté en bourse — gros jeu",
    },
    "unicorn_ipo": {
        "name": "🦄 Unicorn IPO",
        "category": "Action",
        "emoji": "🦄",
        "base_price": 500_000_000,
        "volatility": 0.60,
        "risk": "extreme",
        "desc": "Introduction en bourse d'une licorne tech — tout ou rien",
    },

    # ── Crypto ────────────────────────────────────────────────────────────────
    "farmcoin": {
        "name": "🌾 FarmCoin",
        "category": "Crypto",
        "emoji": "🌾",
        "base_price": 3_000,
        "volatility": 0.40,
        "risk": "high",
        "desc": "Crypto agricole — très volatile",
    },
    "moontoken": {
        "name": "🌕 MoonToken",
        "category": "Crypto",
        "emoji": "🌕",
        "base_price": 500,
        "volatility": 0.90,
        "risk": "extreme",
        "desc": "Nouvelle crypto — to the moon... ou zéro",
    },
    "bitcoin_whale": {
        "name": "🐋 Bitcoin Whale",
        "category": "Crypto",
        "emoji": "🐋",
        "base_price": 50_000_000,
        "volatility": 0.45,
        "risk": "high",
        "desc": "Bloc de BTC massif — pour les vraies baleines",
    },
    "defi_protocol": {
        "name": "🔗 DeFi Protocol X",
        "category": "Crypto",
        "emoji": "🔗",
        "base_price": 1_000_000_000,
        "volatility": 0.75,
        "risk": "extreme",
        "desc": "Protocole DeFi révolutionnaire — jackpot ou ruine",
    },

    # ── Matières premières ────────────────────────────────────────────────────
    "gold_bar": {
        "name": "🥇 Lingot d'Or",
        "category": "Matière première",
        "emoji": "🥇",
        "base_price": 20_000,
        "volatility": 0.08,
        "risk": "safe",
        "desc": "Valeur refuge, très stable",
    },
    "silver_bar": {
        "name": "🥈 Lingot d'Argent",
        "category": "Matière première",
        "emoji": "🥈",
        "base_price": 5_000,
        "volatility": 0.12,
        "risk": "safe",
        "desc": "Moins cher que l'or, assez stable",
    },
    "oil": {
        "name": "🛢️ Baril de Pétrole",
        "category": "Matière première",
        "emoji": "🛢️",
        "base_price": 7_000,
        "volatility": 0.25,
        "risk": "medium",
        "desc": "Fluctue selon l'actualité mondiale",
    },
    "gold_reserve": {
        "name": "🏆 Réserve d'Or Nationale",
        "category": "Matière première",
        "emoji": "🏆",
        "base_price": 500_000_000,
        "volatility": 0.10,
        "risk": "safe",
        "desc": "Réserve d'or souveraine — très stable, très cher",
    },
    "rare_earth": {
        "name": "⚗️ Terres Rares Premium",
        "category": "Matière première",
        "emoji": "⚗️",
        "base_price": 2_000_000_000,
        "volatility": 0.20,
        "risk": "medium",
        "desc": "Stock stratégique de métaux rares — valeur industrielle",
    },

    # ── Immobilier ────────────────────────────────────────────────────────────
    "farmland": {
        "name": "🌾 Terrain Agricole",
        "category": "Immobilier",
        "emoji": "🌾",
        "base_price": 100_000,
        "volatility": 0.05,
        "risk": "safe",
        "desc": "Investissement sûr, rendement modeste",
    },
    "city_flat": {
        "name": "🏙️ Appartement Urbain",
        "category": "Immobilier",
        "emoji": "🏙️",
        "base_price": 250_000,
        "volatility": 0.12,
        "risk": "medium",
        "desc": "Bon potentiel de plus-value",
    },
    "casino_share": {
        "name": "🎰 Part de Casino",
        "category": "Immobilier",
        "emoji": "🎰",
        "base_price": 500_000,
        "volatility": 0.45,
        "risk": "high",
        "desc": "Risqué mais jackpot possible",
    },
    "luxury_tower": {
        "name": "🌆 Tour de Luxe",
        "category": "Immobilier",
        "emoji": "🌆",
        "base_price": 50_000_000,
        "volatility": 0.15,
        "risk": "medium",
        "desc": "Gratte-ciel premium en centre-ville",
    },
    "private_island": {
        "name": "🏝️ Île Privée",
        "category": "Immobilier",
        "emoji": "🏝️",
        "base_price": 500_000_000,
        "volatility": 0.25,
        "risk": "high",
        "desc": "Île paradisiaque — pour les ultra-riches",
    },
    "space_station": {
        "name": "🚀 Station Spatiale",
        "category": "Immobilier",
        "emoji": "🚀",
        "base_price": 2_000_000_000,
        "volatility": 0.50,
        "risk": "extreme",
        "desc": "Propriété en orbite — l'investissement du futur",
    },

    # ── Objets rares ──────────────────────────────────────────────────────────
    "diamond_gem": {
        "name": "💎 Diamant Brut",
        "category": "Objet rare",
        "emoji": "💎",
        "base_price": 80_000,
        "volatility": 0.20,
        "risk": "medium",
        "desc": "Pierre précieuse — marché de niche",
    },
    "ancient_relic": {
        "name": "🏺 Relique Ancienne",
        "category": "Objet rare",
        "emoji": "🏺",
        "base_price": 200_000,
        "volatility": 0.50,
        "risk": "extreme",
        "desc": "Trouvaille archéologique — fortune ou arnaque",
    },
    "masterpiece": {
        "name": "🖼️ Chef-d'œuvre",
        "category": "Objet rare",
        "emoji": "🖼️",
        "base_price": 10_000_000,
        "volatility": 0.35,
        "risk": "high",
        "desc": "Tableau de maître — cote imprévisible",
    },
    "royal_jewel": {
        "name": "👑 Joyau Royal",
        "category": "Objet rare",
        "emoji": "👑",
        "base_price": 100_000_000,
        "volatility": 0.40,
        "risk": "extreme",
        "desc": "Bijou de la couronne — ultra-rare",
    },

    # ── Méga-Investissements ──────────────────────────────────────────────────
    "tech_fund": {
        "name": "💼 Fonds Tech Mondial",
        "category": "Méga-Invest",
        "emoji": "💼",
        "base_price": 10_000_000,
        "volatility": 0.20,
        "risk": "medium",
        "desc": "Fonds d'investissement dans les 500 meilleures tech",
    },
    "sovereign_fund": {
        "name": "🌍 Fonds Souverain",
        "category": "Méga-Invest",
        "emoji": "🌍",
        "base_price": 100_000_000,
        "volatility": 0.12,
        "risk": "safe",
        "desc": "Fonds d'État — très stable, rendement sûr",
    },
    "hedge_fund": {
        "name": "📉📈 Hedge Fund Elite",
        "category": "Méga-Invest",
        "emoji": "📉",
        "base_price": 500_000_000,
        "volatility": 0.35,
        "risk": "high",
        "desc": "Fonds spéculatif agressif — gros gains ou pertes",
    },
    "galaxy_venture": {
        "name": "🌌 Galaxy Venture",
        "category": "Méga-Invest",
        "emoji": "🌌",
        "base_price": 1_000_000_000,
        "volatility": 0.55,
        "risk": "extreme",
        "desc": "Fonds d'exploration galactique — 1 milliard de mise",
    },
    "omega_fund": {
        "name": "♾️ Omega Fund",
        "category": "Méga-Invest",
        "emoji": "♾️",
        "base_price": 2_000_000_000,
        "volatility": 0.70,
        "risk": "extreme",
        "desc": "Le plus grand fonds du monde — 2 milliards, tout ou rien",
    },
}

CATEGORIES = ["Action", "Crypto", "Matière première", "Immobilier", "Objet rare", "Méga-Invest"]


def _assets_by_category(cat: str) -> list:
    return [(k, v) for k, v in ASSETS.items() if v["category"] == cat]


def _current_price(asset_id: str) -> int:
    a = ASSETS[asset_id]
    delta = random.uniform(-a["volatility"], a["volatility"])
    return max(1, int(a["base_price"] * (1 + delta)))


def _risk_emoji(risk: str) -> str:
    return {"safe": "🟢", "medium": "🟡", "high": "🔴", "extreme": "💀"}.get(risk, "⚪")


def _build_market_text_and_keyboard(cat_index: int, page: int) -> tuple:
    cat = CATEGORIES[cat_index]
    assets = _assets_by_category(cat)
    total_pages = max(1, (len(assets) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    slice_ = assets[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    lines = [f"<b>📈 Marché — {cat}</b>  (page {page + 1}/{total_pages})\n"]
    for asset_id, a in slice_:
        price = _current_price(asset_id)
        lines.append(
            f"{a['emoji']} <b>{asset_id}</b>  {_risk_emoji(a['risk'])}\n"
            f"  {a['desc']}\n"
            f"  Prix : ~<b>{_fmt(price)} $</b>\n"
        )
    lines.append("✏️ <code>/buy [asset] [qté]</code> pour acheter.")

    # ── Boutons de catégorie (1 ligne) ────────────────────────────────────────
    cat_row = []
    for i, c in enumerate(CATEGORIES):
        label = f"{'›' if i == cat_index else ''}{c[:6]}{'‹' if i == cat_index else ''}"
        cat_row.append(InlineKeyboardButton(label, callback_data=f"mkt:{i}:0"))

    # ── Boutons pagination (1 ligne) ──────────────────────────────────────────
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Préc.", callback_data=f"mkt:{cat_index}:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Suiv. ➡️", callback_data=f"mkt:{cat_index}:{page + 1}"))

    keyboard = [cat_row]
    if nav_row:
        keyboard.append(nav_row)

    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


# ─── /market ──────────────────────────────────────────────────────────────────

async def market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = _build_market_text_and_keyboard(0, 0)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


# ─── Callback boutons marché ──────────────────────────────────────────────────

async def market_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, cat_str, page_str = query.data.split(":")
        cat_index = int(cat_str)
        page = int(page_str)
    except Exception:
        return

    text, markup = _build_market_text_and_keyboard(cat_index, page)
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


# ─── /buy ─────────────────────────────────────────────────────────────────────

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text(
            "Usage : /buy [asset] [quantité]\nEx : /buy gold_bar 2\nVoir /market pour la liste."
        )

    asset_id = context.args[0].lower()
    if asset_id not in ASSETS:
        return await update.message.reply_text("Asset inconnu. Consultez /market pour la liste.")

    qty = 1
    if len(context.args) >= 2:
        try:
            qty = int(context.args[1])
            assert 1 <= qty <= 100
        except (ValueError, AssertionError):
            return await update.message.reply_text("Quantité invalide (1 à 100).")

    a = ASSETS[asset_id]
    unit_price = _current_price(asset_id)
    total = unit_price * qty
    user = await ensure_user(update.effective_user)

    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.user_id)
        if not u or u.coins < total:
            return await update.message.reply_text(
                f"Solde insuffisant ! Il te faut {_fmt(total)} $ pour acheter {qty}x {a['name']}."
            )

        u.coins -= total
        inv = Investment(
            user_id=user.user_id,
            asset_id=asset_id,
            quantity=qty,
            buy_price=unit_price,
        )
        session.add(inv)
        await session.commit()
        inv_id = inv.id
        new_wallet = u.coins

    await update.message.reply_text(
        f"✅ <b>Achat effectué !</b>\n\n"
        f"{a['emoji']} {a['name']} x{qty}\n"
        f"💰 Prix unitaire : {_fmt(unit_price)} $\n"
        f"💸 Total investi : {_fmt(total)} $\n"
        f"Risque : {_risk_emoji(a['risk'])} {a['risk'].upper()}\n\n"
        f"📋 ID position : <code>#{inv_id}</code>\n"
        f"👛 Portefeuille : {_fmt(new_wallet)} $\n\n"
        f"Utilisez /sell {inv_id} pour vendre.",
        parse_mode=ParseMode.HTML,
    )


# ─── /sell ────────────────────────────────────────────────────────────────────

def _sell_outcome(asset: dict, buy_price: int, current_price: int) -> tuple:
    """
    Taux resserrés — les gains sont modestes, les pertes fréquentes et parfois dévastatrices.

    safe    : gain faible, perte fréquente, jamais de ruine
    medium  : gains plafonnés, pertes lourdes possibles 1/4 du temps
    high    : gros gains rares (20%), pertes massives fréquentes (40%)
    extreme : jackpot ultra-rare (8%), ruine très probable (45%)
    """
    risk = asset["risk"]

    if risk == "safe":
        # 40% petit gain | 25% neutre | 25% perte légère | 10% perte sérieuse
        r = random.random()
        if r < 0.40:
            mult, label = random.uniform(1.01, 1.08), "📈 Petit gain"
        elif r < 0.65:
            mult, label = random.uniform(0.97, 1.00), "➡️ Quasi neutre"
        elif r < 0.90:
            mult, label = random.uniform(0.80, 0.96), "📉 Légère perte"
        else:
            mult, label = random.uniform(0.55, 0.79), "📉📉 Perte sérieuse"

    elif risk == "medium":
        # 30% gain modéré | 15% neutre | 30% perte modérée | 15% perte lourde | 10% désastre
        r = random.random()
        if r < 0.30:
            mult, label = random.uniform(1.05, 1.25), "📈 Gain correct"
        elif r < 0.45:
            mult, label = random.uniform(0.98, 1.04), "➡️ Neutre"
        elif r < 0.75:
            mult, label = random.uniform(0.70, 0.97), "📉 Perte modérée"
        elif r < 0.90:
            mult, label = random.uniform(0.40, 0.69), "📉📉 Perte lourde"
        else:
            mult, label = random.uniform(0.10, 0.39), "💀 Désastre financier !"

    elif risk == "high":
        # 10% gros gain | 10% gain correct | 15% neutre | 30% perte lourde | 20% désastre | 15% ruine
        r = random.random()
        if r < 0.10:
            mult, label = random.uniform(1.30, 1.80), "🚀 Gros gain !"
        elif r < 0.20:
            mult, label = random.uniform(1.05, 1.29), "📈 Gain correct"
        elif r < 0.35:
            mult, label = random.uniform(0.95, 1.04), "➡️ Neutre"
        elif r < 0.65:
            mult, label = random.uniform(0.50, 0.94), "📉 Perte lourde"
        elif r < 0.85:
            mult, label = random.uniform(0.15, 0.49), "💀 Désastre !"
        else:
            mult, label = random.uniform(0.01, 0.14), "☠️ Quasi ruine totale !"

    else:  # extreme
        # 5% jackpot | 8% énorme | 12% gain | 15% neutre | 25% perte lourde | 20% désastre | 15% ruine totale
        r = random.random()
        if r < 0.05:
            mult, label = random.uniform(4.0, 10.0), "🎰 JACKPOT !!!"
        elif r < 0.13:
            mult, label = random.uniform(1.80, 3.99), "🚀🚀 Gain énorme !"
        elif r < 0.25:
            mult, label = random.uniform(1.05, 1.79), "📈 Bon gain"
        elif r < 0.40:
            mult, label = random.uniform(0.90, 1.04), "➡️ Neutre"
        elif r < 0.65:
            mult, label = random.uniform(0.40, 0.89), "📉 Perte lourde"
        elif r < 0.85:
            mult, label = random.uniform(0.05, 0.39), "💀 Désastre total !"
        else:
            mult, label = 0.0, "☠️ RUINE TOTALE !"

    return int(buy_price * mult), label


async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text(
            "Usage : /sell [id]\nVoir /portfolio pour vos positions."
        )
    try:
        inv_id = int(context.args[0].lstrip("#"))
    except ValueError:
        return await update.message.reply_text("ID invalide.")

    user = await ensure_user(update.effective_user)

    async with AsyncSessionLocal() as session:
        inv = (await session.execute(
            select(Investment).where(
                Investment.id == inv_id,
                Investment.user_id == user.user_id,
                Investment.status == "active",
            )
        )).scalar_one_or_none()

        if not inv:
            return await update.message.reply_text("Position introuvable ou déjà vendue.")

        a = ASSETS.get(inv.asset_id)
        if not a:
            return await update.message.reply_text("Asset inconnu.")

        current_price = _current_price(inv.asset_id)
        sell_price, outcome_label = _sell_outcome(a, inv.buy_price, current_price)

        total_received = sell_price * inv.quantity
        total_invested = inv.buy_price * inv.quantity
        profit = total_received - total_invested

        inv.status = "sold"
        inv.sell_price = sell_price
        inv.sold_at = datetime.utcnow()

        u = await get_user(session, user.user_id)
        from database.db import MAX_COINS
        u.coins = u.coins + total_received
        await session.commit()
        new_wallet = u.coins

    profit_str = f"+{_fmt(profit)} $" if profit >= 0 else f"-{_fmt(abs(profit))} $"
    profit_emoji = "🟢" if profit > 0 else ("🔴" if profit < 0 else "⚪")

    await update.message.reply_text(
        f"💼 <b>Position vendue</b>\n\n"
        f"{a['emoji']} {a['name']} x{inv.quantity}\n"
        f"📊 Résultat : <b>{outcome_label}</b>\n\n"
        f"💵 Investi   : {_fmt(total_invested)} $\n"
        f"💰 Reçu      : {_fmt(total_received)} $\n"
        f"{profit_emoji} Profit/Perte : <b>{profit_str}</b>\n\n"
        f"👛 Portefeuille : {_fmt(new_wallet)} $",
        parse_mode=ParseMode.HTML,
    )


# ─── /portfolio ───────────────────────────────────────────────────────────────

async def portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update.effective_user)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Investment).where(
                Investment.user_id == user.user_id,
                Investment.status == "active",
            )
        )
        investments = result.scalars().all()
        u = await get_user(session, user.user_id)

    if not investments:
        return await update.message.reply_text(
            "Tu n'as aucun investissement actif.\nVoir /market pour acheter."
        )

    lines = [f"<b>📈 Portfolio de {update.effective_user.first_name}</b>\n"]
    total_invested = 0
    total_current = 0

    for inv in investments:
        a = ASSETS.get(inv.asset_id, {})
        cur = _current_price(inv.asset_id) if inv.asset_id in ASSETS else inv.buy_price
        invest = inv.buy_price * inv.quantity
        cur_val = cur * inv.quantity
        pnl = cur_val - invest
        pnl_str = f"+{_fmt(pnl)}" if pnl >= 0 else f"-{_fmt(abs(pnl))}"
        pnl_e = "🟢" if pnl >= 0 else "🔴"

        lines.append(
            f"{a.get('emoji', '📊')} <b>#{inv.id} {a.get('name', inv.asset_id)}</b> x{inv.quantity}\n"
            f"  └ Acheté : {_fmt(inv.buy_price)} $ | Actuel : ~{_fmt(cur)} $\n"
            f"  └ P&L : {pnl_e} {pnl_str} $   — /sell {inv.id}\n"
        )
        total_invested += invest
        total_current += cur_val

    total_pnl = total_current - total_invested
    pnl_str = f"+{_fmt(total_pnl)}" if total_pnl >= 0 else f"-{_fmt(abs(total_pnl))}"
    pnl_e = "🟢" if total_pnl >= 0 else "🔴"

    lines.append(f"\n💼 Total investi : {_fmt(total_invested)} $")
    lines.append(f"📊 Valeur actuelle : ~{_fmt(total_current)} $")
    lines.append(f"{pnl_e} P&L total : <b>{pnl_str} $</b>")
    lines.append(f"👛 Portefeuille : {_fmt(u.coins if u else 0)} $")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
