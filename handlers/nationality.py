"""
handlers/nationality.py — Nationalité joueur + Localisation entreprise + Impôts joueurs

Commandes :
  /nationalite              → affiche ta nationalité actuelle
  /nationalite [pays]       → choisit/change de nationalité
  /localisationboite [ville] → PDG définit la ville de son entreprise

Impôts joueurs :
  Prélevés via job_player_tax() — 0.05% des (coins + banque) par cycle
  Les PDG sont exonérés.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select, text
from telegram import Update
from telegram.ext import ContextTypes

from database.db import AsyncSessionLocal, get_user
from database.models import User, Company, CompanyEmployee

logger = logging.getLogger(__name__)

# ─── LISTE DES NATIONALITÉS ──────────────────────────────────────────────────

NATIONALITIES = {
    # Afrique
    "togolaise":        ("🇹🇬", "Togolaise"),
    "beninoise":        ("🇧🇯", "Béninoise"),
    "ivoirienne":       ("🇨🇮", "Ivoirienne"),
    "senegalaise":      ("🇸🇳", "Sénégalaise"),
    "camerounaise":     ("🇨🇲", "Camerounaise"),
    "malienne":         ("🇲🇱", "Malienne"),
    "burkinabe":        ("🇧🇫", "Burkinabè"),
    "ghaneenne":        ("🇬🇭", "Ghanéenne"),
    "nigeriane":        ("🇳🇬", "Nigériane"),
    "congolaise":       ("🇨🇬", "Congolaise"),
    "gabonaise":        ("🇬🇦", "Gabonaise"),
    "malgache":         ("🇲🇬", "Malgache"),
    # Europe
    "francaise":        ("🇫🇷", "Française"),
    "belge":            ("🇧🇪", "Belge"),
    "suisse":           ("🇨🇭", "Suisse"),
    "allemande":        ("🇩🇪", "Allemande"),
    "anglaise":         ("🇬🇧", "Anglaise"),
    # Amériques
    "americaine":       ("🇺🇸", "Américaine"),
    "canadienne":       ("🇨🇦", "Canadienne"),
    "bresilienne":      ("🇧🇷", "Brésilienne"),
    # Asie
    "japonaise":        ("🇯🇵", "Japonaise"),
    "chinoise":         ("🇨🇳", "Chinoise"),
    "indienne":         ("🇮🇳", "Indienne"),
    # Autres
    "apatride":         ("🌍", "Apatride"),
}

# ─── LISTE DES VILLES ────────────────────────────────────────────────────────

CITIES = {
    # Afrique
    "lome":             ("🇹🇬", "Lomé"),
    "abidjan":          ("🇨🇮", "Abidjan"),
    "accra":            ("🇬🇭", "Accra"),
    "dakar":            ("🇸🇳", "Dakar"),
    "douala":           ("🇨🇲", "Douala"),
    "lagos":            ("🇳🇬", "Lagos"),
    "nairobi":          ("🇰🇪", "Nairobi"),
    "libreville":       ("🇬🇦", "Libreville"),
    "cotonou":          ("🇧🇯", "Cotonou"),
    "bamako":           ("🇲🇱", "Bamako"),
    "ouagadougou":      ("🇧🇫", "Ouagadougou"),
    # Europe
    "paris":            ("🇫🇷", "Paris"),
    "bruxelles":        ("🇧🇪", "Bruxelles"),
    "geneve":           ("🇨🇭", "Genève"),
    "berlin":           ("🇩🇪", "Berlin"),
    "londres":          ("🇬🇧", "Londres"),
    # Amériques
    "new_york":         ("🇺🇸", "New York"),
    "miami":            ("🇺🇸", "Miami"),
    "toronto":          ("🇨🇦", "Toronto"),
    "sao_paulo":        ("🇧🇷", "São Paulo"),
    # Asie
    "tokyo":            ("🇯🇵", "Tokyo"),
    "dubai":            ("🇦🇪", "Dubaï"),
    "singapour":        ("🇸🇬", "Singapour"),
}

# ─── UTILITAIRES ─────────────────────────────────────────────────────────────

def _fmt(n: int) -> str:
    if n >= 1_000_000_000: return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:     return f"{n/1_000_000:.1f}M"
    if n >= 1_000:         return f"{n/1_000:.0f}K"
    return str(n)


# ─── COMMANDE : /nationalite [pays] ──────────────────────────────────────────

async def nationalite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche ou change la nationalité du joueur."""
    user = update.effective_user

    if not context.args:
        # Afficher la nationalité actuelle + liste
        async with AsyncSessionLocal() as session:
            db_user = await get_user(session, user.id)
            current = getattr(db_user, "nationality", None)

        if current and current in NATIONALITIES:
            flag, label = NATIONALITIES[current]
            current_str = f"{flag} <b>{label}</b>"
        else:
            current_str = "Non définie"

        # Grouper par région pour l'affichage
        lines = [
            f"🌍 <b>NATIONALITÉ — {user.first_name}</b>",
            f"Actuelle : {current_str}",
            "─────────────────────────────",
            "",
            "🌍 <b>Afrique :</b>",
        ]
        afrique = ["togolaise", "beninoise", "ivoirienne", "senegalaise", "camerounaise",
                   "malienne", "burkinabe", "ghaneenne", "nigeriane", "congolaise", "gabonaise", "malgache"]
        europe = ["francaise", "belge", "suisse", "allemande", "anglaise"]
        ameriques = ["americaine", "canadienne", "bresilienne"]
        asie = ["japonaise", "chinoise", "indienne", "apatride"]

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

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    chosen = context.args[0].lower().replace("-", "_")
    if chosen not in NATIONALITIES:
        await update.message.reply_text(
            f"❌ Nationalité <b>{chosen}</b> inconnue.\n"
            f"💡 Liste complète : <code>/nationalite</code>",
            parse_mode="HTML"
        )
        return

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("UPDATE users SET nationality = :nat WHERE user_id = :uid"),
            {"nat": chosen, "uid": user.id}
        )
        await session.commit()

    flag, label = NATIONALITIES[chosen]
    await update.message.reply_text(
        f"✅ Nationalité mise à jour : {flag} <b>{label}</b>",
        parse_mode="HTML"
    )


# ─── COMMANDE : /localisationboite [ville] ───────────────────────────────────

async def localisationboite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """PDG définit ou change la ville de son entreprise."""
    user = update.effective_user

    if not context.args:
        # Afficher la liste + ville actuelle
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
                city_str = "Non définie"
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

PLAYER_TAX_RATE = 0.0005  # 0.05%

async def job_player_tax(context: ContextTypes.DEFAULT_TYPE):
    """
    Prélève 0.05% sur (coins + solde banque) de chaque joueur non-PDG.
    Le montant va dans la StateCaisse.
    """
    from database.models import StateCaisse, BankAccount

    async with AsyncSessionLocal() as session:
        # Récupérer les PDG actuels pour les exonérer
        pdg_ids = set(
            row[0] for row in (await session.execute(
                select(CompanyEmployee.user_id).where(
                    CompanyEmployee.role == "pdg",
                    CompanyEmployee.left_at == None,
                )
            )).fetchall()
        )

        # Récupérer tous les joueurs actifs (avec coins > 0)
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
                continue  # PDG exonérés

            # Solde banque
            bank_row = (await session.execute(
                select(BankAccount).where(BankAccount.user_id == u.user_id)
            )).scalar_one_or_none()
            bank_balance = bank_row.balance if bank_row else 0

            base = u.coins + bank_balance
            tax = int(base * PLAYER_TAX_RATE)

            if tax <= 0:
                continue

            if u.coins >= tax:
                u.coins -= tax
                caisse.total += tax
                total_collected += tax
            elif u.coins > 0:
                # Payer ce qu'il peut
                partial = u.coins
                u.coins = 0
                caisse.total += partial
                total_collected += partial

        if total_collected > 0:
            await session.commit()
            logger.info(f"[PLAYER_TAX] {_fmt(total_collected)} $ collectés sur les joueurs.")
