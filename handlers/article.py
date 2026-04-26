"""
article.py — /article @user | /article hasard
Génère un article journalistique Breaking News sur un joueur via Groq (gratuit).
"""

import os, random, aiohttp, json
from sqlalchemy import select, func, text
from telegram.constants import ParseMode
from database.db import AsyncSessionLocal, get_user, get_user_by_username
from database.models import (
    User, BankAccount, Loan, Investment, Relationship,
    RelationType, CoupleAccount
)
from utils.helpers import ensure_user, parse_target, mention
from handlers.admin import is_admin

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "llama3-8b-8192"


def _fmt(n) -> str:
    try:
        n = int(n)
        if n >= 1_000_000_000: return f"{n/1_000_000_000:.2f} milliards"
        if n >= 1_000_000:     return f"{n/1_000_000:.1f} millions"
        if n >= 1_000:         return f"{n/1_000:.0f}K"
        return str(n)
    except Exception:
        return str(n)


async def _collect_player_data(user_id: int) -> dict:
    """Collecte toutes les données d'un joueur pour l'article."""
    data = {}

    async with AsyncSessionLocal() as session:
        # Infos de base
        res = await session.execute(
            select(User).where(User.user_id == user_id)
        )
        user = res.scalar_one_or_none()
        if not user:
            return {}

        data["name"]       = user.first_name
        data["username"]   = user.username or user.first_name
        data["coins"]      = user.coins
        data["profile_color"] = getattr(user, "profile_color", "inconnu")

        # Banques
        res2 = await session.execute(
            select(BankAccount).where(BankAccount.user_id == user_id)
        )
        banks = res2.scalars().all()
        data["bank_total"]   = sum(b.balance for b in banks)
        data["bank_count"]   = len(banks)
        data["bank_details"] = [{"bank": b.bank_id, "balance": b.balance} for b in banks]

        # Prêts
        res3 = await session.execute(
            text("SELECT SUM(remaining) FROM loans WHERE user_id = :uid AND status = 'active'"),
            {"uid": user_id}
        )
        row3 = res3.fetchone()
        data["loans_total"] = int(row3[0]) if row3 and row3[0] else 0

        # Portfolio
        res4 = await session.execute(
            text("""
                SELECT asset_id, SUM(quantity) as qty, AVG(buy_price) as avg_price
                FROM investments
                WHERE user_id = :uid AND status = 'active'
                GROUP BY asset_id
            """),
            {"uid": user_id}
        )
        investments = res4.fetchall()
        data["portfolio_count"] = len(investments)
        data["portfolio"]       = [
            {"asset": r[0], "qty": int(r[1]), "avg_price": int(r[2])}
            for r in investments
        ]

        # Compte couple
        res5 = await session.execute(
            select(CoupleAccount).where(
                (CoupleAccount.user1_id == user_id) | (CoupleAccount.user2_id == user_id)
            )
        )
        couple_acc = res5.scalar_one_or_none()
        data["couple_account"] = int(couple_acc.balance) if couple_acc else 0

        # Fortune totale
        data["fortune_totale"] = data["coins"] + data["bank_total"] + data["couple_account"]

        # Relations
        res6 = await session.execute(
            select(Relationship, User)
            .join(User, Relationship.related_user_id == User.user_id)
            .where(Relationship.user_id == user_id)
        )
        relations = res6.all()
        data["spouse"]   = next((u.first_name for r, u in relations if r.relation_type == RelationType.SPOUSE), None)
        data["friends"]  = [u.first_name for r, u in relations if r.relation_type == RelationType.FRIEND]
        data["children"] = []

        res7 = await session.execute(
            select(Relationship, User)
            .join(User, Relationship.user_id == User.user_id)
            .where(
                Relationship.related_user_id == user_id,
                Relationship.relation_type   == RelationType.PARENT
            )
        )
        children_rels = res7.all()
        data["children"] = [u.first_name for r, u in children_rels]

        # Classement richesse
        res8 = await session.execute(
            text("SELECT COUNT(*) FROM users WHERE coins > :c"),
            {"c": user.coins}
        )
        rank_row = res8.fetchone()
        data["rank"] = int(rank_row[0]) + 1 if rank_row else "?"

        # Nombre total de joueurs
        res9 = await session.execute(text("SELECT COUNT(*) FROM users"))
        data["total_players"] = int(res9.fetchone()[0])

    return data


def _build_prompt(data: dict) -> str:
    famille = []
    if data.get("spouse"):
        famille.append(f"marié(e) à {data['spouse']}")
    if data.get("children"):
        famille.append(f"{len(data['children'])} enfant(s) : {', '.join(data['children'])}")
    if data.get("friends"):
        famille.append(f"{len(data['friends'])} ami(s) proche(s)")

    portfolio_desc = ""
    if data.get("portfolio"):
        assets = ", ".join(f"{p['asset']} ({p['qty']} unités)" for p in data["portfolio"][:5])
        portfolio_desc = f"Portfolio de {data['portfolio_count']} actif(s) : {assets}."
    else:
        portfolio_desc = "Aucun investissement en bourse."

    banques_desc = ""
    if data.get("bank_details"):
        banques_desc = ", ".join(
            f"{b['bank']} ({_fmt(b['balance'])} coins)"
            for b in data["bank_details"][:3]
        )
    else:
        banques_desc = "Aucun compte bancaire"

    prompt = f"""Tu es un journaliste de télévision Breaking News dans un jeu Telegram économique fictif.
Écris un article de journal télévisé DRAMATIQUE et stylé en français sur ce joueur.
L'article doit faire entre 200 et 280 mots. Commence directement par le titre sans introduction.
Utilise des emojis. Sois dramatique, sensationnaliste, comme un vrai journal TV.
Mentionne sa fortune, ses banques, son rang, sa famille si pertinent.
Ne mets pas de balises HTML. Commence par un titre accrocheur en majuscules.

DONNÉES DU JOUEUR :
- Nom : {data['name']} (@{data['username']})
- Coins en poche : {_fmt(data['coins'])} coins
- Total banques : {_fmt(data['bank_total'])} coins ({data['bank_count']} compte(s)) — {banques_desc}
- Compte commun couple : {_fmt(data['couple_account'])} coins
- Dettes actives : {_fmt(data['loans_total'])} coins
- FORTUNE TOTALE : {_fmt(data['fortune_totale'])} coins
- Rang dans le classement : #{data['rank']} sur {data['total_players']} joueurs
- {portfolio_desc}
- Situation familiale : {', '.join(famille) if famille else 'célibataire, sans famille connue'}

Génère un article Breaking News percutant sur ce personnage."""
    return prompt


async def _call_groq(prompt: str) -> str:
    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        return "❌ GROQ_API_KEY non configurée dans les variables d'environnement Render."

    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 500,
        "temperature": 0.9,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            GROQ_API_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                text_err = await resp.text()
                return f"❌ Erreur Groq ({resp.status}) : {text_err[:200]}"
            result = await resp.json()
            return result["choices"][0]["message"]["content"].strip()


async def article_cmd(update, context):
    """/article @user | /article hasard"""
    user = update.effective_user

    if not await is_admin(user.id):
        return await update.message.reply_text("❌ Commande réservée aux admins.")

    args = context.args or []

    # Choisir la cible
    target_id   = None
    target_name = None

    if not args:
        return await update.message.reply_text(
            "📰 <b>Système Article</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Usage :\n"
            "• <code>/article @pseudo</code> — article sur un joueur\n"
            "• <code>/article hasard</code> — joueur aléatoire parmi les plus riches",
            parse_mode=ParseMode.HTML
        )

    if args[0].lower() == "hasard":
        async with AsyncSessionLocal() as session:
            res = await session.execute(
                text("SELECT user_id, first_name FROM users ORDER BY coins DESC LIMIT 20")
            )
            rows = res.fetchall()
        if not rows:
            return await update.message.reply_text("❌ Aucun joueur trouvé.")
        chosen    = random.choice(rows)
        target_id = chosen[0]
        target_name = chosen[1]
    else:
        # Mention ou username
        tg_user = await parse_target(update, context)
        if not tg_user:
            return await update.message.reply_text("❌ Joueur introuvable.")
        target_id   = tg_user.id
        target_name = tg_user.first_name

    # Message d'attente
    wait_msgs = [
        "📡 Nos reporters enquêtent sur le terrain...",
        "🎙️ La rédaction prépare l'article...",
        "📰 Collecte des données financières en cours...",
        "🔍 Investigation en cours sur la fortune de la cible...",
    ]
    msg = await update.message.reply_text(random.choice(wait_msgs))

    # Collecter les données
    data = await _collect_player_data(target_id)
    if not data:
        await msg.edit_text("❌ Ce joueur n'a pas de profil en base de données.")
        return

    # Générer l'article via Groq
    prompt  = _build_prompt(data)
    article = await _call_groq(prompt)

    # Formater le message final
    jours = ["Lundi","Mardi","Mercredi","Jeudi","Vendredi","Samedi","Dimanche"]
    mois  = ["janvier","février","mars","avril","mai","juin",
              "juillet","août","septembre","octobre","novembre","décembre"]
    from datetime import date
    today    = date.today()
    date_str = f"{jours[today.weekday()]} {today.day} {mois[today.month-1]} {today.year}"

    final = (
        f"📺 <b>BREAKING NEWS — ÉDITION SPÉCIALE</b>\n"
        f"🗓️ <i>{date_str}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{article}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>📡 Reportage exclusif — Rédaction FarmBot News</i>"
    )

    try:
        await msg.edit_text(final, parse_mode=ParseMode.HTML)
    except Exception:
        # Si trop long, couper
        await msg.edit_text(final[:4000] + "...", parse_mode=ParseMode.HTML)
