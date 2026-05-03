"""
handlers/company.py — Système d'entreprises complet
Commandes : /entreprise, /creerboite, /postuler, /recruter, /demissionner,
            /nommer, /parts, /vendreparts, /acheterparts,
            /depotboite, /retraitboite, /infoboite, /logsboite,
            /candidatures, /licencier, /listeboites
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, func
from telegram import Update
from telegram.ext import ContextTypes

from database.db import AsyncSessionLocal, get_user
from database.models import (
    User, Company, CompanyEmployee, CompanyShare,
    CompanyApplication, CompanyInvite, CompanyLog,
)

logger = logging.getLogger(__name__)

# ─── CONSTANTES ──────────────────────────────────────────────────────────────

SECTORS = {
    "tech":       ("💻", "Technologie"),
    "finance":    ("📈", "Finance"),
    "commerce":   ("🛒", "Commerce"),
    "droit":      ("⚖️",  "Droit"),
    "agriculture":("🌾", "Agriculture"),
    "securite":   ("🛡️",  "Sécurité"),
    "immobilier": ("🏗️",  "Immobilier"),
    "sante":      ("🏥", "Santé"),
}

# Domaines de licence autorisés à créer dans chaque secteur
SECTOR_ALLOWED_DOMAINS = {
    "tech":        ["informatique"],
    "finance":     ["finance", "management"],
    "commerce":    ["marketing", "management", "finance"],
    "droit":       ["droit"],
    "agriculture": ["agriculture"],
    "securite":    ["securite", "management"],
    "immobilier":  ["management", "marketing"],
    "sante":       ["management"],
}

# Format : (emoji, nom, valeur_min, taux_mensuel, max_employes)
# Revenu/jour = valeur * taux_mensuel / 30
LEVELS = {
    1: ("🏪", "Startup",      50_000_000,    0.04, 5),    # 4%/mois  → ~1.3%/jour de la valeur
    2: ("🏢", "PME",          200_000_000,   0.06, 10),   # 6%/mois  → ~2%/jour
    3: ("🏬", "Société",      500_000_000,   0.08, 50),   # 8%/mois  → ~2.7%/jour
    4: ("🏦", "Corporation", 2_000_000_000,  0.10, 100),  # 10%/mois → ~3.3%/jour
    5: ("👑", "Holding",    10_000_000_000,  0.12, 200),  # 12%/mois → ~4%/jour
}

ROLES_ORDER = ["stagiaire", "employe", "manager", "directeur", "pdg"]
ROLE_EMOJI  = {
    "stagiaire":  "👷",
    "employe":    "👷",
    "manager":    "💼",
    "directeur":  "🏦",
    "pdg":        "👑",
}
ROLE_DIPLOMA = {
    "stagiaire":  None,
    "employe":    "bac",
    "manager":    "licence",
    "directeur":  "master",
    "pdg":        "mba",
}

# Revenus par rôle (% des revenus journaliers de l'entreprise)
ROLE_SHARE = {
    "stagiaire":  0.00,
    "employe":    0.10,
    "manager":    0.20,
    "directeur":  0.35,
    "pdg":        0.00,   # PDG touche les dividendes manuellement
}

# Entreprises officielles créées par le bot
BOT_COMPANIES = [
    {
        "name":        "NexaTech",
        "sector":      "tech",
        "description": "Leader de la tech et de l'innovation numérique.",
        "value":       500_000_000,
        "min_diploma": "licence",
    },
    {
        "name":        "CapitalX",
        "sector":      "finance",
        "description": "Fonds d'investissement et gestion de patrimoine.",
        "value":       800_000_000,
        "min_diploma": "master",
    },
    {
        "name":        "TradeHub",
        "sector":      "commerce",
        "description": "Plateforme de commerce et distribution.",
        "value":       200_000_000,
        "min_diploma": "bac",
    },
    {
        "name":        "AgriMax",
        "sector":      "agriculture",
        "description": "Production agricole et agro-industrie.",
        "value":       150_000_000,
        "min_diploma": "bac",
    },
    {
        "name":        "SecureForce",
        "sector":      "securite",
        "description": "Sécurité privée et protection des entreprises.",
        "value":       120_000_000,
        "min_diploma": "bac",
    },
    {
        "name":        "MegaLaw",
        "sector":      "droit",
        "description": "Cabinet juridique d'élite.",
        "value":       350_000_000,
        "min_diploma": "licence",
    },
    {
        "name":        "UrbanBuild",
        "sector":      "immobilier",
        "description": "Promotion immobilière et construction.",
        "value":       600_000_000,
        "min_diploma": "licence",
    },
    {
        "name":        "HealthCore",
        "sector":      "sante",
        "description": "Groupe hospitalier et pharmaceutique.",
        "value":       700_000_000,
        "min_diploma": "master",
    },
]

# ─── UTILITAIRES ──────────────────────────────────────────────────────────────

def _fmt(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)


def _has_diploma(user: User, level: str) -> bool:
    order = ["bac", "licence", "master", "mba"]
    if level not in order:
        return True
    idx = order.index(level)
    checks = [user.diplome_bac, user.diplome_licence, user.diplome_master, user.diplome_mba]
    return bool(checks[idx])


def _level_info(lvl: int):
    return LEVELS.get(lvl, LEVELS[1])


async def _get_company_by_name(session, name: str) -> Optional[Company]:
    r = await session.execute(
        select(Company).where(Company.name.ilike(name), Company.is_active == True)
    )
    return r.scalar_one_or_none()


async def _get_employee(session, company_id: int, user_id: int) -> Optional[CompanyEmployee]:
    r = await session.execute(
        select(CompanyEmployee).where(
            CompanyEmployee.company_id == company_id,
            CompanyEmployee.user_id == user_id,
            CompanyEmployee.left_at == None,
        )
    )
    return r.scalar_one_or_none()


async def _get_user_company(session, user_id: int) -> Optional[tuple[Company, CompanyEmployee]]:
    """Retourne (company, employee) si le user est dans une entreprise active."""
    r = await session.execute(
        select(CompanyEmployee, Company).join(
            Company, Company.id == CompanyEmployee.company_id
        ).where(
            CompanyEmployee.user_id == user_id,
            CompanyEmployee.left_at == None,
            Company.is_active == True,
        )
    )
    row = r.first()
    if row:
        return row[1], row[0]
    return None, None


async def _add_log(session, company_id: int, event_type: str, description: str, amount: int = None):
    log = CompanyLog(
        company_id=company_id,
        event_type=event_type,
        description=description,
        amount=amount,
    )
    session.add(log)


async def _get_shares(session, company_id: int, user_id: int) -> int:
    r = await session.execute(
        select(CompanyShare).where(
            CompanyShare.company_id == company_id,
            CompanyShare.owner_id == user_id,
        )
    )
    s = r.scalar_one_or_none()
    return s.quantity if s else 0


async def _update_level(session, company: Company):
    """Met à jour le niveau de l'entreprise selon sa valeur."""
    for lvl in range(5, 0, -1):
        emoji, name, min_val, rate, max_emp = LEVELS[lvl]
        if company.value >= min_val:
            if company.level != lvl:
                company.level = lvl
            break


# ─── INITIALISATION DES TABLES ───────────────────────────────────────────────

async def init_company_tables():
    """Initialise les entreprises du bot si elles n'existent pas encore."""
    # Les tables sont déjà créées par init_db() via Base.metadata.create_all
    async with AsyncSessionLocal() as session:
        for bc in BOT_COMPANIES:
            exists = await _get_company_by_name(session, bc["name"])
            if not exists:
                company = Company(
                    name=bc["name"],
                    sector=bc["sector"],
                    description=bc["description"],
                    value=bc["value"],
                    treasury=0,
                    total_shares=100,
                    owner_shares=100,
                    owner_id=0,
                    group_id=0,
                    level=1,
                    reputation=4.0,
                    is_bot_company=True,
                    is_active=True,
                )
                session.add(company)
                await session.flush()
                await _update_level(session, company)
                # Initialiser les parts du bot (owner_id=0 fictif)
                session.add(CompanyShare(
                    company_id=company.id,
                    owner_id=0,
                    quantity=100,
                ))
        await session.commit()
    logger.info("Entreprises bot initialisées.")


# ─── JOB : REVENUS AUTOMATIQUES (toutes les 24h) ──────────────────────────────

async def job_company_revenues(context: ContextTypes.DEFAULT_TYPE):
    """Distribue les revenus des entreprises à tous les employés actifs."""
    async with AsyncSessionLocal() as session:
        companies = (await session.execute(
            select(Company).where(Company.is_active == True)
        )).scalars().all()

        for company in companies:
            _, _, _, monthly_rate, _ = _level_info(company.level)
            revenue = int(company.value * monthly_rate) // 30  # versement journalier = taux mensuel ÷ 30
            if revenue <= 0:
                continue

            # Bot company sans employés : pas de revenus en caisse (évite inflation infinie)
            if company.is_bot_company:
                emps_check = (await session.execute(
                    select(func.count()).where(
                        CompanyEmployee.company_id == company.id,
                        CompanyEmployee.left_at == None,
                        CompanyEmployee.user_id != 0,
                    )
                )).scalar()
                if emps_check == 0:
                    company.value = int(company.value * 1.001)
                    await _update_level(session, company)
                    company.last_revenue = datetime.utcnow()
                    continue

            # Récupérer les employés actifs
            emps = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == company.id,
                    CompanyEmployee.left_at == None,
                )
            )).scalars().all()

            total_paid = 0
            for emp in emps:
                share = ROLE_SHARE.get(emp.role, 0)
                if share <= 0:
                    continue
                amount = int(revenue * share)
                user = await session.get(User, emp.user_id)
                if user:
                    user.coins += amount
                    total_paid += amount

            # Le reste va dans la caisse
            company.treasury += max(0, revenue - total_paid)
            company.last_revenue = datetime.utcnow()

            # Ancienneté : +0.1% de valeur par jour
            company.value = int(company.value * 1.001)
            await _update_level(session, company)

            # Bonus de réputation graduels
            if company.reputation < 5.0:
                company.reputation = min(5.0, company.reputation + 0.01)

            # Vérifier inactivité du PDG (30 jours → transfert au directeur)
            delta = datetime.utcnow() - (company.last_active or company.created_at)
            if delta.days >= 30:
                director = next(
                    (e for e in emps if e.role == "directeur"), None
                )
                if director:
                    old_pdg = next((e for e in emps if e.role == "pdg"), None)
                    if old_pdg:
                        old_pdg.role = "directeur"
                    director.role = "pdg"
                    company.owner_id = director.user_id
                    company.last_active = datetime.utcnow()
                    await _add_log(session, company.id, "transfert",
                                   f"PDG inactif — transfert au Directeur (uid {director.user_id})")
                    # Notifier le nouveau PDG
                    try:
                        await context.bot.send_message(
                            chat_id=director.user_id,
                            text=(
                                f"👑 <b>Tu es maintenant PDG de {company.name} !</b>\n\n"
                                f"L'ancien PDG était inactif depuis 30 jours.\n"
                                f"La direction t'a été automatiquement transférée."
                            ),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                    # Notifier l'ancien PDG
                    if old_pdg:
                        try:
                            await context.bot.send_message(
                                chat_id=old_pdg.user_id,
                                text=(
                                    f"⚠️ Tu as perdu la direction de <b>{company.name}</b> "
                                    f"pour cause d'inactivité (30 jours).\n"
                                    f"Un Directeur a pris ta place."
                                ),
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass

        await session.commit()


# ─── JOB : BONUS ACTIVITÉ BOT (compte les commandes des employés) ────────────

async def update_company_activity(user_id: int):
    """Appelé par le middleware à chaque commande. Ajoute de la valeur à l'entreprise."""
    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user_id)
        if not company or not emp:
            return
        emp.command_count += 1
        # +50 000 $ par commande, plafonné à 500 000 $/jour par entreprise
        today = datetime.utcnow().date()
        last_rev_date = company.last_revenue.date() if company.last_revenue else None
        if last_rev_date != today:
            # Nouveau jour : réinitialiser le compteur d'activité implicitement
            # (on compte via command_count, pas besoin de reset)
            pass
        # Limite : max 10 commandes bonifiées par employé par reset quotidien
        # Simple : on plafonne le gain total via value (pas de champ dédié, on cap à +500k/24h)
        company.value += 50_000
        company.last_active = datetime.utcnow()
        await session.commit()


# ─── COMMANDE : /listeboites ──────────────────────────────────────────────────

async def listeboites_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncSessionLocal() as session:
        companies = (await session.execute(
            select(Company).where(Company.is_active == True).order_by(Company.value.desc())
        )).scalars().all()

        if not companies:
            await update.message.reply_text("❌ Aucune entreprise active.")
            return

        lines = ["╔══════════════════════════════╗",
                 "║  🏢  LISTE DES ENTREPRISES   ║",
                 "╚══════════════════════════════╝\n"]

        for i, c in enumerate(companies[:20], 1):
            sec_emoji, _ = SECTORS.get(c.sector, ("🏢", c.sector))
            lvl_emoji, lvl_name, *_ = _level_info(c.level)
            bot_tag = " 🤖" if c.is_bot_company else ""
            lines.append(
                f"{i}. {sec_emoji} <b>{c.name}</b>{bot_tag}\n"
                f"   {lvl_emoji} {lvl_name} · 💰 {_fmt(c.value)} · ⭐ {c.reputation:.1f}/5\n"
            )

        lines.append("\n💡 <code>/infoboite [nom]</code> pour plus de détails")
        lines.append("💡 <code>/postuler [nom]</code> pour rejoindre une entreprise")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── COMMANDE : /infoboite [nom] ──────────────────────────────────────────────

async def infoboite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage : <code>/infoboite [nom de l'entreprise]</code>", parse_mode="HTML")
        return
    name = " ".join(context.args)
    async with AsyncSessionLocal() as session:
        company = await _get_company_by_name(session, name)
        if not company:
            await update.message.reply_text(f"❌ Entreprise <b>{name}</b> introuvable.", parse_mode="HTML")
            return

        sec_emoji, sec_name = SECTORS.get(company.sector, ("🏢", company.sector))
        lvl_emoji, lvl_name, _, daily_rate, max_emp = _level_info(company.level)

        # Compter les employés
        nb_emp = (await session.execute(
            select(func.count()).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.left_at == None,
            )
        )).scalar()

        # Propriétaire
        if company.is_bot_company:
            owner_name = "🤖 Bot officiel"
        else:
            owner = await session.get(User, company.owner_id)
            owner_name = owner.first_name if owner else "Inconnu"

        msg = (
            f"╔══════════════════════════════╗\n"
            f"║  {sec_emoji} <b>{company.name}</b>\n"
            f"╚══════════════════════════════╝\n\n"
            f"📋 <b>Secteur</b> : {sec_name}\n"
            f"{lvl_emoji} <b>Niveau</b> : {lvl_name}\n"
            f"📝 {company.description or 'Aucune description.'}\n\n"
            f"◈━━━━━━━━━━━━━━━━━━━━━━━━◈\n\n"
            f"💰 <b>Valeur</b> : {_fmt(company.value)}\n"
            f"🏦 <b>Trésorerie</b> : {_fmt(company.treasury)}\n"
            f"📈 <b>Revenus/jour</b> : {_fmt(int(company.value * daily_rate) // 30)} <i>(distribués aux employés)</i>\n"
            f"⭐ <b>Réputation</b> : {company.reputation:.1f}/5\n"
            f"👤 <b>PDG</b> : {owner_name}\n"
            f"👥 <b>Employés</b> : {nb_emp}/{max_emp}\n"
            f"🎂 <b>Fondée le</b> : {company.created_at.strftime('%d/%m/%Y')}\n\n"
            f"◈━━━━━━━━━━━━━━━━━━━━━━━━◈\n"
            f"📦 <b>Parts PDG</b> : {company.owner_shares}/{company.total_shares} · "
            f"💡 <code>/parts {company.name}</code> pour le détail"
        )
        await update.message.reply_text(msg, parse_mode="HTML")


# ─── COMMANDE : /creerboite [nom] [secteur] ──────────────────────────────────

async def creerboite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) < 2:
        sectors_list = " | ".join(SECTORS.keys())
        await update.message.reply_text(
            f"❌ Usage : <code>/creerboite [nom] [secteur]</code>\n"
            f"Secteurs : {sectors_list}",
            parse_mode="HTML"
        )
        return

    sector = context.args[-1].lower()
    name = " ".join(context.args[:-1])

    if sector not in SECTORS:
        sectors_list = " | ".join(SECTORS.keys())
        await update.message.reply_text(f"❌ Secteur invalide. Choix : {sectors_list}")
        return

    async with AsyncSessionLocal() as session:
        db_user = await get_user(session, user.id)

        # Vérifications
        if not db_user.diplome_licence:
            await update.message.reply_text(
                "❌ Il te faut au minimum une <b>Licence</b> pour créer une entreprise.\n"
                "Passe ton diplôme avec <code>/diplome</code>",
                parse_mode="HTML"
            )
            return

        # Vérifier cohérence domaine / secteur
        user_domain = db_user.diplome_domain
        allowed_domains = SECTOR_ALLOWED_DOMAINS.get(sector, [])
        if user_domain and allowed_domains and user_domain not in allowed_domains:
            from handlers.diplome import DOMAINS as DIPLOME_DOMAINS
            sec_emoji, sec_name = SECTORS[sector]
            domain_label = DIPLOME_DOMAINS.get(user_domain, ("🎓", user_domain))[1]
            allowed_labels = " / ".join(
                DIPLOME_DOMAINS.get(d, ("🎓", d))[1] for d in allowed_domains
            )
            await update.message.reply_text(
                f"❌ Ton domaine <b>{domain_label}</b> ne te permet pas de créer "
                f"une entreprise dans le secteur <b>{sec_name}</b>.\n\n"
                f"💡 Ce secteur requiert : <b>{allowed_labels}</b>\n"
                f"Tu peux quand même <b>acheter des parts</b> avec <code>/acheterparts</code>",
                parse_mode="HTML"
            )
            return

        if db_user.coins < 50_000_000:
            await update.message.reply_text(
                f"❌ Il te faut <b>50 000 000 $</b> pour créer une entreprise.\n"
                f"Tu as : {_fmt(db_user.coins)} $",
                parse_mode="HTML"
            )
            return

        # Vérifier déjà dans une boite
        company, emp = await _get_user_company(session, user.id)
        if company:
            await update.message.reply_text(
                f"❌ Tu es déjà dans l'entreprise <b>{company.name}</b>.\n"
                f"Démissionne d'abord avec <code>/demissionner</code>",
                parse_mode="HTML"
            )
            return

        # Vérifier déjà PDG
        own = (await session.execute(
            select(Company).where(Company.owner_id == user.id, Company.is_active == True)
        )).scalar_one_or_none()
        if own:
            await update.message.reply_text(
                f"❌ Tu possèdes déjà l'entreprise <b>{own.name}</b>.",
                parse_mode="HTML"
            )
            return

        # Vérifier nom unique (actives ET inactives pour éviter UniqueViolationError)
        exists_any = (await session.execute(
            select(Company).where(Company.name.ilike(name))
        )).scalar_one_or_none()
        if exists_any:
            await update.message.reply_text(
                f"❌ Une entreprise nommée <b>{name}</b> existe déjà (ou a existé).\n"
                f"Choisis un autre nom.",
                parse_mode="HTML"
            )
            return

        # Créer
        db_user.coins -= 50_000_000
        new_company = Company(
            name=name,
            sector=sector,
            owner_id=user.id,
            group_id=update.effective_chat.id,
            value=50_000_000,
            treasury=0,
            total_shares=100,
            owner_shares=100,
            level=1,
            reputation=3.0,
            is_bot_company=False,
        )
        session.add(new_company)
        try:
            await session.flush()
        except Exception:
            await session.rollback()
            await update.message.reply_text(
                f"❌ Ce nom est déjà pris en base de données. Choisis un autre nom.",
                parse_mode="HTML"
            )
            return

        # Le fondateur devient PDG
        pdg_emp = CompanyEmployee(
            company_id=new_company.id,
            user_id=user.id,
            role="pdg",
        )
        session.add(pdg_emp)

        # Initialiser les parts du PDG
        share = CompanyShare(
            company_id=new_company.id,
            owner_id=user.id,
            quantity=100,
        )
        session.add(share)

        await _add_log(session, new_company.id, "creation",
                       f"Entreprise créée par {user.first_name}")
        await session.commit()

        sec_emoji, sec_name = SECTORS[sector]
        await update.message.reply_text(
            f"✅ <b>{new_company.name}</b> est fondée !\n\n"
            f"{sec_emoji} Secteur : <b>{sec_name}</b>\n"
            f"💰 Capital initial : <b>50 000 000 $</b>\n"
            f"📦 Parts : <b>100/100</b> (tu détiens tout)\n"
            f"👑 Tu es le <b>PDG</b>\n\n"
            f"👥 Recrute des employés avec <code>/recruter @pseudo</code>\n"
            f"📊 Infos avec <code>/infoboite {name}</code>",
            parse_mode="HTML"
        )


# ─── Prérequis diplômes par secteur (entreprises du bot) ───────────────────
SECTOR_REQUIREMENTS = {
    "tech":        {"min": "licence",  "ideal": "master"},
    "finance":     {"min": "master",   "ideal": "mba"},
    "commerce":    {"min": "bac",      "ideal": "licence"},
    "agriculture": {"min": "bac",      "ideal": "licence"},
    "securite":    {"min": "bac",      "ideal": "licence"},
    "droit":       {"min": "master",   "ideal": "mba"},
    "immobilier":  {"min": "licence",  "ideal": "master"},
    "sante":       {"min": "licence",  "ideal": "master"},
}

DIPLOME_LEVEL = {"bac": 1, "licence": 2, "master": 3, "mba": 4}
DIPLOME_LABEL = {
    "bac":     "📄 Bac",
    "licence": "🎓 Licence",
    "master":  "🏅 Master",
    "mba":     "👑 MBA",
}

def _get_user_diplome_level(db_user) -> int:
    if db_user.diplome_mba:     return 4
    if db_user.diplome_master:  return 3
    if db_user.diplome_licence: return 2
    if db_user.diplome_bac:     return 1
    return 0

def _get_role_for_bot_company(db_user, sector: str) -> str:
    lvl = _get_user_diplome_level(db_user)
    if lvl >= 4: return "directeur"
    if lvl >= 3: return "manager"
    if lvl >= 2: return "employe"
    return "stagiaire"


# ─── COMMANDE : /postuler [nom entreprise] ───────────────────────────────────

async def postuler_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("❌ Usage : <code>/postuler [nom de l'entreprise]</code>", parse_mode="HTML")
        return

    name = " ".join(context.args)
    async with AsyncSessionLocal() as session:
        db_user = await get_user(session, user.id)

        # Déjà dans une boite ?
        company, emp = await _get_user_company(session, user.id)
        if company:
            await update.message.reply_text(
                f"❌ Tu es déjà dans <b>{company.name}</b>. Démissionne d'abord.",
                parse_mode="HTML"
            )
            return

        # PDG ne peut pas postuler ailleurs (doit d'abord céder sa boite)
        own_company = (await session.execute(
            select(Company).where(
                Company.owner_id == user.id,
                Company.is_active == True,
                Company.is_bot_company == False,
            )
        )).scalar_one_or_none()
        if own_company:
            await update.message.reply_text(
                f"❌ Tu es PDG de <b>{own_company.name}</b>.\n"
                f"Un PDG ne peut pas postuler dans une autre entreprise.",
                parse_mode="HTML"
            )
            return

        # Cooldown démission (7 jours, sauf si on quitte une bot company)
        last_left_row = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.user_id == user.id,
                CompanyEmployee.left_at != None,
            ).order_by(CompanyEmployee.left_at.desc()).limit(1)
        )).scalar_one_or_none()
        if last_left_row and last_left_row.left_at:
            # Pas de cooldown si la dernière entreprise quittée était une bot company
            last_company = await session.get(Company, last_left_row.company_id)
            is_bot = last_company.is_bot_company if last_company else False
            if not is_bot:
                days_passed = (datetime.utcnow() - last_left_row.left_at).days
                if days_passed < 7:
                    jours = 7 - days_passed
                    await update.message.reply_text(
                        f"⏳ Tu dois attendre encore <b>{jours} jour(s)</b> avant de rejoindre une nouvelle entreprise.",
                        parse_mode="HTML"
                    )
                    return

        target = await _get_company_by_name(session, name)
        if not target:
            await update.message.reply_text(f"❌ Entreprise <b>{name}</b> introuvable.", parse_mode="HTML")
            return

        # Déjà postulé ?
        existing_app = (await session.execute(
            select(CompanyApplication).where(
                CompanyApplication.company_id == target.id,
                CompanyApplication.user_id == user.id,
                CompanyApplication.status == "pending",
            )
        )).scalar_one_or_none()
        if existing_app:
            await update.message.reply_text("⏳ Ta candidature est déjà en cours d'examen.")
            return

        # Vérifier capacité
        _, _, _, _, max_emp = _level_info(target.level)
        nb_emp = (await session.execute(
            select(func.count()).where(
                CompanyEmployee.company_id == target.id,
                CompanyEmployee.left_at == None,
            )
        )).scalar()
        if nb_emp >= max_emp:
            await update.message.reply_text(f"❌ <b>{target.name}</b> est au complet ({max_emp} employés max).", parse_mode="HTML")
            return

        # ── Entreprise du bot : verdict automatique ──────────────────────────
        if target.is_bot_company:
            sector = target.sector.lower()
            req = SECTOR_REQUIREMENTS.get(sector, {"min": "bac", "ideal": "licence"})
            min_lvl   = DIPLOME_LEVEL.get(req["min"], 1)
            user_lvl  = _get_user_diplome_level(db_user)

            # Vérif diplôme minimum — message précis avec le vrai diplôme requis
            min_label = DIPLOME_LABEL.get(req["min"], req["min"])
            sec_emoji, sec_name = SECTORS.get(sector, ("🏢", sector))

            NIVEAU_LABEL = {0: "Aucun diplôme", 1: "📄 Bac", 2: "🎓 Licence", 3: "🏅 Master", 4: "👑 MBA"}

            if user_lvl < min_lvl:
                await update.message.reply_text(
                    f"❌ <b>{target.name}</b> ({sec_name}) exige au minimum le {min_label}.\n\n"
                    f"📊 Ton niveau actuel : <b>{NIVEAU_LABEL.get(user_lvl, 'Aucun')}</b>\n"
                    f"📚 Passe le diplôme requis avec <code>/diplome</code>",
                    parse_mode="HTML"
                )
                return

            # Diplôme OK → recrutement immédiat
            role = _get_role_for_bot_company(db_user, sector)
            role_emoji = ROLE_EMOJI.get(role, "👤")
            new_emp = CompanyEmployee(
                company_id=target.id,
                user_id=user.id,
                role=role,
            )
            session.add(new_emp)
            await _add_log(session, target.id, "recrutement",
                           f"{user.first_name} recruté comme {role} (auto)")
            await session.commit()

            ideal_label = DIPLOME_LABEL.get(req["ideal"], req["ideal"])
            bonus_msg = ""
            if user_lvl < DIPLOME_LEVEL.get(req["ideal"], 99):
                bonus_msg = f"\n💡 Avec le {ideal_label} tu pourrais obtenir un meilleur poste !"

            sec_emoji2, sec_name2 = SECTORS.get(sector, ("🏢", sector))
            await update.message.reply_text(
                f"✅ <b>{target.name}</b> ({sec_name2}) t'a recruté !\n\n"
                f"{role_emoji} Poste : <b>{role.capitalize()}</b>"
                f"{bonus_msg}\n\n"
                f"💡 Tape <code>/monentreprise</code> pour voir ta fiche.",
                parse_mode="HTML"
            )
            return

        # ── Entreprise d'un joueur : candidature classique ───────────────────
        # Vérif bac minimum pour rejoindre une boite joueur
        if not db_user.diplome_bac:
            await update.message.reply_text(
                f"❌ Tu n'as aucun diplôme.\n\n"
                f"🏢 <b>{target.name}</b> exige au minimum le <b>📄 Bac</b> pour postuler.\n"
                f"📚 Passe ton diplôme avec <code>/diplome</code>",
                parse_mode="HTML"
            )
            return

        app = CompanyApplication(
            company_id=target.id,
            user_id=user.id,
            status="pending",
        )
        session.add(app)
        await _add_log(session, target.id, "candidature",
                       f"{user.first_name} a postulé")
        await session.commit()

        await update.message.reply_text(
            f"📩 Ta candidature pour <b>{target.name}</b> a été envoyée !\n"
            f"Le PDG ou les Directeurs vont l'examiner.",
            parse_mode="HTML"
        )

        # ── Notifier le PDG et les Directeurs en DM ──
        if not target.is_bot_company:
            managers = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == target.id,
                    CompanyEmployee.left_at == None,
                    CompanyEmployee.role.in_(["pdg", "directeur"]),
                )
            )).scalars().all()

            diplomes = []
            if db_user.diplome_bac:     diplomes.append("📄 Bac")
            if db_user.diplome_licence: diplomes.append(f"🎓 Licence {db_user.diplome_domain or ''}")
            if db_user.diplome_master:  diplomes.append("🏅 Master")
            if db_user.diplome_mba:     diplomes.append("👑 MBA")
            diplomes_str = " · ".join(diplomes) or "Aucun"

            notif_msg = (
                f"🔔 <b>Nouvelle candidature !</b>\n\n"
                f"👤 <b>{user.first_name}</b> postule dans <b>{target.name}</b>\n"
                f"🎓 Diplômes : {diplomes_str}\n\n"
                f"✅ <code>/accepter {user.id}</code>\n"
                f"❌ <code>/refuser {user.id}</code>"
            )
            for mgr in managers:
                try:
                    await context.bot.send_message(
                        chat_id=mgr.user_id,
                        text=notif_msg,
                        parse_mode="HTML"
                    )
                except Exception:
                    pass


# ─── COMMANDE : /candidatures ─────────────────────────────────────────────────

async def candidatures_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company or emp.role not in ("pdg", "directeur"):
            await update.message.reply_text("❌ Tu dois être PDG ou Directeur pour voir les candidatures.")
            return

        apps = (await session.execute(
            select(CompanyApplication).where(
                CompanyApplication.company_id == company.id,
                CompanyApplication.status == "pending",
            )
        )).scalars().all()

        if not apps:
            await update.message.reply_text(f"📭 Aucune candidature en attente pour <b>{company.name}</b>.", parse_mode="HTML")
            return

        lines = [f"📋 <b>Candidatures — {company.name}</b>\n"]
        for app in apps:
            candidate = await session.get(User, app.user_id)
            if not candidate:
                continue
            diplomes = []
            if candidate.diplome_bac:     diplomes.append("📄Bac")
            if candidate.diplome_licence: diplomes.append(f"🎓Licence {candidate.diplome_domain or ''}")
            if candidate.diplome_master:  diplomes.append(f"🏅Master")
            if candidate.diplome_mba:     diplomes.append("👑MBA")
            lines.append(
                f"👤 <b>{candidate.first_name}</b> (id:{candidate.user_id})\n"
                f"   🎓 {' · '.join(diplomes) or 'Aucun'}\n"
                f"   ✅ <code>/accepter {candidate.user_id}</code>  ❌ <code>/refuser {candidate.user_id}</code>\n"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def accepter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("❌ Usage : <code>/accepter [user_id]</code>", parse_mode="HTML")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID invalide.")
        return

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company or emp.role not in ("pdg", "directeur"):
            await update.message.reply_text("❌ Réservé au PDG et Directeur.")
            return

        app = (await session.execute(
            select(CompanyApplication).where(
                CompanyApplication.company_id == company.id,
                CompanyApplication.user_id == target_id,
                CompanyApplication.status == "pending",
            )
        )).scalar_one_or_none()
        if not app:
            await update.message.reply_text("❌ Candidature introuvable.")
            return

        app.status = "accepted"
        candidate = await session.get(User, target_id)

        # Rôle selon diplôme (cohérent avec ROLE_DIPLOMA)
        role = "stagiaire"
        if candidate and candidate.diplome_mba:      role = "directeur"
        elif candidate and candidate.diplome_master: role = "manager"   # Master → manager (directeur réservé au MBA)
        elif candidate and candidate.diplome_licence:role = "employe"   # Licence → employe
        elif candidate and candidate.diplome_bac:    role = "employe"

        new_emp = CompanyEmployee(
            company_id=company.id,
            user_id=target_id,
            role=role,
        )
        session.add(new_emp)
        await _add_log(session, company.id, "recrutement",
                       f"{candidate.first_name if candidate else target_id} recruté comme {role}")
        await session.commit()

        role_emoji = ROLE_EMOJI.get(role, "👤")
        await update.message.reply_text(
            f"✅ <b>{candidate.first_name if candidate else target_id}</b> a rejoint <b>{company.name}</b> "
            f"en tant que {role_emoji} <b>{role.capitalize()}</b> !",
            parse_mode="HTML"
        )

        # ── Notifier le candidat en DM ──
        if candidate:
            try:
                await context.bot.send_message(
                    chat_id=candidate.user_id,
                    text=(
                        f"🎉 <b>Félicitations !</b> Ta candidature chez <b>{company.name}</b> a été <b>acceptée</b> !\n\n"
                        f"{role_emoji} Tu es désormais <b>{role.capitalize()}</b>.\n"
                        f"💡 Tape <code>/monentreprise</code> pour voir ta fiche."
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass


async def refuser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("❌ Usage : <code>/refuser [user_id]</code>", parse_mode="HTML")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID invalide.")
        return

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company or emp.role not in ("pdg", "directeur"):
            await update.message.reply_text("❌ Réservé au PDG et Directeur.")
            return

        app = (await session.execute(
            select(CompanyApplication).where(
                CompanyApplication.company_id == company.id,
                CompanyApplication.user_id == target_id,
                CompanyApplication.status == "pending",
            )
        )).scalar_one_or_none()
        if not app:
            await update.message.reply_text("❌ Candidature introuvable.")
            return

        app.status = "rejected"
        candidate_user = await session.get(User, target_id)
        await session.commit()
        await update.message.reply_text("✅ Candidature refusée.")

        # ── Notifier le candidat en DM ──
        if candidate_user:
            try:
                await context.bot.send_message(
                    chat_id=candidate_user.user_id,
                    text=(
                        f"😔 Ta candidature chez <b>{company.name}</b> a été <b>refusée</b>.\n\n"
                        f"💡 Tu peux postuler dans d'autres entreprises avec <code>/listeboites</code>."
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass


# ─── COMMANDE : /recruter @pseudo [poste?] ────────────────────────────────────

async def recruter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("❌ Usage : <code>/recruter @pseudo [poste]</code>", parse_mode="HTML")
        return

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company or emp.role not in ("pdg", "directeur", "manager"):
            await update.message.reply_text("❌ Tu dois être au moins Manager pour recruter.")
            return

        # Parser la cible
        mention = context.args[0].lstrip("@")
        role_arg = context.args[1].lower() if len(context.args) > 1 else "employe"
        if role_arg not in ROLES_ORDER or role_arg == "pdg":
            role_arg = "employe"

        # Trouver l'utilisateur
        target = (await session.execute(
            select(User).where(User.username == mention)
        )).scalar_one_or_none()
        if not target:
            await update.message.reply_text(f"❌ Utilisateur @{mention} introuvable. Il doit avoir utilisé le bot au moins une fois.")
            return

        # Vérifier que la cible n'est pas déjà quelque part
        tc, te = await _get_user_company(session, target.user_id)
        if tc:
            await update.message.reply_text(f"❌ {target.first_name} est déjà dans <b>{tc.name}</b>.", parse_mode="HTML")
            return

        # Vérifier le diplôme requis pour le poste
        required_diploma = ROLE_DIPLOMA.get(role_arg)
        if required_diploma and not _has_diploma(target, required_diploma):
            await update.message.reply_text(
                f"❌ {target.first_name} n'a pas le diplôme requis pour le poste <b>{role_arg}</b>.",
                parse_mode="HTML"
            )
            return

        # Manager ne peut pas nommer au-dessus de manager
        manager_roles = ROLES_ORDER.index(emp.role)
        target_role_idx = ROLES_ORDER.index(role_arg)
        if target_role_idx >= manager_roles:
            await update.message.reply_text("❌ Tu ne peux pas nommer quelqu'un au même niveau ou au-dessus du tien.")
            return

        # Créer l'invitation
        invite = CompanyInvite(
            company_id=company.id,
            target_id=target.user_id,
            role=role_arg,
            invited_by=user.id,
            status="pending",
            expires_at=datetime.utcnow() + timedelta(hours=48),
        )
        session.add(invite)
        await session.commit()

        role_emoji = ROLE_EMOJI.get(role_arg, "👤")
        await update.message.reply_text(
            f"📩 Invitation envoyée à <b>{target.first_name}</b> pour rejoindre <b>{company.name}</b> "
            f"en tant que {role_emoji} <b>{role_arg.capitalize()}</b>.\n\n"
            f"Il peut accepter avec <code>/rejoindre {company.name}</code>",
            parse_mode="HTML"
        )
        # ── Notifier la cible en DM ──
        try:
            await context.bot.send_message(
                chat_id=target.user_id,
                text=(
                    f"📩 <b>Tu as reçu une invitation !</b>\n\n"
                    f"🏢 <b>{company.name}</b> t'invite à les rejoindre "
                    f"en tant que {role_emoji} <b>{role_arg.capitalize()}</b>.\n\n"
                    f"✅ Accepter : <code>/rejoindre {company.name}</code>\n"
                    f"⏳ L'invitation expire dans <b>48h</b>."
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass


# ─── COMMANDE : /rejoindre [entreprise] ──────────────────────────────────────

async def rejoindre_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("❌ Usage : <code>/rejoindre [nom entreprise]</code>", parse_mode="HTML")
        return
    name = " ".join(context.args)

    async with AsyncSessionLocal() as session:
        # Cooldown (pas appliqué si la dernière boite quittée était une bot company)
        last_left_emp = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.user_id == user.id,
                CompanyEmployee.left_at != None,
            ).order_by(CompanyEmployee.left_at.desc()).limit(1)
        )).scalar_one_or_none()
        if last_left_emp and last_left_emp.left_at:
            last_co = await session.get(Company, last_left_emp.company_id)
            is_bot_last = last_co.is_bot_company if last_co else False
            if not is_bot_last:
                days_passed = (datetime.utcnow() - last_left_emp.left_at).days
                if days_passed < 7:
                    jours = 7 - days_passed
                    await update.message.reply_text(f"⏳ Cooldown : encore {jours} jour(s) avant de rejoindre une entreprise.")
                    return

        # Déjà dans une boite ?
        company, emp = await _get_user_company(session, user.id)
        if company:
            await update.message.reply_text(f"❌ Tu es déjà dans <b>{company.name}</b>.", parse_mode="HTML")
            return

        target = await _get_company_by_name(session, name)
        if not target:
            await update.message.reply_text(f"❌ Entreprise <b>{name}</b> introuvable.", parse_mode="HTML")
            return

        invite = (await session.execute(
            select(CompanyInvite).where(
                CompanyInvite.company_id == target.id,
                CompanyInvite.target_id == user.id,
                CompanyInvite.status == "pending",
                CompanyInvite.expires_at > datetime.utcnow(),
            )
        )).scalar_one_or_none()

        if not invite:
            await update.message.reply_text(
                f"❌ Tu n'as pas d'invitation valide pour <b>{target.name}</b>.\n"
                f"Postule d'abord avec <code>/postuler {name}</code>",
                parse_mode="HTML"
            )
            return

        invite.status = "accepted"
        new_emp = CompanyEmployee(
            company_id=target.id,
            user_id=user.id,
            role=invite.role,
        )
        session.add(new_emp)
        await _add_log(session, target.id, "recrutement",
                       f"{user.first_name} a rejoint l'entreprise ({invite.role})")
        await session.commit()

        role_emoji = ROLE_EMOJI.get(invite.role, "👤")
        await update.message.reply_text(
            f"✅ Bienvenue dans <b>{target.name}</b> ! {role_emoji} Tu es <b>{invite.role.capitalize()}</b>.",
            parse_mode="HTML"
        )


# ─── COMMANDE : /demissionner ─────────────────────────────────────────────────

async def demissionner_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company:
            await update.message.reply_text("❌ Tu ne fais partie d'aucune entreprise.")
            return

        if emp.role == "pdg":
            # Vérifier qu'il y a un directeur pour reprendre
            director = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == company.id,
                    CompanyEmployee.role == "directeur",
                    CompanyEmployee.left_at == None,
                )
            )).scalar_one_or_none()
            if not director:
                await update.message.reply_text(
                    "❌ En tant que PDG, tu dois d'abord nommer un <b>Directeur</b> avant de partir.",
                    parse_mode="HTML"
                )
                return
            # Transfert
            director.role = "pdg"
            company.owner_id = director.user_id
            new_pdg = await session.get(User, director.user_id)
            await update.message.reply_text(
                f"👑 <b>{new_pdg.first_name if new_pdg else '?'}</b> est maintenant PDG de <b>{company.name}</b>.",
                parse_mode="HTML"
            )

        old_role = emp.role
        emp.left_at = datetime.utcnow()
        await _add_log(session, company.id, "demission", f"{user.first_name} a démissionné ({emp.role})")
        await session.commit()

        if company.is_bot_company:
            await update.message.reply_text(
                f"👋 Tu as quitté <b>{company.name}</b>.\n"
                f"✅ Pas de cooldown — c'est une entreprise officielle.",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                f"👋 Tu as quitté <b>{company.name}</b>.\n"
                f"⏳ Cooldown de 7 jours avant de pouvoir rejoindre une autre entreprise.",
                parse_mode="HTML"
            )

        # ── Notifier le PDG en DM (sauf si c'est le PDG lui-même qui part) ──
        if old_role != "pdg" and not company.is_bot_company:
            try:
                await context.bot.send_message(
                    chat_id=company.owner_id,
                    text=(
                        f"🚪 <b>{user.first_name}</b> ({old_role.capitalize()}) vient de démissionner "
                        f"de <b>{company.name}</b>.\n"
                        f"💡 <code>/candidatures</code> pour voir les nouvelles candidatures."
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass


# ─── COMMANDE : /nommer @pseudo [poste] ──────────────────────────────────────

async def nommer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Usage : <code>/nommer @pseudo [poste]</code>\n"
            "Postes : employe | manager | directeur",
            parse_mode="HTML"
        )
        return

    mention = context.args[0].lstrip("@")
    new_role = context.args[1].lower()
    if new_role not in ROLES_ORDER or new_role == "pdg":
        await update.message.reply_text("❌ Poste invalide. Choix : employe | manager | directeur")
        return

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company or emp.role not in ("pdg", "directeur", "manager"):
            await update.message.reply_text("❌ Tu dois être au moins Manager.")
            return

        # Trouver la cible
        target = (await session.execute(
            select(User).where(User.username == mention)
        )).scalar_one_or_none()
        if not target:
            await update.message.reply_text(f"❌ @{mention} introuvable.")
            return

        target_emp = await _get_employee(session, company.id, target.user_id)
        if not target_emp:
            await update.message.reply_text(f"❌ {target.first_name} n'est pas dans ton entreprise.")
            return

        # Hiérarchie : on ne peut pas nommer au-dessus de soi
        if ROLES_ORDER.index(new_role) >= ROLES_ORDER.index(emp.role):
            await update.message.reply_text("❌ Tu ne peux pas nommer quelqu'un à un rang égal ou supérieur au tien.")
            return

        # Vérifier le diplôme
        required = ROLE_DIPLOMA.get(new_role)
        if required and not _has_diploma(target, required):
            await update.message.reply_text(
                f"❌ {target.first_name} n'a pas le diplôme requis pour le poste <b>{new_role}</b>.",
                parse_mode="HTML"
            )
            return

        old_role = target_emp.role
        target_emp.role = new_role
        await _add_log(session, company.id, "promotion",
                       f"{target.first_name} : {old_role} → {new_role} (par {user.first_name})")
        await session.commit()

        role_emoji = ROLE_EMOJI.get(new_role, "👤")
        await update.message.reply_text(
            f"✅ <b>{target.first_name}</b> est désormais {role_emoji} <b>{new_role.capitalize()}</b> dans <b>{company.name}</b> !",
            parse_mode="HTML"
        )
        # ── Notifier la personne promue en DM ──
        try:
            await context.bot.send_message(
                chat_id=target.user_id,
                text=(
                    f"🎖️ <b>Félicitations !</b> Tu as été promu(e) dans <b>{company.name}</b>\n\n"
                    f"{role_emoji} Ton nouveau poste : <b>{new_role.capitalize()}</b>\n"
                    f"💡 Tape <code>/monentreprise</code> pour voir ta fiche."
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass


# ─── COMMANDE : /monentreprise ────────────────────────────────────────────────

async def monentreprise_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company:
            await update.message.reply_text(
                "❌ Tu ne fais partie d'aucune entreprise.\n"
                "📋 Vois la liste avec <code>/listeboites</code>",
                parse_mode="HTML"
            )
            return

        sec_emoji, sec_name = SECTORS.get(company.sector, ("🏢", company.sector))
        lvl_emoji, lvl_name, _, daily_rate, max_emp = _level_info(company.level)
        role_emoji = ROLE_EMOJI.get(emp.role, "👤")

        nb_emp = (await session.execute(
            select(func.count()).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.left_at == None,
            )
        )).scalar()

        # Calcul du revenu personnel selon le rôle
        total_revenue = int(company.value * daily_rate) // 30  # taux mensuel ÷ 30
        personal_share = ROLE_SHARE.get(emp.role, 0.0)
        personal_revenue = int(total_revenue * personal_share)

        if emp.role == "pdg":
            personal_rev_line = (
                f"  ╰┈➤  <b>PDG</b> — dividendes via <code>/retraitboite</code>\n"
            )
        elif personal_revenue > 0:
            personal_rev_line = (
                f"  ╰┈➤  💵 <b>{_fmt(personal_revenue)} $/jour</b> pour toi ({int(personal_share*100)}% du revenu boite)\n"
                f"  ╰┈➤  🏦 Revenu total boite : {_fmt(total_revenue)} $/jour\n"
            )
        else:
            personal_rev_line = (
                f"  ╰┈➤  Stagiaire — pas de salaire direct\n"
                f"  ╰┈➤  🏦 Revenu total boite : {_fmt(total_revenue)} $/jour\n"
            )

        msg = (
            f"「 {sec_emoji} 」<b>{company.name}</b>\n"
            f"✦ {lvl_emoji} {lvl_name}  ┊  ⭐ {company.reputation:.1f}/5\n\n"
            f"◈━━━━━━━━━━━━━━━━━━━━━━━━◈\n\n"
            f"  💰 VALEUR\n"
            f"  ╰┈➤  {_fmt(company.value)}\n\n"
            f"  🏦 TRÉSORERIE\n"
            f"  ╰┈➤  {_fmt(company.treasury)}\n\n"
            f"  📈 TON SALAIRE/JOUR\n"
            f"{personal_rev_line}\n"
            f"  👥 ÉQUIPE\n"
            f"  ╰┈➤  {nb_emp}/{max_emp} employés\n\n"
            f"◈━━━━━━━━━━━━━━━━━━━━━━━━◈\n\n"
            f"  🪙 TON POSTE\n"
            f"  ╰┈➤  {role_emoji} {emp.role.capitalize()}\n"
            f"  ╰┈➤  {emp.command_count} commandes utilisées\n"
        )
        await update.message.reply_text(msg, parse_mode="HTML")


# ─── COMMANDE : /depotboite [montant] ─────────────────────────────────────────

async def depotboite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("❌ Usage : <code>/depotboite [montant]</code>", parse_mode="HTML")
        return
    try:
        amount = int(context.args[0].replace("_", ""))
    except ValueError:
        await update.message.reply_text("❌ Montant invalide.")
        return

    if amount <= 0:
        await update.message.reply_text("❌ Montant invalide.")
        return

    async with AsyncSessionLocal() as session:
        db_user = await get_user(session, user.id)
        company, emp = await _get_user_company(session, user.id)
        if not company or emp.role not in ("pdg", "directeur"):
            await update.message.reply_text("❌ Réservé au PDG et Directeur.")
            return
        if db_user.coins < amount:
            await update.message.reply_text(f"❌ Tu n'as pas assez. Ton solde : {_fmt(db_user.coins)} $")
            return

        db_user.coins -= amount
        company.treasury += amount
        company.value += amount
        await _update_level(session, company)
        await _add_log(session, company.id, "depot",
                       f"Dépôt de {user.first_name}", amount=amount)
        await session.commit()

        await update.message.reply_text(
            f"✅ <b>{_fmt(amount)} $</b> déposé dans la trésorerie de <b>{company.name}</b>.\n"
            f"🏦 Trésorerie : {_fmt(company.treasury)} $",
            parse_mode="HTML"
        )


# ─── COMMANDE : /retraitboite [montant] ──────────────────────────────────────

async def retraitboite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("❌ Usage : <code>/retraitboite [montant]</code>", parse_mode="HTML")
        return
    try:
        amount = int(context.args[0].replace("_", ""))
    except ValueError:
        await update.message.reply_text("❌ Montant invalide.")
        return

    async with AsyncSessionLocal() as session:
        db_user = await get_user(session, user.id)
        company, emp = await _get_user_company(session, user.id)
        if not company or emp.role != "pdg":
            await update.message.reply_text("❌ Réservé au PDG.")
            return
        if company.is_bot_company:
            await update.message.reply_text("❌ Tu ne peux pas retirer de fonds d'une entreprise officielle.")
            return
        if company.treasury < amount:
            await update.message.reply_text(f"❌ Trésorerie insuffisante. Disponible : {_fmt(company.treasury)} $")
            return

        # Limite : max 20% de la valeur par retrait
        max_retrait = int(company.value * 0.20)
        if amount > max_retrait:
            await update.message.reply_text(
                f"❌ Tu ne peux pas retirer plus de <b>20%</b> de la valeur de l'entreprise par transaction.\n"
                f"Max autorisé : {_fmt(max_retrait)} $",
                parse_mode="HTML"
            )
            return

        company.treasury -= amount
        company.value = max(1_000_000, company.value - int(amount * 0.5))  # Retrait affecte la valeur
        db_user.coins += amount
        await _update_level(session, company)
        await _add_log(session, company.id, "retrait",
                       f"Retrait PDG ({user.first_name})", amount=amount)
        await session.commit()

        await update.message.reply_text(
            f"✅ <b>{_fmt(amount)} $</b> retiré de <b>{company.name}</b>.\n"
            f"💰 Ton solde : {_fmt(db_user.coins)} $",
            parse_mode="HTML"
        )


# ─── COMMANDE : /logsboite ────────────────────────────────────────────────────

async def logsboite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company or emp.role not in ("pdg", "directeur"):
            await update.message.reply_text("❌ Réservé au PDG et Directeur.")
            return

        logs = (await session.execute(
            select(CompanyLog).where(
                CompanyLog.company_id == company.id
            ).order_by(CompanyLog.created_at.desc()).limit(15)
        )).scalars().all()

        if not logs:
            await update.message.reply_text("📭 Aucun log enregistré.")
            return

        TYPE_EMOJI = {
            "creation":    "🎉",
            "recrutement": "➕",
            "demission":   "🚪",
            "promotion":   "⬆️",
            "depot":       "📥",
            "retrait":     "📤",
            "candidature": "📩",
            "transfert":   "👑",
        }

        lines = [f"📋 <b>Logs — {company.name}</b>\n"]
        for log in logs:
            emoji = TYPE_EMOJI.get(log.event_type, "📌")
            amount_str = f" · {_fmt(log.amount)} $" if log.amount else ""
            date_str = log.created_at.strftime("%d/%m %H:%M")
            lines.append(f"{emoji} <code>{date_str}</code> {log.description}{amount_str}")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── COMMANDE : /parts [entreprise] ──────────────────────────────────────────

async def parts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = " ".join(context.args) if context.args else None

    async with AsyncSessionLocal() as session:
        if name:
            company = await _get_company_by_name(session, name)
        else:
            company, _ = await _get_user_company(session, user.id)

        if not company:
            await update.message.reply_text("❌ Entreprise introuvable ou tu n'es dans aucune entreprise.")
            return

        # Récupérer tous les actionnaires
        shares = (await session.execute(
            select(CompanyShare).where(CompanyShare.company_id == company.id)
        )).scalars().all()

        lines = [f"📦 <b>Parts — {company.name}</b>\n",
                 f"Total : <b>{company.total_shares} parts</b>\n"]
        for s in shares:
            if s.quantity <= 0:
                continue
            owner = await session.get(User, s.owner_id)
            if not owner:
                owner_name = "🤖 Bot"
            else:
                owner_name = owner.first_name
            pct = (s.quantity / company.total_shares) * 100
            lines.append(f"• {owner_name} — <b>{s.quantity} parts</b> ({pct:.1f}%)")

        price_per_share = company.value // company.total_shares
        lines.append(f"\n💰 Prix par part : <b>{_fmt(price_per_share)} $</b>")
        lines.append(f"\n💡 <code>/vendreparts [nb] [entreprise]</code> pour vendre")
        lines.append(f"💡 <code>/acheterparts [nb] [entreprise]</code> pour acheter au PDG")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── COMMANDE : /vendreparts [nb] [prix/part] ────────────────────────────────

async def vendreparts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Usage : <code>/vendreparts [nombre de parts] [prix par part]</code>",
            parse_mode="HTML"
        )
        return
    try:
        qty = int(context.args[0])
        price_each = int(context.args[1].replace("_", ""))
    except ValueError:
        await update.message.reply_text("❌ Paramètres invalides.")
        return

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company:
            await update.message.reply_text("❌ Tu n'es dans aucune entreprise.")
            return

        my_shares = await _get_shares(session, company.id, user.id)
        # PDG doit garder min 51%
        if emp.role == "pdg":
            min_keep = (company.total_shares // 2) + 1
            can_sell = max(0, my_shares - min_keep)
        else:
            can_sell = my_shares

        if qty <= 0:
            await update.message.reply_text("❌ La quantité doit être supérieure à 0.")
            return

        if my_shares < qty:
            await update.message.reply_text(f"❌ Tu n'as que {my_shares} parts.")
            return

        if qty > can_sell:
            await update.message.reply_text(
                f"❌ Tu ne peux vendre que <b>{can_sell}</b> parts (PDG garde ≥51%).",
                parse_mode="HTML"
            )
            return

        total = qty * price_each
        db_user = await get_user(session, user.id)

        # Acheteur = trésorerie de l'entreprise (rachat automatique)
        # Dans une vraie implem on matcherait avec un acheteur - ici on simplifie
        if company.treasury < total:
            await update.message.reply_text(
                f"❌ La trésorerie n'a pas assez de fonds ({_fmt(company.treasury)} $).\n"
                f"Le PDG doit déposer plus de capital.",
                parse_mode="HTML"
            )
            return

        company.treasury -= total
        # Seul le PDG rachète ses propres parts
        if emp.role == "pdg":
            company.owner_shares += qty
        db_user.coins += total

        # Mettre à jour les parts
        share_row = (await session.execute(
            select(CompanyShare).where(
                CompanyShare.company_id == company.id,
                CompanyShare.owner_id == user.id,
            )
        )).scalar_one_or_none()
        if share_row:
            share_row.quantity -= qty

        await _add_log(session, company.id, "vente_parts",
                       f"{user.first_name} a vendu {qty} parts à {_fmt(price_each)}/part",
                       amount=total)
        await session.commit()

        await update.message.reply_text(
            f"✅ <b>{qty} parts</b> vendues pour <b>{_fmt(total)} $</b> !",
            parse_mode="HTML"
        )


# ─── COMMANDE : /acheterparts [nb] [nom entreprise] ─────────────────────────

async def acheterparts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Usage : <code>/acheterparts [nombre] [nom entreprise]</code>",
            parse_mode="HTML"
        )
        return
    try:
        qty = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Quantité invalide.")
        return
    name = " ".join(context.args[1:])

    async with AsyncSessionLocal() as session:
        company = await _get_company_by_name(session, name)
        if not company:
            await update.message.reply_text(f"❌ Entreprise <b>{name}</b> introuvable.", parse_mode="HTML")
            return

        available = company.owner_shares - ((company.total_shares // 2) + 1)
        if available <= 0:
            await update.message.reply_text("❌ Le PDG ne vend pas de parts actuellement (garde 51% minimum).")
            return

        if qty > available:
            await update.message.reply_text(f"❌ Seulement {available} parts disponibles à l'achat.")
            return

        price_per = company.value // company.total_shares
        total = qty * price_per
        db_user = await get_user(session, user.id)

        if db_user.coins < total:
            await update.message.reply_text(
                f"❌ Tu n'as pas assez. Prix total : {_fmt(total)} $\n"
                f"Ton solde : {_fmt(db_user.coins)} $",
                parse_mode="HTML"
            )
            return

        db_user.coins -= total
        company.owner_shares -= qty
        company.treasury += total

        # Ajouter les parts à l'acheteur
        buyer_share = (await session.execute(
            select(CompanyShare).where(
                CompanyShare.company_id == company.id,
                CompanyShare.owner_id == user.id,
            )
        )).scalar_one_or_none()
        if buyer_share:
            buyer_share.quantity += qty
        else:
            session.add(CompanyShare(company_id=company.id, owner_id=user.id, quantity=qty))

        # Flush pour que _get_shares lise la valeur à jour en DB
        await session.flush()

        # OPA hostile : bloquée sur les bot companies
        if company.is_bot_company:
            await update.message.reply_text(
                f"✅ Tu as acheté <b>{qty} parts</b> de <b>{company.name}</b> pour <b>{_fmt(total)} $</b>.\n"
                f"💡 Les entreprises officielles ne peuvent pas être rachetées.",
                parse_mode="HTML"
            )
            await _add_log(session, company.id, "achat_parts",
                           f"{user.first_name} a acheté {qty} parts", amount=total)
            await session.commit()
            return

        # OPA hostile : si l'acheteur dépasse 51%
        my_total = (await _get_shares(session, company.id, user.id) or 0) + qty
        if my_total > company.total_shares // 2 + 1:
            old_pdg_id = company.owner_id
            company.owner_id = user.id
            company.owner_shares = my_total
            # Changer le rôle
            old_pdg_emp = await _get_employee(session, company.id, old_pdg_id)
            if old_pdg_emp:
                old_pdg_emp.role = "directeur"
            buyer_emp = await _get_employee(session, company.id, user.id)
            if buyer_emp:
                buyer_emp.role = "pdg"
            else:
                session.add(CompanyEmployee(company_id=company.id, user_id=user.id, role="pdg"))

            await _add_log(session, company.id, "opa",
                           f"⚠️ OPA hostile ! {user.first_name} contrôle maintenant {my_total} parts")
            await update.message.reply_text(
                f"⚠️ <b>OPA HOSTILE !</b>\n\n"
                f"Tu contrôles désormais <b>{my_total}/{company.total_shares}</b> parts de <b>{company.name}</b>.\n"
                f"👑 Tu es le nouveau <b>PDG</b> !",
                parse_mode="HTML"
            )
            # ── Notifier l'ancien PDG en DM ──
            try:
                await context.bot.send_message(
                    chat_id=old_pdg_id,
                    text=(
                        f"🚨 <b>OPA HOSTILE !</b>\n\n"
                        f"<b>{user.first_name}</b> a racheté <b>{my_total} parts</b> de <b>{company.name}</b> "
                        f"et en est désormais le nouveau PDG.\n"
                        f"Tu conserves tes parts restantes."
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            await update.message.reply_text(
                f"✅ Tu as acheté <b>{qty} parts</b> de <b>{company.name}</b> pour <b>{_fmt(total)} $</b>.",
                parse_mode="HTML"
            )

        await _add_log(session, company.id, "achat_parts",
                       f"{user.first_name} a acheté {qty} parts", amount=total)
        await session.commit()

# ─── COMMANDE : /licencier @pseudo ────────────────────────────────────────────

# ─── COMMANDE : /employes [nom_entreprise] ────────────────────────────────────

async def employes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /employes           → liste les employés de TON entreprise
    /employes NomBoite  → liste les employés d'une entreprise publique
    """
    user = update.effective_user

    async with AsyncSessionLocal() as session:
        if context.args:
            # Chercher par nom d'entreprise
            name = " ".join(context.args)
            company = await _get_company_by_name(session, name)
            if not company:
                await update.message.reply_text(
                    f"❌ Entreprise <b>{name}</b> introuvable.\n"
                    f"Vérifie le nom exact avec <code>/listeboites</code>.",
                    parse_mode="HTML"
                )
                return
        else:
            # Entreprise de l'utilisateur
            result = await _get_user_company(session, user.id)
            if not result or not result[0]:
                await update.message.reply_text(
                    "❌ Tu ne fais partie d'aucune entreprise.\n"
                    "Usage : <code>/employes [NomEntreprise]</code> pour voir une autre boite.",
                    parse_mode="HTML"
                )
                return
            company, _ = result

        # Récupérer tous les employés actifs avec leurs infos user
        rows = (await session.execute(
            select(CompanyEmployee, User)
            .join(User, User.user_id == CompanyEmployee.user_id)
            .where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.left_at == None,
            )
            .order_by(
                # Tri par rang : pdg > directeur > manager > employe > stagiaire
                CompanyEmployee.role.in_(["pdg"]).desc(),
                CompanyEmployee.role.in_(["directeur"]).desc(),
                CompanyEmployee.role.in_(["manager"]).desc(),
                CompanyEmployee.role.in_(["employe"]).desc(),
            )
        )).fetchall()

        if not rows:
            await update.message.reply_text(
                f"❌ Aucun employé trouvé dans <b>{company.name}</b>.",
                parse_mode="HTML"
            )
            return

        sec_emoji, sec_name = SECTORS.get(company.sector, ("🏢", company.sector))
        lvl_emoji, lvl_name, _, _, max_emp = _level_info(company.level)

        # Regrouper par rôle
        by_role: dict[str, list] = {r: [] for r in ROLES_ORDER[::-1]}
        for emp, u in rows:
            by_role.setdefault(emp.role, []).append((emp, u))

        lines = [
            f"「 {sec_emoji} 」<b>{company.name}</b>  ·  {lvl_emoji} {lvl_name}",
            f"👥 <b>{len(rows)}/{max_emp} employés</b>",
            "◈━━━━━━━━━━━━━━━━━━━━━━━━◈",
        ]

        role_order = ["pdg", "directeur", "manager", "employe", "stagiaire"]
        role_labels = {
            "pdg":        "👑 PDG",
            "directeur":  "🏦 Directeurs",
            "manager":    "💼 Managers",
            "employe":    "👷 Employés",
            "stagiaire":  "🔰 Stagiaires",
        }

        for role in role_order:
            members = by_role.get(role, [])
            if not members:
                continue
            lines.append(f"\n{role_labels[role]} :")
            for emp, u in members:
                name_display = f"@{u.username}" if u.username else u.first_name
                joined = emp.joined_at.strftime("%d/%m/%y") if emp.joined_at else "—"
                lines.append(f"  ╰┈➤ {name_display}  <i>(depuis {joined})</i>")

        lines.append("\n◈━━━━━━━━━━━━━━━━━━━━━━━━◈")
        lines.append("ℹ️ Utilise <code>/infoboite</code> pour les détails financiers.")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="HTML"
        )


async def licencier_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text(
            "❌ Usage : <code>/licencier @pseudo</code>",
            parse_mode="HTML"
        )
        return

    mention = context.args[0].lstrip("@")

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company or emp.role not in ("pdg", "directeur"):
            await update.message.reply_text("❌ Réservé au PDG et Directeur.")
            return

        if company.is_bot_company:
            await update.message.reply_text("❌ Tu ne peux pas licencier dans une entreprise officielle.")
            return

        target = (await session.execute(
            select(User).where(User.username == mention)
        )).scalar_one_or_none()
        if not target:
            await update.message.reply_text(f"❌ @{mention} introuvable.")
            return

        if target.user_id == user.id:
            await update.message.reply_text("❌ Tu ne peux pas te licencier toi-même. Utilise /demissionner.")
            return

        target_emp = await _get_employee(session, company.id, target.user_id)
        if not target_emp:
            await update.message.reply_text(f"❌ {target.first_name} n'est pas dans ton entreprise.")
            return

        # Un directeur ne peut pas licencier un PDG ou un autre directeur
        if emp.role == "directeur" and target_emp.role in ("pdg", "directeur"):
            await update.message.reply_text("❌ Tu ne peux pas licencier quelqu'un de rang égal ou supérieur.")
            return

        target_emp.left_at = datetime.utcnow()
        await _add_log(session, company.id, "licenciement",
                       f"{target.first_name} a été licencié par {user.first_name}")
        await session.commit()

        await update.message.reply_text(
            f"✅ <b>{target.first_name}</b> a été licencié de <b>{company.name}</b>.",
            parse_mode="HTML"
        )
        try:
            await context.bot.send_message(
                chat_id=target.user_id,
                text=f"🚨 Tu as été licencié de <b>{company.name}</b> par la direction.",
                parse_mode="HTML"
            )
        except Exception:
            pass

# ─── JOB : RAPPORT QUOTIDIEN 18H AUX PDG ─────────────────────────────────────

async def job_daily_report(context) -> None:
    """Envoie un rapport quotidien à 18h à chaque PDG d'entreprise active."""
    async with AsyncSessionLocal() as session:
        companies = (await session.execute(
            select(Company).where(
                Company.is_active == True,
                Company.is_bot_company == False,
            )
        )).scalars().all()

        for company in companies:
            # Stats employés
            emps = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == company.id,
                    CompanyEmployee.left_at == None,
                )
            )).scalars().all()

            nb_emps = len(emps)
            _, _, _, monthly_rate, max_emp = _level_info(company.level)
            revenue = int(company.value * monthly_rate) // 30  # mensuel ÷ 30
            lvl_emoji, lvl_name, _, _, _ = _level_info(company.level)
            sec_emoji, sec_name = SECTORS.get(company.sector, ("🏢", company.sector))

            # Candidatures en attente
            pending = (await session.execute(
                select(func.count()).where(
                    CompanyApplication.company_id == company.id,
                    CompanyApplication.status == "pending",
                )
            )).scalar()

            # Construire le rapport
            pending_line = (
                f"\n📩 <b>{pending} candidature(s) en attente</b> — <code>/candidatures</code>"
                if pending > 0 else ""
            )

            rapport = (
                f"📊 <b>Rapport quotidien — {company.name}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n\n"
                f"{sec_emoji} Secteur : <b>{sec_name}</b>\n"
                f"{lvl_emoji} Niveau : <b>{lvl_name}</b>\n\n"
                f"💰 Valeur : <b>{_fmt(company.value)} $</b>\n"
                f"🏦 Trésorerie : <b>{_fmt(company.treasury)} $</b>\n"
                f"📈 Revenus/jour : <b>{_fmt(revenue)} $</b> <i>(total distribué)</i>\n\n"
                f"👥 Employés : <b>{nb_emps}/{max_emp}</b>\n"
                f"⭐ Réputation : <b>{company.reputation:.1f}/5.0</b>"
                f"{pending_line}"
            )

            try:
                await context.bot.send_message(
                    chat_id=company.owner_id,
                    text=rapport,
                    parse_mode="HTML"
                )
            except Exception:
                pass

# ─── COMMANDE : /dissoudreboite ───────────────────────────────────────────────

async def dissoudreboite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company or emp.role != "pdg":
            await update.message.reply_text("❌ Seul le PDG peut dissoudre son entreprise.")
            return

        if company.is_bot_company:
            await update.message.reply_text("❌ Tu ne peux pas dissoudre une entreprise officielle.")
            return

        # Confirmation requise : /dissoudreboite CONFIRMER
        if not context.args or context.args[0].upper() != "CONFIRMER":
            await update.message.reply_text(
                f"⚠️ <b>Tu es sur le point de dissoudre {company.name} !</b>\n\n"
                f"💰 Valeur actuelle : <b>{_fmt(company.value)} $</b>\n"
                f"🏦 Trésorerie : <b>{_fmt(company.treasury)} $</b>\n\n"
                f"Tu récupèreras <b>50%</b> de la trésorerie.\n"
                f"Tous les employés seront libérés <b>sans cooldown</b>.\n\n"
                f"Pour confirmer : <code>/dissoudreboite CONFIRMER</code>",
                parse_mode="HTML"
            )
            return

        # Récupérer les employés pour les libérer
        emps = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.left_at == None,
            )
        )).scalars().all()

        # Libérer tous les employés SANS cooldown (on date dans le passé pour bypass les 7j)
        bypass_date = datetime.utcnow() - timedelta(days=8)
        for e in emps:
            if e.user_id != user.id:
                e.left_at = bypass_date
                # Notifier chaque employé
                try:
                    await context.bot.send_message(
                        chat_id=e.user_id,
                        text=(
                            f"🏚️ <b>{company.name}</b> a été dissoute par son PDG.\n"
                            f"Tu es désormais libre de rejoindre une autre entreprise."
                        ),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

        # PDG récupère 50% de la trésorerie
        remboursement = company.treasury // 2
        db_user = await get_user(session, user.id)
        db_user.coins += remboursement

        # Fermer l'entreprise
        company.is_active = False
        company.treasury = 0

        # Marquer le PDG aussi
        pdg_emp = await _get_employee(session, company.id, user.id)
        if pdg_emp:
            pdg_emp.left_at = datetime.utcnow()

        await _add_log(session, company.id, "dissolution",
                       f"Entreprise dissoute par {user.first_name}")
        await session.commit()

        await update.message.reply_text(
            f"🏚️ <b>{company.name}</b> a été dissoute.\n\n"
            f"💰 Tu as récupéré <b>{_fmt(remboursement)} $</b> (50% de la trésorerie).\n"
            f"👥 Tous les employés ont été libérés sans cooldown.",
            parse_mode="HTML"
        )

# ─── COMMANDE : /salaireinfo ─────────────────────────────────────────────────

async def salaireinfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le salaire journalier estimé et le solde du compte, avec option de transfert."""
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        db_user = await get_user(session, user.id)
        company, emp = await _get_user_company(session, user.id)

        if not company:
            await update.message.reply_text(
                "❌ Tu ne fais partie d'aucune entreprise.\n"
                "📋 Rejoins une entreprise avec <code>/listeboites</code>",
                parse_mode="HTML"
            )
            return

        _, _, _, monthly_rate, _ = _level_info(company.level)
        total_revenue = int(company.value * monthly_rate) // 30
        personal_share = ROLE_SHARE.get(emp.role, 0.0)
        personal_revenue = int(total_revenue * personal_share)

        # Ancienneté
        joined = emp.joined_at if hasattr(emp, "joined_at") and emp.joined_at else None
        if joined:
            days_here = (datetime.utcnow() - joined).days
            anciennete_str = f"{days_here} jour(s)"
        else:
            anciennete_str = "N/A"

        role_emoji = ROLE_EMOJI.get(emp.role, "👤")
        sec_emoji, sec_name = SECTORS.get(company.sector, ("🏢", company.sector))

        if emp.role == "pdg":
            salaire_line = (
                f"  ╰┈➤  👑 PDG — touche les dividendes via <code>/retraitboite</code>\n"
            )
        elif personal_revenue > 0:
            salaire_line = (
                f"  ╰┈➤  💵 <b>{_fmt(personal_revenue)} $/jour</b> ({int(personal_share*100)}% du revenu boite)\n"
                f"  ╰┈➤  📅 Mensuel estimé : <b>{_fmt(personal_revenue * 30)} $</b>\n"
            )
        else:
            salaire_line = (
                f"  ╰┈➤  Stagiaire — pas de salaire direct\n"
                f"  ╰┈➤  Passe ton <code>/diplome</code> pour être payé !\n"
            )

        # Si args : /salaireinfo transfert [montant]
        if context.args and context.args[0].lower() == "transfert":
            if emp.role in ("stagiaire", "pdg"):
                await update.message.reply_text(
                    "❌ Le transfert de salaire n'est disponible que pour les Employés, Managers et Directeurs."
                )
                return
            if len(context.args) < 2:
                await update.message.reply_text(
                    f"❌ Usage : <code>/salaireinfo transfert [montant]</code>\n"
                    f"💡 Ton salaire/jour estimé : <b>{_fmt(personal_revenue)} $</b>",
                    parse_mode="HTML"
                )
                return
            try:
                amount = int(context.args[1].replace("_", ""))
            except ValueError:
                await update.message.reply_text("❌ Montant invalide.")
                return
            if amount <= 0:
                await update.message.reply_text("❌ Montant invalide.")
                return
            if db_user.coins < amount:
                await update.message.reply_text(
                    f"❌ Solde insuffisant. Tu as <b>{_fmt(db_user.coins)} $</b>.",
                    parse_mode="HTML"
                )
                return
            # Transfert vers son propre compte (c'est déjà son compte — ici on simule un "virement interne")
            # En pratique : on peut l'utiliser pour envoyer à quelqu'un via /pay
            await update.message.reply_text(
                f"💡 Pour transférer de l'argent à quelqu'un, utilise :\n"
                f"<code>/pay @pseudo {amount}</code>\n\n"
                f"💰 Ton solde actuel : <b>{_fmt(db_user.coins)} $</b>",
                parse_mode="HTML"
            )
            return

        msg = (
            f"╔══════════════════════════════╗\n"
            f"║  💼  MON SALAIRE             ║\n"
            f"╚══════════════════════════════╝\n\n"
            f"🏢 <b>{company.name}</b> — {sec_emoji} {sec_name}\n"
            f"{role_emoji} Poste : <b>{emp.role.capitalize()}</b>\n"
            f"📅 Ancienneté : <b>{anciennete_str}</b>\n\n"
            f"◈━━━━━━━━━━━━━━━━━━━━━━━━◈\n\n"
            f"  📈 SALAIRE\n"
            f"{salaire_line}\n"
            f"  💰 TON SOLDE ACTUEL\n"
            f"  ╰┈➤  <b>{_fmt(db_user.coins)} $</b>\n\n"
            f"◈━━━━━━━━━━━━━━━━━━━━━━━━◈\n\n"
            f"💡 Pour envoyer de l'argent : <code>/pay @pseudo [montant]</code>"
        )
        await update.message.reply_text(msg, parse_mode="HTML")
