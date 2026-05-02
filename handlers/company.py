"""
handlers/company.py — Système d'entreprises complet
Commandes : /entreprise, /creerboite, /postuler, /recruter, /demissionner,
            /nommer, /parts, /vendreparts, /acheterparts,
            /depotboite, /retraitboite, /infoboite, /logsboite,
            /candidatures, /saboter, /listeboites
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text, select, func
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

LEVELS = {
    1: ("🏪", "Startup",     50_000_000,    0.01, 5),
    2: ("🏢", "PME",         200_000_000,   0.02, 10),
    3: ("🏬", "Société",     500_000_000,   0.03, 20),
    4: ("🏦", "Corporation", 2_000_000_000, 0.04, 40),
    5: ("👑", "Holding",     10_000_000_000,0.05, 100),
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
    from database.db import engine
    from database.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Créer les entreprises du bot si elles n'existent pas
    async with AsyncSessionLocal() as session:
        for bc in BOT_COMPANIES:
            exists = await _get_company_by_name(session, bc["name"])
            if not exists:
                # Bot owner_id = 0 (fictif)
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
                # Partager en bourse (le bot détient 100 parts)
        await session.commit()
    logger.info("Tables entreprises initialisées.")


# ─── JOB : REVENUS AUTOMATIQUES (toutes les 24h) ──────────────────────────────

async def job_company_revenues(context: ContextTypes.DEFAULT_TYPE):
    """Distribue les revenus des entreprises à tous les employés actifs."""
    async with AsyncSessionLocal() as session:
        companies = (await session.execute(
            select(Company).where(Company.is_active == True)
        )).scalars().all()

        for company in companies:
            _, _, _, daily_rate, _ = _level_info(company.level)
            revenue = int(company.value * daily_rate)
            if revenue <= 0:
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

        await session.commit()


# ─── JOB : BONUS ACTIVITÉ BOT (compte les commandes des employés) ────────────

async def update_company_activity(user_id: int):
    """Appelé par le middleware à chaque commande. Ajoute de la valeur à l'entreprise."""
    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user_id)
        if not company or not emp:
            return
        emp.command_count += 1
        # +50 000 $ par commande utilisée par un employé
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
            f"📈 <b>Revenus/jour</b> : {_fmt(int(company.value * daily_rate))}\n"
            f"⭐ <b>Réputation</b> : {company.reputation:.1f}/5\n"
            f"👤 <b>PDG</b> : {owner_name}\n"
            f"👥 <b>Employés</b> : {nb_emp}/{max_emp}\n"
            f"🎂 <b>Fondée le</b> : {company.created_at.strftime('%d/%m/%Y')}\n\n"
            f"◈━━━━━━━━━━━━━━━━━━━━━━━━◈\n"
            f"📦 <b>Parts</b> : {company.owner_shares}/{company.total_shares} détenues par le PDG"
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

        # Vérifier nom unique
        exists = await _get_company_by_name(session, name)
        if exists:
            await update.message.reply_text(f"❌ Une entreprise nommée <b>{name}</b> existe déjà.", parse_mode="HTML")
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
        await session.flush()

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


# ─── COMMANDE : /postuler [nom entreprise] ───────────────────────────────────

async def postuler_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("❌ Usage : <code>/postuler [nom de l'entreprise]</code>", parse_mode="HTML")
        return

    name = " ".join(context.args)
    async with AsyncSessionLocal() as session:
        db_user = await get_user(session, user.id)

        if not db_user.diplome_bac:
            await update.message.reply_text(
                "❌ Il te faut au minimum le <b>Bac</b> pour postuler.\n"
                "Passe ton diplôme avec <code>/diplome</code>",
                parse_mode="HTML"
            )
            return

        # Déjà dans une boite ?
        company, emp = await _get_user_company(session, user.id)
        if company:
            await update.message.reply_text(
                f"❌ Tu es déjà dans <b>{company.name}</b>. Démissionne d'abord.",
                parse_mode="HTML"
            )
            return

        # Cooldown démission (7 jours)
        last_left = (await session.execute(
            select(func.max(CompanyEmployee.left_at)).where(
                CompanyEmployee.user_id == user.id
            )
        )).scalar()
        if last_left and (datetime.utcnow() - last_left).days < 7:
            jours = 7 - (datetime.utcnow() - last_left).days
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

        # Rôle selon diplôme
        role = "stagiaire"
        if candidate and candidate.diplome_mba:      role = "directeur"
        elif candidate and candidate.diplome_master: role = "manager"
        elif candidate and candidate.diplome_licence:role = "manager"
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
        await session.commit()
        await update.message.reply_text("✅ Candidature refusée.")


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


# ─── COMMANDE : /rejoindre [entreprise] ──────────────────────────────────────

async def rejoindre_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("❌ Usage : <code>/rejoindre [nom entreprise]</code>", parse_mode="HTML")
        return
    name = " ".join(context.args)

    async with AsyncSessionLocal() as session:
        # Cooldown
        last_left = (await session.execute(
            select(func.max(CompanyEmployee.left_at)).where(
                CompanyEmployee.user_id == user.id
            )
        )).scalar()
        if last_left and (datetime.utcnow() - last_left).days < 7:
            jours = 7 - (datetime.utcnow() - last_left).days
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

        emp.left_at = datetime.utcnow()
        await _add_log(session, company.id, "demission", f"{user.first_name} a démissionné ({emp.role})")
        await session.commit()

        await update.message.reply_text(
            f"👋 Tu as quitté <b>{company.name}</b>.\n"
            f"⏳ Cooldown de 7 jours avant de pouvoir rejoindre une autre entreprise.",
            parse_mode="HTML"
        )


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

        msg = (
            f"「 {sec_emoji} 」<b>{company.name}</b>\n"
            f"✦ {lvl_emoji} {lvl_name}  ┊  ⭐ {company.reputation:.1f}/5\n\n"
            f"◈━━━━━━━━━━━━━━━━━━━━━━━━◈\n\n"
            f"  💰 VALEUR\n"
            f"  ╰┈➤  {_fmt(company.value)}\n\n"
            f"  🏦 TRÉSORERIE\n"
            f"  ╰┈➤  {_fmt(company.treasury)}\n\n"
            f"  📈 REVENUS/JOUR\n"
            f"  ╰┈➤  {_fmt(int(company.value * daily_rate))}\n\n"
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
            "sabotage":    "💣",
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

        if qty > can_sell:
            await update.message.reply_text(
                f"❌ Tu ne peux vendre que <b>{can_sell}</b> parts (PDG garde ≥51%).",
                parse_mode="HTML"
            )
            return

        if qty <= 0 or my_shares < qty:
            await update.message.reply_text(f"❌ Tu n'as que {my_shares} parts.")
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
        company.owner_shares += qty  # retour dans le pool du PDG
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
        else:
            await update.message.reply_text(
                f"✅ Tu as acheté <b>{qty} parts</b> de <b>{company.name}</b> pour <b>{_fmt(total)} $</b>.",
                parse_mode="HTML"
            )

        await _add_log(session, company.id, "achat_parts",
                       f"{user.first_name} a acheté {qty} parts", amount=total)
        await session.commit()


# ─── COMMANDE : /saboter [entreprise] ────────────────────────────────────────

async def saboter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("❌ Usage : <code>/saboter [nom entreprise]</code>", parse_mode="HTML")
        return
    name = " ".join(context.args)

    async with AsyncSessionLocal() as session:
        db_user = await get_user(session, user.id)
        company, emp = await _get_user_company(session, user.id)

        target = await _get_company_by_name(session, name)
        if not target:
            await update.message.reply_text(f"❌ Entreprise <b>{name}</b> introuvable.", parse_mode="HTML")
            return

        if company and company.id == target.id:
            await update.message.reply_text("❌ Tu ne peux pas saboter ta propre entreprise.")
            return

        cost = int(target.value * 0.02)  # coût = 2% valeur cible
        if db_user.coins < cost:
            await update.message.reply_text(
                f"❌ Saboter <b>{target.name}</b> coûte <b>{_fmt(cost)} $</b>.\n"
                f"Ton solde : {_fmt(db_user.coins)} $",
                parse_mode="HTML"
            )
            return

        db_user.coins -= cost
        damage = int(target.value * 0.05)  # dommages = 5% valeur
        target.value = max(1_000_000, target.value - damage)
        target.reputation = max(0.0, target.reputation - 0.3)
        await _update_level(session, target)
        await _add_log(session, target.id, "sabotage",
                       f"Sabotage par un inconnu — perte de {_fmt(damage)} $",
                       amount=damage)
        await session.commit()

        await update.message.reply_text(
            f"💣 <b>Sabotage réussi !</b>\n\n"
            f"🎯 Cible : <b>{target.name}</b>\n"
            f"💥 Dégâts : <b>-{_fmt(damage)} $</b> de valeur\n"
            f"⭐ Réputation : <b>-0.3</b>\n"
            f"💸 Coût : <b>-{_fmt(cost)} $</b>",
            parse_mode="HTML"
        )
