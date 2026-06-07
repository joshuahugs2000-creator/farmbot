"""
handlers/company_sector.py — Systèmes sectoriels des entreprises

4 systèmes :
  1. 🎲 Événements aléatoires par secteur (toutes les 48-72h)
  2. 🤝 Contrats inter-secteurs (/proposercontrat, /acceptercontrat, /mescontrats)
  3. 🏆 Classement global + récompenses hebdo top 3 PDG (/classement)
  4. 🎓 Bonus revenus selon diplôme du PDG dans le bon domaine
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, func, Column, Integer, BigInteger, String, Boolean, DateTime, Float, ForeignKey, Text, Date
from sqlalchemy.orm import DeclarativeBase
from telegram import Update
from telegram.ext import ContextTypes

from database.db import AsyncSessionLocal, get_user
from database.models import (
    Base, User, Company, CompanyEmployee, CompanyLog,
)

logger = logging.getLogger(__name__)

# ─── CONSTANTES SECTEUR ───────────────────────────────────────────────────────

SECTORS = {
    "tech":        ("💻", "Technologie"),
    "finance":     ("📈", "Finance"),
    "commerce":    ("🛒", "Commerce"),
    "droit":       ("⚖️",  "Droit"),
    "agriculture": ("🌾", "Agriculture"),
    "securite":    ("🛡️",  "Sécurité"),
    "immobilier":  ("🏗️",  "Immobilier"),
    "sante":       ("🏥", "Santé"),
}

# ─── SYSTÈME 4 : BONUS DIPLÔME PAR SECTEUR ───────────────────────────────────
# Domaine de diplôme qui donne le bonus dans chaque secteur
SECTOR_DOMAIN_BONUS = {
    "tech":        "informatique",
    "finance":     "finance",
    "commerce":    "marketing",
    "droit":       "droit",
    "agriculture": "agriculture",
    "securite":    "securite",
    "immobilier":  "management",
    "sante":       "management",
}

# Multiplicateur de revenus selon niveau diplôme dans le bon domaine
DIPLOMA_BONUS_RATE = {
    "bac":     0.00,   # +0%
    "licence": 0.05,   # +5%
    "master":  0.10,   # +10%
    "mba":     0.15,   # +15%
}

def get_diploma_bonus(pdg: User, sector: str) -> float:
    """Retourne le multiplicateur de bonus revenus pour le PDG selon son diplôme/domaine."""
    required_domain = SECTOR_DOMAIN_BONUS.get(sector)
    if not required_domain or pdg.diplome_domain != required_domain:
        return 0.0
    if pdg.diplome_mba:     return DIPLOMA_BONUS_RATE["mba"]
    if pdg.diplome_master:  return DIPLOMA_BONUS_RATE["master"]
    if pdg.diplome_licence: return DIPLOMA_BONUS_RATE["licence"]
    if pdg.diplome_bac:     return DIPLOMA_BONUS_RATE["bac"]
    return 0.0

# ─── SYSTÈME 1 : ÉVÉNEMENTS ALÉATOIRES PAR SECTEUR ───────────────────────────

# Format : (type, emoji, titre, description, effet)
# effet : dict avec clés "value_pct", "treasury_pct", "reputation"
# Les valeurs sont des pourcentages ou montants absolus
SECTOR_EVENTS = {
    "tech": [
        # Positifs
        ("boom",   "🚀", "Levée de fonds",        "Un investisseur injecte des capitaux !",                {"value_pct": +0.15, "treasury_pct": 0,     "reputation": +0.2}),
        ("boom",   "🤖", "Contrat IA signé",       "Un grand groupe vous confie son projet IA.",            {"value_pct": +0.10, "treasury_pct": +0.08,  "reputation": +0.1}),
        ("boom",   "📱", "Lancement produit viral", "Votre application cartonne sur les stores !",          {"value_pct": +0.08, "treasury_pct": +0.05,  "reputation": +0.15}),
        # Négatifs
        ("crise",  "🔓", "Cyberattaque",            "Des hackers ont vidé une partie de la trésorerie.",    {"value_pct": -0.05, "treasury_pct": -0.20,  "reputation": -0.3}),
        ("crise",  "⚠️", "Fuite de données",        "Un scandale de données éclate dans la presse.",        {"value_pct": -0.08, "treasury_pct": 0,      "reputation": -0.4}),
        ("crise",  "📉", "Bug critique en prod",    "Un bug majeur paralyse vos services pendant 24h.",     {"value_pct": -0.06, "treasury_pct": -0.10,  "reputation": -0.2}),
    ],
    "finance": [
        ("boom",   "📈", "Bull market",             "Les marchés s'envolent, vos actifs progressent !",     {"value_pct": +0.12, "treasury_pct": +0.10,  "reputation": +0.1}),
        ("boom",   "🏦", "Partenariat bancaire",    "Une grande banque vous choisit comme partenaire.",     {"value_pct": +0.08, "treasury_pct": +0.12,  "reputation": +0.2}),
        ("boom",   "💹", "Dividendes exceptionnels","Vos portefeuilles surperforment ce trimestre.",        {"value_pct": +0.06, "treasury_pct": +0.15,  "reputation": +0.1}),
        ("crise",  "📉", "Krach boursier",          "Les marchés s'effondrent, votre valeur chute.",        {"value_pct": -0.15, "treasury_pct": -0.05,  "reputation": -0.2}),
        ("crise",  "🕵️", "Contrôle fiscal",         "Le fisc enquête sur vos comptes.",                     {"value_pct": -0.05, "treasury_pct": -0.15,  "reputation": -0.3}),
        ("crise",  "❄️", "Gel des actifs",          "Une procédure judiciaire bloque temporairement vos fonds.",{"value_pct": -0.08, "treasury_pct": -0.12, "reputation": -0.2}),
    ],
    "commerce": [
        ("boom",   "🛍️", "Saison des fêtes",        "Les ventes explosent pendant les fêtes !",             {"value_pct": +0.10, "treasury_pct": +0.18,  "reputation": +0.1}),
        ("boom",   "🌍", "Expansion internationale","Vous ouvrez un nouveau marché à l'export.",            {"value_pct": +0.12, "treasury_pct": +0.08,  "reputation": +0.2}),
        ("boom",   "⭐", "Viral sur les réseaux",   "Une campagne marketing devient virale.",                {"value_pct": +0.07, "treasury_pct": +0.10,  "reputation": +0.3}),
        ("crise",  "🔥", "Incendie en entrepôt",    "Un incendie détruit une partie de vos stocks.",        {"value_pct": -0.08, "treasury_pct": -0.18,  "reputation": -0.2}),
        ("crise",  "🚫", "Rappel de produits",      "Un lot défectueux provoque un rappel massif.",          {"value_pct": -0.06, "treasury_pct": -0.12,  "reputation": -0.4}),
        ("crise",  "⛽", "Crise logistique",        "La hausse des coûts de transport grève vos marges.",   {"value_pct": -0.05, "treasury_pct": -0.08,  "reputation": -0.1}),
    ],
    "droit": [
        ("boom",   "⚖️", "Procès emblématique gagné","Vous remportez un dossier historique !",              {"value_pct": +0.10, "treasury_pct": +0.20,  "reputation": +0.4}),
        ("boom",   "🤝", "Gros client gouvernemental","L'État vous confie un dossier stratégique.",         {"value_pct": +0.08, "treasury_pct": +0.15,  "reputation": +0.3}),
        ("boom",   "🏅", "Prix d'excellence juridique","Votre cabinet est classé meilleur du pays.",        {"value_pct": +0.05, "treasury_pct": 0,       "reputation": +0.5}),
        ("crise",  "😤", "Conflit d'intérêts",      "Un conflit d'intérêts éclate, la presse s'en mêle.",  {"value_pct": -0.08, "treasury_pct": 0,       "reputation": -0.5}),
        ("crise",  "💼", "Procès perdu",             "Vous perdez un dossier majeur face à la partie adverse.",{"value_pct": -0.06, "treasury_pct": -0.10, "reputation": -0.3}),
        ("crise",  "🔍", "Enquête déontologique",   "L'ordre des avocats ouvre une enquête sur vos pratiques.",{"value_pct": -0.05, "treasury_pct": 0,    "reputation": -0.4}),
    ],
    "agriculture": [
        ("boom",   "🌦️", "Saison exceptionnelle",   "Les conditions météo sont parfaites, récolte record!", {"value_pct": +0.12, "treasury_pct": +0.20,  "reputation": +0.2}),
        ("boom",   "🌿", "Label bio obtenu",         "Vous obtenez la certification bio tant attendue.",     {"value_pct": +0.08, "treasury_pct": 0,       "reputation": +0.4}),
        ("boom",   "🚜", "Subvention agricole",      "L'État vous octroie une subvention agricole.",         {"value_pct": +0.05, "treasury_pct": +0.15,  "reputation": +0.1}),
        ("crise",  "🌵", "Sécheresse",               "La sécheresse dévaste vos cultures cette saison.",     {"value_pct": -0.12, "treasury_pct": -0.15,  "reputation": -0.1}),
        ("crise",  "🐛", "Invasion de parasites",    "Une invasion de nuisibles détruit une partie des récoltes.",{"value_pct": -0.08, "treasury_pct": -0.10, "reputation": -0.2}),
        ("crise",  "🌊", "Inondations",              "Des inondations endommagent vos terres agricoles.",    {"value_pct": -0.10, "treasury_pct": -0.12,  "reputation": -0.1}),
    ],
    "securite": [
        ("boom",   "🏅", "Contrat gouvernemental",   "Vous sécurisez un contrat avec les autorités.",       {"value_pct": +0.10, "treasury_pct": +0.18,  "reputation": +0.3}),
        ("boom",   "🛡️", "Certification sécurité",   "Vous obtenez la plus haute certification du secteur.",{"value_pct": +0.07, "treasury_pct": 0,       "reputation": +0.5}),
        ("boom",   "🤝", "Partenariat entreprises",  "Plusieurs grandes entreprises vous font confiance.",   {"value_pct": +0.08, "treasury_pct": +0.12,  "reputation": +0.2}),
        ("crise",  "😱", "Incident de sécurité",     "Un incident embarrassant ternit votre réputation.",   {"value_pct": -0.08, "treasury_pct": 0,       "reputation": -0.5}),
        ("crise",  "📰", "Enquête journalistique",   "Des journalistes enquêtent sur vos méthodes.",        {"value_pct": -0.05, "treasury_pct": 0,       "reputation": -0.4}),
        ("crise",  "⚖️", "Procès en responsabilité", "Un client vous attaque en justice après un incident.", {"value_pct": -0.07, "treasury_pct": -0.15,  "reputation": -0.3}),
    ],
    "immobilier": [
        ("boom",   "🏙️", "Boom immobilier",          "Les prix s'envolent, votre portefeuille progresse !", {"value_pct": +0.15, "treasury_pct": +0.10,  "reputation": +0.1}),
        ("boom",   "🏗️", "Grand projet urbain",      "Vous remportez un appel d'offres municipal.",         {"value_pct": +0.10, "treasury_pct": +0.20,  "reputation": +0.2}),
        ("boom",   "🌟", "Promotion résidentielle",  "Votre résidence haut de gamme se vend en 48h.",       {"value_pct": +0.08, "treasury_pct": +0.18,  "reputation": +0.3}),
        ("crise",  "📉", "Crise immobilière",        "Le marché se retourne, vos biens perdent de la valeur.",{"value_pct": -0.15, "treasury_pct": -0.05,  "reputation": -0.1}),
        ("crise",  "🏚️", "Malfaçons découvertes",    "Des défauts de construction sont révélés au public.", {"value_pct": -0.08, "treasury_pct": -0.20,  "reputation": -0.5}),
        ("crise",  "📋", "Permis de construire refusé","La mairie refuse vos permis de construire.",         {"value_pct": -0.06, "treasury_pct": 0,       "reputation": -0.2}),
    ],
    "sante": [
        ("boom",   "💊", "Médicament approuvé",      "Votre nouveau traitement reçoit son autorisation !",  {"value_pct": +0.15, "treasury_pct": +0.10,  "reputation": +0.4}),
        ("boom",   "🏥", "Contrat hôpital public",   "Vous signez un partenariat avec un CHU.",             {"value_pct": +0.10, "treasury_pct": +0.15,  "reputation": +0.3}),
        ("boom",   "🔬", "Percée scientifique",      "Vos chercheurs publient une étude mondiale.",          {"value_pct": +0.08, "treasury_pct": 0,       "reputation": +0.5}),
        ("crise",  "⚠️", "Scandale sanitaire",       "Un scandale sanitaire éclabousse votre groupe.",      {"value_pct": -0.12, "treasury_pct": 0,       "reputation": -0.6}),
        ("crise",  "💉", "Rappel de médicament",     "Un lot de médicaments défectueux est rappelé.",       {"value_pct": -0.08, "treasury_pct": -0.15,  "reputation": -0.4}),
        ("crise",  "🕵️", "Inspection sanitaire",     "Les autorités ouvrent une inspection de vos sites.",  {"value_pct": -0.05, "treasury_pct": 0,       "reputation": -0.2}),
    ],
}

# Secteurs compatibles pour les contrats (paires)
CONTRACT_COMPATIBLE = [
    ("tech",        "finance"),
    ("tech",        "securite"),
    ("tech",        "sante"),
    ("tech",        "commerce"),
    ("finance",     "immobilier"),
    ("finance",     "commerce"),
    ("commerce",    "agriculture"),
    ("commerce",    "immobilier"),
    ("sante",       "agriculture"),
    ("securite",    "immobilier"),
    ("droit",       "finance"),
    ("droit",       "immobilier"),
    ("droit",       "securite"),
]

def sectors_compatible(s1: str, s2: str) -> bool:
    return (s1, s2) in CONTRACT_COMPATIBLE or (s2, s1) in CONTRACT_COMPATIBLE

# ─── MODÈLES DB ──────────────────────────────────────────────────────────────

class CompanySectorEvent(Base):
    """Historique des événements sectoriels déclenchés."""
    __tablename__ = "company_sector_events"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    company_id   = Column(Integer, ForeignKey("companies.id"), nullable=False)
    event_type   = Column(String(20), nullable=False)   # boom / crise
    title        = Column(String(100), nullable=False)
    description  = Column(String(300), nullable=False)
    value_change = Column(BigInteger, default=0)
    treasury_change = Column(BigInteger, default=0)
    reputation_change = Column(Float, default=0.0)
    created_at   = Column(DateTime, default=datetime.utcnow)


class CompanyContract(Base):
    """Contrat entre deux entreprises de secteurs compatibles."""
    __tablename__ = "company_contracts"
    id              = Column(Integer, primary_key=True, autoincrement=True)
    company_a_id    = Column(Integer, ForeignKey("companies.id"), nullable=False)
    company_b_id    = Column(Integer, ForeignKey("companies.id"), nullable=False)
    proposed_by     = Column(BigInteger, nullable=False)   # user_id du PDG proposant
    status          = Column(String(20), default="pending")  # pending/active/expired/refused
    bonus_rate      = Column(Float, default=0.05)   # +5% revenus pour les deux
    duration_days   = Column(Integer, default=7)
    created_at      = Column(DateTime, default=datetime.utcnow)
    accepted_at     = Column(DateTime, nullable=True)
    expires_at      = Column(DateTime, nullable=True)


class CompanyRankingReward(Base):
    """Historique des récompenses hebdomadaires de classement."""
    __tablename__ = "company_ranking_rewards"
    id          = Column(Integer, primary_key=True, autoincrement=True)
    week        = Column(String(10), nullable=False)   # "2026-W01"
    company_id  = Column(Integer, ForeignKey("companies.id"), nullable=False)
    owner_id    = Column(BigInteger, nullable=False)
    rank        = Column(Integer, nullable=False)      # 1, 2, 3
    reward      = Column(BigInteger, nullable=False)   # coins donnés
    created_at  = Column(DateTime, default=datetime.utcnow)


class CompanyDailySnapshot(Base):
    """Snapshot journalier de la valeur de chaque entreprise (pour calcul moyenne hebdo)."""
    __tablename__ = "company_daily_snapshots"
    id          = Column(Integer, primary_key=True, autoincrement=True)
    company_id  = Column(Integer, ForeignKey("companies.id"), nullable=False)
    owner_id    = Column(BigInteger, nullable=False)
    value       = Column(BigInteger, nullable=False)
    snap_date   = Column(Date, nullable=False)       # date du snapshot (UTC)


# ─── UTILITAIRES ─────────────────────────────────────────────────────────────

def _fmt(n: int) -> str:
    if n >= 1_000_000_000: return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:     return f"{n/1_000_000:.1f}M"
    if n >= 1_000:         return f"{n/1_000:.0f}K"
    return str(n)

async def _get_company_employees_active(session, company_id: int):
    """Retourne tous les employés actifs d'une entreprise."""
    rows = (await session.execute(
        select(CompanyEmployee).where(
            CompanyEmployee.company_id == company_id,
            CompanyEmployee.left_at == None,
        )
    )).scalars().all()
    return rows

async def _notify_all_employees(context, company: Company, message: str):
    """Envoie un DM à tous les employés actifs d'une entreprise."""
    async with AsyncSessionLocal() as session:
        emps = await _get_company_employees_active(session, company.id)
        for emp in emps:
            try:
                await context.bot.send_message(
                    chat_id=emp.user_id,
                    text=message,
                    parse_mode="HTML"
                )
            except Exception:
                pass

async def _add_log(session, company_id: int, event_type: str, description: str, amount: int = None):
    from database.models import CompanyLog
    log = CompanyLog(
        company_id=company_id,
        event_type=event_type,
        description=description,
        amount=amount,
    )
    session.add(log)

# ─── SYSTÈME 1 : JOB ÉVÉNEMENTS ALÉATOIRES (48-72h) ─────────────────────────

async def job_sector_events(context: ContextTypes.DEFAULT_TYPE):
    """
    Déclenche aléatoirement un événement sectoriel sur chaque entreprise active.
    Probabilité : ~40% de chance qu'un événement touche une entreprise à chaque run.
    Le job tourne toutes les 48h, avec un random entre 48-72h simulé via proba.
    """
    async with AsyncSessionLocal() as session:
        companies = (await session.execute(
            select(Company).where(Company.is_active == True)
        )).scalars().all()

        for company in companies:
            # 40% de chance d'avoir un événement (pour simuler 48-72h d'aléatoire)
            if random.random() > 0.40:
                continue

            sector = company.sector
            events = SECTOR_EVENTS.get(sector)
            if not events:
                continue

            # Choisir un événement aléatoire
            evt_type, emoji, title, description, effet = random.choice(events)

            # Calculer les impacts
            value_change    = int(company.value    * effet.get("value_pct", 0))
            treasury_change = int(company.treasury * effet.get("treasury_pct", 0)) if company.treasury > 0 else 0
            rep_change      = effet.get("reputation", 0.0)

            # Appliquer
            company.value    = max(1_000_000, company.value + value_change)
            company.treasury = max(0, company.treasury + treasury_change)
            company.reputation = max(0.0, min(5.0, company.reputation + rep_change))

            # Enregistrer l'événement
            event_row = CompanySectorEvent(
                company_id=company.id,
                event_type=evt_type,
                title=title,
                description=description,
                value_change=value_change,
                treasury_change=treasury_change,
                reputation_change=rep_change,
            )
            session.add(event_row)
            await _add_log(session, company.id, f"event_{evt_type}",
                           f"{emoji} {title} : {description}")

            # Construire le message de notification
            sign_v = "+" if value_change >= 0 else ""
            sign_t = "+" if treasury_change >= 0 else ""
            sign_r = "+" if rep_change >= 0 else ""

            impact_lines = []
            if value_change != 0:
                impact_lines.append(f"📊 Valeur : {sign_v}{_fmt(abs(value_change))} $")
            if treasury_change != 0:
                impact_lines.append(f"🏦 Trésorerie : {sign_t}{_fmt(abs(treasury_change))} $")
            if rep_change != 0:
                impact_lines.append(f"⭐ Réputation : {sign_r}{abs(rep_change):.1f}")

            impact_str = "\n".join(impact_lines) if impact_lines else "Aucun impact chiffré."

            if evt_type == "boom":
                header = f"🟢 <b>ÉVÉNEMENT POSITIF — {company.name}</b>"
            else:
                header = f"🔴 <b>ÉVÉNEMENT NÉGATIF — {company.name}</b>"

            sec_emoji, sec_name = SECTORS.get(sector, ("🏢", sector))
            msg = (
                f"{header}\n"
                f"{sec_emoji} Secteur : <b>{sec_name}</b>\n\n"
                f"{emoji} <b>{title}</b>\n"
                f"{description}\n\n"
                f"<b>Impact :</b>\n{impact_str}"
            )

            # Notifier tous les employés
            emps = await _get_company_employees_active(session, company.id)
            for emp in emps:
                try:
                    await context.bot.send_message(
                        chat_id=emp.user_id,
                        text=msg,
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

        await session.commit()
    logger.info("Job événements sectoriels terminé.")


# ─── SYSTÈME 2 : CONTRATS INTER-SECTEURS ─────────────────────────────────────

async def proposercontrat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /proposercontrat [nom_entreprise] [durée_jours?]
    Propose un contrat de partenariat à une autre entreprise compatible.
    """
    user = update.effective_user
    if not context.args:
        await update.message.reply_text(
            "❌ Usage : <code>/proposercontrat [nom entreprise] [durée en jours (optionnel, max 30)]</code>",
            parse_mode="HTML"
        )
        return

    # Parser durée optionnelle (dernier arg si c'est un nombre)
    args = context.args
    duration = 7  # défaut
    if args[-1].isdigit():
        duration = max(1, min(30, int(args[-1])))
        args = args[:-1]

    target_name = " ".join(args)

    async with AsyncSessionLocal() as session:
        # Trouver l'entreprise du proposant (cherche PDG/directeur en priorité pour éviter MultipleResultsFound)
        my_emp = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.user_id == user.id,
                CompanyEmployee.left_at == None,
                CompanyEmployee.role.in_(["pdg", "directeur"]),
            ).order_by(CompanyEmployee.role.desc()).limit(1)
        )).scalar_one_or_none()

        if not my_emp:
            await update.message.reply_text("❌ Seul le PDG ou Directeur peut proposer un contrat.")
            return

        my_company = await session.get(Company, my_emp.company_id)
        if not my_company or not my_company.is_active:
            await update.message.reply_text("❌ Ton entreprise est inactive.")
            return

        # Trouver l'entreprise cible
        target = (await session.execute(
            select(Company).where(Company.name.ilike(target_name), Company.is_active == True)
        )).scalar_one_or_none()

        if not target:
            await update.message.reply_text(f"❌ Entreprise <b>{target_name}</b> introuvable.", parse_mode="HTML")
            return

        if target.id == my_company.id:
            await update.message.reply_text("❌ Tu ne peux pas contracter avec ta propre entreprise.")
            return

        # Vérifier compatibilité secteurs
        if not sectors_compatible(my_company.sector, target.sector):
            my_sec = SECTORS.get(my_company.sector, ("🏢", my_company.sector))[1]
            tgt_sec = SECTORS.get(target.sector, ("🏢", target.sector))[1]
            await update.message.reply_text(
                f"❌ Les secteurs <b>{my_sec}</b> et <b>{tgt_sec}</b> ne sont pas compatibles pour un contrat.\n\n"
                f"💡 Secteurs compatibles : Tech↔Finance, Tech↔Sécurité, Finance↔Immobilier, Commerce↔Agriculture, etc.",
                parse_mode="HTML"
            )
            return

        # Vérifier contrat déjà en cours entre ces deux
        existing = (await session.execute(
            select(CompanyContract).where(
                CompanyContract.status.in_(["pending", "active"]),
                (
                    (CompanyContract.company_a_id == my_company.id) & (CompanyContract.company_b_id == target.id)
                ) | (
                    (CompanyContract.company_a_id == target.id) & (CompanyContract.company_b_id == my_company.id)
                )
            )
        )).scalar_one_or_none()

        if existing:
            await update.message.reply_text(
                f"❌ Un contrat existe déjà entre <b>{my_company.name}</b> et <b>{target.name}</b>.",
                parse_mode="HTML"
            )
            return

        # Bonus selon niveau des deux entreprises combinés
        avg_level = (my_company.level + target.level) / 2
        bonus_rate = round(0.03 + (avg_level * 0.01), 3)  # 4% à 8% selon niveaux

        contract = CompanyContract(
            company_a_id=my_company.id,
            company_b_id=target.id,
            proposed_by=user.id,
            status="pending",
            bonus_rate=bonus_rate,
            duration_days=duration,
        )
        session.add(contract)
        await session.flush()

        my_sec_emoji, my_sec_name = SECTORS.get(my_company.sector, ("🏢", my_company.sector))
        tgt_sec_emoji, tgt_sec_name = SECTORS.get(target.sector, ("🏢", target.sector))

        await update.message.reply_text(
            f"📤 <b>Proposition de contrat envoyée !</b>\n\n"
            f"🏢 <b>{my_company.name}</b> ({my_sec_emoji} {my_sec_name})\n"
            f"🤝 → <b>{target.name}</b> ({tgt_sec_emoji} {tgt_sec_name})\n\n"
            f"📈 Bonus revenus : <b>+{int(bonus_rate*100)}%</b> pour les deux entreprises\n"
            f"⏳ Durée : <b>{duration} jours</b>\n\n"
            f"Le PDG de {target.name} doit accepter avec :\n"
            f"<code>/acceptercontrat {contract.id}</code>",
            parse_mode="HTML"
        )

        # Notifier le PDG de la cible
        try:
            await context.bot.send_message(
                chat_id=target.owner_id,
                text=(
                    f"🤝 <b>Proposition de contrat reçue !</b>\n\n"
                    f"<b>{my_company.name}</b> ({my_sec_emoji} {my_sec_name}) propose un partenariat avec <b>{target.name}</b>.\n\n"
                    f"📈 Bonus revenus : <b>+{int(bonus_rate*100)}%</b> pour les deux\n"
                    f"⏳ Durée : <b>{duration} jours</b>\n\n"
                    f"✅ Accepter : <code>/acceptercontrat {contract.id}</code>\n"
                    f"❌ Refuser : <code>/refusercontrat {contract.id}</code>"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass

        await session.commit()


async def acceptercontrat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /acceptercontrat [id_contrat]
    Le PDG de l'entreprise cible accepte le contrat.
    """
    user = update.effective_user
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("❌ Usage : <code>/acceptercontrat [id_contrat]</code>", parse_mode="HTML")
        return

    contract_id = int(context.args[0])

    async with AsyncSessionLocal() as session:
        contract = await session.get(CompanyContract, contract_id)
        if not contract or contract.status != "pending":
            await update.message.reply_text("❌ Contrat introuvable ou déjà traité.")
            return

        company_b = await session.get(Company, contract.company_b_id)
        if not company_b or company_b.owner_id != user.id:
            # Vérifier aussi si c'est un directeur de l'entreprise B
            emp = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == contract.company_b_id,
                    CompanyEmployee.user_id == user.id,
                    CompanyEmployee.left_at == None,
                    CompanyEmployee.role.in_(["pdg", "directeur"]),
                )
            )
            ).limit(1).scalar_one_or_none()
            if not emp:
                await update.message.reply_text("❌ Seul le PDG ou Directeur de l'entreprise concernée peut accepter.")
                return

        company_a = await session.get(Company, contract.company_a_id)

        contract.status = "active"
        contract.accepted_at = datetime.utcnow()
        contract.expires_at = datetime.utcnow() + timedelta(days=contract.duration_days)

        await _add_log(session, company_a.id, "contrat", f"Contrat signé avec {company_b.name} (+{int(contract.bonus_rate*100)}%/{contract.duration_days}j)")
        await _add_log(session, company_b.id, "contrat", f"Contrat signé avec {company_a.name} (+{int(contract.bonus_rate*100)}%/{contract.duration_days}j)")
        await session.commit()

        sec_a = SECTORS.get(company_a.sector, ("🏢", company_a.sector))
        sec_b = SECTORS.get(company_b.sector, ("🏢", company_b.sector))

        confirmation = (
            f"✅ <b>Contrat signé !</b>\n\n"
            f"{sec_a[0]} <b>{company_a.name}</b> ↔ {sec_b[0]} <b>{company_b.name}</b>\n\n"
            f"📈 Bonus revenus : <b>+{int(contract.bonus_rate*100)}%</b> pour les deux entreprises\n"
            f"⏳ Valable jusqu'au : <b>{contract.expires_at.strftime('%d/%m/%Y')}</b>\n\n"
            f"💡 Voir vos contrats actifs : <code>/mescontrats</code>"
        )
        await update.message.reply_text(confirmation, parse_mode="HTML")

        # Notifier le PDG de l'entreprise A
        try:
            await context.bot.send_message(
                chat_id=company_a.owner_id,
                text=(
                    f"🎉 <b>Contrat accepté !</b>\n\n"
                    f"<b>{company_b.name}</b> a accepté votre proposition de partenariat.\n"
                    f"📈 Bonus : <b>+{int(contract.bonus_rate*100)}%</b> sur vos revenus pendant {contract.duration_days} jours !"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass


async def refusercontrat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /refusercontrat [id_contrat]
    """
    user = update.effective_user
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("❌ Usage : <code>/refusercontrat [id_contrat]</code>", parse_mode="HTML")
        return

    contract_id = int(context.args[0])

    async with AsyncSessionLocal() as session:
        contract = await session.get(CompanyContract, contract_id)
        if not contract or contract.status != "pending":
            await update.message.reply_text("❌ Contrat introuvable ou déjà traité.")
            return

        company_b = await session.get(Company, contract.company_b_id)
        if not company_b or company_b.owner_id != user.id:
            emp = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == contract.company_b_id,
                    CompanyEmployee.user_id == user.id,
                    CompanyEmployee.left_at == None,
                    CompanyEmployee.role.in_(["pdg", "directeur"]),
                )
            )
            ).limit(1).scalar_one_or_none()
            if not emp:
                await update.message.reply_text("❌ Seul le PDG ou Directeur peut refuser.")
                return

        company_a = await session.get(Company, contract.company_a_id)
        contract.status = "refused"
        await session.commit()

        await update.message.reply_text(f"✅ Contrat avec <b>{company_a.name}</b> refusé.", parse_mode="HTML")

        try:
            await context.bot.send_message(
                chat_id=company_a.owner_id,
                text=f"😔 <b>{company_b.name}</b> a refusé votre proposition de contrat.",
                parse_mode="HTML"
            )
        except Exception:
            pass


async def mescontrats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /mescontrats — Voir les contrats actifs/en attente de ton entreprise.
    """
    user = update.effective_user

    async with AsyncSessionLocal() as session:
        emp = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.user_id == user.id,
                CompanyEmployee.left_at == None,
            )
        )
        ).limit(1).scalar_one_or_none()

        if not emp:
            await update.message.reply_text("❌ Tu ne fais partie d'aucune entreprise.")
            return

        company = await session.get(Company, emp.company_id)
        if not company:
            return

        contracts = (await session.execute(
            select(CompanyContract).where(
                CompanyContract.status.in_(["pending", "active"]),
                (CompanyContract.company_a_id == company.id) | (CompanyContract.company_b_id == company.id)
            ).order_by(CompanyContract.created_at.desc())
        )).scalars().all()

        if not contracts:
            await update.message.reply_text(
                f"📭 <b>{company.name}</b> n'a aucun contrat actif.\n\n"
                f"💡 Propose un contrat avec <code>/proposercontrat [nom entreprise]</code>",
                parse_mode="HTML"
            )
            return

        lines = [f"📋 <b>Contrats — {company.name}</b>\n"]
        for c in contracts:
            other_id = c.company_b_id if c.company_a_id == company.id else c.company_a_id
            other = await session.get(Company, other_id)
            other_name = other.name if other else "?"
            other_sec = SECTORS.get(other.sector, ("🏢", "?"))[0] if other else "🏢"

            status_emoji = "✅" if c.status == "active" else "⏳"
            expires = c.expires_at.strftime("%d/%m/%Y") if c.expires_at else "—"
            role = "Proposant" if c.company_a_id == company.id else "Partenaire"

            lines.append(
                f"{status_emoji} <b>#{c.id}</b> | {other_sec} {other_name} ({role})\n"
                f"   📈 +{int(c.bonus_rate*100)}% revenus · ⏳ expire le {expires}\n"
            )

        lines.append("\n💡 <code>/proposercontrat [nom]</code> pour un nouveau contrat")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def job_expire_contracts(context: ContextTypes.DEFAULT_TYPE):
    """Expire les contrats dépassés et notifie les PDG."""
    async with AsyncSessionLocal() as session:
        expired = (await session.execute(
            select(CompanyContract).where(
                CompanyContract.status == "active",
                CompanyContract.expires_at <= datetime.utcnow(),
            )
        )).scalars().all()

        for c in expired:
            c.status = "expired"
            company_a = await session.get(Company, c.company_a_id)
            company_b = await session.get(Company, c.company_b_id)

            for co in [company_a, company_b]:
                if not co:
                    continue
                try:
                    await context.bot.send_message(
                        chat_id=co.owner_id,
                        text=(
                            f"⏰ <b>Contrat expiré</b>\n\n"
                            f"Le partenariat entre <b>{company_a.name if company_a else '?'}</b> "
                            f"et <b>{company_b.name if company_b else '?'}</b> est arrivé à terme.\n"
                            f"💡 Renouvelez-le avec <code>/proposercontrat</code>"
                        ),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

        if expired:
            await session.commit()


def get_contract_bonus(company_id: int, contracts) -> float:
    """Retourne le bonus total de revenus des contrats actifs pour une entreprise."""
    total = 0.0
    for c in contracts:
        if c.status == "active" and (c.company_a_id == company_id or c.company_b_id == company_id):
            if c.expires_at and datetime.utcnow() < c.expires_at:
                total += c.bonus_rate
    return total


# ─── SYSTÈME 3 : CLASSEMENT GLOBAL + RÉCOMPENSES HEBDO ───────────────────────

RANKING_REWARDS = {
    1: (50_000_000, "🥇"),
    2: (25_000_000, "🥈"),
    3: (10_000_000, "🥉"),
}

async def classement_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /classement          → classement global toutes entreprises
    /classement [secteur] → filtré par secteur
    """
    sector_filter = context.args[0].lower() if context.args else None

    async with AsyncSessionLocal() as session:
        query = select(Company).where(Company.is_active == True)
        if sector_filter:
            if sector_filter not in SECTORS:
                sectors_list = " | ".join(SECTORS.keys())
                await update.message.reply_text(
                    f"❌ Secteur invalide.\nChoix : {sectors_list}",
                    parse_mode="HTML"
                )
                return
            query = query.where(Company.sector == sector_filter)

        companies = (await session.execute(
            query.order_by(Company.value.desc()).limit(20)
        )).scalars().all()

        if not companies:
            await update.message.reply_text("❌ Aucune entreprise trouvée.")
            return

        if sector_filter:
            sec_emoji, sec_name = SECTORS[sector_filter]
            title = f"🏆 CLASSEMENT — {sec_emoji} {sec_name}"
        else:
            title = "🏆 CLASSEMENT GLOBAL DES ENTREPRISES"

        lines = [
            f"╔══════════════════════════════╗",
            f"║  {title[:28]:<28}  ║" if len(title) <= 28 else f"║  {title}",
            f"╚══════════════════════════════╝\n",
        ]

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}

        for i, c in enumerate(companies, 1):
            sec_emoji, _ = SECTORS.get(c.sector, ("🏢", c.sector))
            lvl_info = {1: "🏪", 2: "🏢", 3: "🏬", 4: "🏦", 5: "👑"}
            lvl_emoji = lvl_info.get(c.level, "🏢")
            bot_tag = " 🤖" if c.is_bot_company else ""
            medal = medals.get(i, f"{i}.")

            # PDG
            if c.is_bot_company:
                pdg_name = "Bot"
            else:
                pdg = await session.get(User, c.owner_id)
                pdg_name = pdg.first_name if pdg else "?"

            lines.append(
                f"{medal} {sec_emoji} <b>{c.name}</b>{bot_tag}\n"
                f"   {lvl_emoji} · 💰 {_fmt(c.value)} · ⭐ {c.reputation:.1f} · 👤 {pdg_name}\n"
            )

        # Prochaine récompense
        now = datetime.utcnow()
        days_until_sunday = (6 - now.weekday()) % 7
        if days_until_sunday == 0:
            days_until_sunday = 7
        lines.append(f"\n🎁 Récompenses hebdo dans <b>{days_until_sunday} jour(s)</b>")
        lines.append(f"🥇 +50M$ · 🥈 +25M$ · 🥉 +10M$ pour les PDG du top 3")
        if not sector_filter:
            lines.append(f"\n💡 <code>/classement [secteur]</code> pour filtrer par secteur")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def job_weekly_ranking_reward(context: ContextTypes.DEFAULT_TYPE):
    """
    Job hebdomadaire (dimanche) : récompense les PDG du top 3 basé sur
    la MOYENNE des snapshots journaliers de la semaine (hors entreprises bot).
    """
    week_str = datetime.utcnow().strftime("%Y-W%W")

    async with AsyncSessionLocal() as session:
        # Vérifier si déjà distribué cette semaine
        already = (await session.execute(
            select(CompanyRankingReward).where(CompanyRankingReward.week == week_str)
        )).first()
        if already:
            logger.info(f"Récompenses classement semaine {week_str} déjà distribuées.")
            return

        # Calculer la moyenne des snapshots de la semaine (lundi → dimanche)
        from datetime import date as date_type
        today = datetime.utcnow().date()
        week_start = today - timedelta(days=today.weekday())  # Lundi

        avg_rows = (await session.execute(
            select(
                CompanyDailySnapshot.company_id,
                CompanyDailySnapshot.owner_id,
                func.avg(CompanyDailySnapshot.value).label("avg_value"),
            ).where(
                CompanyDailySnapshot.snap_date >= week_start,
            ).group_by(
                CompanyDailySnapshot.company_id,
                CompanyDailySnapshot.owner_id,
            ).order_by(func.avg(CompanyDailySnapshot.value).desc()).limit(10)
        )).all()

        # Filtrer les bot companies et prendre top 3
        top3 = []
        for row in avg_rows:
            cid = row[0]
            company = await session.get(Company, cid)
            if company and company.is_active and not company.is_bot_company:
                company._avg_value = int(row[2])
                top3.append(company)
            if len(top3) == 3:
                break

        # Fallback si pas de snapshots cette semaine
        if not top3:
            top3_raw = (await session.execute(
                select(Company).where(
                    Company.is_active == True,
                    Company.is_bot_company == False,
                ).order_by(Company.value.desc()).limit(3)
            )).scalars().all()
            for c in top3_raw:
                c._avg_value = c.value
                top3.append(c)

        if not top3:
            return

        results_msg_lines = ["🏆 <b>CLASSEMENT HEBDOMADAIRE — RÉCOMPENSES</b>\n"]

        for rank, company in enumerate(top3, 1):
            reward_coins, medal = RANKING_REWARDS[rank]

            # Donner les coins au PDG
            pdg = await session.get(User, company.owner_id)
            if pdg:
                pdg.coins += reward_coins

            # Enregistrer
            session.add(CompanyRankingReward(
                week=week_str,
                company_id=company.id,
                owner_id=company.owner_id,
                rank=rank,
                reward=reward_coins,
            ))

            sec_emoji, sec_name = SECTORS.get(company.sector, ("🏢", company.sector))
            pdg_name = pdg.first_name if pdg else "?"

            results_msg_lines.append(
                f"{medal} <b>{company.name}</b> ({sec_emoji} {sec_name})\n"
                f"   👤 PDG : {pdg_name}\n"
                f"   💰 Moy. semaine : {_fmt(getattr(company, '_avg_value', company.value))} $\n"
                f"   🎁 Récompense : +{_fmt(reward_coins)} $\n"
            )

            await _add_log(session, company.id, "recompense_classement",
                           f"Top {rank} hebdomadaire — PDG récompensé de {_fmt(reward_coins)} $",
                           amount=reward_coins)

            # Notifier le PDG
            if pdg:
                try:
                    await context.bot.send_message(
                        chat_id=pdg.user_id,
                        text=(
                            f"{medal} <b>Félicitations !</b>\n\n"
                            f"<b>{company.name}</b> termine <b>#{rank}</b> au classement hebdomadaire !\n\n"
                            f"💰 Tu reçois <b>{_fmt(reward_coins)} $</b> en récompense.\n"
                            f"Continue à développer ton empire ! 🚀"
                        ),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

        await session.commit()

        # Annoncer les résultats dans le groupe (si group_id connu des top entreprises)
        broadcast_msg = "\n".join(results_msg_lines)
        # On notifie aussi tous les employés des top 3 via DM
        for company in top3:
            emps = await _get_company_employees_active(session, company.id)
            for emp in emps:
                if emp.user_id == company.owner_id:
                    continue  # PDG déjà notifié
                try:
                    await context.bot.send_message(
                        chat_id=emp.user_id,
                        text=(
                            f"🎉 Ton entreprise <b>{company.name}</b> est dans le <b>Top 3</b> cette semaine !\n"
                            f"Le PDG a reçu sa récompense. 💪"
                        ),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

    logger.info(f"Récompenses classement semaine {week_str} distribuées.")


# ─── SYSTÈME 4 : INTÉGRATION BONUS DIPLÔME DANS LES REVENUS ─────────────────
# Cette fonction est appelée depuis company.py dans job_company_revenues
# pour calculer le bonus additionnel.

async def get_all_active_contracts(session) -> list:
    """Retourne tous les contrats actifs (pour calcul bonus revenus)."""
    contracts = (await session.execute(
        select(CompanyContract).where(
            CompanyContract.status == "active",
            CompanyContract.expires_at > datetime.utcnow(),
        )
    )).scalars().all()
    return contracts



# ─── JOB : SNAPSHOT JOURNALIER + CLASSEMENT QUOTIDIEN (18h) ─────────────────

async def job_daily_ranking_broadcast(context: ContextTypes.DEFAULT_TYPE):
    """
    Job quotidien à 18h :
      1. Prend un snapshot de la valeur de toutes les entreprises actives (pour la moyenne hebdo)
      2. Envoie le top 10 du classement dans chaque groupe actif
    """
    from datetime import date as date_type
    from database.models import BotGroup

    today = datetime.utcnow().date()

    async with AsyncSessionLocal() as session:
        # ── 1. Snapshot journalier ──────────────────────────────────────────
        companies = (await session.execute(
            select(Company).where(Company.is_active == True, Company.is_bot_company == False)
        )).scalars().all()

        for company in companies:
            # Vérifier si déjà snapshoté aujourd'hui
            existing = (await session.execute(
                select(CompanyDailySnapshot).where(
                    CompanyDailySnapshot.company_id == company.id,
                    CompanyDailySnapshot.snap_date == today,
                )
            )).scalar_one_or_none()

            if not existing:
                session.add(CompanyDailySnapshot(
                    company_id=company.id,
                    owner_id=company.owner_id,
                    value=company.value,
                    snap_date=today,
                ))

        await session.flush()

        # ── 2. Construire le message classement ────────────────────────────
        top10 = (await session.execute(
            select(Company).where(Company.is_active == True)
            .order_by(Company.value.desc()).limit(10)
        )).scalars().all()

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lvl_icons = {1: "🏪", 2: "🏢", 3: "🏬", 4: "🏦", 5: "👑"}

        now = datetime.utcnow()
        days_until_sunday = (6 - now.weekday()) % 7
        if days_until_sunday == 0:
            days_until_sunday = 7

        lines = [
            f"🏆 <b>CLASSEMENT ENTREPRISES — {now.strftime('%d/%m/%Y')}</b>\n",
        ]

        for i, c in enumerate(top10, 1):
            sec_emoji = SECTORS.get(c.sector, ("🏢",))[0]
            lvl_emoji = lvl_icons.get(c.level, "🏢")
            medal = medals.get(i, f"{i}.")
            bot_tag = " 🤖" if c.is_bot_company else ""

            if c.is_bot_company:
                pdg_name = "Bot"
            else:
                pdg = await session.get(User, c.owner_id)
                pdg_name = pdg.first_name if pdg else "?"

            lines.append(
                f"{medal} {sec_emoji} <b>{c.name}</b>{bot_tag}\n"
                f"   {lvl_emoji} {_fmt(c.value)} $ · ⭐ {c.reputation:.1f} · 👤 {pdg_name}"
            )

        lines.append(
            f"\n🎁 Récompenses dans <b>{days_until_sunday} jour(s)</b> (moyenne de la semaine)\n"
            f"🥇 +50M$ · 🥈 +25M$ · 🥉 +10M$\n"
            f"💡 <code>/classement [secteur]</code> pour filtrer"
        )

        ranking_msg = "\n".join(lines)

        # ── 3. Broadcast dans tous les groupes actifs ──────────────────────
        groups = (await session.execute(
            select(BotGroup).where(BotGroup.is_active == True)
        )).scalars().all()

        await session.commit()

    for group in groups:
        try:
            await context.bot.send_message(
                chat_id=group.group_id,
                text=ranking_msg,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.debug(f"Impossible d'envoyer le classement au groupe {group.group_id}: {e}")
        await asyncio.sleep(1.5)  # délai anti-flood Telegram (max ~20 msg/30s)

    logger.info(f"Classement quotidien broadcasted dans {len(groups)} groupe(s).")

# ─── COMMANDE : /evenements [nom_entreprise?] ────────────────────────────────

async def evenements_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /evenements           → derniers événements de ton entreprise
    /evenements [nom]     → d'une entreprise spécifique
    """
    user = update.effective_user

    async with AsyncSessionLocal() as session:
        if context.args:
            name = " ".join(context.args)
            company = (await session.execute(
                select(Company).where(Company.name.ilike(name), Company.is_active == True)
            )).scalar_one_or_none()
            if not company:
                await update.message.reply_text(f"❌ Entreprise <b>{name}</b> introuvable.", parse_mode="HTML")
                return
        else:
            emp = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.user_id == user.id,
                    CompanyEmployee.left_at == None,
                )
            )
            ).limit(1).scalar_one_or_none()
            if not emp:
                await update.message.reply_text("❌ Tu ne fais partie d'aucune entreprise.")
                return
            company = await session.get(Company, emp.company_id)

        events = (await session.execute(
            select(CompanySectorEvent).where(
                CompanySectorEvent.company_id == company.id
            ).order_by(CompanySectorEvent.created_at.desc()).limit(10)
        )).scalars().all()

        if not events:
            await update.message.reply_text(
                f"📭 Aucun événement sectoriel pour <b>{company.name}</b> pour l'instant.",
                parse_mode="HTML"
            )
            return

        sec_emoji, sec_name = SECTORS.get(company.sector, ("🏢", company.sector))
        lines = [f"📋 <b>Événements sectoriels — {company.name}</b>\n{sec_emoji} {sec_name}\n"]

        for evt in events:
            sign = "🟢" if evt.event_type == "boom" else "🔴"
            date_str = evt.created_at.strftime("%d/%m %H:%M")
            impact = []
            if evt.value_change:    impact.append(f"val {'+' if evt.value_change>0 else ''}{_fmt(evt.value_change)}$")
            if evt.treasury_change: impact.append(f"tréso {'+' if evt.treasury_change>0 else ''}{_fmt(evt.treasury_change)}$")
            if evt.reputation_change: impact.append(f"rép {'+' if evt.reputation_change>0 else ''}{evt.reputation_change:.1f}")
            impact_str = " · ".join(impact) if impact else "—"
            lines.append(f"{sign} <code>{date_str}</code> <b>{evt.title}</b>\n   {impact_str}\n")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── INITIALISATION DES NOUVELLES TABLES ─────────────────────────────────────

async def init_sector_tables():
    """Crée les nouvelles tables sectorielles si elles n'existent pas."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from config import DATABASE_URL
    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    logger.info("Tables sectorielles initialisées.")
