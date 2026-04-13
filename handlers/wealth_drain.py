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
    "coffre":     ("Coffre-fort",       "🔐",  1_000_000,  "Protège 10% de tes coins en cas de cambriolage."),
    "montre":     ("Montre de luxe",    "⌚",  3_000_000,  "Bling-bling."),
    "jet_ski":    ("Jet-ski",           "🌊",  1_500_000,  "Pour les week-ends."),
    "casino":     ("Casino privé",      "🎰",  25_000_000, "Le summum du luxe."),
    "manoir":     ("Manoir",            "🏰",  50_000_000, "Plus grand que la villa."),
}

# Taux d'imposition par tranche (sur les coins EN MAIN uniquement)
TAX_BRACKETS = [
    (1_000_000,   0.00),   # < 1M : 0%
    (5_000_000,   0.02),   # 1M–5M : 2%
    (20_000_000,  0.05),   # 5M–20M : 5%
    (100_000_000, 0.10),   # 20M–100M : 10%
    (float("inf"),0.18),   # > 100M : 18%
]


def _compute_tax(coins: int) -> int:
    """Calcule l'impôt journalier sur les coins en main."""
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


# ══════════════════════════════════════════════════════════════════
# /shop — BOUTIQUE
# ══════════════════════════════════════════════════════════════════

async def shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["🏪 <b>Boutique Luxe</b> — objets non-revendables au bot\n"]
    for item_id, (nom, emoji, prix, desc) in CATALOGUE.items():
        lines.append(f"{emoji} <b>{nom}</b> — <code>{_fmt(prix)} $</code>")
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
                f"❌ Pas assez de coins.\n"
                f"Prix : <b>{_fmt(prix)} $</b> | Ton solde : <b>{_fmt(solde)} $</b>",
                parse_mode=ParseMode.HTML
            )

        u.coins -= prix

        await session.execute(text("""
            INSERT INTO shop_items (owner_id, group_id, item_id)
            VALUES (:uid, :gid, :iid)
        """), {"uid": user.user_id, "gid": group_id, "iid": item_id})

        await session.commit()

    await update.message.reply_text(
        f"{emoji} {mention(user)} a acheté <b>{nom}</b> pour <b>{_fmt(prix)} $</b> !\n"
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
            lines.append(f"  {emoji} <b>{nom}</b> (#{row_id}) — acheté {_fmt(prix)} $")
    lines.append("\n💬 Pour vendre : <code>/revendre &lt;#id&gt; @joueur &lt;prix&gt;</code>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ══════════════════════════════════════════════════════════════════
# /revendre — PROPOSER UN OBJET À UN JOUEUR
# ══════════════════════════════════════════════════════════════════

async def revendre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_tg  = update.effective_user
    user     = await ensure_user(user_tg)
    group_id = update.effective_chat.id

    # Usage : /revendre <row_id> @joueur <prix>
    if len(context.args) < 3:
        return await update.message.reply_text(
            "Usage : <code>/revendre &lt;#id_objet&gt; @joueur &lt;prix&gt;</code>\n"
            "Trouve le #id dans /inventaire.",
            parse_mode=ParseMode.HTML
        )

    try:
        row_id = int(context.args[0].lstrip("#"))
        prix   = int(context.args[-1].replace(",", "").replace(" ", ""))
    except ValueError:
        return await update.message.reply_text("❌ ID et prix doivent être des nombres.")

    if prix <= 0:
        return await update.message.reply_text("❌ Prix invalide.")

    # Récupérer le buyer via mention ou reply
    target_tg = await parse_target(update, context)
    if not target_tg or target_tg.id == user_tg.id:
        return await update.message.reply_text(
            "❌ Mentionne ou réponds au message du joueur à qui tu veux vendre."
        )

    buyer = await ensure_user(target_tg)

    async with AsyncSessionLocal() as session:
        # Vérifier que l'objet appartient au vendeur
        row = (await session.execute(text("""
            SELECT id, item_id FROM shop_items
            WHERE id = :rid AND owner_id = :uid AND group_id = :gid
        """), {"rid": row_id, "uid": user.user_id, "gid": group_id})).fetchone()

        if not row:
            return await update.message.reply_text(
                f"❌ Tu ne possèdes pas l'objet #{row_id} dans ce groupe."
            )

        item_id = row[1]
        nom, emoji, _, _ = CATALOGUE.get(item_id, ("Objet", "📦", 0, ""))

        # Supprimer les offres précédentes sur cet objet
        await session.execute(text(
            "DELETE FROM shop_offers WHERE item_row_id = :rid"
        ), {"rid": row_id})

        # Créer l'offre
        await session.execute(text("""
            INSERT INTO shop_offers (item_row_id, seller_id, buyer_id, group_id, price)
            VALUES (:rid, :sid, :bid, :gid, :price)
        """), {"rid": row_id, "sid": user.user_id, "bid": buyer.user_id,
               "gid": group_id, "price": prix})

        offer_id = (await session.execute(text(
            "SELECT id FROM shop_offers WHERE item_row_id = :rid AND buyer_id = :bid ORDER BY id DESC LIMIT 1"
        ), {"rid": row_id, "bid": buyer.user_id})).scalar()

        await session.commit()

    await update.message.reply_text(
        f"💼 {mention(user)} propose {emoji} <b>{nom}</b> à {mention(buyer)}\n"
        f"💰 Prix : <b>{_fmt(prix)} $</b>\n\n"
        f"➡️ {target_tg.first_name} : <code>/acceptrevente {offer_id}</code> pour accepter.",
        parse_mode=ParseMode.HTML
    )


# ══════════════════════════════════════════════════════════════════
# /acceptrevente — ACCEPTER UNE OFFRE
# ══════════════════════════════════════════════════════════════════

async def acceptrevente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_tg  = update.effective_user
    user     = await ensure_user(user_tg)

    if not context.args:
        return await update.message.reply_text("Usage : <code>/acceptrevente &lt;id_offre&gt;</code>", parse_mode=ParseMode.HTML)

    try:
        offer_id = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("❌ ID invalide.")

    async with AsyncSessionLocal() as session:
        offer = (await session.execute(text("""
            SELECT o.id, o.item_row_id, o.seller_id, o.buyer_id, o.price, s.item_id
            FROM shop_offers o
            JOIN shop_items s ON s.id = o.item_row_id
            WHERE o.id = :oid
        """), {"oid": offer_id})).fetchone()

        if not offer:
            return await update.message.reply_text("❌ Offre introuvable ou déjà traitée.")

        oid, item_row_id, seller_id, buyer_id, prix, item_id = offer

        if buyer_id != user.user_id:
            return await update.message.reply_text("❌ Cette offre ne t'est pas destinée.")

        buyer_u  = await get_user(session, buyer_id)
        seller_u = await get_user(session, seller_id)

        if not buyer_u or buyer_u.coins < prix:
            have = buyer_u.coins if buyer_u else 0
            return await update.message.reply_text(
                f"❌ Pas assez de coins. Besoin : {_fmt(prix)} $ | Solde : {_fmt(have)} $"
            )

        # Transfert argent (seller reçoit, buyer paie)
        buyer_u.coins  -= prix
        seller_u.coins += prix

        # Transfert objet
        await session.execute(text(
            "UPDATE shop_items SET owner_id = :new_owner WHERE id = :iid"
        ), {"new_owner": buyer_id, "iid": item_row_id})

        # Supprimer l'offre
        await session.execute(text("DELETE FROM shop_offers WHERE id = :oid"), {"oid": oid})

        await session.commit()

    nom, emoji, _, _ = CATALOGUE.get(item_id, ("Objet", "📦", 0, ""))
    await update.message.reply_text(
        f"✅ Transaction réussie !\n"
        f"{emoji} <b>{nom}</b> transféré à {mention(user)}\n"
        f"💰 <b>{_fmt(prix)} $</b> versés au vendeur.",
        parse_mode=ParseMode.HTML
    )


# ══════════════════════════════════════════════════════════════════
# /impots — VOIR SON TAUX + ESTIMATION
# ══════════════════════════════════════════════════════════════════

async def impots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = await ensure_user(update.effective_user)

    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.user_id)
        if not u:
            return await update.message.reply_text("Compte introuvable.")
        coins = u.coins

    tax      = _compute_tax(coins)
    taux     = _tax_rate_display(coins)

    lines = [
        "🏛️ <b>Impôts — Estimation quotidienne</b>\n",
        f"💰 Coins en main : <b>{_fmt(coins)} $</b>",
        f"📊 Tranche : <b>{taux}</b>",
        f"💸 Taxe du jour : <b>{_fmt(tax)} $</b>",
        "",
        "📋 <b>Tranches d'imposition :</b>",
        "  &lt; 1M $      → 0%",
        "  1M – 5M $   → 2%",
        "  5M – 20M $  → 5%",
        "  20M – 100M $→ 10%",
        "  &gt; 100M $   → 18%",
        "",
        "⏰ Les impôts sont prélevés chaque jour à <b>12h00 GMT</b>.",
        "💡 Astuce : dépose à la banque ou achète des objets en boutique !",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ══════════════════════════════════════════════════════════════════
# JOB IMPÔTS — prélèvement quotidien à 12h00 GMT
# ══════════════════════════════════════════════════════════════════

async def job_collect_taxes(context: ContextTypes.DEFAULT_TYPE):
    """Prélève les impôts sur tous les utilisateurs chaque jour à 12h00 GMT."""
    total_collected = 0
    total_players   = 0

    async with AsyncSessionLocal() as session:
        users = (await session.execute(text("SELECT user_id, coins FROM users WHERE coins > 1000000"))).fetchall()

        for uid, coins in users:
            tax = _compute_tax(coins)
            if tax <= 0:
                continue
            tax = min(tax, coins)  # ne jamais prendre plus que ce que le joueur a
            await session.execute(text(
                "UPDATE users SET coins = coins - :tax WHERE user_id = :uid"
            ), {"tax": tax, "uid": uid})
            total_collected += tax
            total_players   += 1

        await session.commit()

    logger.info(f"[IMPÔTS] {total_players} joueurs taxés — {total_collected:,} $ collectés et retirés de l'économie.")

    # Annonce dans tous les groupes actifs
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
                    f"💸 Total retiré de l'économie : <b>{_fmt(total_collected)} $</b>\n\n"
                    f"Utilisez /impots pour voir votre taux.\n"
                    f"💡 Déposez à la banque pour réduire votre base imposable !"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning(f"Impossible d'envoyer annonce impôts groupe {gid}: {e}")


# ══════════════════════════════════════════════════════════════════
# /alarme — PROTECTION CONTRE LE CAMBRIOLAGE
# ══════════════════════════════════════════════════════════════════

ALARM_LEVELS = {
    1: ("🔔 Alarme basique",   200_000,  0.40),  # 40% de bloquer
    2: ("🔒 Alarme avancée",   800_000,  0.65),
    3: ("🛡️ Forteresse",       3_000_000, 0.85),
}


async def alarme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_tg  = update.effective_user
    user     = await ensure_user(user_tg)
    group_id = update.effective_chat.id

    if not context.args:
        lines = ["🔔 <b>Systèmes d'alarme disponibles</b>\n"]
        for lvl, (nom, prix, prot) in ALARM_LEVELS.items():
            lines.append(f"  Niveau {lvl} — {nom} : <b>{_fmt(prix)} $</b> (protection {int(prot*100)}%)")
        lines.append("\nUsage : <code>/alarme &lt;1|2|3&gt;</code>")
        return await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    try:
        level = int(context.args[0])
        assert level in ALARM_LEVELS
    except (ValueError, AssertionError):
        return await update.message.reply_text("❌ Niveau invalide. Choisis 1, 2 ou 3.")

    nom, prix, prot = ALARM_LEVELS[level]

    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.user_id)
        if not u or u.coins < prix:
            return await update.message.reply_text(
                f"❌ Pas assez de coins. Besoin : <b>{_fmt(prix)} $</b>",
                parse_mode=ParseMode.HTML
            )

        u.coins -= prix

        await session.execute(text("""
            INSERT INTO alarm_shield (user_id, group_id, level, bought_at)
            VALUES (:uid, :gid, :lvl, NOW())
            ON CONFLICT (user_id) DO UPDATE SET level = :lvl, group_id = :gid, bought_at = NOW()
        """), {"uid": user.user_id, "gid": group_id, "lvl": level})

        await session.commit()

    await update.message.reply_text(
        f"✅ {mention(user)} a installé {nom} !\n"
        f"🛡️ Protection : <b>{int(prot*100)}%</b> de bloquer les cambrioleurs.\n"
        f"💸 Coût : <b>{_fmt(prix)} $</b> retirés de l'économie.",
        parse_mode=ParseMode.HTML
    )


# ══════════════════════════════════════════════════════════════════
# /cambrioler — VOLER LES OBJETS D'UN JOUEUR
# ══════════════════════════════════════════════════════════════════

CAMBRIOLAGE_COOLDOWN_MINUTES = 60
CAMBRIOLAGE_SUCCESS_BASE     = 0.40   # 40% de base sans alarme


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
                    f"👥 {nb} membre(s) — 🏦 Butin potentiel : <b>{_fmt(int(pot * HEIST_BANK_MULTIPLIER))} $</b>",
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
                        f"❌ Mise minimale : {_fmt(HEIST_MIN_STAKE)} $"
                    )

            u = await get_user(session, user.user_id)
            if not u or u.coins < stake:
                return await update.message.reply_text(
                    f"❌ Pas assez de coins. Mise : {_fmt(stake)} $"
                )

            u.coins -= stake
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
                f"🦹 {mention(user)} a rejoint le braquage ! (mise : {_fmt(stake)} $)\n"
                f"👥 Équipe : <b>{new_nb}</b> | 💰 Butin potentiel : <b>{_fmt(int(new_pot * HEIST_BANK_MULTIPLIER))} $</b>\n"
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
                    f"❌ Mise minimale : {_fmt(HEIST_MIN_STAKE)} $\n"
                    f"Usage : /braquage [mise]"
                )

        u = await get_user(session, user.user_id)
        if not u or u.coins < stake:
            return await update.message.reply_text(f"❌ Pas assez de coins.")

        u.coins -= stake
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
        f"💰 Mise chef : <b>{_fmt(stake)} $</b>\n\n"
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
                f"  💰 {nom} : +{_fmt(int(butin * (stake/pot)))} $"
                for _, stake, nom in members
            )
            await context.bot.send_message(
                chat_id=group_id,
                text=(
                    f"🏦💥 <b>BRAQUAGE RÉUSSI !</b>\n\n"
                    f"👥 Équipe ({nb}) : {noms}\n"
                    f"🎲 Probabilité : {int(prob*100)}%\n\n"
                    f"💵 Butin total : <b>{_fmt(butin)} $</b> (x{HEIST_BANK_MULTIPLIER} la mise)\n\n"
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
                    f"💸 <b>{_fmt(pot)} $</b> confisqués par la police !\n"
                    f"😭 Personne n'est remboursé."
                ),
                parse_mode=ParseMode.HTML,
            )


# ══════════════════════════════════════════════════════════════════
# SETUP JOBS
# ══════════════════════════════════════════════════════════════════

def setup_drain_jobs(app: Application):
    from datetime import time as dtime
    app.job_queue.run_daily(
        job_collect_taxes,
        time=dtime(hour=12, minute=0, tzinfo=timezone.utc),
        name="collect_taxes",
    )
