"""
handlers/nationality.py — Nationalité joueur + Localisation entreprise + Impôts joueurs

Commandes :
  /nationalite              → affiche ta nationalité actuelle + liste de base
  /nationalite [pays]       → choisit/change de nationalité (IA accepte tout pays valide)
  /localisationboite [ville] → PDG définit la ville de son entreprise

Impôts joueurs :
  Prélevés via job_player_tax() — 0.05% des (coins + banque) par cycle
  Les PDG sont exonérés.
"""
from __future__ import annotations

import json
import logging
import os
import re
import aiohttp
from datetime import datetime

from sqlalchemy import select, text
from telegram import Update
from telegram.ext import ContextTypes

from database.db import AsyncSessionLocal, get_user
from database.models import User, Company, CompanyEmployee

logger = logging.getLogger(__name__)

# ─── GEMINI ───────────────────────────────────────────────────────────────────

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

async def _call_gemini(prompt: str) -> str | None:
    """Appelle Groq (compatible OpenAI) pour valider les nationalités."""
    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        logger.warning("[NATIONALITE] GROQ_API_KEY manquante")
        return None
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 300,
        "temperature": 0.3,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                GROQ_API_URL,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=25),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"[NATIONALITE] Groq HTTP {resp.status}: {body[:200]}")
                    return None
                data = await resp.json()
                return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning(f"[NATIONALITE] Groq error: {type(e).__name__}: {e}")
        return None


# ─── LISTE DE BASE DES NATIONALITÉS ──────────────────────────────────────────

NATIONALITIES = {
    "togolaise":    ("🇹🇬", "Togolaise"),
    "beninoise":    ("🇧🇯", "Béninoise"),
    "ivoirienne":   ("🇨🇮", "Ivoirienne"),
    "senegalaise":  ("🇸🇳", "Sénégalaise"),
    "camerounaise": ("🇨🇲", "Camerounaise"),
    "malienne":     ("🇲🇱", "Malienne"),
    "burkinabe":    ("🇧🇫", "Burkinabè"),
    "ghaneenne":    ("🇬🇭", "Ghanéenne"),
    "nigeriane":    ("🇳🇬", "Nigériane"),
    "congolaise":   ("🇨🇬", "Congolaise"),
    "gabonaise":    ("🇬🇦", "Gabonaise"),
    "malgache":     ("🇲🇬", "Malgache"),
    "marocaine":    ("🇲🇦", "Marocaine"),
    "algerienne":   ("🇩🇿", "Algérienne"),
    "tunisienne":   ("🇹🇳", "Tunisienne"),
    "egyptienne":   ("🇪🇬", "Égyptienne"),
    "francaise":    ("🇫🇷", "Française"),
    "belge":        ("🇧🇪", "Belge"),
    "suisse":       ("🇨🇭", "Suisse"),
    "allemande":    ("🇩🇪", "Allemande"),
    "anglaise":     ("🇬🇧", "Anglaise"),
    "espagnole":    ("🇪🇸", "Espagnole"),
    "italienne":    ("🇮🇹", "Italienne"),
    "americaine":   ("🇺🇸", "Américaine"),
    "canadienne":   ("🇨🇦", "Canadienne"),
    "bresilienne":  ("🇧🇷", "Brésilienne"),
    "mexicaine":    ("🇲🇽", "Mexicaine"),
    "japonaise":    ("🇯🇵", "Japonaise"),
    "chinoise":     ("🇨🇳", "Chinoise"),
    "indienne":     ("🇮🇳", "Indienne"),
    "emiratie":     ("🇦🇪", "Émiratie"),
    "apatride":     ("🌍", "Apatride"),
}

# ─── LISTE DES VILLES ────────────────────────────────────────────────────────

CITIES = {
    "lome":          ("🇹🇬", "Lomé"),
    "abidjan":       ("🇨🇮", "Abidjan"),
    "accra":         ("🇬🇭", "Accra"),
    "dakar":         ("🇸🇳", "Dakar"),
    "douala":        ("🇨🇲", "Douala"),
    "lagos":         ("🇳🇬", "Lagos"),
    "nairobi":       ("🇰🇪", "Nairobi"),
    "libreville":    ("🇬🇦", "Libreville"),
    "cotonou":       ("🇧🇯", "Cotonou"),
    "bamako":        ("🇲🇱", "Bamako"),
    "ouagadougou":   ("🇧🇫", "Ouagadougou"),
    "casablanca":    ("🇲🇦", "Casablanca"),
    "paris":         ("🇫🇷", "Paris"),
    "bruxelles":     ("🇧🇪", "Bruxelles"),
    "geneve":        ("🇨🇭", "Genève"),
    "berlin":        ("🇩🇪", "Berlin"),
    "londres":       ("🇬🇧", "Londres"),
    "new_york":      ("🇺🇸", "New York"),
    "miami":         ("🇺🇸", "Miami"),
    "toronto":       ("🇨🇦", "Toronto"),
    "sao_paulo":     ("🇧🇷", "São Paulo"),
    "tokyo":         ("🇯🇵", "Tokyo"),
    "dubai":         ("🇦🇪", "Dubaï"),
    "singapour":     ("🇸🇬", "Singapour"),
}

# ─── UTILITAIRES ─────────────────────────────────────────────────────────────

def _fmt(n: int) -> str:
    if n >= 1_000_000_000: return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:     return f"{n/1_000_000:.1f}M"
    if n >= 1_000:         return f"{n/1_000:.0f}K"
    return str(n)


# ─── RÉSOLUTION IA D'UNE NATIONALITÉ INCONNUE ────────────────────────────────

async def _resolve_nationality_ai(raw: str, player_name: str) -> dict | None:
    """
    Demande à Gemini de valider et enrichir une nationalité libre.
    Retourne un dict avec : valid, flag, label, fun_fact, bonus_hint
    ou None si l'IA est indisponible.
    """
    prompt = f"""Tu es le système de gestion des nationalités d'un jeu économique Telegram appelé Your family ❤️.

Un joueur nommé "{player_name}" veut définir sa nationalité comme : "{raw}"

Réponds UNIQUEMENT en JSON valide (aucun texte avant/après), format :
{{
  "valid": true/false,
  "flag": "🇹🇬",
  "label": "Togolaise",
  "fun_fact": "⚡ Bonus nationalité : les Togolais sont réputés pour leur sens des affaires — +2% sur les revenus de marché.",
  "bonus_hint": "🎯 Trait national : Résilient — tu récupères 10% plus vite d'une faillite."
}}

Règles :
- Si c'est une nationalité de pays réel (même orthographe approx.), valid=true avec le bon drapeau emoji et nom propre.
- fun_fact : une phrase fun et immersive liée à la culture/réputation du pays dans le contexte d'un jeu économique. Invente un petit bonus fictif sympa.
- bonus_hint : un trait de caractère national fictif et amusant pour le jeu.
- Si c'est totalement inventé ou insultant, valid=false.
- Réponds toujours en français."""

    raw_response = await _call_gemini(prompt)
    if not raw_response:
        return None

    try:
        # Nettoyer les backticks markdown si présents
        clean = re.sub(r"```json|```", "", raw_response).strip()
        return json.loads(clean)
    except Exception:
        logger.warning(f"[NATIONALITE] JSON parse failed: {raw_response[:200]}")
        return None


# ─── COMMANDE : /nationalite [pays] ──────────────────────────────────────────

async def nationalite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche ou change la nationalité du joueur. L'IA accepte n'importe quel pays valide."""
    user = update.effective_user

    if not context.args:
        # ── Afficher nationalité actuelle + liste de base ──
        async with AsyncSessionLocal() as session:
            db_user = await get_user(session, user.id)
            current = getattr(db_user, "nationality", None)
            current_label = getattr(db_user, "nationality_label", None)

        if current:
            if current in NATIONALITIES:
                flag, label = NATIONALITIES[current]
            else:
                # Nationalité IA stockée — on affiche telle quelle
                flag = "🌍"
                label = current_label or current.capitalize()
            current_str = f"{flag} <b>{label}</b>"
        else:
            current_str = "Non définie"

        lines = [
            f"🌍 <b>NATIONALITÉ — {user.first_name}</b>",
            f"Actuelle : {current_str}",
            "─────────────────────────────",
            "",
            "🌍 <b>Afrique :</b>",
        ]
        afrique  = ["togolaise","beninoise","ivoirienne","senegalaise","camerounaise",
                    "malienne","burkinabe","ghaneenne","nigeriane","congolaise","gabonaise",
                    "malgache","marocaine","algerienne","tunisienne","egyptienne"]
        europe   = ["francaise","belge","suisse","allemande","anglaise","espagnole","italienne"]
        ameriques= ["americaine","canadienne","bresilienne","mexicaine"]
        asie     = ["japonaise","chinoise","indienne","emiratie","apatride"]

        for nat in afrique:
            flag, label = NATIONALITIES[nat]
            lines.append(f"  {flag} <code>/nationalite {nat}</code> — {label}")
        lines.append("\n🌍 <b>Europe :</b>")
        for nat in europe:
            flag, label = NATIONALITIES[nat]
            lines.append(f"  {flag} <code>/nationalite {nat}</code> — {label}")
        lines.append("\n🌍 <b>Amériques :</b>")
        for nat in ameriques:
            flag, label = NATIONALITIES[nat]
            lines.append(f"  {flag} <code>/nationalite {nat}</code> — {label}")
        lines.append("\n🌍 <b>Asie & autres :</b>")
        for nat in asie:
            flag, label = NATIONALITIES[nat]
            lines.append(f"  {flag} <code>/nationalite {nat}</code> — {label}")

        lines.append("\n💡 <i>Ton pays n'est pas dans la liste ? Tape-le quand même !</i>")
        lines.append("<i>Ex: <code>/nationalite russe</code>, <code>/nationalite portugaise</code>...</i>")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    # ── Choisir une nationalité ──
    chosen = " ".join(context.args).lower().strip().replace("-", "_")

    CHANGE_COST = 10_000_000_000  # 10B

    # Vérifier si le joueur a déjà une nationalité (= changement payant)
    async with AsyncSessionLocal() as session:
        db_user = await get_user(session, user.id)
        current_nat = getattr(db_user, "nationality", None)
        current_coins = db_user.coins if db_user else 0

    if current_nat:
        # Changement → coûte 10B
        if current_coins < CHANGE_COST:
            manquant = CHANGE_COST - current_coins
            await update.message.reply_text(
                f"💸 <b>Changer de nationalité coûte 10 000 000 000 💰</b>\n\n"
                f"Ton solde : <b>{current_coins:,} 💰</b>\n"
                f"Il te manque : <b>{manquant:,} 💰</b>\n\n"
                f"📌 Ta nationalité actuelle est conservée.",
                parse_mode="HTML"
            )
            return
        # Débiter les 10B
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("UPDATE users SET coins = coins - :cost WHERE user_id = :uid"),
                {"cost": CHANGE_COST, "uid": user.id}
            )
            await session.commit()

    # 1. Dans la liste de base OU hors liste → toujours passer par l'IA pour le fun fact
    if chosen in NATIONALITIES:
        flag, label = NATIONALITIES[chosen]
    else:
        flag, label = None, None

    # Toujours appeler l'IA pour avoir le fun fact
    thinking_msg = await update.message.reply_text("🌍 Vérification en cours...")

    # Pour les nationalités de la liste, on fournit déjà flag+label à l'IA
    result = await _resolve_nationality_ai(chosen, user.first_name)

    if result is None or (flag is None and not result.get("valid", False)):
        if flag is None:
            await thinking_msg.edit_text(
                f"❌ Nationalité <b>{chosen}</b> non reconnue.\n"
                f"💡 Utilise <code>/nationalite</code> pour voir la liste.",
                parse_mode="HTML"
            )
            return
        # IA indisponible mais nationalité connue → afficher sans fun fact
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("UPDATE users SET nationality = :nat WHERE user_id = :uid"),
                {"nat": chosen, "uid": user.id}
            )
            await session.commit()
        paid_str = f"\n💸 <b>-10 000 000 000 💰</b> débités." if current_nat else ""
        await thinking_msg.edit_text(
            f"✅ Nationalité définie : {flag} <b>{label}</b>{paid_str}\n\n"
            f"🌍 Tu représentes fièrement ton pays dans Your family ❤️, {user.first_name} !",
            parse_mode="HTML"
        )
        return

    # IA a répondu
    if result.get("valid", False) or flag is not None:
        # Priorité au flag/label de la liste si connus, sinon prendre ceux de l'IA
        final_flag  = flag  or result.get("flag",  "🌍")
        final_label = label or result.get("label", chosen.capitalize())
        fun_fact   = result.get("fun_fact",   "")
        bonus_hint = result.get("bonus_hint", "")

        async with AsyncSessionLocal() as session:
            await session.execute(
                text("UPDATE users SET nationality = :nat WHERE user_id = :uid"),
                {"nat": chosen[:50], "uid": user.id}
            )
            await session.commit()

        paid_str = f"\n💸 <b>-10 000 000 000 💰</b> débités." if current_nat else ""
        lines = [f"✅ Nationalité définie : {final_flag} <b>{final_label}</b>{paid_str}", ""]
        if fun_fact:
            lines.append(fun_fact)
        if bonus_hint:
            lines.append(bonus_hint)
        lines.append("")
        lines.append(f"🌍 Bienvenue dans Your family ❤️, {user.first_name} !")
        await thinking_msg.edit_text("\n".join(lines), parse_mode="HTML")
        return
    else:
        await thinking_msg.edit_text(
            f"❌ <b>{chosen.capitalize()}</b> ne correspond à aucun pays reconnu.\n"
            f"💡 Essaie autrement ou tape <code>/nationalite</code> pour la liste.",
            parse_mode="HTML"
        )
        return




# ─── COMMANDE : /localisationboite [ville] ───────────────────────────────────

async def localisationboite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """PDG définit ou change la ville de son entreprise."""
    user = update.effective_user

    if not context.args:
        async with AsyncSessionLocal() as session:
            r = await session.execute(
                select(CompanyEmployee, Company).join(
                    Company, Company.id == CompanyEmployee.company_id
                ).where(
                    CompanyEmployee.user_id == user.id,
                    CompanyEmployee.left_at == None,
                    Company.is_active == True,
                    CompanyEmployee.role == "pdg",
                )
            )
            row = r.first()

        if row:
            _, company = row
            current_city = getattr(company, "city", None)
            if current_city and current_city in CITIES:
                flag, city_label = CITIES[current_city]
                city_str = f"{flag} <b>{city_label}</b>"
            else:
                city_str = current_city or "Non définie"
            company_name = company.name
        else:
            city_str = "—"
            company_name = "ton entreprise"

        lines = [
            f"🌆 <b>LOCALISATION — {company_name}</b>",
            f"Ville actuelle : {city_str}",
            "─────────────────────────────",
            "",
        ]
        for city_key, (flag, label) in CITIES.items():
            lines.append(f"  {flag} <code>/localisationboite {city_key}</code> — {label}")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    chosen = context.args[0].lower().replace("-", "_")
    if chosen not in CITIES:
        await update.message.reply_text(
            f"❌ Ville <b>{chosen}</b> inconnue.\n"
            f"💡 Liste : <code>/localisationboite</code>",
            parse_mode="HTML"
        )
        return

    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(CompanyEmployee, Company).join(
                Company, Company.id == CompanyEmployee.company_id
            ).where(
                CompanyEmployee.user_id == user.id,
                CompanyEmployee.left_at == None,
                Company.is_active == True,
                CompanyEmployee.role == "pdg",
            )
        )
        row = r.first()

        if not row:
            await update.message.reply_text("❌ Tu n'es PDG d'aucune entreprise.")
            return

        emp, company = row
        await session.execute(
            text("UPDATE companies SET city = :city WHERE id = :cid"),
            {"city": chosen, "cid": company.id}
        )
        await session.commit()

    flag, city_label = CITIES[chosen]
    await update.message.reply_text(
        f"✅ <b>{company.name}</b> est maintenant localisée à {flag} <b>{city_label}</b> !",
        parse_mode="HTML"
    )


# ─── JOB : IMPÔTS JOUEURS (0.05% par cycle, PDG exonérés) ────────────────────

PLAYER_TAX_RATE = 0.0005

async def job_player_tax(context: ContextTypes.DEFAULT_TYPE):
    """
    Prélève 0.05% sur (coins + solde banque) de chaque joueur non-PDG.
    Le montant va dans la StateCaisse.
    """
    from database.models import StateCaisse, BankAccount

    async with AsyncSessionLocal() as session:
        pdg_ids = set(
            row[0] for row in (await session.execute(
                select(CompanyEmployee.user_id).where(
                    CompanyEmployee.role == "pdg",
                    CompanyEmployee.left_at == None,
                )
            )).fetchall()
        )

        users = (await session.execute(
            select(User).where(User.coins > 10_000)
        )).scalars().all()

        caisse = (await session.execute(select(StateCaisse))).scalar_one_or_none()
        if not caisse:
            caisse = StateCaisse(total=0)
            session.add(caisse)

        total_collected = 0

        for u in users:
            if u.user_id in pdg_ids:
                continue

            bank_row = (await session.execute(
                select(BankAccount).where(BankAccount.user_id == u.user_id)
            )).scalar_one_or_none()
            bank_balance = bank_row.balance if bank_row else 0

            base = u.coins + bank_balance
            tax  = int(base * PLAYER_TAX_RATE)

            if tax <= 0:
                continue

            if u.coins >= tax:
                u.coins -= tax
                caisse.total += tax
                total_collected += tax
            elif u.coins > 0:
                partial = u.coins
                u.coins = 0
                caisse.total += partial
                total_collected += partial

        if total_collected > 0:
            await session.commit()
            logger.info(f"[PLAYER_TAX] {_fmt(total_collected)} $ collectés sur les joueurs.")
