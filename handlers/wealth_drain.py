"""
Système de drainage de richesse 💸
────────────────────────────────────────────────────────────────
BOUTIQUE (objets non-revendables au bot, échangeables entre joueurs)
  /shop                  — voir la boutique
  /acheter <id>          — acheter un objet
  /inventaire            — voir ses objets
  /revendre <id> @joueur <prix>  — proposer un objet à un joueur
  /acceptrevente <id>    — accepter une offre de revente

IMPÔTS (automatiques, quotidiens à 12h00 GMT)
  /impots                — voir son taux d'imposition et estimation

CAMBRIOLAGE (voler les objets d'un joueur)
  /cambrioler @joueur    — tenter de cambrioler un joueur
  /alarme                — installer une alarme chez soi (protection)

BRAQUAGE DE BANQUE (événement collectif)
  /braquage              — lancer / rejoindre un braquage
  /annulerbraquage       — annuler (créateur uniquement)
────────────────────────────────────────────────────────────────
"""

import random
import logging
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, Application
from telegram.constants import ParseMode
from sqlalchemy import select, text

from database.db import AsyncSessionLocal, get_user, add_coins
from utils.helpers import ensure_user, parse_target, mention
from config import CURRENCY

logger = logging.getLogger(__name__)


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


# ══════════════════════════════════════════════════════════════════
# INITIALISATION DES TABLES
# ══════════════════════════════════════════════════════════════════

async def init_drain_tables():
    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS shop_items (
                id          SERIAL PRIMARY KEY,
                owner_id    BIGINT NOT NULL,
                group_id    BIGINT NOT NULL,
                item_id     VARCHAR(50) NOT NULL,
                bought_at   TIMESTAMP DEFAULT NOW()
            )
        """))
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS shop_offers (
                id          SERIAL PRIMARY KEY,
                item_row_id INT NOT NULL,
                seller_id   BIGINT NOT NULL,
                buyer_id    BIGINT NOT NULL,
                group_id    BIGINT NOT NULL,
                price       BIGINT NOT NULL,
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """))
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS alarm_shield (
                user_id     BIGINT PRIMARY KEY,
                group_id    BIGINT NOT NULL,
                level       INT DEFAULT 1,
                bought_at   TIMESTAMP DEFAULT NOW()
            )
        """))
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS heist_sessions (
                id          SERIAL PRIMARY KEY,
                group_id    BIGINT NOT NULL,
                leader_id   BIGINT NOT NULL,
                status      VARCHAR(20) DEFAULT 'recruiting',
                pot         BIGINT DEFAULT 0,
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """))
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS heist_members (
                id          SERIAL PRIMARY KEY,
                heist_id    INT NOT NULL,
                user_id     BIGINT NOT NULL,
                stake       BIGINT NOT NULL
            )
        """))
        await session.commit()


# ══════════════════════════════════════════════════════════════════
# CATALOGUE DE LA BOUTIQUE
# ══════════════════════════════════════════════════════════════════

CATALOGUE = {
    # id          : (nom, emoji, prix, description)
    "voiture":    ("Voiture de luxe",   "🚗",  2_000_000,  "Symbole de richesse. +prestige."),
    "villa":      ("Villa privée",      "🏡",  8_000_000,  "Résidence haut de gamme."),
    "yacht":      ("Yacht",             "🛥️",  15_000_000, "Pour naviguer avec style."),
    "jet":        ("Jet privé",         "✈️",  40_000_000, "Voyages en classe supérieure."),
    "garde":      ("Garde du corps",    "🕵️",  500_000,    "Réduit les risques de cambriolage."),
    "coffre":     ("Coffre-fort",       "🔐",  1_000_000,  "Protège 10% de tes $ en cas de cambriolage."),
    "montre":     ("Montre de luxe",    "⌚",  3_000_000,  "Bling-bling."),
    "jet_ski":    ("Jet-ski",           "🌊",  1_500_000,  "Pour les week-ends."),
    "casino":     ("Casino privé",      "🎰",  25_000_000, "Le summum du luxe."),
    "manoir":     ("Manoir",            "🏰",  50_000_000, "Plus grand que la villa."),
}

# ══════════════════════════════════════════════════════════════════
# NOUVEAU SYSTÈME FISCAL — Taxation agressive top 30
# ══════════════════════════════════════════════════════════════════

# Taux de base (appliqué 1x/jour à 12h sur le liquide)
TAX_BRACKETS = [
    (5_000_000,         0.00),   # < 5M : exonéré (relevé pour protéger les ruinés)
    (20_000_000,        0.01),   # 5M–20M : 1%
    (100_000_000,       0.03),   # 20M–100M : 3%
    (500_000_000,       0.06),   # 100M–500M : 6%
    (1_000_000_000,     0.10),   # 500M–1G : 10%
    (float("inf"),      0.15),   # > 1G : 15%
]

# Taxation spéciale top 10 — 2x par jour (toutes les 12h) sur fortune totale
TOP10_TAX_RATE     = 0.06   # réduit de 12% → 6%
TOP10_TAX_INTERVAL = 12     # espacé de 6h → 12h

# Taxation top 11–30 — 1x/jour supplémentaire sur fortune totale
TOP30_TAX_RATE = 0.03       # réduit de 6% → 3%

TOP10_MESSAGES = [
    "🏛️ Le gouvernement a décidé de te ponctionner davantage. Bienvenue dans le club des ultra-riches.",
    "💼 L'État a les yeux sur toi. Ton empire attire trop l'attention.",
    "⚖️ La justice fiscale frappe. Nul n'échappe au fisc.",
    "🎯 Ta fortune fait de toi une cible prioritaire du Trésor Public.",
    "🔍 Les inspecteurs des impôts ont audité tes comptes. Résultat : salé.",
    "📊 Trop de richesse tue la richesse. L'État rééquilibre la balance.",
    "🦅 L'aigle fiscal t'a repéré. Impossible de te cacher à ce niveau.",
]

TOP30_MESSAGES = [
    "📋 Avis d'imposition reçu. Le top 30 paie sa part.",
    "🏦 Le fisc te rappelle à l'ordre.",
    "💸 Taxe de solidarité prélevée.",
    "📬 L'enveloppe bleue est arrivée. C'est l'heure de payer.",
]


def _compute_tax(coins: int) -> int:
    """Calcule l'impôt journalier de base sur les coins en main."""
    if coins <= 0:
        return 0
    tax = 0
    prev = 0
    for limit, rate in TAX_BRACKETS:
        if coins <= prev:
            break
        tranche = min(coins, limit) - prev
        tax += int(tranche * rate)
        prev = limit
    return tax


def _tax_rate_display(coins: int) -> str:
    for limit, rate in TAX_BRACKETS:
        if coins < limit:
            return f"{rate*100:.0f}%"
    return f"{TAX_BRACKETS[-1][1]*100:.0f}%"


async def _get_top30_fortunes(session) -> list:
    """Retourne les 30 plus grandes fortunes (liquide + banque)."""
    rows = (await session.execute(text("""
        SELECT u.user_id, u.first_name, u.coins,
               COALESCE(SUM(b.balance), 0) AS bank_total,
               u.coins + COALESCE(SUM(b.balance), 0) AS fortune_totale
        FROM users u
        LEFT JOIN bank_accounts b ON b.user_id = u.user_id
        GROUP BY u.user_id, u.first_name, u.coins
        ORDER BY fortune_totale DESC
        LIMIT 30
    """))).fetchall()
    return rows


# ══════════════════════════════════════════════════════════════════
# /shop — BOUTIQUE
# ══════════════════════════════════════════════════════════════════

async def shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["🏪 <b>Boutique Luxe</b> — objets non-revendables au bot\n"]
    for item_id, (nom, emoji, prix, desc) in CATALOGUE.items():
        lines.append(f"{emoji} <b>{nom}</b> — <code>{_fmt(prix)} {CURRENCY}</code>")
        lines.append(f"   ↳ {desc}")
        lines.append(f"   ➡️ <code>/acheter {item_id}</code>\n")
    lines.append("📦 Pour vendre un objet entre joueurs : <code>/revendre &lt;id&gt; @joueur &lt;prix&gt;</code>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ══════════════════════════════════════════════════════════════════
# /acheter — ACHETER UN OBJET
# ══════════════════════════════════════════════════════════════════

async def acheter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_tg = update.effective_user
    user    = await ensure_user(user_tg)

    if not context.args:
        return await update.message.reply_text(
            "Usage : <code>/acheter &lt;id_objet&gt;</code>\nEx : /acheter voiture\n\nVoir la liste : /shop",
            parse_mode=ParseMode.HTML
        )

    item_id = context.args[0].lower()
    if item_id not in CATALOGUE:
        return await update.message.reply_text(
            f"❌ Objet inconnu : <code>{item_id}</code>\nFais /shop pour voir la liste.",
            parse_mode=ParseMode.HTML
        )

    nom, emoji, prix, desc = CATALOGUE[item_id]
    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.user_id)
        if not u or u.coins < prix:
            solde = u.coins if u else 0
            return await update.message.reply_text(
                f"❌ Pas assez de {CURRENCY}.\n"
                f"Prix : <b>{_fmt(prix)} {CURRENCY}</b> | Ton solde : <b>{_fmt(solde)} {CURRENCY}</b>",
                parse_mode=ParseMode.HTML
            )

        await session.execute(
            text("UPDATE users SET coins = CAST(coins AS BIGINT) - CAST(:amt AS BIGINT) WHERE user_id = :uid"),
            {"amt": prix, "uid": user.user_id}
        )

        await session.execute(text("""
            INSERT INTO shop_items (owner_id, group_id, item_id)
            VALUES (:uid, :gid, :iid)
        """), {"uid": user.user_id, "gid": group_id, "iid": item_id})

        await session.commit()

    await update.message.reply_text(
        f"{emoji} {mention(user)} a acheté <b>{nom}</b> pour <b>{_fmt(prix)} {CURRENCY}</b> !\n"
        f"💸 Cette somme quitte l'économie.",
        parse_mode=ParseMode.HTML
    )


# ══════════════════════════════════════════════════════════════════
# /inventaire — VOIR SES OBJETS
# ══════════════════════════════════════════════════════════════════

async def inventaire(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_tg  = update.effective_user
    user     = await ensure_user(user_tg)
    group_id = update.effective_chat.id

    # Cibler quelqu'un d'autre ?
    target_tg = await parse_target(update, context)
    if target_tg and target_tg.id != user_tg.id:
        user    = await ensure_user(target_tg)
        user_tg = target_tg

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text("""
            SELECT id, item_id FROM shop_items
            WHERE owner_id = :uid AND group_id = :gid
            ORDER BY bought_at DESC
        """), {"uid": user.user_id, "gid": group_id})).fetchall()

    if not rows:
        return await update.message.reply_text(
            f"📦 {user_tg.first_name} ne possède aucun objet."
        )

    lines = [f"🎒 <b>Inventaire de {user_tg.first_name}</b>\n"]
    for row_id, iid in rows:
        if iid in CATALOGUE:
            nom, emoji, prix, _ = CATALOGUE[iid]
            lines.append(f"  {emoji} <b>{nom}</b> (#{row_id}) — acheté {_fmt(prix)} {CURRENCY}")
    lines.append("\n💬 Pour vendre : <code>/revendre &lt;#id&gt; @joueur &lt;prix&gt;</code>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ══════════════════════════════════════════════════════════════════
# /revendre — PROPOSER UN OBJET À UN JOUEUR
# ══════════════════════════════════════════════════════════════════


async def impots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update.effective_user)

    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.user_id)
        if not u:
            return await update.message.reply_text("Compte introuvable.")
        coins = u.coins

        # Fortune totale (liquide + banque)
        bank_res = (await session.execute(text(
            "SELECT COALESCE(SUM(balance), 0) FROM bank_accounts WHERE user_id = :uid"
        ), {"uid": user.user_id})).scalar()
        fortune_totale = coins + int(bank_res or 0)

        # Rang dans le classement
        rank_res = (await session.execute(text("""
            SELECT COUNT(*) + 1 FROM (
                SELECT u2.user_id,
                       u2.coins + COALESCE(SUM(b2.balance), 0) AS fortune
                FROM users u2
                LEFT JOIN bank_accounts b2 ON b2.user_id = u2.user_id
                GROUP BY u2.user_id, u2.coins
            ) sub
            WHERE sub.fortune > :fortune
        """), {"fortune": fortune_totale})).scalar()
        rank = int(rank_res or 1)

    tax_base = _compute_tax(coins)
    taux     = _tax_rate_display(coins)

    # Calcul des taxes spéciales selon le rang
    if rank <= 10:
        tax_speciale_par_cycle = int(fortune_totale * TOP10_TAX_RATE)
        tax_speciale_jour      = tax_speciale_par_cycle * 4
        rang_label = f"🔥 TOP {rank} — Taxation maximale"
        extra = (
            f"\n⚡ <b>Taxation top 10 :</b>\n"
            f"  └ {int(TOP10_TAX_RATE*100)}% de ta fortune toutes les 6h\n"
            f"  └ Par cycle : <b>-{_fmt(tax_speciale_par_cycle)} {CURRENCY}</b>\n"
            f"  └ Par jour (x4) : <b>-{_fmt(tax_speciale_jour)} {CURRENCY}</b>\n"
        )
    elif rank <= 30:
        tax_speciale_jour = int(fortune_totale * TOP30_TAX_RATE)
        rang_label = f"📋 TOP {rank} — Taxe de solidarité"
        extra = (
            f"\n📋 <b>Taxe top 30 :</b>\n"
            f"  └ {int(TOP30_TAX_RATE*100)}% de ta fortune 1x/jour\n"
            f"  └ Par jour : <b>-{_fmt(tax_speciale_jour)} {CURRENCY}</b>\n"
        )
    else:
        rang_label = f"🏅 Rang #{rank} — Régime standard"
        extra = ""

    lines = [
        f"🏛️ <b>Impôts — Ton bilan fiscal</b>\n",
        f"🏆 {rang_label}",
        f"💰 Liquide : <b>{_fmt(coins)} {CURRENCY}</b>",
        f"🏦 Banques : <b>{_fmt(int(bank_res or 0))} {CURRENCY}</b>",
        f"💼 Fortune totale : <b>{_fmt(fortune_totale)} {CURRENCY}</b>\n",
        f"📊 Impôt de base (liquide, 1x/jour) :",
        f"  └ Tranche : {taux} → <b>-{_fmt(tax_base)} {CURRENCY}/jour</b>",
        extra,
        "📋 <b>Tranches de base :</b>",
        "  &lt; 1M $       → 0%",
        "  1M – 5M $    → 2%",
        "  5M – 20M $   → 5%",
        "  20M – 100M $ → 10%",
        "  100M – 1B $  → 18%",
        "  &gt; 1B $       → 28%",
        "\n⏰ Impôts de base : <b>12h00 UTC</b>",
        "⚡ Taxation top 10 : <b>toutes les 6h</b>",
        "📋 Taxation top 30 : <b>18h00 UTC</b>",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ══════════════════════════════════════════════════════════════════
# JOB IMPÔTS — prélèvement quotidien à 12h00 GMT
# ══════════════════════════════════════════════════════════════════

async def job_collect_taxes(context: ContextTypes.DEFAULT_TYPE):
    """Prélève les impôts de base sur tous les joueurs (1x/jour à 12h) — coins ET banque."""
    total_collected = 0
    total_players   = 0

    async with AsyncSessionLocal() as session:
        # Taxe sur les coins
        users = (await session.execute(text("SELECT user_id, coins FROM users WHERE coins > 1000000"))).fetchall()
        for uid, coins in users:
            tax = _compute_tax(coins)
            if tax <= 0:
                continue
            tax = min(tax, coins)
            await session.execute(text(
                "UPDATE users SET coins = coins - :tax WHERE user_id = :uid"
            ), {"tax": tax, "uid": uid})
            total_collected += tax
            total_players   += 1

        # Taxe sur les comptes bancaires (2% sur les soldes > 50M, 5% > 200M)
        bank_accounts = (await session.execute(text(
            "SELECT id, user_id, balance FROM bank_accounts WHERE balance > 50000000"
        ))).fetchall()
        for bid, uid, balance in bank_accounts:
            if balance > 200_000_000:
                bank_tax = int(balance * 0.05)
            else:
                bank_tax = int(balance * 0.02)
            bank_tax = min(bank_tax, balance)
            await session.execute(text(
                "UPDATE bank_accounts SET balance = balance - :tax WHERE id = :bid"
            ), {"tax": bank_tax, "bid": bid})
            total_collected += bank_tax

        await session.commit()

    logger.info(f"[IMPÔTS BASE] {total_players} joueurs taxés — {total_collected:,} {CURRENCY} retirés (coins + banque).")

    from database.models import GroupSettings
    async with AsyncSessionLocal() as session:
        groups = (await session.execute(text("SELECT group_id FROM group_settings"))).fetchall()
    for (gid,) in groups:
        try:
            await context.bot.send_message(
                chat_id=gid,
                text=(
                    f"🏛️ <b>COLLECTE DES IMPÔTS</b>\n\n"
                    f"👥 Joueurs taxés : <b>{total_players}</b>\n"
                    f"💸 Total retiré : <b>{_fmt(total_collected)} {CURRENCY}</b>\n\n"
                    f"Utilisez /impots pour voir votre taux."
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning(f"Impossible d'envoyer annonce impôts groupe {gid}: {e}")


async def job_tax_top10(context: ContextTypes.DEFAULT_TYPE):
    """
    Taxation agressive du top 10 — toutes les 6h.
    Prélève 12% de la fortune totale (liquide + banque).
    Notifie chaque joueur en DM.
    """
    import random as _random

    async with AsyncSessionLocal() as session:
        top30 = await _get_top30_fortunes(session)
        top10 = top30[:10]

        for rank, row in enumerate(top10, 1):
            uid          = row.user_id
            fortune      = int(row.fortune_totale)
            liquide      = int(row.coins)
            banque_total = int(row.bank_total)
            prenom       = row.first_name

            if fortune <= 0:
                continue

            tax_total = int(fortune * TOP10_TAX_RATE)
            if tax_total <= 0:
                continue

            # Prélever sur le liquide d'abord, puis sur la banque si insuffisant
            from_liquide = min(tax_total, liquide)
            reste        = tax_total - from_liquide
            from_banque  = min(reste, banque_total)
            tax_total_reel = from_liquide + from_banque

            if from_liquide > 0:
                await session.execute(text(
                    "UPDATE users SET coins = GREATEST(0, coins::bigint - :amt::bigint) WHERE user_id = :uid"
                ), {"amt": from_liquide, "uid": uid})

            if from_banque > 0:
                # Répartit proportionnellement sur tous les comptes bancaires
                accounts = (await session.execute(text(
                    "SELECT id, balance FROM bank_accounts WHERE user_id = :uid AND balance > 0 ORDER BY balance DESC"
                ), {"uid": uid})).fetchall()

                remaining_to_deduct = from_banque
                for acc_id, acc_balance in accounts:
                    if remaining_to_deduct <= 0:
                        break
                    deduct = min(remaining_to_deduct, int(acc_balance))
                    await session.execute(text(
                        "UPDATE bank_accounts SET balance = GREATEST(0, balance::bigint - :amt::bigint) WHERE id = :aid"
                    ), {"amt": deduct, "aid": acc_id})
                    remaining_to_deduct -= deduct

            msg_flavor = _random.choice(TOP10_MESSAGES)

            # Notif DM
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=(
                        f"💀 <b>TAXATION TOP {rank} — PRÉLÈVEMENT D'URGENCE</b>\n\n"
                        f"{msg_flavor}\n\n"
                        f"📊 Ta fortune totale : <b>{_fmt(fortune)} {CURRENCY}</b>\n"
                        f"🔥 Taux appliqué : <b>{int(TOP10_TAX_RATE*100)}%</b>\n"
                        f"💸 Prélevé sur liquide : <b>-{_fmt(from_liquide)} {CURRENCY}</b>\n"
                        f"🏦 Prélevé sur banque : <b>-{_fmt(from_banque)} {CURRENCY}</b>\n"
                        f"━━━━━━━━━━━━━━━━━\n"
                        f"💀 <b>Total ponctionné : -{_fmt(tax_total_reel)} {CURRENCY}</b>\n\n"
                        f"🏆 Tu es #{rank} du classement. Le fisc ne t'oubliera pas."
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.warning(f"[TAX TOP10] Impossible de notifier {uid}: {e}")

        await session.commit()

    logger.info(f"[TAX TOP10] Cycle terminé — {len(top10)} joueurs ponctionnés à {TOP10_TAX_RATE*100:.0f}%.")


async def job_tax_top30(context: ContextTypes.DEFAULT_TYPE):
    """
    Taxation top 11–30 — 1x/jour.
    Prélève 6% de la fortune totale. Notifie en DM.
    """
    import random as _random

    async with AsyncSessionLocal() as session:
        top30 = await _get_top30_fortunes(session)
        targets = top30[10:30]  # rangs 11 à 30

        for rank, row in enumerate(targets, 11):
            uid          = row.user_id
            fortune      = int(row.fortune_totale)
            liquide      = int(row.coins)
            banque_total = int(row.bank_total)
            prenom       = row.first_name

            if fortune <= 0:
                continue

            tax_total = int(fortune * TOP30_TAX_RATE)
            if tax_total <= 0:
                continue

            from_liquide   = min(tax_total, liquide)
            reste          = tax_total - from_liquide
            from_banque    = min(reste, banque_total)
            tax_total_reel = from_liquide + from_banque

            if from_liquide > 0:
                await session.execute(text(
                    "UPDATE users SET coins = GREATEST(0, coins::bigint - :amt::bigint) WHERE user_id = :uid"
                ), {"amt": from_liquide, "uid": uid})

            if from_banque > 0:
                accounts = (await session.execute(text(
                    "SELECT id, balance FROM bank_accounts WHERE user_id = :uid AND balance > 0 ORDER BY balance DESC"
                ), {"uid": uid})).fetchall()

                remaining_to_deduct = from_banque
                for acc_id, acc_balance in accounts:
                    if remaining_to_deduct <= 0:
                        break
                    deduct = min(remaining_to_deduct, int(acc_balance))
                    await session.execute(text(
                        "UPDATE bank_accounts SET balance = GREATEST(0, balance::bigint - :amt::bigint) WHERE id = :aid"
                    ), {"amt": deduct, "aid": acc_id})
                    remaining_to_deduct -= deduct

            msg_flavor = _random.choice(TOP30_MESSAGES)

            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=(
                        f"📋 <b>TAXE DE SOLIDARITÉ — TOP {rank}</b>\n\n"
                        f"{msg_flavor}\n\n"
                        f"📊 Fortune totale : <b>{_fmt(fortune)} {CURRENCY}</b>\n"
                        f"🔥 Taux : <b>{int(TOP30_TAX_RATE*100)}%</b>\n"
                        f"💸 Liquide : <b>-{_fmt(from_liquide)} {CURRENCY}</b>\n"
                        f"🏦 Banque : <b>-{_fmt(from_banque)} {CURRENCY}</b>\n"
                        f"━━━━━━━━━━━━━━━━━\n"
                        f"💀 <b>Total : -{_fmt(tax_total_reel)} {CURRENCY}</b>"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.warning(f"[TAX TOP30] Impossible de notifier {uid}: {e}")

        await session.commit()

    logger.info(f"[TAX TOP30] Cycle terminé — {len(targets)} joueurs taxés à {TOP30_TAX_RATE*100:.0f}%.")


# ══════════════════════════════════════════════════════════════════
# /alarme — PROTECTION CONTRE LE CAMBRIOLAGE
# ══════════════════════════════════════════════════════════════════

ALARM_LEVELS = {
    1: ("🔔 Alarme basique",   200_000,  0.40),  # 40% de bloquer
    2: ("🔒 Alarme avancée",   800_000,  0.65),
    3: ("🛡️ Forteresse",       3_000_000, 0.85),
}


async def cambrioler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voleur_tg = update.effective_user
    voleur    = await ensure_user(voleur_tg)
    group_id  = update.effective_chat.id

    target_tg = await parse_target(update, context)
    if not target_tg:
        return await update.message.reply_text(
            "❌ Mentionne ou réponds au message de ta cible.\n"
            "Usage : <code>/cambrioler @joueur</code>",
            parse_mode=ParseMode.HTML
        )
    if target_tg.id == voleur_tg.id:
        return await update.message.reply_text("😂 Tu ne peux pas te cambrioler toi-même !")

    target = await ensure_user(target_tg)

    async with AsyncSessionLocal() as session:
        # Cooldown voleur
        last = (await session.execute(text("""
            SELECT MAX(bought_at) FROM shop_items
            WHERE owner_id = :uid AND item_id = '__cambriolage_cd__'
        """), {"uid": voleur.user_id})).scalar()

        # On utilise un vrai cooldown via une table dédiée
        cd_row = (await session.execute(text("""
            SELECT last_attempt FROM cambriolage_cd WHERE user_id = :uid
        """), {"uid": voleur.user_id})).fetchone()

        now = datetime.utcnow()
        if cd_row:
            delta = (now - cd_row[0]).total_seconds() / 60
            if delta < CAMBRIOLAGE_COOLDOWN_MINUTES:
                reste = int(CAMBRIOLAGE_COOLDOWN_MINUTES - delta)
                return await update.message.reply_text(
                    f"⏳ Cooldown cambriolage : encore <b>{reste} min</b>.",
                    parse_mode=ParseMode.HTML
                )

        # Alarm de la cible
        alarm = (await session.execute(text("""
            SELECT level FROM alarm_shield WHERE user_id = :uid AND group_id = :gid
        """), {"uid": target.user_id, "gid": group_id})).fetchone()

        alarm_block_chance = ALARM_LEVELS[alarm[0]][2] if alarm else 0.0
        success_chance = CAMBRIOLAGE_SUCCESS_BASE * (1 - alarm_block_chance)

        # Mettre à jour le cooldown
        await session.execute(text("""
            INSERT INTO cambriolage_cd (user_id, last_attempt)
            VALUES (:uid, NOW())
            ON CONFLICT (user_id) DO UPDATE SET last_attempt = NOW()
        """), {"uid": voleur.user_id})

        if random.random() > success_chance:
            # ÉCHEC — alarme ou malchance
            raison = "🔔 L'alarme s'est déclenchée !" if alarm else "😬 Tu t'es fait repérer !"
            await session.commit()
            return await update.message.reply_text(
                f"❌ <b>Cambriolage raté !</b>\n{raison}\n"
                f"⏳ Prochain essai dans <b>{CAMBRIOLAGE_COOLDOWN_MINUTES} min</b>.",
                parse_mode=ParseMode.HTML
            )

        # Récupérer les objets de la cible
        objets = (await session.execute(text("""
            SELECT id, item_id FROM shop_items
            WHERE owner_id = :uid AND group_id = :gid
            ORDER BY RANDOM() LIMIT 1
        """), {"uid": target.user_id, "gid": group_id})).fetchone()

        if not objets:
            await session.commit()
            return await update.message.reply_text(
                f"😔 {target_tg.first_name} n'a aucun objet à voler !"
            )

        item_row_id, item_id = objets
        nom, emoji, _, _ = CATALOGUE.get(item_id, ("Objet", "📦", 0, ""))

        # Transfert de l'objet
        await session.execute(text(
            "UPDATE shop_items SET owner_id = :new_owner WHERE id = :iid"
        ), {"new_owner": voleur.user_id, "iid": item_row_id})

        await session.commit()

    await update.message.reply_text(
        f"🦹 <b>CAMBRIOLAGE RÉUSSI !</b>\n\n"
        f"{mention(voleur)} a volé {emoji} <b>{nom}</b> à {mention(target)} !\n"
        f"😱 {target_tg.first_name} devrait investir dans une alarme (/alarme).",
        parse_mode=ParseMode.HTML
    )


async def _ensure_cambriolage_cd_table():
    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS cambriolage_cd (
                user_id      BIGINT PRIMARY KEY,
                last_attempt TIMESTAMP NOT NULL
            )
        """))
        await session.commit()


# ══════════════════════════════════════════════════════════════════
# BRAQUAGE DE BANQUE — événement collectif
# ══════════════════════════════════════════════════════════════════

HEIST_JOIN_WINDOW   = 3 * 60   # 3 minutes pour rejoindre
HEIST_MIN_PLAYERS   = 2
HEIST_MAX_PLAYERS   = 6
HEIST_MIN_STAKE     = 50_000   # mise minimale pour participer
HEIST_SUCCESS_TABLE = {        # prob. de succès selon nb joueurs
    1: 0.20, 2: 0.35, 3: 0.50,
    4: 0.60, 5: 0.70, 6: 0.80,
}
HEIST_BANK_MULTIPLIER = 3.0    # le butin = 3x la mise totale


async def braquage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_tg  = update.effective_user
    user     = await ensure_user(user_tg)
    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        # Vérifier si un braquage est déjà en cours
        existing = (await session.execute(text("""
            SELECT id, leader_id, created_at FROM heist_sessions
            WHERE group_id = :gid AND status = 'recruiting'
        """), {"gid": group_id})).fetchone()

        if existing:
            heist_id, leader_id, created_at = existing
            nb = (await session.execute(text(
                "SELECT COUNT(*) FROM heist_members WHERE heist_id = :hid"
            ), {"hid": heist_id})).scalar()

            pot = (await session.execute(text(
                "SELECT SUM(stake) FROM heist_members WHERE heist_id = :hid"
            ), {"hid": heist_id})).scalar() or 0

            # Déjà membre ?
            deja = (await session.execute(text("""
                SELECT id FROM heist_members WHERE heist_id = :hid AND user_id = :uid
            """), {"hid": heist_id, "uid": user.user_id})).fetchone()

            if deja:
                return await update.message.reply_text(
                    f"⚠️ Tu participes déjà au braquage en cours !\n"
                    f"👥 {nb} membre(s) — 🏦 Butin potentiel : <b>{_fmt(int(pot * HEIST_BANK_MULTIPLIER))} {CURRENCY}</b>",
                    parse_mode=ParseMode.HTML
                )

            # Rejoindre le braquage existant
            if nb >= HEIST_MAX_PLAYERS:
                return await update.message.reply_text("❌ Le braquage est complet (6 joueurs max).")

            # Lire la mise depuis les args ou utiliser le minimum
            stake = HEIST_MIN_STAKE
            if context.args:
                try:
                    stake = int(context.args[0].replace(",", "").replace(" ", ""))
                    assert stake >= HEIST_MIN_STAKE
                except (ValueError, AssertionError):
                    return await update.message.reply_text(
                        f"❌ Mise minimale : {_fmt(HEIST_MIN_STAKE)} {CURRENCY}"
                    )

            u = await get_user(session, user.user_id)
            if not u or u.coins < stake:
                return await update.message.reply_text(
                    f"❌ Pas assez de {CURRENCY}. Mise : {_fmt(stake)} {CURRENCY}"
                )

            await session.execute(
                text("UPDATE users SET coins = CAST(coins AS BIGINT) - CAST(:amt AS BIGINT) WHERE user_id = :uid"),
                {"amt": stake, "uid": user.user_id}
            )
            await session.execute(text("""
                INSERT INTO heist_members (heist_id, user_id, stake)
                VALUES (:hid, :uid, :stake)
            """), {"hid": heist_id, "uid": user.user_id, "stake": stake})
            await session.execute(text(
                "UPDATE heist_sessions SET pot = pot + :s WHERE id = :hid"
            ), {"s": stake, "hid": heist_id})
            await session.commit()

            new_nb  = nb + 1
            new_pot = pot + stake
            prob    = int(HEIST_SUCCESS_TABLE.get(new_nb, 0.80) * 100)
            return await update.message.reply_text(
                f"🦹 {mention(user)} a rejoint le braquage ! (mise : {_fmt(stake)} {CURRENCY})\n"
                f"👥 Équipe : <b>{new_nb}</b> | 💰 Butin potentiel : <b>{_fmt(int(new_pot * HEIST_BANK_MULTIPLIER))} {CURRENCY}</b>\n"
                f"🎲 Probabilité de succès : <b>{prob}%</b>",
                parse_mode=ParseMode.HTML
            )

        # ── Créer un nouveau braquage ──
        stake = HEIST_MIN_STAKE
        if context.args:
            try:
                stake = int(context.args[0].replace(",", "").replace(" ", ""))
                assert stake >= HEIST_MIN_STAKE
            except (ValueError, AssertionError):
                return await update.message.reply_text(
                    f"❌ Mise minimale : {_fmt(HEIST_MIN_STAKE)} {CURRENCY}\n"
                    f"Usage : /braquage [mise]"
                )

        u = await get_user(session, user.user_id)
        if not u or u.coins < stake:
            return await update.message.reply_text(f"❌ Pas assez de {CURRENCY}.")

        await session.execute(
            text("UPDATE users SET coins = CAST(coins AS BIGINT) - CAST(:amt AS BIGINT) WHERE user_id = :uid"),
            {"amt": stake, "uid": user.user_id}
        )
        r = await session.execute(text("""
            INSERT INTO heist_sessions (group_id, leader_id, pot)
            VALUES (:gid, :uid, :stake)
            RETURNING id
        """), {"gid": group_id, "uid": user.user_id, "stake": stake})
        heist_id = r.scalar()
        await session.execute(text("""
            INSERT INTO heist_members (heist_id, user_id, stake)
            VALUES (:hid, :uid, :stake)
        """), {"hid": heist_id, "uid": user.user_id, "stake": stake})
        await session.commit()

    # Programmer le lancement automatique
    context.job_queue.run_once(
        _launch_heist,
        when=HEIST_JOIN_WINDOW,
        data={"heist_id": heist_id, "group_id": group_id},
        name=f"heist_{heist_id}",
    )

    await update.message.reply_text(
        f"🏦 <b>BRAQUAGE EN PRÉPARATION !</b>\n\n"
        f"👑 Chef : {mention(user)}\n"
        f"💰 Mise chef : <b>{_fmt(stake)} {CURRENCY}</b>\n\n"
        f"⏳ Vous avez <b>3 minutes</b> pour rejoindre !\n"
        f"➡️ Tape <code>/braquage [mise]</code> pour participer.\n"
        f"🎯 Plus on est nombreux, plus on a de chances !",
        parse_mode=ParseMode.HTML
    )


async def annulerbraquage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_tg  = update.effective_user
    user     = await ensure_user(user_tg)
    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        heist = (await session.execute(text("""
            SELECT id, leader_id FROM heist_sessions
            WHERE group_id = :gid AND status = 'recruiting'
        """), {"gid": group_id})).fetchone()

        if not heist:
            return await update.message.reply_text("Aucun braquage en cours.")

        heist_id, leader_id = heist
        if leader_id != user.user_id:
            return await update.message.reply_text("⛔ Seul le chef peut annuler.")

        # Rembourser
        members = (await session.execute(text("""
            SELECT user_id, stake FROM heist_members WHERE heist_id = :hid
        """), {"hid": heist_id})).fetchall()
        for uid, stake in members:
            await session.execute(text(
                "UPDATE users SET coins = coins + :s WHERE user_id = :uid"
            ), {"s": stake, "uid": uid})

        await session.execute(text(
            "UPDATE heist_sessions SET status = 'cancelled' WHERE id = :hid"
        ), {"hid": heist_id})
        await session.commit()

    await update.message.reply_text(
        "❌ Braquage annulé. Tous les participants ont été remboursés."
    )


async def _launch_heist(context: ContextTypes.DEFAULT_TYPE):
    """Lancé automatiquement après la fenêtre de recrutement."""
    data     = context.job.data
    heist_id = data["heist_id"]
    group_id = data["group_id"]

    async with AsyncSessionLocal() as session:
        heist = (await session.execute(text("""
            SELECT id, status, pot FROM heist_sessions WHERE id = :hid
        """), {"hid": heist_id})).fetchone()

        if not heist or heist[1] != "recruiting":
            return

        pot     = heist[2]
        members = (await session.execute(text("""
            SELECT hm.user_id, hm.stake, u.first_name
            FROM heist_members hm
            JOIN users u ON u.user_id = hm.user_id
            WHERE hm.heist_id = :hid
        """), {"hid": heist_id})).fetchall()

        nb       = len(members)
        prob     = HEIST_SUCCESS_TABLE.get(nb, 0.20)
        success  = random.random() < prob
        butin    = int(pot * HEIST_BANK_MULTIPLIER)

        await session.execute(text(
            "UPDATE heist_sessions SET status = 'done' WHERE id = :hid"
        ), {"hid": heist_id})

        noms = ", ".join(f[2] for f in members)

        if success:
            # Distribuer le butin proportionnellement
            for uid, stake, nom in members:
                part = int(butin * (stake / pot))
                await session.execute(text(
                    "UPDATE users SET coins = coins + :part WHERE user_id = :uid"
                ), {"part": part, "uid": uid})
            await session.commit()

            parts_txt = "\n".join(
                f"  💰 {nom} : +{_fmt(int(butin * (stake/pot)))} {CURRENCY}"
                for _, stake, nom in members
            )
            await context.bot.send_message(
                chat_id=group_id,
                text=(
                    f"🏦💥 <b>BRAQUAGE RÉUSSI !</b>\n\n"
                    f"👥 Équipe ({nb}) : {noms}\n"
                    f"🎲 Probabilité : {int(prob*100)}%\n\n"
                    f"💵 Butin total : <b>{_fmt(butin)} {CURRENCY}</b> (x{HEIST_BANK_MULTIPLIER} la mise)\n\n"
                    f"{parts_txt}"
                ),
                parse_mode=ParseMode.HTML,
            )
        else:
            # Pas de remboursement — l'argent est perdu (braquage raté = arrestation)
            await session.commit()
            await context.bot.send_message(
                chat_id=group_id,
                text=(
                    f"🚔 <b>BRAQUAGE RATÉ !</b>\n\n"
                    f"👥 Équipe ({nb}) : {noms}\n"
                    f"🎲 Probabilité : {int(prob*100)}%\n\n"
                    f"💸 <b>{_fmt(pot)} {CURRENCY}</b> confisqués par la police !\n"
                    f"😭 Personne n'est remboursé."
                ),
                parse_mode=ParseMode.HTML,
            )


# ══════════════════════════════════════════════════════════════════
# SETUP JOBS
# ══════════════════════════════════════════════════════════════════

def setup_drain_jobs(app: Application):
    from datetime import time as dtime
    from datetime import timedelta as tdelta

    # Impôts de base — 1x/jour à 12h UTC (tout le monde)
    app.job_queue.run_daily(
        job_collect_taxes,
        time=dtime(hour=12, minute=0, tzinfo=timezone.utc),
        name="collect_taxes",
    )

    # Taxation top 10 — toutes les 12h (2x/jour), commence 5min après démarrage
    app.job_queue.run_repeating(
        job_tax_top10,
        interval=tdelta(hours=12),
        first=tdelta(minutes=5),
        name="tax_top10",
    )

    # Taxation top 11-30 — 1x/jour à 18h UTC
    app.job_queue.run_daily(
        job_tax_top30,
        time=dtime(hour=18, minute=0, tzinfo=timezone.utc),
        name="tax_top30",
    )

    # Événements économiques aléatoires — 3x/jour (8h, 14h, 20h UTC)
    for h in [8, 14, 20]:
        app.job_queue.run_daily(
            job_random_economic_event,
            time=dtime(hour=h, minute=0, tzinfo=timezone.utc),
            name=f"eco_event_{h}h",
        )


# ─── ÉVÉNEMENTS ÉCONOMIQUES ALÉATOIRES ───────────────────────────────────────

ECONOMIC_EVENTS = [
    {
        "name": "💹 Boom économique",
        "desc": "Une vague de prospérité déferle sur la région ! Tous les joueurs reçoivent un bonus.",
        "type": "bonus_all",
        "min_pct": 0.05,   # +5% des coins
        "max_pct": 0.15,   # +15% des coins
        "probability": 15,
    },
    {
        "name": "📉 Crise financière",
        "desc": "Les marchés s'effondrent ! Tout le monde perd une partie de ses coins.",
        "type": "malus_coins",
        "min_pct": 0.05,
        "max_pct": 0.20,
        "probability": 15,
    },
    {
        "name": "🏦 Taxation d'urgence",
        "desc": "Le gouvernement prélève une taxe d'urgence sur les grandes fortunes bancaires !",
        "type": "malus_bank_rich",   # Touche uniquement les comptes > 50M
        "min_pct": 0.08,
        "max_pct": 0.18,
        "probability": 12,
    },
    {
        "name": "🎰 Fièvre du jeu",
        "desc": "Une fièvre de générosité s'empare des casinos ! Tous les joueurs reçoivent un cadeau.",
        "type": "bonus_fixed",
        "amount": 500_000,
        "probability": 10,
    },
    {
        "name": "🌪️ Inflation galopante",
        "desc": "L'inflation frappe dur. Les plus petites fortunes sont épargnées, les grandes perdent plus.",
        "type": "malus_progressive",  # Plus tu as, plus tu perds
        "probability": 13,
    },
    {
        "name": "💰 Jackpot national",
        "desc": "Le gouvernement redistribue les surplus fiscaux ! Un bonus pour tous.",
        "type": "bonus_fixed",
        "amount": 1_000_000,
        "probability": 8,
    },
    {
        "name": "🔒 Gel des comptes bancaires",
        "desc": "Les autorités gèlent temporairement 10% des dépôts bancaires dans tous les établissements.",
        "type": "malus_bank_all",
        "min_pct": 0.08,
        "max_pct": 0.12,
        "probability": 12,
    },
    {
        "name": "😴 Rien à signaler",
        "desc": "Les marchés sont calmes. Profitez-en pour jouer !",
        "type": "none",
        "probability": 15,
    },
]


async def job_random_economic_event(context):
    """Déclenche un événement économique aléatoire 3x/jour."""
    import random as _rnd

    weights = [e["probability"] for e in ECONOMIC_EVENTS]
    event   = _rnd.choices(ECONOMIC_EVENTS, weights=weights, k=1)[0]

    if event["type"] == "none":
        return  # Pas d'annonce, rien ne se passe

    affected = 0
    total_delta = 0

    async with AsyncSessionLocal() as session:
        if event["type"] == "bonus_all":
            pct = _rnd.uniform(event["min_pct"], event["max_pct"])
            users = (await session.execute(text("SELECT user_id, coins FROM users"))).fetchall()
            for uid, coins in users:
                bonus = int(coins * pct)
                if bonus <= 0:
                    continue
                await session.execute(text(
                    "UPDATE users SET coins = coins + :b WHERE user_id = :uid"
                ), {"b": bonus, "uid": uid})
                total_delta += bonus
                affected += 1

        elif event["type"] == "malus_coins":
            pct = _rnd.uniform(event["min_pct"], event["max_pct"])
            users = (await session.execute(text("SELECT user_id, coins FROM users WHERE coins > 100000"))).fetchall()
            for uid, coins in users:
                malus = int(coins * pct)
                malus = min(malus, coins - 1000)  # Garder 1000 minimum
                if malus <= 0:
                    continue
                await session.execute(text(
                    "UPDATE users SET coins = coins - :m WHERE user_id = :uid"
                ), {"m": malus, "uid": uid})
                total_delta -= malus
                affected += 1

        elif event["type"] == "malus_bank_rich":
            pct = _rnd.uniform(event["min_pct"], event["max_pct"])
            accounts = (await session.execute(text(
                "SELECT id, balance FROM bank_accounts WHERE balance > 50000000"
            ))).fetchall()
            for bid, balance in accounts:
                malus = int(balance * pct)
                malus = min(malus, balance)
                if malus <= 0:
                    continue
                await session.execute(text(
                    "UPDATE bank_accounts SET balance = balance - :m WHERE id = :bid"
                ), {"m": malus, "bid": bid})
                total_delta -= malus
                affected += 1

        elif event["type"] == "bonus_fixed":
            amount = event["amount"]
            users = (await session.execute(text("SELECT user_id FROM users"))).fetchall()
            for (uid,) in users:
                await session.execute(text(
                    "UPDATE users SET coins = coins + :a WHERE user_id = :uid"
                ), {"a": amount, "uid": uid})
                total_delta += amount
                affected += 1

        elif event["type"] == "malus_progressive":
            users = (await session.execute(text("SELECT user_id, coins FROM users WHERE coins > 500000"))).fetchall()
            for uid, coins in users:
                if coins > 500_000_000:
                    pct = 0.15
                elif coins > 100_000_000:
                    pct = 0.10
                elif coins > 10_000_000:
                    pct = 0.06
                else:
                    pct = 0.03
                malus = int(coins * pct)
                malus = min(malus, coins - 1000)
                if malus <= 0:
                    continue
                await session.execute(text(
                    "UPDATE users SET coins = coins - :m WHERE user_id = :uid"
                ), {"m": malus, "uid": uid})
                total_delta -= malus
                affected += 1

        elif event["type"] == "malus_bank_all":
            pct = _rnd.uniform(event["min_pct"], event["max_pct"])
            accounts = (await session.execute(text(
                "SELECT id, balance FROM bank_accounts WHERE balance > 0"
            ))).fetchall()
            for bid, balance in accounts:
                malus = int(balance * pct)
                malus = min(malus, balance)
                if malus <= 0:
                    continue
                await session.execute(text(
                    "UPDATE bank_accounts SET balance = balance - :m WHERE id = :bid"
                ), {"m": malus, "bid": bid})
                total_delta -= malus
                affected += 1

        await session.commit()

    # Annoncer dans tous les groupes actifs
    sign   = "+" if total_delta >= 0 else ""
    resume = f"{sign}{_fmt(total_delta)}" if total_delta != 0 else ""
    msg = (
        f"📰 <b>ÉVÉNEMENT ÉCONOMIQUE</b>\n\n"
        f"{event['name']}\n"
        f"<i>{event['desc']}</i>\n\n"
        f"👥 Joueurs impactés : <b>{affected}</b>"
        + (f"\n💸 Impact total : <b>{resume} {CURRENCY}</b>" if resume else "")
    )

    from database.models import GroupSettings
    async with AsyncSessionLocal() as session:
        groups = (await session.execute(text("SELECT group_id FROM group_settings"))).fetchall()
    for (gid,) in groups:
        try:
            await context.bot.send_message(chat_id=gid, text=msg, parse_mode="HTML")
        except Exception:
            pass

    logger.info(f"[ÉVÉNEMENT ÉCO] {event['name']} — {affected} joueurs, delta={total_delta:+,}")
