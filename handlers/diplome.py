"""
handlers/diplome.py — Système de diplômes avec examens générés par Groq.

Commandes :
  /diplome        — voir ses diplômes et lancer un examen
  /mondomaine     — voir son domaine de spécialisation

Flux d'un examen :
  1. /diplome → bouton "Passer le Bac / Licence / ..."
  2. Pour Licence → choisir un domaine (définitif)
  3. Bot appelle Groq → génère N questions QCM
  4. Questions posées une par une via boutons inline
  5. Résultat → diplôme accordé ou cooldown
"""

import json
import os
import logging
import httpx
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from sqlalchemy import text

from database.db import AsyncSessionLocal, get_user
from utils.helpers import ensure_user

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


# ── Configuration ──────────────────────────────────────────────────────────────

DOMAINS: dict[str, tuple[str, str]] = {
    "finance":      ("📈", "Finance"),
    "informatique": ("💻", "Informatique"),
    "marketing":    ("📣", "Marketing"),
    "droit":        ("⚖️",  "Droit"),
    "management":   ("🏢", "Management"),
    "agriculture":  ("🌾", "Agriculture"),
    "securite":     ("🛡️", "Sécurité"),
}

EXAMS: dict[str, dict] = {
    "bac":     {"emoji": "📄", "label": "Bac",     "n": 10, "required": 7,  "cost": 0,          "cooldown_fail": 6},
    "licence": {"emoji": "🎓", "label": "Licence", "n": 10, "required": 8,  "cost": 500_000,    "cooldown_fail": 12},
    "master":  {"emoji": "🏅", "label": "Master",  "n": 10, "required": 8,  "cost": 5_000_000,  "cooldown_fail": 24},
    "mba":     {"emoji": "👑", "label": "MBA",      "n": 15, "required": 12, "cost": 50_000_000, "cooldown_fail": 24},
}

WORK_BONUS: dict[str, int] = {
    "bac": 10, "licence": 25, "master": 50, "mba": 100,
}

LEVEL_ORDER = ["none", "bac", "licence", "master", "mba"]


# ── Questions de secours (si Groq échoue) ────────────────────────────────────

FALLBACK: dict[str, list] = {
    "bac": [
        {"question": "Quelle est la capitale de la France ?",                      "choices": ["A. Lyon", "B. Marseille", "C. Paris", "D. Bordeaux"],          "correct": 2},
        {"question": "Combien font 15 % de 200 ?",                                 "choices": ["A. 25", "B. 30", "C. 35", "D. 20"],                            "correct": 1},
        {"question": "Quel est le plus grand océan du monde ?",                    "choices": ["A. Atlantique", "B. Indien", "C. Arctique", "D. Pacifique"],    "correct": 3},
        {"question": "En quelle année a eu lieu la Révolution française ?",        "choices": ["A. 1789", "B. 1799", "C. 1776", "D. 1815"],                    "correct": 0},
        {"question": "Quel est le symbole chimique de l'or ?",                     "choices": ["A. Or", "B. Au", "C. Ag", "D. Go"],                            "correct": 1},
        {"question": "Combien de continents y a-t-il sur Terre ?",                 "choices": ["A. 5", "B. 6", "C. 7", "D. 8"],                               "correct": 2},
        {"question": "Qui a peint la Joconde ?",                                   "choices": ["A. Picasso", "B. Michel-Ange", "C. Raphaël", "D. Léonard de Vinci"], "correct": 3},
        {"question": "Quelle est la vitesse de la lumière ?",                      "choices": ["A. 300 000 km/s", "B. 150 000 km/s", "C. 500 000 km/s", "D. 200 000 km/s"], "correct": 0},
        {"question": "Quel gaz représente ~78 % de l'atmosphère terrestre ?",      "choices": ["A. Oxygène", "B. Hydrogène", "C. Azote", "D. CO₂"],            "correct": 2},
        {"question": "Combien de secondes dans une heure ?",                       "choices": ["A. 3 000", "B. 3 600", "C. 6 000", "D. 1 200"],                "correct": 1},
    ],
    "finance": [
        {"question": "Qu'est-ce qu'un dividende ?",                                "choices": ["A. Un impôt sur les bénéfices", "B. Une part des bénéfices versée aux actionnaires", "C. Un prêt bancaire", "D. Une cotisation sociale"], "correct": 1},
        {"question": "Que mesure le taux d'intérêt ?",                            "choices": ["A. Le risque d'une action", "B. Le coût de l'emprunt", "C. Le rendement d'un bien immobilier", "D. La croissance du PIB"], "correct": 1},
        {"question": "Qu'est-ce qu'une action en bourse ?",                       "choices": ["A. Un titre de créance", "B. Une part du capital d'une entreprise", "C. Un contrat d'assurance", "D. Un dépôt bancaire"], "correct": 1},
        {"question": "Qu'est-ce qu'une obligation ?",                              "choices": ["A. Un titre de propriété d'une entreprise", "B. Un titre de dette émis par une société ou un État", "C. Un contrat à terme", "D. Une devise étrangère"], "correct": 1},
        {"question": "Que signifie le sigle ROI ?",                               "choices": ["A. Return On Investment", "B. Rate Of Inflation", "C. Risk Of Insolvency", "D. Ratio Of Income"], "correct": 0},
        {"question": "Qu'est-ce que la liquidité d'un actif ?",                   "choices": ["A. Sa profitabilité", "B. Sa facilité à être converti en cash", "C. Son niveau de risque", "D. Sa durée de vie"], "correct": 1},
        {"question": "Qu'est-ce que le PIB ?",                                    "choices": ["A. Produit Intérieur Brut", "B. Prix Indicatif Bancaire", "C. Plan d'Investissement Boursier", "D. Profit Inter-Bancaire"], "correct": 0},
        {"question": "Qu'est-ce qu'une plus-value ?",                             "choices": ["A. Un bénéfice réalisé lors de la vente d'un actif", "B. Une taxe sur les revenus", "C. Un intérêt composé", "D. Un remboursement de TVA"], "correct": 0},
        {"question": "Quel organisme régule les marchés financiers en France ?",   "choices": ["A. La Banque de France", "B. L'AMF", "C. La BCE", "D. Le FMI"], "correct": 1},
        {"question": "Qu'est-ce qu'un bilan comptable ?",                         "choices": ["A. Un état des flux de trésorerie", "B. Un document listant actifs et passifs", "C. Un compte de résultat", "D. Un plan de financement"], "correct": 1},
    ],
    "informatique": [
        {"question": "Qu'est-ce qu'une boucle 'for' ?",                           "choices": ["A. Une condition logique", "B. Une structure répétant un bloc N fois", "C. Une fonction récursive", "D. Un type de données"], "correct": 1},
        {"question": "Que signifie HTTP ?",                                        "choices": ["A. HyperText Transfer Protocol", "B. High Tech Transmission Process", "C. Hybrid Text Transport Protocol", "D. Hyper Transfer Text Program"], "correct": 0},
        {"question": "Quelle est la base du système binaire ?",                   "choices": ["A. 8", "B. 10", "C. 2", "D. 16"],                              "correct": 2},
        {"question": "Qu'est-ce qu'une base de données relationnelle ?",          "choices": ["A. Une base stockant des fichiers multimédia", "B. Une base organisant les données en tables liées", "C. Une base distribuée sur plusieurs serveurs", "D. Une base en mémoire vive"], "correct": 1},
        {"question": "Que fait la commande 'git commit' ?",                       "choices": ["A. Supprime l'historique", "B. Envoie le code sur GitHub", "C. Enregistre les modifications localement", "D. Fusionne deux branches"], "correct": 2},
        {"question": "Qu'est-ce que le CPU ?",                                    "choices": ["A. Central Processing Unit", "B. Computer Power Unit", "C. Core Program Utility", "D. Central Programmable Unit"], "correct": 0},
        {"question": "Quelle est la différence entre RAM et ROM ?",               "choices": ["A. La RAM est permanente, la ROM est volatile", "B. La RAM est volatile, la ROM est permanente", "C. Les deux sont identiques", "D. La RAM est plus lente"], "correct": 1},
        {"question": "Qu'est-ce qu'une API ?",                                    "choices": ["A. Application Protocol Interface", "B. Application Programming Interface", "C. Advanced Program Integration", "D. Automated Protocol Interface"], "correct": 1},
        {"question": "Quel langage est principalement utilisé pour les pages web ?", "choices": ["A. Python", "B. Java", "C. HTML/CSS", "D. C++"],           "correct": 2},
        {"question": "Que signifie 'SQL' ?",                                      "choices": ["A. Structured Query Language", "B. Simple Query Loop", "C. System Queue Logic", "D. Structured Queue Link"], "correct": 0},
    ],
}

# Pour les domaines sans fallback, on utilise les questions Bac
for _d in DOMAINS:
    if _d not in FALLBACK:
        FALLBACK[_d] = FALLBACK["bac"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _user_level(u) -> str:
    if getattr(u, "diplome_mba",     False): return "mba"
    if getattr(u, "diplome_master",  False): return "master"
    if getattr(u, "diplome_licence", False): return "licence"
    if getattr(u, "diplome_bac",     False): return "bac"
    return "none"


def _next_level(current: str) -> str | None:
    idx = LEVEL_ORDER.index(current)
    return LEVEL_ORDER[idx + 1] if idx < len(LEVEL_ORDER) - 1 else None


# ── Génération Groq ───────────────────────────────────────────────────────────

async def _groq_questions(level: str, domain: str, n: int) -> list | None:
    if not GROQ_API_KEY:
        return None

    domain_label = DOMAINS.get(domain, ("", domain))[1]
    level_label  = EXAMS[level]["label"]

    import random
    seed = random.randint(1000, 9999)  # seed aléatoire pour forcer la variété

    if level == "bac":
        themes = [
            "géographie mondiale", "histoire", "sciences naturelles",
            "mathématiques de base", "culture générale", "économie de base",
            "littérature", "sports et records", "gastronomie et culture",
            "technologie et inventions", "politique mondiale", "astronomie",
        ]
        random.shuffle(themes)
        themes_choisis = ", ".join(themes[:4])
        prompt = (
            f"[SEED:{seed}] Tu dois générer EXACTEMENT {n} questions QCM, ni plus ni moins. "
            f"Thème : culture générale niveau Bac. Thèmes à couvrir : {themes_choisis}. "
            f"Les questions doivent être VARIÉES et ORIGINALES. "
            f"Réponds UNIQUEMENT avec un tableau JSON de {n} objets, sans markdown ni texte autour. "
            f'Format: [{{"question":"...","choices":["A. ...","B. ...","C. ...","D. ..."],"correct":0}}] '
            "Le champ 'correct' est l'index 0-3 de la bonne réponse."
        )
    else:
        hardness = {"licence": "intermédiaire", "master": "avancé", "mba": "expert"}[level]
        prompt = (
            f"[SEED:{seed}] Tu dois générer EXACTEMENT {n} questions QCM, ni plus ni moins. "
            f"Niveau : {hardness}. Domaine : {domain_label} (niveau {level_label}). "
            f"Questions professionnelles, réalistes et VARIÉES. "
            f"Réponds UNIQUEMENT avec un tableau JSON de {n} objets, sans markdown ni texte autour. "
            f'Format: [{{"question":"...","choices":["A. ...","B. ...","C. ...","D. ..."],"correct":0}}] '
            "Le champ 'correct' est l'index 0-3 de la bonne réponse."
        )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.95,
                    "max_tokens": 6000,  # 6000 pour supporter jusqu'à 15 questions MBA
                },
            )
        data = resp.json()
        raw  = data["choices"][0]["message"]["content"].strip()
        # Nettoyer les éventuels backticks
        if "```" in raw:
            parts = raw.split("```")
            for p in parts:
                if p.startswith("json"):
                    raw = p[4:].strip()
                    break
                elif p.strip().startswith("["):
                    raw = p.strip()
                    break
        questions = json.loads(raw)
        assert isinstance(questions, list) and len(questions) >= n
        for q in questions[:n]:
            assert "question" in q and "choices" in q and "correct" in q
            assert len(q["choices"]) == 4
            assert 0 <= int(q["correct"]) <= 3
        return questions[:n]
    except Exception as e:
        logger.error(f"Groq échec: {e}")
        return None


# ── /diplome ──────────────────────────────────────────────────────────────────

async def diplome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.user_id)

    current = _user_level(u)
    domain  = getattr(u, "diplome_domain", None)
    d_emoji, d_label = DOMAINS.get(domain, ("🎓", "—")) if domain else ("—", "—")

    # Cooldown
    cd = getattr(u, "exam_cooldown", None)
    cd_active = cd and cd > datetime.utcnow()
    cd_line = ""
    if cd_active:
        delta = cd - datetime.utcnow()
        h = int(delta.total_seconds() // 3600)
        m = int((delta.total_seconds() % 3600) // 60)
        cd_line = f"\n⏳ <b>Prochain examen dans :</b> {h}h{m:02d}m"

    # Affichage des niveaux
    def _status(lvl):
        return "✅" if getattr(u, f"diplome_{lvl}", False) else "⬜"

    bonus_line = ""
    if current != "none":
        bonus_line = f"\n💰 Bonus /work actif : <b>+{WORK_BONUS.get(current, 0)}%</b>"

    lines = [
        "🎓 <b>VOS DIPLÔMES</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"{_status('bac')}  📄 <b>Bac</b>  — Gratuit  (7/10 requis)",
        f"{_status('licence')}  🎓 <b>Licence</b>  — {_fmt(500_000)} 💰  (8/10 requis)",
        f"{_status('master')}  🏅 <b>Master</b>  — {_fmt(5_000_000)} 💰  (8/10 requis)",
        f"{_status('mba')}  👑 <b>MBA</b>  — {_fmt(50_000_000)} 💰  (12/15 requis)",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"{'📌' if domain else '—'} <b>Domaine :</b> {d_emoji + ' ' + d_label if domain else '—  (choisi à la Licence)'}",
        bonus_line,
        cd_line,
    ]

    next_lvl = _next_level(current)
    keyboard = None

    if next_lvl and not cd_active:
        info     = EXAMS[next_lvl]
        cost_str = f"— {_fmt(info['cost'])} 💰" if info["cost"] else "— Gratuit"

        if next_lvl == "bac":
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"📝 Passer le {info['label']}  {cost_str}", callback_data="exam:begin:bac:bac")
            ]])
        elif next_lvl == "licence" and not domain:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"📝 Passer la {info['label']}  {cost_str}", callback_data="exam:domain:licence")
            ]])
        elif next_lvl in ("master", "mba") and domain:
            # Domaine verrouillé — obligatoire de repasser dans le même domaine
            d_em, d_lb = DOMAINS.get(domain, ("🎓", domain))
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"📝 Passer le {info['label']}  {d_em} {d_lb}  {cost_str}",
                    callback_data=f"exam:begin:{next_lvl}:{domain}"
                )
            ]])
        else:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"📝 Passer le {info['label']}  {cost_str}", callback_data=f"exam:begin:{next_lvl}:{domain or 'general'}")
            ]])
    elif current == "mba":
        lines.append("\n🏆 Tu as tous les diplômes ! Félicitations.")

    await update.message.reply_text(
        "\n".join(l for l in lines if l is not None),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# ── Callbacks ─────────────────────────────────────────────────────────────────

async def diplome_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    uid     = query.from_user.id
    parts   = query.data.split(":")   # exam:action:...
    action  = parts[1]

    if action == "domain":
        # Afficher les choix de domaine
        level = parts[2]
        rows  = []
        row   = []
        for key, (emoji, label) in DOMAINS.items():
            row.append(InlineKeyboardButton(f"{emoji} {label}", callback_data=f"exam:begin:{level}:{key}"))
            if len(row) == 2:
                rows.append(row); row = []
        if row:
            rows.append(row)
        await query.edit_message_text(
            "🎓 <b>Choisissez votre domaine de spécialisation</b>\n\n"
            "⚠️ Ce choix est <b>définitif</b> — il s'appliquera à votre Licence, Master et MBA.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )

    elif action == "begin":
        level  = parts[2]
        domain = parts[3]
        await _start_exam(query, context, uid, level, domain)

    elif action == "answer":
        level  = parts[2]
        domain = parts[3]
        q_idx  = int(parts[4])
        answer = int(parts[5])
        await _handle_answer(query, context, uid, level, domain, q_idx, answer)


# ── Déroulement de l'examen ───────────────────────────────────────────────────

async def _start_exam(query, context, uid: int, level: str, domain: str):
    async with AsyncSessionLocal() as session:
        row = await session.execute(text("SELECT * FROM users WHERE user_id = :uid"), {"uid": uid})
        u   = row.fetchone()

    if not u:
        return await query.edit_message_text("❌ Compte introuvable. Fais /start d'abord.")

    # Cooldown
    cd = getattr(u, "exam_cooldown", None)
    if cd and cd > datetime.utcnow():
        delta = cd - datetime.utcnow()
        h = int(delta.total_seconds() // 3600)
        m = int((delta.total_seconds() % 3600) // 60)
        return await query.edit_message_text(f"⏳ Examen disponible dans <b>{h}h{m:02d}m</b>.", parse_mode=ParseMode.HTML)

    # Ordre des niveaux
    current = _user_level(u)
    if _next_level(current) != level:
        return await query.edit_message_text("❌ Tu dois obtenir le diplôme précédent d'abord.")

    # Verif domaine verrouille (Master / MBA dans le meme domaine que la Licence)
    saved_domain = getattr(u, "diplome_domain", None)
    if level in ("master", "mba") and saved_domain and domain != saved_domain:
        d_em, d_lb = DOMAINS.get(saved_domain, ("🎓", saved_domain))
        return await query.edit_message_text(
            f"❌ Tu dois passer le {level.capitalize()} dans ton domaine de Licence : "
            f"<b>{d_em} {d_lb}</b>",
            parse_mode=ParseMode.HTML,
        )

    # Ancienneté Master (20 jours)
    if level == "master" and u.created_at:
        days = (datetime.utcnow() - u.created_at).days
        if days < 20:
            return await query.edit_message_text(
                f"❌ Le Master requiert <b>20 jours</b> d'ancienneté.\nTu en as {days}/20.",
                parse_mode=ParseMode.HTML,
            )

    # Coût
    info = EXAMS[level]
    cost = info["cost"]
    if cost > 0:
        if u.coins < cost:
            return await query.edit_message_text(
                f"❌ Il te faut <b>{_fmt(cost)} 💰</b> pour cet examen.\nTon solde : <b>{_fmt(u.coins)} 💰</b>",
                parse_mode=ParseMode.HTML,
            )
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("UPDATE users SET coins = CAST(coins AS BIGINT) - :c WHERE user_id = :uid"),
                {"c": cost, "uid": uid},
            )
            await session.commit()

    # Générer les questions
    await query.edit_message_text(
        f"🔄 <b>Génération de l'examen en cours…</b>\n"
        f"{info['emoji']} {info['label']}"
        + (f"  ·  {DOMAINS.get(domain, ('',''))[1]}" if level != 'bac' else ""),
        parse_mode=ParseMode.HTML,
    )

    questions = await _groq_questions(level, domain, info["n"])
    if not questions:
        # Fallback local
        fb_key    = domain if domain in FALLBACK else "bac"
        questions = FALLBACK.get(fb_key, FALLBACK["bac"])[: info["n"]]
        if not questions:
            # Rembourser
            if cost > 0:
                async with AsyncSessionLocal() as session:
                    await session.execute(
                        text("UPDATE users SET coins = CAST(coins AS BIGINT) + :c WHERE user_id = :uid"),
                        {"c": cost, "uid": uid},
                    )
                    await session.commit()
            return await query.edit_message_text("❌ Impossible de générer l'examen. Réessaie dans quelques minutes.")

    # Stocker la session
    context.user_data[f"exam_{uid}"] = {
        "level":     level,
        "domain":    domain,
        "questions": questions,
        "score":     0,
        "total":     info["n"],
    }

    await _show_question(query, context, uid, 0)


QUESTION_TIMEOUT = 20  # secondes par question


def _timer_bar(remaining: int, total: int = QUESTION_TIMEOUT) -> str:
    """Barre de progression du timer."""
    filled = round((remaining / total) * 10)
    bar = "🟩" * filled + "⬜" * (10 - filled)
    return bar


async def _show_question(query, context, uid: int, q_idx: int):
    data = context.user_data.get(f"exam_{uid}")
    if not data:
        return

    import asyncio

    q      = data["questions"][q_idx]
    level  = data["level"]
    domain = data["domain"]
    total  = data["total"]
    score  = data["score"]
    info   = EXAMS[level]

    buttons = [
        [InlineKeyboardButton(choice, callback_data=f"exam:answer:{level}:{domain}:{q_idx}:{i}")]
        for i, choice in enumerate(q["choices"])
    ]

    # Marquer la question active + générer un token unique pour ce tour
    import time
    turn_token = time.monotonic()
    data["current_q"]    = q_idx
    data["turn_token"]   = turn_token

    def _build_text(remaining: int) -> str:
        bar = _timer_bar(remaining)
        return (
            f"{info['emoji']} <b>{info['label']}</b>  ·  Question {q_idx + 1}/{total}\n"
            f"✅ Score : {score}/{q_idx}  |  ⏱ {bar} <b>{remaining}s</b>\n\n"
            f"❓ <b>{q['question']}</b>"
        )

    async def _edit_msg(msg_obj, text, markup=None):
        """Édite peu importe si c'est un CallbackQuery ou un Message."""
        try:
            if hasattr(msg_obj, "edit_message_text"):
                await msg_obj.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            else:
                await msg_obj.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception:
            pass

    # Affichage initial
    await _edit_msg(query, _build_text(QUESTION_TIMEOUT), InlineKeyboardMarkup(buttons))

    # Récupérer l'objet message pour le countdown
    # On a besoin du vrai Message pour edit_text dans la tâche de fond
    try:
        if hasattr(query, "message"):
            msg = query.message   # CallbackQuery → .message
        else:
            msg = query           # déjà un Message
    except Exception:
        msg = query

    # Countdown en tâche de fond
    async def _countdown():
        try:
            for remaining in range(QUESTION_TIMEOUT - 5, 0, -5):
                await asyncio.sleep(5)
                current_data = context.user_data.get(f"exam_{uid}")
                # Stopper si : session perdue, question changée, ou token différent
                if (not current_data
                        or current_data.get("current_q") != q_idx
                        or current_data.get("turn_token") != turn_token):
                    return
                try:
                    await msg.edit_text(
                        _build_text(remaining),
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup(buttons),
                    )
                except Exception:
                    pass

            # Dernier tick — vérifier une ultime fois
            await asyncio.sleep(5)
            current_data = context.user_data.get(f"exam_{uid}")
            if (not current_data
                    or current_data.get("current_q") != q_idx
                    or current_data.get("turn_token") != turn_token):
                return  # Le joueur a répondu entre-temps

            # Temps écoulé → question ratée
            current_data["current_q"]  = -1
            current_data["turn_token"] = None
            next_idx = q_idx + 1

            try:
                await msg.edit_text(
                    f"⏰ <b>Temps écoulé !</b>\n\n"
                    f"❌ Question {q_idx + 1} ratée — pas de réponse.\n"
                    f"Score : {current_data['score']}/{q_idx + 1}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

            await asyncio.sleep(2)
            if next_idx >= current_data["total"]:
                await _finish_exam(msg, context, uid, current_data)
            else:
                await _show_question(msg, context, uid, next_idx)

        except Exception as e:
            logger.debug(f"Countdown error q{q_idx}: {e}")

    asyncio.create_task(_countdown())


async def _handle_answer(query, context, uid: int, level: str, domain: str, q_idx: int, answer: int):
    data = context.user_data.get(f"exam_{uid}")
    if not data:
        return await query.edit_message_text("❌ Session expirée. Refais /diplome pour recommencer.")

    # Ignorer si cette question a déjà été traitée (double-clic ou timer)
    if data.get("current_q") != q_idx:
        try:
            await query.answer("⚠️ Réponse déjà enregistrée !", show_alert=False)
        except Exception:
            pass
        return

    # Verrouiller immédiatement pour stopper le countdown
    data["current_q"]  = -1
    data["turn_token"] = None

    correct = int(data["questions"][q_idx]["correct"])
    is_correct = (answer == correct)
    if is_correct:
        data["score"] += 1

    # Feedback visuel rapide
    emoji = "✅" if is_correct else "❌"
    bonne = data["questions"][q_idx]["choices"][correct]
    try:
        await query.edit_message_text(
            f"{emoji} <b>{'Bonne réponse !' if is_correct else 'Mauvaise réponse...'}</b>\n"
            f"{'✔️' if is_correct else f'La bonne réponse était : <b>{bonne}</b>'}\n\n"
            f"Score : {data['score']}/{q_idx + 1}",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    import asyncio
    await asyncio.sleep(1.5)

    next_idx = q_idx + 1
    if next_idx >= data["total"]:
        await _finish_exam(query, context, uid, data)
    else:
        await _show_question(query, context, uid, next_idx)


async def _finish_exam(query, context, uid: int, data: dict):
    """
    query peut être un CallbackQuery (réponse bouton) ou un Message (timeout timer).
    On normalise l'appel edit pour supporter les deux cas.
    """
    level    = data["level"]
    domain   = data["domain"]
    score    = data["score"]
    total    = data["total"]
    info     = EXAMS[level]
    required = info["required"]
    success  = score >= required

    async def _edit(text: str, **kwargs):
        """Édite le message peu importe si c'est un CallbackQuery ou un Message."""
        try:
            if hasattr(query, "edit_message_text"):
                # CallbackQuery
                await query.edit_message_text(text, **kwargs)
            else:
                # Message (appelé depuis le timer)
                await query.edit_text(text, **kwargs)
        except Exception as e:
            logger.warning(f"_finish_exam edit error: {e}")

    async with AsyncSessionLocal() as session:
        params = {"uid": uid}
        sets   = []

        if success:
            sets.append(f"diplome_{level} = TRUE")
            # Sauvegarder le domaine à la Licence (définitif)
            if level == "licence" and domain not in ("bac", "general", None):
                sets.append("diplome_domain = :dom")
                params["dom"] = domain
            sets.append("exam_cooldown = NULL")
        else:
            cd_dt = datetime.utcnow() + timedelta(hours=info["cooldown_fail"])
            sets.append("exam_cooldown = :cd")
            params["cd"] = cd_dt

        await session.execute(
            text(f"UPDATE users SET {', '.join(sets)} WHERE user_id = :uid"),
            params,
        )
        await session.commit()

    context.user_data.pop(f"exam_{uid}", None)

    if success:
        bonus = WORK_BONUS.get(level, 0)
        d_str = ""
        if level != "bac" and domain not in ("bac", "general", None):
            d_str = f"  ·  {DOMAINS.get(domain, ('',''))[1]}"
        await _edit(
            f"🎉 <b>FÉLICITATIONS !</b>\n\n"
            f"✅ Diplôme obtenu : <b>{info['emoji']} {info['label']}{d_str}</b>\n"
            f"📊 Score : <b>{score}/{total}</b>\n\n"
            f"💰 Bonus /work permanent : <b>+{bonus}%</b>\n"
            f"🏆 Badge visible sur ton /me !\n\n"
            f"Tape /diplome pour voir ta progression.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await _edit(
            f"❌ <b>ÉCHEC</b>\n\n"
            f"Score : <b>{score}/{total}</b>  (minimum requis : <b>{required}/{total}</b>)\n\n"
            f"⏳ Nouveau tentative disponible dans <b>{info['cooldown_fail']}h</b>.\n"
            f"Tape /diplome pour voir le cooldown.",
            parse_mode=ParseMode.HTML,
        )
