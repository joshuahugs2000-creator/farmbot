"""
Système de drames économiques — FarmBot

Commandes admin uniquement :
  /drame scandale @user [seuil]     — Scandale médiatique (perte % coins)
  /drame catastrophe @user [seuil]  — Catastrophe naturelle (perte portfolio)
  /drame fisc @user [seuil]         — Contrôle fiscal (impôts forcés massifs)
  /drame crise @user [seuil]        — Crise économique (perte coins + portfolio)
  /drame info @user                 — Voir la fortune actuelle d'un joueur
  /setdramesesuil [montant]         — Définir le seuil global (défaut : 100M)

Le seuil est le minimum de coins pour être ciblé.
Si le joueur est en dessous du seuil, le drame est annulé.
"""

import random
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from sqlalchemy import text

from database.db import AsyncSessionLocal, get_user
from handlers.admin import is_admin, _deny
from utils.helpers import parse_target, mention

logger = logging.getLogger(__name__)

# ─── SEUIL GLOBAL (modifiable par admin) ─────────────────────────────────────

DRAME_SEUIL: int = 100_000_000  # 100 millions par défaut

def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")

def _fmt_short(n: int) -> str:
    if n >= 1_000_000_000_000:
        return f"{n / 1_000_000_000_000:.1f}Md"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}Md"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)

# ─── CATALOGUES DE DRAMES ─────────────────────────────────────────────────────

SCANDALES = [
    ("💋", "Liaison secrète révélée par les paparazzi",          (15, 40)),
    ("🍾", "Fête de luxe filmée en cachette et diffusée",        (20, 50)),
    ("💼", "Documents confidentiels fuités sur internet",        (10, 35)),
    ("🎰", "Addiction au jeu révélée en direct à la TV",         (25, 55)),
    ("🐀", "Trahison d'un associé qui vend des infos privées",   (30, 60)),
    ("📱", "Conversations privées hackées et publiées",          (15, 45)),
    ("🕵️", "Fausse identité découverte par un journaliste",      (35, 65)),
    ("🦠", "Accusé d'avoir contaminé volontairement ses rivaux", (40, 70)),
]

CATASTROPHES = [
    ("🌊", "Tsunami détruit ses entrepôts côtiers",              (20, 60)),
    ("🔥", "Incendie ravage ses bureaux et ses serveurs",        (25, 55)),
    ("⚡", "Panne électrique mondiale efface ses données",       (15, 45)),
    ("🌪️", "Tornade emporte ses biens immobiliers",              (30, 65)),
    ("🏚️", "Effondrement de son siège social principal",         (35, 70)),
    ("💥", "Explosion mystérieuse dans son usine principale",    (40, 75)),
    ("🌋", "Éruption volcanique détruit ses ressources rares",   (20, 50)),
    ("❄️", "Vague de froid extrême paralyse toutes ses activités",(15, 40)),
]

CONTROLES_FISC = [
    ("🏦", "Redressement fiscal sur 5 ans d'arriérés",           (30, 55)),
    ("👮", "Saisie d'actifs par les autorités fiscales",         (40, 65)),
    ("📋", "Audit international sur ses comptes offshore",       (25, 50)),
    ("⚖️", "Procès fiscal retentissant — amende record",         (45, 70)),
    ("🔍", "Enquête anti-blanchiment déclenchée",                (35, 60)),
    ("🧾", "Faux en écriture découvert — pénalités maximales",   (50, 75)),
    ("💸", "Rappel de TVA sur 10 ans d'opérations",             (20, 45)),
    ("🌐", "Accord international oblige au rapatriement fiscal", (30, 55)),
]

CRISES_ECON = [
    ("📉", "Krach boursier mondial engloutit ses placements",    (40, 80)),
    ("🏦", "Faillite de sa banque principale",                   (35, 70)),
    ("💱", "Effondrement de la devise dans laquelle il investit",(30, 65)),
    ("🚢", "Crise logistique mondiale bloque toutes ses livraisons",(25, 55)),
    ("🤝", "Partenaire principal fait faillite et l'entraîne",   (45, 75)),
    ("🌍", "Embargo international sur ses marchés clés",         (40, 70)),
    ("🏗️", "Bulle immobilière éclate — pertes massives",         (50, 85)),
    ("⚙️", "Disruption technologique rend son empire obsolète",  (35, 65)),
]

# ─── COMMANDE /setdramesesuil ─────────────────────────────────────────────────

async def setdramesesuil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DRAME_SEUIL
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    if not context.args:
        await update.message.reply_text(
            f"📊 Seuil actuel : <b>{_fmt(DRAME_SEUIL)} coins</b>\n\n"
            f"Usage : <code>/setdramesesuil [montant]</code>\n"
            f"Exemple : <code>/setdramesesuil 500000000</code> (500M)",
            parse_mode=ParseMode.HTML
        )
        return

    try:
        new_seuil = int(context.args[0].replace("_", "").replace(" ", ""))
    except ValueError:
        await update.message.reply_text("❌ Montant invalide.")
        return

    DRAME_SEUIL = new_seuil
    await update.message.reply_text(
        f"✅ Seuil des drames mis à jour : <b>{_fmt(DRAME_SEUIL)} coins</b>",
        parse_mode=ParseMode.HTML
    )

# ─── HELPERS INTERNES ─────────────────────────────────────────────────────────

async def _get_target_info(update, context):
    """Résout la cible et vérifie le seuil. Retourne (db_user, seuil_effectif) ou (None, None)."""
    target = await parse_target(update, context)
    if not target:
        await update.message.reply_text(
            "❌ Cible introuvable. Mentionne un joueur ou réponds à son message."
        )
        return None, None

    # Seuil personnalisé en 3e argument ?
    seuil = DRAME_SEUIL
    args = context.args or []
    for arg in args:
        try:
            val = int(arg.replace("_", "").replace(" ", ""))
            if val > 0:
                seuil = val
                break
        except ValueError:
            pass

    db_user = await get_user(target.id)
    if not db_user:
        await update.message.reply_text("❌ Ce joueur n'existe pas en base de données.")
        return None, None

    if db_user.coins < seuil:
        await update.message.reply_text(
            f"⚠️ <b>{db_user.first_name}</b> n'atteint pas le seuil !\n"
            f"💰 Sa fortune : <b>{_fmt(db_user.coins)} coins</b>\n"
            f"📊 Seuil requis : <b>{_fmt(seuil)} coins</b>\n\n"
            f"Utilise <code>/setdramesesuil [montant]</code> pour ajuster le seuil.",
            parse_mode=ParseMode.HTML
        )
        return None, None

    return db_user, seuil

async def _deduct_coins(user_id: int, percent: int) -> tuple[int, int]:
    """Retire X% des coins. Retourne (montant_perdu, nouveau_solde)."""
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            text("SELECT coins FROM users WHERE user_id = :uid"), {"uid": user_id}
        )
        row = res.fetchone()
        coins = row[0] if row else 0
        perte = int(coins * percent / 100)
        nouveau = max(0, coins - perte)
        await session.execute(
            text("UPDATE users SET coins = :c WHERE user_id = :uid"),
            {"c": nouveau, "uid": user_id}
        )
        await session.commit()
    return perte, nouveau

async def _destroy_portfolio(user_id: int, percent: int) -> tuple[int, int]:
    """Détruit X% de la valeur du portfolio (supprime des positions actives).
    Retourne (valeur_détruite_estimée, nb_positions_supprimées)."""
    async with AsyncSessionLocal() as session:
        res = await session.execute(text("""
            SELECT id, buy_price, quantity FROM investments
            WHERE user_id = :uid AND status = 'active'
            ORDER BY RANDOM()
        """), {"uid": user_id})
        positions = res.fetchall()

        if not positions:
            return 0, 0

        n_detruire = max(1, int(len(positions) * percent / 100))
        cibles = positions[:n_detruire]
        valeur_detruite = sum(p.buy_price * p.quantity for p in cibles)
        ids = [p.id for p in cibles]

        await session.execute(text(
            f"UPDATE investments SET status = 'destroyed' WHERE id = ANY(:ids)"
        ), {"ids": ids})
        await session.commit()

    return valeur_detruite, n_detruire

# ─── /drame scandale ──────────────────────────────────────────────────────────

async def _drame_scandale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_user, seuil = await _get_target_info(update, context)
    if not db_user:
        return

    emoji, desc, (pmin, pmax) = random.choice(SCANDALES)
    percent = random.randint(pmin, pmax)
    perte, nouveau = await _deduct_coins(db_user.user_id, percent)

    await update.message.reply_text(
        f"{emoji} <b>SCANDALE MÉDIATIQUE !</b>\n\n"
        f"👤 Victime : <b>{db_user.first_name}</b>\n"
        f"📰 {desc}\n\n"
        f"📉 Perte : <b>{percent}%</b> de sa fortune\n"
        f"💸 Montant perdu : <b>{_fmt(perte)} coins</b>\n"
        f"💰 Solde restant : <b>{_fmt(nouveau)} coins</b>\n\n"
        f"😱 <i>La réputation coûte cher...</i>",
        parse_mode=ParseMode.HTML
    )

# ─── /drame catastrophe ───────────────────────────────────────────────────────

async def _drame_catastrophe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_user, seuil = await _get_target_info(update, context)
    if not db_user:
        return

    emoji, desc, (pmin, pmax) = random.choice(CATASTROPHES)
    percent = random.randint(pmin, pmax)

    valeur_detruite, nb_positions = await _destroy_portfolio(db_user.user_id, percent)

    if nb_positions == 0:
        # Pas de portfolio → perte de coins à la place
        perte, nouveau = await _deduct_coins(db_user.user_id, percent)
        await update.message.reply_text(
            f"{emoji} <b>CATASTROPHE NATURELLE !</b>\n\n"
            f"👤 Victime : <b>{db_user.first_name}</b>\n"
            f"🌍 {desc}\n\n"
            f"📊 Pas de portfolio — pertes directes sur les coins\n"
            f"💸 Montant perdu : <b>{_fmt(perte)} coins</b>\n"
            f"💰 Solde restant : <b>{_fmt(nouveau)} coins</b>\n\n"
            f"😰 <i>La nature est impitoyable...</i>",
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text(
            f"{emoji} <b>CATASTROPHE NATURELLE !</b>\n\n"
            f"👤 Victime : <b>{db_user.first_name}</b>\n"
            f"🌍 {desc}\n\n"
            f"📊 <b>{nb_positions} position(s)</b> détruites dans son portfolio\n"
            f"💸 Valeur anéantie : <b>~{_fmt(valeur_detruite)} coins</b>\n\n"
            f"😰 <i>La nature est impitoyable...</i>",
            parse_mode=ParseMode.HTML
        )

# ─── /drame fisc ──────────────────────────────────────────────────────────────

async def _drame_fisc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_user, seuil = await _get_target_info(update, context)
    if not db_user:
        return

    emoji, desc, (pmin, pmax) = random.choice(CONTROLES_FISC)
    percent = random.randint(pmin, pmax)
    perte, nouveau = await _deduct_coins(db_user.user_id, percent)

    await update.message.reply_text(
        f"{emoji} <b>CONTRÔLE FISCAL !</b>\n\n"
        f"👤 Contribuable : <b>{db_user.first_name}</b>\n"
        f"📋 {desc}\n\n"
        f"🏛️ Taux d'imposition forcé : <b>{percent}%</b>\n"
        f"💸 Impôts prélevés : <b>{_fmt(perte)} coins</b>\n"
        f"💰 Solde restant : <b>{_fmt(nouveau)} coins</b>\n\n"
        f"⚖️ <i>Nul n'est au-dessus des lois fiscales.</i>",
        parse_mode=ParseMode.HTML
    )

# ─── /drame crise ─────────────────────────────────────────────────────────────

async def _drame_crise(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_user, seuil = await _get_target_info(update, context)
    if not db_user:
        return

    emoji, desc, (pmin, pmax) = random.choice(CRISES_ECON)
    percent_coins = random.randint(pmin, pmax)
    percent_portfolio = random.randint(30, 70)

    perte_coins, nouveau_solde = await _deduct_coins(db_user.user_id, percent_coins)
    valeur_detruite, nb_positions = await _destroy_portfolio(db_user.user_id, percent_portfolio)

    msg = (
        f"{emoji} <b>CRISE ÉCONOMIQUE !</b>\n\n"
        f"👤 Victime : <b>{db_user.first_name}</b>\n"
        f"📉 {desc}\n\n"
        f"<b>Dégâts totaux :</b>\n"
        f"💸 Coins perdus ({percent_coins}%) : <b>{_fmt(perte_coins)} coins</b>\n"
        f"💰 Solde restant : <b>{_fmt(nouveau_solde)} coins</b>\n"
    )
    if nb_positions > 0:
        msg += (
            f"📊 Portfolio ({percent_portfolio}% détruit) : "
            f"<b>{nb_positions} position(s)</b> — ~{_fmt(valeur_detruite)} perdus\n"
        )
    msg += f"\n🌍 <i>Les crises ne préviennent pas...</i>"

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

# ─── /drame info ──────────────────────────────────────────────────────────────

async def _drame_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = await parse_target(update, context)
    if not target:
        await update.message.reply_text("❌ Cible introuvable.")
        return

    db_user = await get_user(target.id)
    if not db_user:
        await update.message.reply_text("❌ Joueur introuvable en base.")
        return

    async with AsyncSessionLocal() as session:
        res = await session.execute(text("""
            SELECT COUNT(*), COALESCE(SUM(buy_price * quantity), 0)
            FROM investments WHERE user_id = :uid AND status = 'active'
        """), {"uid": db_user.user_id})
        row = res.fetchone()
        nb_positions = row[0]
        valeur_portfolio = row[1]

        res2 = await session.execute(text("""
            SELECT COALESCE(SUM(balance), 0) FROM bank_accounts WHERE user_id = :uid
        """), {"uid": db_user.user_id})
        bank_balance = res2.fetchone()[0]

    total = db_user.coins + valeur_portfolio + bank_balance
    eligible = "✅ Éligible" if db_user.coins >= DRAME_SEUIL else "❌ En dessous du seuil"

    await update.message.reply_text(
        f"🔍 <b>Fiche financière — {db_user.first_name}</b>\n\n"
        f"💰 Coins en poche : <b>{_fmt(db_user.coins)}</b>\n"
        f"🏦 En banque : <b>{_fmt(bank_balance)}</b>\n"
        f"📊 Portfolio ({nb_positions} positions) : <b>~{_fmt(valeur_portfolio)}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🏆 Fortune totale : <b>{_fmt(total)} coins</b>\n\n"
        f"📊 Seuil drames : <b>{_fmt(DRAME_SEUIL)}</b>\n"
        f"🎯 Statut : {eligible}",
        parse_mode=ParseMode.HTML
    )

# ─── DISPATCHER /drame ────────────────────────────────────────────────────────

SOUS_COMMANDES = {
    "scandale":    _drame_scandale,
    "catastrophe": _drame_catastrophe,
    "fisc":        _drame_fisc,
    "crise":       _drame_crise,
    "info":        _drame_info,
}

async def drame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return await _deny(update)

    if not context.args:
        await update.message.reply_text(
            "🎭 <b>Système de Drames</b>\n\n"
            "<b>Types disponibles :</b>\n"
            "💋 <code>/drame scandale @user</code> — Scandale médiatique (15-70% coins)\n"
            "🌊 <code>/drame catastrophe @user</code> — Catastrophe naturelle (portfolio)\n"
            "🏛️ <code>/drame fisc @user</code> — Contrôle fiscal (20-75% coins)\n"
            "📉 <code>/drame crise @user</code> — Crise économique (coins + portfolio)\n"
            "🔍 <code>/drame info @user</code> — Voir la fortune du joueur\n\n"
            "<b>Seuil actuel :</b> <code>/setdramesesuil</code> → "
            f"<b>{_fmt(DRAME_SEUIL)} coins</b>\n\n"
            "💡 <i>Ajoute un montant pour un seuil ponctuel :\n"
            "/drame fisc @user 500000000</i>",
            parse_mode=ParseMode.HTML
        )
        return

    sous_cmd = context.args[0].lower()
    if sous_cmd not in SOUS_COMMANDES:
        await update.message.reply_text(
            f"❌ Type de drame inconnu : <code>{sous_cmd}</code>\n\n"
            f"Disponibles : {', '.join(SOUS_COMMANDES.keys())}",
            parse_mode=ParseMode.HTML
        )
        return

    # Retirer le premier arg pour que parse_target fonctionne sur la mention
    context.args = context.args[1:]
    await SOUS_COMMANDES[sous_cmd](update, context)
