"""
Système d'investissement.

Mix d'actions boursières et d'assets physiques.
Les prix fluctuent aléatoirement à chaque consultation / achat.
Résultat de la vente : gain important / gain moyen / perte partielle / perte totale.

Commandes :
  /market       — voir tous les assets disponibles
  /buy [asset] [quantité] — acheter
  /sell [id]    — vendre une position
  /portfolio    — voir ses investissements actifs
"""

import random
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from sqlalchemy import select

from database.db import AsyncSessionLocal, get_user, add_coins
from database.models import Investment
from utils.helpers import ensure_user

logger = logging.getLogger(__name__)


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


# ─── Définition des assets ────────────────────────────────────────────────────
# base_price  : prix de référence
# volatility  : amplitude de variation à l'affichage (±%)
# risk        : profil de gain/perte à la vente
#   "safe"    → gain modeste quasi garanti
#   "medium"  → gain moyen, petit risque de perte
#   "high"    → gros gain possible, grosse perte possible
#   "extreme" → jackpot ou ruine totale

ASSETS = {
    # ── Actions ───────────────────────────────────────────────────────────────
    "farmcorp": {
        "name":       "📊 FarmCorp Action",
        "category":   "Action",
        "emoji":      "📊",
        "base_price": 5_000,
        "volatility": 0.15,
        "risk":       "safe",
        "desc":       "Action stable d'une grande entreprise agricole",
    },
    "techbot": {
        "name":       "🤖 TechBot Inc.",
        "category":   "Action",
        "emoji":      "🤖",
        "base_price": 12_000,
        "volatility": 0.25,
        "risk":       "medium",
        "desc":       "Action d'une startup IA prometteuse",
    },
    "energyx": {
        "name":       "⚡ EnergyX Corp",
        "category":   "Action",
        "emoji":      "⚡",
        "base_price": 8_000,
        "volatility": 0.20,
        "risk":       "medium",
        "desc":       "Leader de l'énergie renouvelable",
    },
    "luxuria": {
        "name":       "💍 Luxuria Group",
        "category":   "Action",
        "emoji":      "💍",
        "base_price": 50_000,
        "volatility": 0.30,
        "risk":       "high",
        "desc":       "Conglomérat du luxe mondial — très volatile",
    },
    "memestock": {
        "name":       "🎭 MemeStock",
        "category":   "Action",
        "emoji":      "🎭",
        "base_price": 1_000,
        "volatility": 0.80,
        "risk":       "extreme",
        "desc":       "Action mème — x10 ou banqueroute",
    },
    # ── Crypto ────────────────────────────────────────────────────────────────
    "farmcoin": {
        "name":       "🌾 FarmCoin",
        "category":   "Crypto",
        "emoji":      "🌾",
        "base_price": 3_000,
        "volatility": 0.40,
        "risk":       "high",
        "desc":       "Crypto agricole — très volatile",
    },
    "moontoken": {
        "name":       "🌕 MoonToken",
        "category":   "Crypto",
        "emoji":      "🌕",
        "base_price": 500,
        "volatility": 0.90,
        "risk":       "extreme",
        "desc":       "Nouvelle crypto — to the moon... ou zéro",
    },
    # ── Matières premières ────────────────────────────────────────────────────
    "gold_bar": {
        "name":       "🥇 Lingot d'Or",
        "category":   "Matière première",
        "emoji":      "🥇",
        "base_price": 20_000,
        "volatility": 0.08,
        "risk":       "safe",
        "desc":       "Valeur refuge, très stable",
    },
    "silver_bar": {
        "name":       "🥈 Lingot d'Argent",
        "category":   "Matière première",
        "emoji":      "🥈",
        "base_price": 5_000,
        "volatility": 0.12,
        "risk":       "safe",
        "desc":       "Moins cher que l'or, assez stable",
    },
    "oil": {
        "name":       "🛢️ Baril de Pétrole",
        "category":   "Matière première",
        "emoji":      "🛢️",
        "base_price": 7_000,
        "volatility": 0.25,
        "risk":       "medium",
        "desc":       "Fluctue selon l'actualité mondiale",
    },
    # ── Immobilier ────────────────────────────────────────────────────────────
    "farmland": {
        "name":       "🌾 Terrain Agricole",
        "category":   "Immobilier",
        "emoji":      "🌾",
        "base_price": 100_000,
        "volatility": 0.05,
        "risk":       "safe",
        "desc":       "Investissement sûr, rendement modeste",
    },
    "city_flat": {
        "name":       "🏙️ Appartement Urbain",
        "category":   "Immobilier",
        "emoji":      "🏙️",
        "base_price": 250_000,
        "volatility": 0.12,
        "risk":       "medium",
        "desc":       "Bon potentiel de plus-value",
    },
    "casino_share": {
        "name":       "🎰 Part de Casino",
        "category":   "Immobilier",
        "emoji":      "🎰",
        "base_price": 500_000,
        "volatility": 0.45,
        "risk":       "high",
        "desc":       "Risqué mais jackpot possible",
    },
    # ── Objets rares ──────────────────────────────────────────────────────────
    "diamond_gem": {
        "name":       "💎 Diamant Brut",
        "category":   "Objet rare",
        "emoji":      "💎",
        "base_price": 80_000,
        "volatility": 0.20,
        "risk":       "medium",
        "desc":       "Pierre précieuse — marché de niche",
    },
    "ancient_relic": {
        "name":       "🏺 Relique Ancienne",
        "category":   "Objet rare",
        "emoji":      "🏺",
        "base_price": 200_000,
        "volatility": 0.50,
        "risk":       "extreme",
        "desc":       "Trouvaille archéologique — fortune ou arnaque",
    },
}

ASSET_KEYS = list(ASSETS.keys())
CATEGORIES = ["Action", "Crypto", "Matière première", "Immobilier", "Objet rare"]


def _current_price(asset_id: str) -> int:
    """Génère un prix dynamique avec volatilité aléatoire."""
    a     = ASSETS[asset_id]
    delta = random.uniform(-a["volatility"], a["volatility"])
    return max(1, int(a["base_price"] * (1 + delta)))


def _sell_outcome(asset: dict, buy_price: int, current_price: int) -> tuple[int, str]:
    """
    Calcule le prix de vente effectif et un message de résultat.
    Basé sur le profil de risque de l'asset.
    """
    risk = asset["risk"]

    if risk == "safe":
        # 70% gain 2-15%, 20% neutre, 10% légère perte
        r = random.random()
        if r < 0.70:
            mult = random.uniform(1.02, 1.15)
            label = "📈 Gain modeste"
        elif r < 0.90:
            mult = 1.0
            label = "➡️ Neutre"
        else:
            mult = random.uniform(0.85, 0.98)
            label = "📉 Légère perte"

    elif risk == "medium":
        # 50% gain 5-30%, 15% neutre, 25% perte 5-20%, 10% perte lourde
        r = random.random()
        if r < 0.50:
            mult = random.uniform(1.05, 1.30)
            label = "📈 Bon gain"
        elif r < 0.65:
            mult = 1.0
            label = "➡️ Neutre"
        elif r < 0.90:
            mult = random.uniform(0.80, 0.95)
            label = "📉 Perte modérée"
        else:
            mult = random.uniform(0.50, 0.75)
            label = "📉📉 Perte lourde"

    elif risk == "high":
        # 30% gros gain 20-100%, 20% bon gain, 20% neutre, 20% perte, 10% ruine partielle
        r = random.random()
        if r < 0.30:
            mult = random.uniform(1.20, 2.00)
            label = "🚀 Gros gain !"
        elif r < 0.50:
            mult = random.uniform(1.05, 1.20)
            label = "📈 Bon gain"
        elif r < 0.70:
            mult = 1.0
            label = "➡️ Neutre"
        elif r < 0.90:
            mult = random.uniform(0.50, 0.85)
            label = "📉 Perte significative"
        else:
            mult = random.uniform(0.10, 0.40)
            label = "💀 Grosse perte !"

    else:  # extreme
        # Profil jackpot ou ruine
        r = random.random()
        if r < 0.15:
            mult = random.uniform(5.0, 15.0)
            label = "🎰 JACKPOT !!!"
        elif r < 0.35:
            mult = random.uniform(1.50, 5.0)
            label = "🚀🚀 Gain énorme !"
        elif r < 0.50:
            mult = random.uniform(1.05, 1.50)
            label = "📈 Gain correct"
        elif r < 0.65:
            mult = random.uniform(0.70, 0.99)
            label = "📉 Légère perte"
        elif r < 0.85:
            mult = random.uniform(0.20, 0.60)
            label = "💀 Perte massive !"
        else:
            mult = 0.0
            label = "☠️ RUINE TOTALE !"

    sell_price = int(buy_price * mult)
    return sell_price, label


# ─── /market ──────────────────────────────────────────────────────────────────

async def market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Filtrer par catégorie si argument
    filter_cat = None
    if context.args:
        filter_cat = context.args[0].lower()

    lines = ["<b>📈 Marché des Investissements</b>\n"]

    for cat in CATEGORIES:
        cat_assets = [(k, v) for k, v in ASSETS.items()
                      if v["category"] == cat
                      and (filter_cat is None or filter_cat in cat.lower() or filter_cat == k)]
        if not cat_assets:
            continue

        lines.append(f"<b>── {cat} ──</b>")
        for asset_id, a in cat_assets:
            price = _current_price(asset_id)
            risk_emoji = {"safe": "🟢", "medium": "🟡", "high": "🔴", "extreme": "💀"}.get(a["risk"], "⚪")
            lines.append(
                f"{a['emoji']} <b>{asset_id}</b>  {risk_emoji}\n"
                f"  {a['desc']}\n"
                f"  Prix actuel : ~{_fmt(price)} $\n"
            )

    lines.append("Utilisez /buy [asset] [quantité] pour acheter.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ─── /buy ─────────────────────────────────────────────────────────────────────

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text(
            "Usage : /buy [asset] [quantité]\nEx : /buy gold_bar 2\nVoir /market pour la liste."
        )

    asset_id = context.args[0].lower()
    if asset_id not in ASSETS:
        return await update.message.reply_text(
            f"Asset inconnu. Consultez /market pour la liste."
        )

    qty = 1
    if len(context.args) >= 2:
        try:
            qty = int(context.args[1])
            assert 1 <= qty <= 100
        except (ValueError, AssertionError):
            return await update.message.reply_text("Quantité invalide (1 à 100).")

    a         = ASSETS[asset_id]
    unit_price = _current_price(asset_id)
    total      = unit_price * qty
    user       = await ensure_user(update.effective_user)

    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.user_id)
        if not u or u.coins < total:
            return await update.message.reply_text(
                f"Solde insuffisant ! Il te faut {_fmt(total)} $ pour acheter {qty}x {a['name']}."
            )

        u.coins -= total

        inv = Investment(
            user_id   = user.user_id,
            asset_id  = asset_id,
            quantity  = qty,
            buy_price = unit_price,
        )
        session.add(inv)
        await session.commit()
        inv_id     = inv.id
        new_wallet = u.coins

    risk_emoji = {"safe": "🟢", "medium": "🟡", "high": "🔴", "extreme": "💀"}.get(a["risk"], "⚪")
    await update.message.reply_text(
        f"✅ <b>Achat effectué !</b>\n\n"
        f"{a['emoji']} {a['name']} x{qty}\n"
        f"💰 Prix unitaire : {_fmt(unit_price)} $\n"
        f"💸 Total investi : {_fmt(total)} $\n"
        f"Risque : {risk_emoji} {a['risk'].upper()}\n\n"
        f"📋 ID position : <code>#{inv_id}</code>\n"
        f"👛 Portefeuille : {_fmt(new_wallet)} $\n\n"
        f"Utilisez /sell {inv_id} pour vendre.",
        parse_mode=ParseMode.HTML,
    )


# ─── /sell ────────────────────────────────────────────────────────────────────

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
                Investment.id      == inv_id,
                Investment.user_id == user.user_id,
                Investment.status  == "active",
            )
        )).scalar_one_or_none()

        if not inv:
            return await update.message.reply_text(
                "Position introuvable ou déjà vendue."
            )

        a             = ASSETS.get(inv.asset_id)
        if not a:
            return await update.message.reply_text("Asset inconnu.")

        current_price = _current_price(inv.asset_id)
        sell_price, outcome_label = _sell_outcome(a, inv.buy_price, current_price)

        total_received = sell_price * inv.quantity
        total_invested = inv.buy_price * inv.quantity
        profit         = total_received - total_invested

        inv.status     = "sold"
        inv.sell_price = sell_price
        inv.sold_at    = datetime.utcnow()

        u = await get_user(session, user.user_id)
        u.coins += total_received
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
                Investment.status  == "active",
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
    total_current  = 0

    for inv in investments:
        a      = ASSETS.get(inv.asset_id, {})
        cur    = _current_price(inv.asset_id)
        invest = inv.buy_price * inv.quantity
        cur_val = cur * inv.quantity
        pnl    = cur_val - invest
        pnl_str = f"+{_fmt(pnl)}" if pnl >= 0 else f"-{_fmt(abs(pnl))}"
        pnl_e  = "🟢" if pnl >= 0 else "🔴"

        lines.append(
            f"{a.get('emoji','📊')} <b>#{inv.id} {a.get('name', inv.asset_id)}</b> x{inv.quantity}\n"
            f"  └ Acheté : {_fmt(inv.buy_price)} $ | Actuel : ~{_fmt(cur)} $\n"
            f"  └ P&L : {pnl_e} {pnl_str} $   — /sell {inv.id}\n"
        )
        total_invested += invest
        total_current  += cur_val

    total_pnl = total_current - total_invested
    pnl_str   = f"+{_fmt(total_pnl)}" if total_pnl >= 0 else f"-{_fmt(abs(total_pnl))}"
    pnl_e     = "🟢" if total_pnl >= 0 else "🔴"

    lines.append(f"\n💼 Total investi : {_fmt(total_invested)} $")
    lines.append(f"📊 Valeur actuelle : ~{_fmt(total_current)} $")
    lines.append(f"{pnl_e} P&L total : <b>{pnl_str} $</b>")
    lines.append(f"👛 Portefeuille : {_fmt(u.coins if u else 0)} $")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
