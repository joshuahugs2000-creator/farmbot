"""
article.py — /article @user | /article hasard
Génère un article journalistique Breaking News sur un joueur.
Utilise Gemini si disponible, sinon fallback template automatique.
"""

import os, random, aiohttp, json, html
from sqlalchemy import select, text
from telegram.constants import ParseMode
from database.db import AsyncSessionLocal, get_user, get_user_by_username, get_all_groups
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


# ─── PROMPT GEMINI ────────────────────────────────────────────────────────────

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

    # Surnom court pour éviter la répétition du pseudo complet
    short_name = data['name'].split()[0] if data['name'] else data['name']

    system_prompt = (
        "Tu es le présentateur vedette d'une chaîne d'info fictive dans un jeu Telegram économique. "
        "Tu rédiges des articles Breaking News dramatiques, percutants et divertissants. "
        "RÈGLES ABSOLUES :\n"
        "1. N'écris le nom complet du joueur QU'UNE SEULE FOIS (dans le titre). "
        "Ensuite utilise uniquement son prénom court, 'notre sujet', 'l'intéressé(e)', 'la source', 'il/elle', etc.\n"
        "2. Zéro balise HTML. Zéro astérisque markdown.\n"
        "3. Les chiffres doivent être intégrés dans la narration — pas listés comme un tableau.\n"
        "4. Chaque article est unique : rythme, révélations et formulations différentes à chaque fois.\n"
        "5. Emojis autorisés avec parcimonie (2-3 max) pour ponctuer les moments forts.\n"
        "Tu écris UNIQUEMENT le texte de l'article (titre + corps). Pas de commentaires."
    )

    user_prompt = f"""ANGLE ÉDITORIAL : {angle}
TON : {ton}

FICHE CONFIDENTIELLE :
━━━━━━━━━━━━━━━━━━━━━━━━
👤 Nom complet  : {data['name']}
💎 Fortune totale : {_fmt(data['fortune_totale'])} {CURRENCY} [{data['fortune_label']}]
🏆 Classement   : #{data['rank']} sur {data['total_players']} joueurs
💰 Liquide poche : {_fmt(data['coins'])} {CURRENCY}
🏦 En banque    : {_fmt(data['bank_total'])} {CURRENCY} ({data['bank_count']} compte(s))
💳 Dettes       : {_fmt(data['loans_total'])} {CURRENCY}
📈 Bourse       : {portfolio_str}
⭐ Karma        : {karma_str} [{karma_label}]
🎓 Formation    : {diplomes_str}
🏢 Emploi       : {entreprise_str}
👨‍👩‍👧 Vie privée  : {famille_str}
━━━━━━━━━━━━━━━━━━━━━━━━

Rédige un article Breaking News de 230 à 290 mots.
— Commence DIRECTEMENT par un titre choc en MAJUSCULES (max 12 mots, utilise le nom complet une seule fois ici).
— 3 paragraphes narratifs : accroche dramatique / analyse financière intégrée dans la narration / vie privée + chute percutante.
— Le prénom court à utiliser après le titre : "{short_name}"
— Aucune balise HTML. Aucun astérisque. Emojis avec parcimonie."""

    return system_prompt, user_prompt


# ─── APPEL GEMINI ─────────────────────────────────────────────────────────────

async def _call_gemini(system_prompt: str, user_prompt: str) -> str | None:
    """Appelle Gemini. Retourne None si quota dépassé, erreur ou clé absente."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return None

    full_prompt = f"{system_prompt}\n\n{user_prompt}"
    payload = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 700,
            "temperature":     1.0,
            "topP":            0.95,
        },
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{GEMINI_API_URL}?key={api_key}",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=25),
            ) as resp:
                if resp.status != 200:
                    return None  # 429 quota, 500 erreur → fallback silencieux
                result = await resp.json()
                return result["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return None


# ─── FALLBACK TEMPLATE ────────────────────────────────────────────────────────

TITRES_FB = [
    "FORTUNE COLOSSALE : {NAME} DANS LE VISEUR DE NOS ENQUÊTEURS",
    "DOSSIER CONFIDENTIEL : QUI SE CACHE VRAIMENT DERRIÈRE {NAME} ?",
    "RÉVÉLATIONS EXCLUSIVES : L'EMPIRE SILENCIEUX DE {NAME}",
    "ALERTE : {NAME} ET SES MILLIARDS FONT TREMBLER LE CLASSEMENT",
    "ENQUÊTE CHOC : {NAME}, GÉNIE FINANCIER OU SIMPLE COUP DE CHANCE ?",
    "{NAME} : LA VÉRITÉ QUE PERSONNE N'OSAIT DIRE",
    "PORTRAIT SANS FILTRE : {NAME} — L'ASCENSION QUI DÉRANGE",
    "BREAKING : {NAME} PROPULSE LE BOT DANS UNE NOUVELLE ÈRE",
]

INTROS_FB = [
    "Nos reporters ont passé plusieurs jours à éplucher les données. Ce qu'ils ont trouvé dépasse tout ce qu'on imaginait.",
    "Le dossier était sur nos bureaux depuis longtemps. Ce soir, on l'ouvre. Et les révélations sont troublantes.",
    "Certains noms reviennent en boucle dans les couloirs du pouvoir. Celui-là, on ne pouvait plus l'ignorer.",
    "Une source anonyme nous a transmis le dossier complet. Après vérification, nous pouvons confirmer : c'est réel.",
    "Ça fait des semaines que ce nom circule dans les hautes sphères. Notre cellule investigation a enfin le tableau complet.",
]

MIDDLES_FB = [
    (
        "#{rank} sur {total} joueurs — voilà où se situe notre sujet dans la hiérarchie du bot. "
        "Avec {fortune} {cur} de fortune totale, le statut de {label} n'est pas usurpé. "
        "L'essentiel tourne en liquide — {coins} {cur} gardés à portée de main — "
        "pendant que {bank} {cur} dorment sagement en banque. "
        "{debt_line}"
    ),
    (
        "Le chiffre qui retient l'attention : {fortune} {cur} de patrimoine total, "
        "propulsant notre protagoniste au rang #{rank} sur {total} acteurs du marché. "
        "{coins} {cur} en cash immédiat, {bank} {cur} placés — une stratégie de liquidité assumée. "
        "{debt_line}"
    ),
    (
        "Quand on parle de {label}, les chiffres doivent suivre. Ils suivent : "
        "{fortune} {cur} de fortune cumulée, classement #{rank} parmi {total} joueurs recensés. "
        "La trésorerie personnelle ? {coins} {cur} disponibles, {bank} {cur} en réserve bancaire. "
        "{debt_line}"
    ),
]

DEBT_LINES_FB = {
    "zero": [
        "Zéro dette. Aucune créance. Un profil d'une netteté déconcertante.",
        "Pas un centime de dette à l'horizon. Certains appellent ça de la discipline. D'autres, de la chance.",
        "Le passif ? Inexistant. Ce niveau de rigueur financière est rarissime.",
    ],
    "low": [
        "Quelques dettes en cours — {loans} {cur} — rien d'alarmant pour un empire de cette envergure.",
        "{loans} {cur} de créances actives. Un levier mesuré dans une stratégie qui semble maîtrisée.",
    ],
    "high": [
        "L'ombre au tableau : {loans} {cur} de dettes. Qui finance ? Sous quelles conditions ? Les questions s'accumulent.",
        "{loans} {cur} de passif — une prise de risque considérable qui divise nos analystes.",
    ],
}

PERSO_FB = [
    "En dehors des marchés, {prenom} mène une vie {karma_vibe}. {famille_line} {karma_line} {emploi_line}",
    "Le volet humain du dossier : {famille_line} {karma_line} {emploi_line} Un profil qui ne laisse pas indifférent.",
    "Derrière les chiffres, il y a un individu. {famille_line} {karma_line} {emploi_line}",
]

CONCLUSIONS_FB = [
    "Family Bot News continuera de surveiller ce dossier. On ne lâche rien.",
    "L'histoire n'est pas terminée. Nos équipes restent sur le terrain. Restez connectés.",
    "Ce n'est qu'un début. D'autres révélations arrivent. Vous avez été prévenus.",
    "Le radar est activé. Rien ne nous échappe. Affaire à suivre.",
    "On reviendra sur ce cas. Comptez sur nous. Family Bot News, toujours en première ligne.",
]


def _build_fallback_article(data: dict) -> str:
    name   = data["name"]
    prenom = name.split()[0] if name else name
    karma  = data.get("karma", 0)
    kstr   = f"+{karma}" if karma >= 0 else str(karma)
    loans  = data["loans_total"]

    if karma > 30:
        klabel     = "irréprochable"
        karma_vibe = "irréprochable"
        karma_line = f"Son karma à {kstr} en fait l'une des figures les plus respectées du bot."
    elif karma < -10:
        klabel     = "persona non grata"
        karma_vibe = "controversée"
        karma_line = f"Son karma à {kstr} parle de lui-même — une réputation qui précède et inquiète."
    else:
        klabel     = "discrète"
        karma_vibe = "discrète"
        karma_line = f"Karma à {kstr} — ni saint, ni paria. Un profil qui sait rester dans l'ombre."

    diplomes_str = "autodidacte, sans diplôme officiel référencé"
    if data["diplomes"]:
        diplomes_str = " puis ".join(data["diplomes"])
        if data.get("diplome_domain"):
            diplomes_str += f" en {data['diplome_domain']}"

    famille_parts = []
    if data.get("spouse"):
        famille_parts.append(f"en couple avec {data['spouse']}")
    if data.get("children"):
        n = len(data["children"])
        famille_parts.append(f"parent de {n} enfant{'s' if n > 1 else ''}")
    if data.get("friends"):
        famille_parts.append(f"proche de {data['friends'][0]}")
    famille_line = (", ".join(famille_parts) + ".") if famille_parts else "Aucune attache familiale connue."

    if data.get("company"):
        emploi_line = f"Poste actuel : {data['company_role']} chez {data['company']}. Formation : {diplomes_str}."
    else:
        emploi_line = f"Sans emploi déclaré à ce jour. Parcours : {diplomes_str}."

    # Dette
    if loans == 0:
        debt_line = random.choice(DEBT_LINES_FB["zero"])
    elif loans < data["fortune_totale"] * 0.1:
        debt_line = random.choice(DEBT_LINES_FB["low"]).format(loans=_fmt(loans), cur=CURRENCY)
    else:
        debt_line = random.choice(DEBT_LINES_FB["high"]).format(loans=_fmt(loans), cur=CURRENCY)

    titre  = random.choice(TITRES_FB).format(NAME=name.upper())
    intro  = random.choice(INTROS_FB)
    middle = random.choice(MIDDLES_FB).format(
        rank=data["rank"], total=data["total_players"],
        fortune=_fmt(data["fortune_totale"]), cur=CURRENCY,
        label=data["fortune_label"].lower(),
        coins=_fmt(data["coins"]), bank=_fmt(data["bank_total"]),
        debt_line=debt_line,
    )
    perso  = random.choice(PERSO_FB).format(
        prenom=prenom, karma_vibe=karma_vibe,
        famille_line=famille_line, karma_line=karma_line, emploi_line=emploi_line,
    )
    conclu = random.choice(CONCLUSIONS_FB)

    return f"{titre}\n\n{intro}\n\n{middle}\n\n{perso}\n\n{conclu}"


# ─── COMMANDE /article ────────────────────────────────────────────────────────

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

    # Tentative Gemini — fallback template si quota dépassé ou erreur
    system_prompt, user_prompt = _build_prompt(data)
    article_text = await _call_gemini(system_prompt, user_prompt)

    ai_used = article_text is not None
    if not article_text:
        article_text = _build_fallback_article(data)

    article_escaped = html.escape(article_text)

    jours = ["Lundi","Mardi","Mercredi","Jeudi","Vendredi","Samedi","Dimanche"]
    mois  = ["janvier","février","mars","avril","mai","juin",
              "juillet","août","septembre","octobre","novembre","décembre"]
    from datetime import date
    today    = date.today()
    date_str = f"{jours[today.weekday()]} {today.day} {mois[today.month-1]} {today.year}"

    source_line = "📡 Reportage exclusif — Rédaction Family Bot News ❤️" if ai_used else "📰 Rédaction Family Bot News ❤️ — Édition Standard"

    final = (
        f"📺 <b>BREAKING NEWS — ÉDITION SPÉCIALE</b>\n"
        f"🗓️ <i>{date_str}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{article_escaped}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>{source_line}</i>"
    )

    if len(final) > 4000:
        final = final[:3960] + f"...\n\n<i>{source_line}</i>"

    try:
        await msg.edit_text(final, parse_mode=ParseMode.HTML)
    except Exception:
        await msg.edit_text(article_text[:3800])

    # ── Broadcast dans tous les groupes actifs ────────────────────────────────
    current_chat_id = update.effective_chat.id
    groups = await get_all_groups(active_only=True)

    sent_ok  = 0
    sent_err = 0
    for grp in groups:
        gid = grp[0]  # group_id est la 1ère colonne
        if gid == current_chat_id:
            continue  # déjà envoyé dans ce chat
        try:
            await context.bot.send_message(
                chat_id=gid,
                text=final,
                parse_mode=ParseMode.HTML
            )
            sent_ok += 1
        except Exception:
            sent_err += 1

    if groups:
        recap = (
            f"📡 Article diffusé dans <b>{sent_ok}</b> groupe(s)"
            + (f" — {sent_err} échec(s)" if sent_err else "") + "."
        )
        await update.message.reply_text(recap, parse_mode=ParseMode.HTML)
