"""
journal.py — Journal Breaking News quotidien à 19h00
Enregistre les événements de la journée et poste un résumé stylé dans tous les groupes.
"""

from __future__ import annotations
import random
from datetime import datetime, date, timedelta
from sqlalchemy import text
from telegram.constants import ParseMode
from database.db import AsyncSessionLocal
from database.models import GroupSettings, User
from sqlalchemy import select

# ─── ENREGISTREMENT D'ÉVÉNEMENTS ─────────────────────────────────────────────

async def init_journal_table():
    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS journal_events (
                id         SERIAL PRIMARY KEY,
                event_type VARCHAR(40)  NOT NULL,
                data       TEXT         NOT NULL,
                created_at TIMESTAMP    DEFAULT NOW()
            )
        """))
        await session.commit()


async def log_event(event_type: str, **kwargs):
    """Enregistre un événement dans le journal. Appelé depuis les autres handlers."""
    import json
    data = json.dumps(kwargs, ensure_ascii=False)
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("INSERT INTO journal_events (event_type, data) VALUES (:t, :d)"),
            {"t": event_type, "d": data}
        )
        await session.commit()


# ─── COMPILATION DU JOURNAL ──────────────────────────────────────────────────

def _fmt(n) -> str:
    try:
        n = int(n)
        if n >= 1_000_000_000: return f"{n/1_000_000_000:.1f}B"
        if n >= 1_000_000:     return f"{n/1_000_000:.1f}M"
        if n >= 1_000:         return f"{n/1_000:.0f}K"
        return str(n)
    except Exception:
        return str(n)


BREAKING_INTROS = [
    "🔴 EN DIRECT",
    "⚡ FLASH INFO",
    "📡 BREAKING NEWS",
    "🚨 ALERTE INFO",
    "📺 ÉDITION SPÉCIALE",
]

OUTROS = [
    "📺 <i>Restez connectés pour la suite des événements.</i>",
    "🎙️ <i>Notre équipe reste mobilisée. Bonne soirée.</i>",
    "📡 <i>Retrouvez-nous demain pour un nouveau bulletin.</i>",
    "🔴 <i>La rédaction vous souhaite une bonne soirée.</i>",
    "🎬 <i>C'est tout pour aujourd'hui. À demain !</i>",
]

EVENT_TEMPLATES = {
    "marriage":     ("💍", "{a} et {b} se sont <b>mariés</b> aujourd'hui ! La rédaction leur souhaite beaucoup de bonheur."),
    "divorce":      ("💔", "<b>{a}</b> et <b>{b}</b> ont divorcé. Une page se tourne..."),
    "adoption":     ("👶", "<b>{a}</b> a officiellement adopté <b>{b}</b>. Bienvenue dans la famille !"),
    "disown":       ("🚪", "<b>{a}</b> a désavoué <b>{b}</b>. La famille, c'est compliqué."),
    "drame_scandale": ("💋", "SCANDALE — <b>{victim}</b> a perdu <b>{amount}</b> coins suite à une affaire explosive !"),
    "drame_fisc":   ("🏛️", "FISC — <b>{victim}</b> a été taxé de <b>{amount}</b> coins. L'État reprend ses droits."),
    "drame_catastrophe": ("🌊", "CATASTROPHE — Le portefeuille de <b>{victim}</b> a été dévasté. {nb} position(s) anéanties."),
    "drame_crise":  ("📉", "CRISE — <b>{victim}</b> a tout perdu ou presque : <b>{amount}</b> coins partis en fumée."),
    "rob_success":  ("🔫", "CRIME — <b>{robber}</b> a volé <b>{amount}</b> coins à <b>{victim}</b> ! La police enquête."),
    "prison":       ("⛓️", "<b>{user}</b> a été arrêté et envoyé en prison. Durée : {duration} min."),
    "casino_big_win": ("🎰", "CASINO — <b>{user}</b> a décroché la mise : <b>{amount}</b> coins gagnés !"),
    "lottery_win":  ("🎟️", "LOTERIE — <b>{winner}</b> remporte le jackpot de <b>{amount}</b> coins !"),
    "richlist_1":   ("👑", "AU SOMMET — <b>{user}</b> trône en tête du classement avec <b>{amount}</b> coins."),
    "big_transfer": ("💸", "TRANSFERT — <b>{from_user}</b> a envoyé <b>{amount}</b> coins à <b>{to_user}</b>. Généreux !"),
    "heist_success":("🏦", "BRAQUAGE — <b>{leader}</b> a mené un braquage réussi ! Butin : <b>{amount}</b> coins."),
    "auction_win":  ("🔨", "ENCHÈRES — <b>{winner}</b> a remporté <b>{item}</b> pour <b>{amount}</b> coins !"),
}


async def _fetch_today_events() -> list[dict]:
    import json
    async with AsyncSessionLocal() as session:
        res = await session.execute(text("""
            SELECT event_type, data, created_at
            FROM journal_events
            WHERE created_at >= NOW() - INTERVAL '24 hours'
            ORDER BY created_at DESC
        """))
        rows = res.fetchall()

    events = []
    for row in rows:
        try:
            d = json.loads(row[1])
            events.append({"type": row[0], "data": d, "at": row[2]})
        except Exception:
            pass
    return events


async def _fetch_stats() -> dict:
    """Stats du jour : top riche, plus gros vol, etc."""
    async with AsyncSessionLocal() as session:
        # Top 1 richlist
        res = await session.execute(text("""
            SELECT first_name, coins FROM users ORDER BY coins DESC LIMIT 1
        """))
        top = res.fetchone()

        # Nombre de joueurs actifs aujourd'hui (qui ont des coins > 0)
        res2 = await session.execute(text("""
            SELECT COUNT(*) FROM users WHERE coins > 0
        """))
        nb_players = res2.fetchone()[0]

    return {
        "top_name":   top[0] if top else "?",
        "top_coins":  top[1] if top else 0,
        "nb_players": nb_players,
    }


def _build_journal(events: list[dict], stats: dict, today: date) -> str:
    jours = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
    mois  = ["janvier","février","mars","avril","mai","juin",
              "juillet","août","septembre","octobre","novembre","décembre"]
    jour_str = f"{jours[today.weekday()]} {today.day} {mois[today.month-1]} {today.year}"

    intro  = random.choice(BREAKING_INTROS)
    outro  = random.choice(OUTROS)

    lines = [
        f"{intro} — <b>📰 LE JOURNAL DU JOUR</b>",
        f"🗓️ <i>{jour_str} | 19h00</i>",
        "━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    # Événements du jour
    seen_types = {}
    news_lines = []
    for ev in events:
        t = ev["type"]
        d = ev["data"]
        if t not in EVENT_TEMPLATES:
            continue
        # Max 2 événements par type pour éviter le spam
        seen_types[t] = seen_types.get(t, 0) + 1
        if seen_types[t] > 2:
            continue
        emoji, template = EVENT_TEMPLATES[t]
        try:
            text_line = f"{emoji} {template.format(**d)}"
            news_lines.append(text_line)
        except KeyError:
            pass

    if news_lines:
        lines += news_lines
    else:
        lines.append("📭 <i>Journée calme... Aucun événement majeur à signaler.</i>")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━")

    # Stats du jour
    lines.append(
        f"👑 <b>Leader actuel :</b> {stats['top_name']} — {_fmt(stats['top_coins'])} coins\n"
        f"👥 <b>Joueurs actifs :</b> {stats['nb_players']}\n"
        f"📊 <b>Événements aujourd'hui :</b> {len(events)}"
    )
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(outro)

    return "\n".join(lines)


# ─── JOB QUOTIDIEN ───────────────────────────────────────────────────────────

async def post_daily_journal(context):
    """Job déclenché à 19h00 — poste le journal dans tous les groupes."""
    events = await _fetch_today_events()
    stats  = await _fetch_stats()
    today  = date.today()
    msg    = _build_journal(events, stats, today)

    async with AsyncSessionLocal() as session:
        res    = await session.execute(select(GroupSettings))
        groups = res.scalars().all()

    sent, failed = 0, 0
    for g in groups:
        try:
            await context.bot.send_message(
                chat_id=g.group_id,
                text=msg,
                parse_mode=ParseMode.HTML,
            )
            sent += 1
        except Exception:
            failed += 1

    # Purge les events de plus de 48h
    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            DELETE FROM journal_events WHERE created_at < NOW() - INTERVAL '48 hours'
        """))
        await session.commit()


def setup_journal_jobs(app):
    from datetime import time as dtime
    app.job_queue.run_daily(
        post_daily_journal,
        time=dtime(hour=19, minute=0),
        name="daily_journal",
    )


# ─── /testjournal (admin) ─────────────────────────────────────────────────────

async def testjournal_cmd(update, context):
    """Force l'envoi du journal maintenant — commande admin."""
    from handlers.admin import is_admin
    if not await is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Réservé aux admins.")

    await update.message.reply_text("📡 Envoi du journal en cours...")
    await post_daily_journal(context)
    await update.message.reply_text("✅ Journal envoyé dans tous les groupes.")
