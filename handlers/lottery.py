"""
journal.py — Journal Breaking News quotidien à 19h00
L'IA Gemini collecte toute l'activité du jour (events loggés + stats DB)
et rédige un vrai article journalistique. Fallback template si l'IA échoue.
"""

from __future__ import annotations
import os, json, random, aiohttp
from datetime import datetime, date, timedelta
from sqlalchemy import text, select
from telegram.constants import ParseMode
from database.db import AsyncSessionLocal
from database.models import GroupSettings
from config import CURRENCY

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"


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
    data = json.dumps(kwargs, ensure_ascii=False)
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("INSERT INTO journal_events (event_type, data) VALUES (:t, :d)"),
            {"t": event_type, "d": data}
        )
        await session.commit()


# ─── COLLECTE DES DONNÉES ────────────────────────────────────────────────────

def _fmt(n) -> str:
    try:
        n = int(n)
        if n >= 1_000_000_000: return f"{n/1_000_000_000:.1f}B {CURRENCY}"
        if n >= 1_000_000:     return f"{n/1_000_000:.1f}M {CURRENCY}"
        if n >= 1_000:         return f"{n/1_000:.0f}K {CURRENCY}"
        return f"{n} {CURRENCY}"
    except Exception:
        return str(n)


async def _fetch_today_events() -> list[dict]:
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
            events.append({"type": row[0], "data": d, "at": str(row[2])})
        except Exception:
            pass
    return events


async def _fetch_rich_stats() -> dict:
    """Collecte les stats globales du bot pour nourrir l'IA."""
    stats = {}
    async with AsyncSessionLocal() as session:

        # Top 5 richest
        res = await session.execute(text(
            "SELECT first_name, coins FROM users ORDER BY coins DESC LIMIT 5"
        ))
        stats["top_rich"] = [{"name": r[0], "coins": r[1]} for r in res.fetchall()]

        # Nombre total joueurs
        res = await session.execute(text("SELECT COUNT(*) FROM users"))
        stats["total_players"] = res.fetchone()[0]

        # Joueurs avec prêts actifs
        res = await session.execute(text(
            "SELECT COUNT(DISTINCT user_id) FROM loans WHERE status='active'"
        ))
        stats["players_in_debt"] = res.fetchone()[0]

        # Volume de coins total en circulation
        res = await session.execute(text("SELECT SUM(coins) FROM users WHERE coins > 0"))
        row = res.fetchone()
        stats["total_coins_circulation"] = int(row[0]) if row and row[0] else 0

        # Top entreprises par valeur
        try:
            res = await session.execute(text("""
                SELECT c.name, c.sector, u.first_name, c.value, c.treasury, c.level
                FROM companies c
                JOIN users u ON c.owner_id = u.user_id
                WHERE c.is_active = TRUE
                ORDER BY c.value DESC LIMIT 5
            """))
            stats["top_companies"] = [
                {"name": r[0], "sector": r[1], "owner": r[2],
                 "value": r[3], "treasury": r[4], "level": r[5]}
                for r in res.fetchall()
            ]
        except Exception:
            stats["top_companies"] = []

        # Nombre total d'entreprises actives
        try:
            res = await session.execute(text(
                "SELECT COUNT(*) FROM companies WHERE is_active=TRUE AND is_bot_company=FALSE"
            ))
            stats["nb_companies"] = res.fetchone()[0]
        except Exception:
            stats["nb_companies"] = 0

        # Nouveaux couples du jour
        try:
            res = await session.execute(text("""
                SELECT COUNT(*) FROM relationships
                WHERE relation_type='spouse'
                AND created_at >= NOW() - INTERVAL '24 hours'
            """))
            stats["new_couples_today"] = res.fetchone()[0]
        except Exception:
            stats["new_couples_today"] = 0

        # Total joueurs diplômés
        try:
            res = await session.execute(text("""
                SELECT COUNT(*) FROM users
                WHERE (diplome_bac=TRUE OR diplome_licence=TRUE
                    OR diplome_master=TRUE OR diplome_mba=TRUE)
            """))
            stats["total_diplomed"] = res.fetchone()[0]
        except Exception:
            stats["total_diplomed"] = 0

        # Karma extremes
        try:
            res = await session.execute(text(
                "SELECT first_name, karma FROM users ORDER BY karma DESC LIMIT 1"
            ))
            row = res.fetchone()
            stats["most_loved"] = {"name": row[0], "karma": row[1]} if row else None

            res = await session.execute(text(
                "SELECT first_name, karma FROM users ORDER BY karma ASC LIMIT 1"
            ))
            row = res.fetchone()
            stats["most_hated"] = {"name": row[0], "karma": row[1]} if row else None
        except Exception:
            stats["most_loved"] = None
            stats["most_hated"] = None

        # Commandes populaires du jour
        try:
            res = await session.execute(text("""
                SELECT command, COUNT(*) as cnt
                FROM activity_logs
                WHERE created_at >= NOW() - INTERVAL '24 hours'
                GROUP BY command ORDER BY cnt DESC LIMIT 5
            """))
            stats["top_commands"] = [{"cmd": r[0], "count": r[1]} for r in res.fetchall()]
        except Exception:
            stats["top_commands"] = []

        # Prêts du jour
        try:
            res = await session.execute(text("""
                SELECT SUM(amount), COUNT(*) FROM loans
                WHERE created_at >= NOW() - INTERVAL '24 hours'
            """))
            row = res.fetchone()
            stats["loans_today"] = {"total": int(row[0] or 0), "count": int(row[1] or 0)}
        except Exception:
            stats["loans_today"] = {"total": 0, "count": 0}

    return stats


# ─── GÉNÉRATION IA ────────────────────────────────────────────────────────────

EVENT_LABELS = {
    "marriage":           "MARIAGE",
    "divorce":            "DIVORCE",
    "adoption":           "ADOPTION",
    "disown":             "DESAVEU",
    "drame_scandale":     "SCANDALE FINANCIER",
    "drame_fisc":         "CONTROLE FISCAL",
    "drame_catastrophe":  "CATASTROPHE BOURSIERE",
    "drame_crise":        "CRISE MAJEURE",
    "rob_success":        "BRAQUAGE REUSSI",
    "prison":             "ARRESTATION",
    "casino_big_win":     "JACKPOT CASINO",
    "lottery_win":        "GAGNANT LOTERIE",
    "richlist_1":         "N1 CLASSEMENT",
    "big_transfer":       "TRANSFERT MASSIF",
    "heist_success":      "BRAQUAGE BANQUE",
    "auction_win":        "VENTE AUX ENCHERES",
    "company_created":    "NOUVELLE ENTREPRISE",
    "company_payroll":    "PAIE VERSEE",
    "crime_success":      "CRIME REUSSI",
}


def _build_ai_prompt(events: list[dict], stats: dict, today: date) -> tuple[str, str]:
    jours = ["Lundi","Mardi","Mercredi","Jeudi","Vendredi","Samedi","Dimanche"]
    mois  = ["janvier","fevrier","mars","avril","mai","juin",
              "juillet","aout","septembre","octobre","novembre","decembre"]
    jour_str = f"{jours[today.weekday()]} {today.day} {mois[today.month-1]} {today.year}"

    # Résumé des événements loggés
    events_summary = []
    seen = {}
    for ev in events[:20]:
        t = ev["type"]
        label = EVENT_LABELS.get(t, t.upper())
        seen[t] = seen.get(t, 0) + 1
        if seen[t] > 3:
            continue
        d = ev["data"]
        parts = [f"{k}={v}" for k, v in d.items()]
        events_summary.append(f"- {label}: {', '.join(parts)}")

    if not events_summary:
        events_summary = ["- Aucun evenement majeur enregistre aujourd'hui."]

    top_rich_str = "\n".join(
        f"  #{i+1}. {p['name']} - {_fmt(p['coins'])}"
        for i, p in enumerate(stats.get("top_rich", []))
    ) or "  (aucune donnee)"

    top_companies_str = "\n".join(
        f"  - {c['name']} ({c['sector']}) PDG:{c['owner']} Valeur:{_fmt(c['value'])} Tresorerie:{_fmt(c['treasury'])}"
        for c in stats.get("top_companies", [])
    ) or "  (aucune entreprise)"

    loved = stats.get("most_loved")
    hated = stats.get("most_hated")
    karma_str = ""
    if loved:
        karma_str += f"  Le plus aime: {loved['name']} (karma +{loved['karma']})\n"
    if hated:
        karma_str += f"  Le plus deteste: {hated['name']} (karma {hated['karma']})"

    top_cmds_str = ", ".join(
        f"/{c['cmd']} x{c['count']}" for c in stats.get("top_commands", [])
    ) or "(aucune donnee)"

    loans = stats.get("loans_today", {})

    system_prompt = (
        "Tu es le presentateur vedette de Family Bot News, une chaine d'info fictive dans un jeu economique Telegram. "
        "Tu rediges chaque soir le journal quotidien : un article DRAMATIQUE, PERCUTANT et DIVERTISSANT. "
        "Tu as acces aux statistiques reelles du bot et aux evenements de la journee. "
        "Ton style : journaliste sensationnaliste Breaking News, avec des commentaires piquants sur les personnages. "
        "Tu nommes les joueurs par leur vrai prenom et tu exageres legerement les faits pour dramatiser. "
        "Invente des citations fictives plausibles entre joueurs. Cree des rivalites, des histoires. "
        "Format : 1) Titre accrocheur en MAJUSCULES. 2) Intro punchy 2-3 phrases. "
        "3) 3-5 paragraphes courts sur les faits saillants. 4) Chute finale memorable. "
        "INTERDIT : balises HTML. Emojis avec parcimonie. Longueur : 250 a 320 mots."
    )

    user_prompt = f"""DATE : {jour_str}

EVENEMENTS DU JOUR :
{chr(10).join(events_summary)}

CLASSEMENT FORTUNE (TOP 5) :
{top_rich_str}

ECONOMIE GLOBALE :
  Total en circulation : {_fmt(stats.get('total_coins_circulation', 0))}
  Joueurs actifs : {stats.get('total_players', '?')}
  Joueurs endettes : {stats.get('players_in_debt', 0)}
  Prets aujourd'hui : {loans.get('count', 0)} prets ({_fmt(loans.get('total', 0))})
  Nouveaux couples : {stats.get('new_couples_today', 0)}
  Joueurs diplomes : {stats.get('total_diplomed', 0)}

TOP ENTREPRISES :
{top_companies_str}
  Entreprises actives : {stats.get('nb_companies', 0)}

KARMA :
{karma_str or '  (donnees indisponibles)'}

COMMANDES POPULAIRES DU JOUR :
  {top_cmds_str}

Redige maintenant le journal du soir. Sois creatif, dramatique, memorable."""

    return system_prompt, user_prompt


async def _call_gemini(system_prompt: str, user_prompt: str) -> str | None:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return None

    full_prompt = f"{system_prompt}\n\n{user_prompt}"
    payload = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 900,
            "temperature": 1.1,
            "topP": 0.95,
        },
    }

    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f"{GEMINI_API_URL}?key={api_key}",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    return None
                result = await resp.json()
                return result["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return None


# ─── FALLBACK TEMPLATE ────────────────────────────────────────────────────────

BREAKING_INTROS = [
    "🔴 EN DIRECT", "⚡ FLASH INFO", "📡 BREAKING NEWS",
    "🚨 ALERTE INFO", "📺 ÉDITION SPÉCIALE",
]
OUTROS = [
    "📺 <i>Restez connectés pour la suite des événements.</i>",
    "🎙️ <i>Notre équipe reste mobilisée. Bonne soirée.</i>",
    "📡 <i>Retrouvez-nous demain pour un nouveau bulletin.</i>",
    "🔴 <i>La rédaction Family Bot News ❤️ vous souhaite une bonne soirée.</i>",
]
EVENT_TEMPLATES = {
    "marriage":          ("💍", "{a} et {b} se sont <b>mariés</b> aujourd'hui !"),
    "divorce":           ("💔", "<b>{a}</b> et <b>{b}</b> ont divorcé. Une page se tourne..."),
    "adoption":          ("👶", "<b>{a}</b> a officiellement adopté <b>{b}</b>."),
    "disown":            ("🚪", "<b>{a}</b> a désavoué <b>{b}</b>. La famille, c'est compliqué."),
    "drame_scandale":    ("💋", "SCANDALE — <b>{victim}</b> a perdu <b>{amount}</b> suite à une affaire explosive !"),
    "drame_fisc":        ("🏛️", "FISC — <b>{victim}</b> a été taxé de <b>{amount}</b>. L'État reprend ses droits."),
    "drame_catastrophe": ("🌊", "CATASTROPHE — Le portefeuille de <b>{victim}</b> a été dévasté. {nb} position(s) anéanties."),
    "drame_crise":       ("📉", "CRISE — <b>{victim}</b> a tout perdu ou presque : <b>{amount}</b> partis en fumée."),
    "rob_success":       ("🔫", "CRIME — <b>{robber}</b> a volé <b>{amount}</b> à <b>{victim}</b> !"),
    "prison":            ("⛓️", "<b>{user}</b> a été arrêté. Durée : {duration} min."),
    "casino_big_win":    ("🎰", "CASINO — <b>{user}</b> a décroché la mise : <b>{amount}</b> gagnés !"),
    "lottery_win":       ("🎟️", "LOTERIE — <b>{winner}</b> remporte le jackpot de <b>{amount}</b> !"),
    "richlist_1":        ("👑", "AU SOMMET — <b>{user}</b> trône en tête avec <b>{amount}</b>."),
    "big_transfer":      ("💸", "TRANSFERT — <b>{from_user}</b> a envoyé <b>{amount}</b> à <b>{to_user}</b>."),
    "heist_success":     ("🏦", "BRAQUAGE — <b>{leader}</b> a mené un braquage réussi ! Butin : <b>{amount}</b>."),
    "auction_win":       ("🔨", "ENCHÈRES — <b>{winner}</b> a remporté <b>{item}</b> pour <b>{amount}</b> !"),
    "company_created":   ("🏢", "NOUVEAU — <b>{owner}</b> a fondé <b>{name}</b> dans le secteur {sector}."),
    "company_payroll":   ("💵", "PAIE — <b>{pdg}</b> a versé les salaires chez <b>{company}</b>."),
}


def _build_fallback(events: list[dict], stats: dict, today: date) -> str:
    jours = ["Lundi","Mardi","Mercredi","Jeudi","Vendredi","Samedi","Dimanche"]
    mois  = ["janvier","février","mars","avril","mai","juin",
              "juillet","août","septembre","octobre","novembre","décembre"]
    jour_str = f"{jours[today.weekday()]} {today.day} {mois[today.month-1]} {today.year}"

    intro = random.choice(BREAKING_INTROS)
    outro = random.choice(OUTROS)

    lines = [
        f"{intro} — <b>📰 LE JOURNAL DU JOUR</b>",
        f"🗓️ <i>{jour_str} | 19h00</i>",
        "━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    seen_types = {}
    news_lines = []
    for ev in events:
        t = ev["type"]
        d = ev["data"]
        if t not in EVENT_TEMPLATES:
            continue
        seen_types[t] = seen_types.get(t, 0) + 1
        if seen_types[t] > 2:
            continue
        emoji, template = EVENT_TEMPLATES[t]
        try:
            news_lines.append(f"{emoji} {template.format(**d)}")
        except KeyError:
            pass

    if news_lines:
        lines += news_lines
    else:
        lines.append("📭 <i>Journée calme... Aucun événement majeur à signaler.</i>")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━")

    top = stats.get("top_rich", [])
    leader = top[0] if top else {"name": "?", "coins": 0}
    lines.append(
        f"👑 <b>Leader :</b> {leader['name']} — {_fmt(leader['coins'])}\n"
        f"👥 <b>Joueurs :</b> {stats.get('total_players', '?')}\n"
        f"🏢 <b>Entreprises :</b> {stats.get('nb_companies', 0)}\n"
        f"📊 <b>Événements :</b> {len(events)}"
    )
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(outro)
    lines.append("<i>📡 Family Bot News ❤️ — Votre source exclusive</i>")
    return "\n".join(lines)


# ─── JOB QUOTIDIEN ───────────────────────────────────────────────────────────

async def post_daily_journal(context):
    """Job déclenché à 19h00 — poste le journal dans tous les groupes."""
    import html as html_mod

    events = await _fetch_today_events()
    stats  = await _fetch_rich_stats()
    today  = date.today()

    jours = ["Lundi","Mardi","Mercredi","Jeudi","Vendredi","Samedi","Dimanche"]
    mois  = ["janvier","février","mars","avril","mai","juin",
              "juillet","août","septembre","octobre","novembre","décembre"]
    jour_str = f"{jours[today.weekday()]} {today.day} {mois[today.month-1]} {today.year}"

    # Tentative IA
    sys_p, usr_p = _build_ai_prompt(events, stats, today)
    ai_text = await _call_gemini(sys_p, usr_p)

    if ai_text:
        article_escaped = html_mod.escape(ai_text)
        msg = (
            f"📺 <b>BREAKING NEWS — JOURNAL DU SOIR</b>\n"
            f"🗓️ <i>{jour_str} | 19h00</i>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{article_escaped}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>📡 Family Bot News ❤️ — Propulsé par l'IA Gemini</i>"
        )
    else:
        msg = _build_fallback(events, stats, today)

    if len(msg) > 4000:
        msg = msg[:3960] + "...\n\n<i>📡 Family Bot News ❤️</i>"

    async with AsyncSessionLocal() as session:
        res    = await session.execute(select(GroupSettings))
        groups = res.scalars().all()

    for g in groups:
        try:
            await context.bot.send_message(
                chat_id=g.group_id,
                text=msg,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    # Purge events de plus de 48h
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

    await update.message.reply_text("📡 Génération du journal IA en cours...")
    await post_daily_journal(context)
    await update.message.reply_text("✅ Journal envoyé dans tous les groupes.")
