"""
Système de compétition inter-entreprises.

Commandes :
  /startcompet [prize_coins]  — Admin : lance une compétition 3 jours
  /compet                     — Classement en temps réel
  /stopcompet                 — Admin : clôture et distribue les récompenses
"""

import logging
from datetime import datetime, timedelta
from sqlalchemy import select, func, text

from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database.db import AsyncSessionLocal
from database.models import Company, CompanyEmployee
from handlers.admin import is_admin

logger = logging.getLogger(__name__)

CURRENCY = "$"

# ─── Stockage en mémoire (survit aux redémarrages via DB) ────────────────────

def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


# ─── Helpers DB ──────────────────────────────────────────────────────────────

async def _ensure_compet_table(session):
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS competitions (
            id          SERIAL PRIMARY KEY,
            started_at  TIMESTAMP NOT NULL DEFAULT NOW(),
            ends_at     TIMESTAMP NOT NULL,
            prize_coins BIGINT NOT NULL DEFAULT 50000000,
            status      VARCHAR(20) NOT NULL DEFAULT 'active',
            winner_id   INTEGER REFERENCES companies(id)
        )
    """))
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS competition_snapshots (
            id           SERIAL PRIMARY KEY,
            compet_id    INTEGER REFERENCES competitions(id),
            company_id   INTEGER REFERENCES companies(id),
            cmds_at_start BIGINT NOT NULL DEFAULT 0
        )
    """))
    await session.commit()


async def _get_active_compet(session):
    result = await session.execute(
        text("SELECT * FROM competitions WHERE status = 'active' ORDER BY id DESC LIMIT 1")
    )
    return result.mappings().first()


async def _get_standings(session, compet_id: int) -> list:
    """Retourne le classement : [(company, cmds_done)] trié desc."""
    result = await session.execute(text("""
        SELECT 
            c.id,
            c.name,
            c.reputation,
            c.owner_id,
            cs.cmds_at_start,
            COALESCE(SUM(ce.command_count), 0) AS total_now,
            GREATEST(0, COALESCE(SUM(ce.command_count), 0) - cs.cmds_at_start) AS cmds_done
        FROM competition_snapshots cs
        JOIN companies c ON c.id = cs.company_id
        LEFT JOIN company_employees ce ON ce.company_id = c.id AND ce.left_at IS NULL
        WHERE cs.compet_id = :cid
        GROUP BY c.id, c.name, c.reputation, c.owner_id, cs.cmds_at_start
        ORDER BY cmds_done DESC
    """), {"cid": compet_id})
    return result.mappings().all()


# ─── /startcompet ─────────────────────────────────────────────────────────────

async def startcompet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await is_admin(user.id):
        await update.message.reply_text("⛔ Réservé aux admins.")
        return

    prizes_coins = [50_000_000, 25_000_000, 12_500_000]
    prizes_rep   = [0.5, 0.3, 0.2]

    if context.args:
        try:
            if len(context.args) == 6:
                prizes_coins = [int(context.args[0]), int(context.args[1]), int(context.args[2])]
                prizes_rep   = [float(context.args[3]), float(context.args[4]), float(context.args[5])]
            elif len(context.args) == 1:
                p = int(context.args[0])
                prizes_coins = [p, p // 2, p // 4]
            else:
                await update.message.reply_text(
                    "❌ Usage :\n"
                    "<code>/startcompet coins1 coins2 coins3 rep1 rep2 rep3</code>\n"
                    "Ex : <code>/startcompet 100000000 50000000 25000000 1.0 0.5 0.3</code>\n\n"
                    "Ou simple : <code>/startcompet 50000000</code>",
                    parse_mode=ParseMode.HTML
                )
                return
        except ValueError:
            await update.message.reply_text(
                "❌ Valeurs invalides. Usage : <code>/startcompet coins1 coins2 coins3 rep1 rep2 rep3</code>",
                parse_mode=ParseMode.HTML
            )
            return

    async with AsyncSessionLocal() as session:
        await _ensure_compet_table(session)

        # Vérifier si compet déjà active
        existing = await _get_active_compet(session)
        if existing:
            ends = existing["ends_at"].strftime("%d/%m %H:%M")
            await update.message.reply_text(
                f"❌ Une compétition est déjà en cours jusqu'au <b>{ends} UTC</b>.\n"
                f"Utilise /stopcompet pour la clôturer d'abord.",
                parse_mode=ParseMode.HTML
            )
            return

        # Créer la compétition
        now = datetime.utcnow()
        ends_at = now + timedelta(days=3)

        await session.execute(text("ALTER TABLE competitions ADD COLUMN IF NOT EXISTS prize_coins_2 BIGINT DEFAULT 0"))
        await session.execute(text("ALTER TABLE competitions ADD COLUMN IF NOT EXISTS prize_coins_3 BIGINT DEFAULT 0"))
        await session.execute(text("ALTER TABLE competitions ADD COLUMN IF NOT EXISTS prize_rep_1 FLOAT DEFAULT 0.5"))
        await session.execute(text("ALTER TABLE competitions ADD COLUMN IF NOT EXISTS prize_rep_2 FLOAT DEFAULT 0.3"))
        await session.execute(text("ALTER TABLE competitions ADD COLUMN IF NOT EXISTS prize_rep_3 FLOAT DEFAULT 0.2"))

        result = await session.execute(text("""
            INSERT INTO competitions (started_at, ends_at, prize_coins, prize_coins_2, prize_coins_3, prize_rep_1, prize_rep_2, prize_rep_3, status)
            VALUES (:now, :ends, :p1, :p2, :p3, :r1, :r2, :r3, 'active')
            RETURNING id
        """), {"now": now, "ends": ends_at, "p1": prizes_coins[0], "p2": prizes_coins[1], "p3": prizes_coins[2], "r1": prizes_rep[0], "r2": prizes_rep[1], "r3": prizes_rep[2]})
        compet_id = result.scalar()

        # Snapshot de toutes les entreprises actives
        companies = (await session.execute(
            select(Company).where(
                Company.is_active == True,
                Company.is_bot_company == False,
            )
        )).scalars().all()

        count = 0
        for company in companies:
            total = (await session.execute(
                select(func.sum(CompanyEmployee.command_count)).where(
                    CompanyEmployee.company_id == company.id,
                    CompanyEmployee.left_at == None,
                )
            )).scalar() or 0

            await session.execute(text("""
                INSERT INTO competition_snapshots (compet_id, company_id, cmds_at_start)
                VALUES (:cid, :company_id, :cmds)
            """), {"cid": compet_id, "company_id": company.id, "cmds": total})
            count += 1

        await session.commit()

    ends_str = ends_at.strftime("%d/%m/%Y à %H:%M UTC")
    await update.message.reply_text(
        f"🏆 <b>COMPÉTITION INTER-ENTREPRISES LANCÉE !</b>\n\n"
        f"⚔️ <b>{count} entreprises</b> participent\n"
        f"📅 Durée : <b>3 jours</b> → fin le <b>{ends_str}</b>\n"
        f"🎯 Critère : <b>nombre de commandes des employés</b>\n\n"
        f"🥇 1ère place : <b>{_fmt(prizes_coins[0])} {CURRENCY}</b> + <b>+{prizes_rep[0]}⭐</b>\n"
        f"🥈 2ème place : <b>{_fmt(prizes_coins[1])} {CURRENCY}</b> + <b>+{prizes_rep[1]}⭐</b>\n"
        f"🥉 3ème place : <b>{_fmt(prizes_coins[2])} {CURRENCY}</b> + <b>+{prizes_rep[2]}⭐</b>\n\n"
        f"💪 Mobilisez vos équipes ! /compet pour suivre le classement.",
        parse_mode=ParseMode.HTML
    )


# ─── /compet ──────────────────────────────────────────────────────────────────

async def compet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncSessionLocal() as session:
        await _ensure_compet_table(session)
        compet = await _get_active_compet(session)

        if not compet:
            await update.message.reply_text(
                "😴 Aucune compétition en cours.\n"
                "Les admins peuvent en lancer une avec /startcompet."
            )
            return

        standings = await _get_standings(session, compet["id"])
        ends_at = compet["ends_at"]
        prizes_coins = [compet.get("prize_coins",50_000_000), compet.get("prize_coins_2",25_000_000), compet.get("prize_coins_3",12_500_000)]
        prizes_rep = [compet.get("prize_rep_1",0.5), compet.get("prize_rep_2",0.3), compet.get("prize_rep_3",0.2)]

        now = datetime.utcnow()
        remaining = ends_at - now
        if remaining.total_seconds() > 0:
            h = int(remaining.total_seconds() // 3600)
            m = int((remaining.total_seconds() % 3600) // 60)
            time_str = f"⏳ Fin dans <b>{h}h {m:02d}min</b>"
        else:
            time_str = "⏰ <b>Temps écoulé — en attente de clôture</b>"

        medals = ["🥇", "🥈", "🥉"]

        lines = [
            f"🏆 <b>COMPÉTITION INTER-ENTREPRISES</b>",
            f"{time_str}",
            f"━━━━━━━━━━━━━━━━━━━━━━\n",
        ]

        if not standings:
            lines.append("Aucune entreprise enregistrée.")
        else:
            top = standings[0]["cmds_done"] or 1
            for i, row in enumerate(standings[:15]):
                cmds = row["cmds_done"]
                bar_fill = int((cmds / top) * 10) if top > 0 else 0
                bar = "█" * bar_fill + "░" * (10 - bar_fill)
                medal = medals[i] if i < 3 else f"{i+1}."
                prize_str = f" → {_fmt(prizes_coins[i])} {CURRENCY} + +{prizes_rep[i]}⭐" if i < 3 else ""
                lines.append(
                    f"{medal} <b>{row['name']}</b>{prize_str}\n"
                    f"   [{bar}] <b>{_fmt(cmds)}</b> cmds\n"
                )

        lines.append(f"━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"👥 {len(standings)} entreprises en lice")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ─── /stopcompet ──────────────────────────────────────────────────────────────

async def stopcompet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await is_admin(user.id):
        await update.message.reply_text("⛔ Réservé aux admins.")
        return

    async with AsyncSessionLocal() as session:
        await _ensure_compet_table(session)
        compet = await _get_active_compet(session)

        if not compet:
            await update.message.reply_text("❌ Aucune compétition active.")
            return

        standings = await _get_standings(session, compet["id"])
        prize = compet["prize_coins"]
        prizes_coins = [prize, prize // 2, prize // 4]
        prizes_rep   = [0.5, 0.3, 0.2]

        winners_lines = []
        medals = ["🥇", "🥈", "🥉"]

        for i, row in enumerate(standings[:3]):
            if row["cmds_done"] == 0:
                break

            company_id = row["id"]
            p_coins = prizes_coins[i]
            p_rep   = prizes_rep[i]

            # Créditer trésorerie
            await session.execute(text("""
                UPDATE companies
                SET treasury = treasury + :coins,
                    value = value + :coins,
                    reputation = LEAST(5.0, COALESCE(reputation, 3.0) + :rep)
                WHERE id = :cid
            """), {"coins": p_coins, "rep": p_rep, "cid": company_id})

            winners_lines.append(
                f"{medals[i]} <b>{row['name']}</b>\n"
                f"   {_fmt(row['cmds_done'])} cmds · +{_fmt(p_coins)} {CURRENCY} · +{p_rep}⭐\n"
            )

        # Clôturer
        await session.execute(text("""
            UPDATE competitions SET status = 'finished' WHERE id = :cid
        """), {"cid": compet["id"]})
        await session.commit()

    if not winners_lines:
        await update.message.reply_text(
            "🏁 Compétition clôturée — aucune équipe n'a marqué de points.",
            parse_mode=ParseMode.HTML
        )
        return

    await update.message.reply_text(
        f"🏁 <b>COMPÉTITION TERMINÉE !</b>\n\n"
        f"🏆 <b>Palmarès :</b>\n\n"
        + "\n".join(winners_lines) +
        f"\nLes récompenses ont été versées en trésorerie. 🎉",
        parse_mode=ParseMode.HTML
    )


# ─── Job auto-clôture après 72h ───────────────────────────────────────────────

async def compet_autoclose_job(context: ContextTypes.DEFAULT_TYPE):
    """Clôture automatique si la compétition a expiré."""
    async with AsyncSessionLocal() as session:
        try:
            await _ensure_compet_table(session)
        except Exception:
            return

        compet = await _get_active_compet(session)
        if not compet:
            return
        if datetime.utcnow() < compet["ends_at"]:
            return

        standings = await _get_standings(session, compet["id"])
        prizes_coins = [compet.get("prize_coins",50_000_000), compet.get("prize_coins_2",25_000_000), compet.get("prize_coins_3",12_500_000)]
        prizes_rep = [compet.get("prize_rep_1",0.5), compet.get("prize_rep_2",0.3), compet.get("prize_rep_3",0.2)]
        medals = ["🥇", "🥈", "🥉"]
        winners_lines = []

        for i, row in enumerate(standings[:3]):
            if row["cmds_done"] == 0:
                break
            await session.execute(text("""
                UPDATE companies
                SET treasury = treasury + :coins,
                    value = value + :coins,
                    reputation = LEAST(5.0, COALESCE(reputation, 3.0) + :rep)
                WHERE id = :cid
            """), {"coins": prizes_coins[i], "rep": prizes_rep[i], "cid": row["id"]})
            winners_lines.append(
                f"{medals[i]} <b>{row['name']}</b> — {_fmt(row['cmds_done'])} cmds "
                f"→ +{_fmt(prizes_coins[i])} {CURRENCY} · +{prizes_rep[i]}⭐"
            )

        await session.execute(text("""
            UPDATE competitions SET status = 'finished' WHERE id = :cid
        """), {"cid": compet["id"]})
        await session.commit()

        if winners_lines and context.bot_data.get("main_group_id"):
            try:
                await context.bot.send_message(
                    chat_id=context.bot_data["main_group_id"],
                    text=(
                        f"🏁 <b>COMPÉTITION INTER-ENTREPRISES — RÉSULTATS !</b>\n\n"
                        + "\n".join(winners_lines) +
                        "\n\n🎉 Récompenses versées en trésorerie !"
                    ),
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.warning(f"compet_autoclose broadcast failed: {e}")
