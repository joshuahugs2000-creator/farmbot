"""
article.py — /article @user | /article hasard
Génère un article journalistique Breaking News sur un joueur via Groq.
"""

import os, random, aiohttp, json, html
from sqlalchemy import select, func, text
from telegram.constants import ParseMode
from database.db import AsyncSessionLocal, get_user, get_user_by_username
from database.models import (
    User, BankAccount, Loan, Investment, Relationship,
    RelationType, CoupleAccount
)
from utils.helpers import ensure_user, parse_target, mention
from handlers.admin import is_admin
from config import CURRENCY

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"


def _fmt(n) -> str:
    try:
        n = int(n)
        if n >= 1_000_000_000: return f"{n/1_000_000_000:.2f} milliards"
        if n >= 1_000_000:     return f"{n/1_000_000:.1f} millions"
        if n >= 1_000:         return f"{n/1_000:.0f}K"
        return str(n)
    except Exception:
        return str(n)


def _fortune_label(fortune: int) -> str:
    if fortune >= 10_000_000: return "MÉGA-MILLIARDAIRE LÉGENDAIRE"
    if fortune >= 1_000_000:  return "MILLIONNAIRE INFLUENT"
    if fortune >= 500_000:    return "NOTABLE FORTUNÉ"
    if fortune >= 100_000:    return "BOURGEOIS MONTANT"
    if fortune >= 10_000:     return "CITOYEN ORDINAIRE"
    return "QUIDAM DÉSARGENTÉ"


async def _collect_player_data(user_id: int) -> dict:
    data = {}
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.user_id == user_id))
        user = res.scalar_one_or_none()
        if not user:
            return {}

        data["name"]          = user.first_name
        data["username"]      = user.username or user.first_name
        data["coins"]         = user.coins
        data["karma"]         = getattr(user, "karma", 0)
        data["profile_color"] = getattr(user, "profile_color", "inconnu")

        diplomes = []
        if getattr(user, "diplome_bac",     False): diplomes.append("Bac")
        if getattr(user, "diplome_licence", False): diplomes.append("Licence")
        if getattr(user, "diplome_master",  False): diplomes.append("Master")
        if getattr(user, "diplome_mba",     False): diplomes.append("MBA")
        data["diplomes"]       = diplomes
        data["diplome_domain"] = getattr(user, "diplome_domain", None)

        res2 = await session.execute(select(BankAccount).where(BankAccount.user_id == user_id))
        banks = res2.scalars().all()
        data["bank_total"]   = sum(b.balance for b in banks)
        data["bank_count"]   = len(banks)
        data["bank_details"] = [{"bank": b.bank_id, "balance": b.balance} for b in banks]

        res3 = await session.execute(
            text("SELECT SUM(remaining) FROM loans WHERE user_id = :uid AND status = 'active'"),
            {"uid": user_id}
        )
        row3 = res3.fetchone()
        data["loans_total"] = int(row3[0]) if row3 and row3[0] else 0

        res4 = await session.execute(
            text("""
                SELECT asset_id, SUM(quantity) as qty, AVG(buy_price) as avg_price
                FROM investments WHERE user_id = :uid AND status = 'active'
                GROUP BY asset_id
            """),
            {"uid": user_id}
        )
        investments = res4.fetchall()
        data["portfolio_count"] = len(investments)
        data["portfolio"] = [{"asset": r[0], "qty": int(r[1]), "avg_price": int(r[2])} for r in investments]

        res5 = await session.execute(
            select(CoupleAccount).where(
                (CoupleAccount.user1_id == user_id) | (CoupleAccount.user2_id == user_id)
            )
        )
        couple_acc = res5.scalar_one_or_none()
        data["couple_account"] = int(couple_acc.balance) if couple_acc else 0
        data["fortune_totale"] = data["coins"] + data["bank_total"] + data["couple_account"]
        data["fortune_label"]  = _fortune_label(data["fortune_totale"])

        res6 = await session.execute(
            select(Relationship, User)
            .join(User, Relationship.related_user_id == User.user_id)
            .where(Relationship.user_id == user_id)
        )
        relations = res6.all()
        data["spouse"]  = next((u.first_name for r, u in relations if r.relation_type == RelationType.SPOUSE), None)
        data["friends"] = [u.first_name for r, u in relations if r.relation_type == RelationType.FRIEND]

        res7 = await session.execute(
            select(Relationship, User)
            .join(User, Relationship.user_id == User.user_id)
            .where(
                Relationship.related_user_id == user_id,
                Relationship.relation_type   == RelationType.PARENT
            )
        )
        data["children"] = [u.first_name for r, u in res7.all()]

        res8 = await session.execute(text("SELECT COUNT(*) FROM users WHERE coins > :c"), {"c": user.coins})
        rank_row = res8.fetchone()
        data["rank"] = int(rank_row[0]) + 1 if rank_row else "?"

        res9 = await session.execute(text("SELECT COUNT(*) FROM users"))
        data["total_players"] = int(res9.fetchone()[0])

        try:
            res_emp = await session.execute(
                text("""
                    SELECT c.name, ce.role FROM company_employees ce
                    JOIN companies c ON ce.company_id = c.id
                    WHERE ce.user_id = :uid AND ce.left_at IS NULL LIMIT 1
                """),
                {"uid": user_id}
            )
            emp_row = res_emp.fetchone()
            data["company"]      = emp_row[0] if emp_row else None
            data["company_role"] = emp_row[1] if emp_row else None
        except Exception:
            data["company"]      = None
            data["company_role"] = None

    return data


ANGLES = [
    "enquête exclusive sur une fortune mystérieuse qui fait trembler les marchés",
    "révélations chocs : les secrets financiers d'un personnage controversé",
    "portrait sans filtre — ascension fulgurante ou chute imminente ?",
    "les dessous d'un empire bâti dans l'ombre du groupe",
    "dossier confidentiel : qui est vraiment ce joueur hors du commun ?",
    "fortune, famille, pouvoir — le portrait d'une figure incontournable",
    "scandale ou génie ? la vérité sur ce personnage qui divise",
    "dans les coulisses d'une réussite qui laisse tout le monde bouche bée",
]

TONS = [
    "sensationnaliste et dramatique, style TMZ en feu",
    "grave et solennel comme le journal de 20h un soir de crise nationale",
    "sarcastique et mordant, style talk-show de fin de soirée",
    "épique et héroïque comme un documentaire Netflix de prestige",
    "paranoïaque et conspirationniste, style chaîne d'info en continu à 3h du matin",
]


def _build_prompt(data: dict) -> tuple:
    famille_parts = []
    if data.get("spouse"):
        famille_parts.append(f"en couple avec {data['spouse']}")
    if data.get("children"):
        famille_parts.append(f"parent de {len(data['children'])} enfant(s) : {', '.join(data['children'])}")
    if data.get("friends"):
        famille_parts.append(f"allié(e) à {', '.join(data['friends'][:3])}")
    famille_str = " · ".join(famille_parts) if famille_parts else "célibataire, sans famille connue"

    portfolio_str = "aucun investissement boursier"
    if data.get("portfolio"):
        assets = ", ".join(f"{p['asset']} ×{p['qty']}" for p in data["portfolio"][:4])
        portfolio_str = f"{data['portfolio_count']} actif(s) : {assets}"

    banques_str = "aucun compte bancaire"
    if data.get("bank_details"):
        banques_str = " / ".join(f"{b['bank']} : {_fmt(b['balance'])} {CURRENCY}" for b in data["bank_details"][:3])

    diplomes_str = "autodidacte sans diplôme"
    if data["diplomes"]:
        diplomes_str = ", ".join(data["diplomes"])
        if data.get("diplome_domain"):
            diplomes_str += f" en {data['diplome_domain']}"

    entreprise_str = "sans emploi déclaré"
    if data.get("company"):
        entreprise_str = f"{data['company_role']} chez {data['company']}"

    karma = data.get("karma", 0)
    karma_str   = f"+{karma}" if karma >= 0 else str(karma)
    karma_label = "SAINT LOCAL" if karma > 30 else ("PARIA DÉTESTÉ" if karma < -10 else "CITOYEN LAMBDA")

    angle = random.choice(ANGLES)
    ton   = random.choice(TONS)

    system_prompt = (
        "Tu es le présentateur vedette d'une chaîne d'info fictive dans un jeu Telegram économique. "
        "Tu rédiges des articles Breaking News dramatiques, excessifs, divertissants et originaux. "
        "Chaque article doit sembler unique — varie les formulations, les révélations, le rythme. "
        "Tu écris UNIQUEMENT le texte de l'article (titre + corps). Pas de méta-commentaires. "
        "INTERDIT : balises HTML (<b>, <i>, <u>, etc.). "
        "Utilise les données fournies pour construire une narration cohérente et percutante. "
        "Les emojis sont autorisés avec parcimonie pour ponctuer les moments forts."
    )

    user_prompt = f"""ANGLE ÉDITORIAL : {angle}
TON : {ton}

FICHE CONFIDENTIELLE :
━━━━━━━━━━━━━━━━━━━━━━━━
👤 Identité     : {data['name']} (@{data['username']})
💰 Cash poche   : {_fmt(data['coins'])} {CURRENCY}
🏦 Banques      : {_fmt(data['bank_total'])} {CURRENCY} ({data['bank_count']} compte(s)) — {banques_str}
💑 Compte commun: {_fmt(data['couple_account'])} {CURRENCY}
💳 Dettes       : {_fmt(data['loans_total'])} {CURRENCY}
💎 FORTUNE TOTALE : {_fmt(data['fortune_totale'])} {CURRENCY} [{data['fortune_label']}]
🏆 Classement   : #{data['rank']} sur {data['total_players']} joueurs
⭐ Karma        : {karma_str} [{karma_label}]
📈 Bourse       : {portfolio_str}
🎓 Formation    : {diplomes_str}
🏢 Emploi       : {entreprise_str}
👨‍👩‍👧 Famille     : {famille_str}
━━━━━━━━━━━━━━━━━━━━━━━━

Rédige un article Breaking News de 220 à 280 mots.
— Commence DIRECTEMENT par un titre accrocheur tout en MAJUSCULES (une ligne).
— Ensuite 2 à 3 paragraphes narratifs dans le ton demandé.
— Aucune balise HTML. Emojis avec parcimonie."""

    return system_prompt, user_prompt


async def _call_groq(system_prompt: str, user_prompt: str) -> str:
    """Appelle Google Gemini (API gratuite) pour générer du texte."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return "❌ GEMINI_API_KEY non configurée dans les variables d'environnement."

    # Gemini n'a pas de "system" séparé — on fusionne les deux prompts
    full_prompt = f"{system_prompt}\n\n{user_prompt}"

    payload = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 700,
            "temperature":     1.0,
            "topP":            0.95,
        },
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{GEMINI_API_URL}?key={api_key}",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=25),
        ) as resp:
            if resp.status != 200:
                text_err = await resp.text()
                return f"❌ Erreur Gemini ({resp.status}) : {text_err[:300]}"
            result = await resp.json()
            return result["candidates"][0]["content"]["parts"][0]["text"].strip()


async def article_cmd(update, context):
    """/article @user | /article hasard"""
    user = update.effective_user

    if not await is_admin(user.id):
        return await update.message.reply_text("❌ Commande réservée aux admins.")

    args = context.args or []

    if not args:
        return await update.message.reply_text(
            "📰 <b>Système Article</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Usage :\n"
            "• <code>/article @pseudo</code> — article sur un joueur\n"
            "• <code>/article hasard</code> — joueur aléatoire parmi les plus riches",
            parse_mode=ParseMode.HTML
        )

    target_id   = None
    target_name = None

    if args[0].lower() == "hasard":
        async with AsyncSessionLocal() as session:
            res = await session.execute(
                text("SELECT user_id, first_name FROM users ORDER BY coins DESC LIMIT 20")
            )
            rows = res.fetchall()
        if not rows:
            return await update.message.reply_text("❌ Aucun joueur trouvé.")
        chosen      = random.choice(rows)
        target_id   = chosen[0]
        target_name = chosen[1]
    else:
        tg_user = await parse_target(update, context)
        if not tg_user:
            raw = args[0].lstrip("@").lower()
            async with AsyncSessionLocal() as session:
                db_fallback = await get_user_by_username(session, f"@{raw}")
            if db_fallback:
                target_id   = db_fallback.user_id
                target_name = db_fallback.first_name
            else:
                return await update.message.reply_text(
                    "❌ Joueur introuvable.\n"
                    "<i>Astuce : le joueur doit avoir utilisé /start au moins une fois.</i>",
                    parse_mode=ParseMode.HTML
                )
        else:
            target_id   = tg_user.id
            target_name = tg_user.first_name

    wait_msgs = [
        f"📡 Nos reporters enquêtent sur <b>{html.escape(target_name)}</b>...",
        f"🎙️ La rédaction prépare l'article sur <b>{html.escape(target_name)}</b>...",
        f"📰 Collecte des données financières de <b>{html.escape(target_name)}</b>...",
        f"🔍 Investigation en cours sur <b>{html.escape(target_name)}</b>...",
    ]
    msg = await update.message.reply_text(random.choice(wait_msgs), parse_mode=ParseMode.HTML)

    data = await _collect_player_data(target_id)
    if not data:
        await msg.edit_text("❌ Ce joueur n'a pas de profil en base de données.")
        return

    system_prompt, user_prompt = _build_prompt(data)
    article_text = await _call_groq(system_prompt, user_prompt)

    # Échapper les caractères HTML pour éviter les conflits avec le parse_mode Telegram
    article_escaped = html.escape(article_text)

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
        f"{article_escaped}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>📡 Reportage exclusif — Rédaction Family Bot News ❤️</i>"
    )

    if len(final) > 4000:
        final = final[:3960] + "...\n\n<i>📡 Reportage exclusif — Rédaction Family Bot News ❤️</i>"

    try:
        await msg.edit_text(final, parse_mode=ParseMode.HTML)
    except Exception:
        # Fallback sans HTML si le parsing échoue malgré l'échappement
        await msg.edit_text(article_text[:3800])
