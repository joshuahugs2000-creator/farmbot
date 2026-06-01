"""
handlers/company.py — Système d'entreprises complet
Commandes : /entreprise, /creerboite, /postuler, /recruter, /demissionner,
            /nommer, /parts, /vendreparts, /acheterparts,
            /depotboite, /retraitboite, /infoboite, /logsboite,
            /candidatures, /licencier, /listeboites, /cederentreprise,
            /presences, /versersalaires (PDG), /payeremploye (PDG), /renommerboite (PDG)

Hiérarchie des postes (du plus bas au plus haut) :
  🔰 Stagiaire → 🗂️ Secrétaire → 👷 Employé → 💼 Manager → 🏦 Directeur → 👑 PDG → 👑 PDG

Système de paie :
  PDG → /versersalaires  (calcul automatique selon rôle + activité, cooldown 12h)
  PDG → /payeremploye @pseudo [montant]  (montant libre, depuis la trésorerie)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, func, update as sa_update, text as sa_text
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from database.db import AsyncSessionLocal, get_user
from utils.helpers import ensure_user
from database.models import (
    User, Company, CompanyEmployee, CompanyShare,
    CompanyApplication, CompanyInvite, CompanyLog, CompanyShareOffer,
)
from handlers.journal import log_event

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
    "sante":       ["sante", "management"],  # Fix : domaine "sante" ajouté
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

ROLES_ORDER = ["stagiaire", "secretaire", "employe", "manager", "directeur", "pdg"]
ROLE_EMOJI  = {
    "stagiaire":  "🔰",
    "secretaire": "🗂️",
    "employe":    "👷",
    "manager":    "💼",
    "directeur":  "🏦",
    "pdg":        "👑",
}
ROLE_DIPLOMA = {
    "stagiaire":  None,
    "secretaire": "bac",
    "employe":    "bac",
    "manager":    "licence",
    "directeur":  "master",
    "pdg":        "mba",
}

# Revenus par rôle (% des revenus journaliers de l'entreprise)
# Le PDG et le PDG reçoivent leur part via /versersalaires.
# /retraitboite reste disponible pour des retraits ponctuels.
ROLE_SHARE = {
    "stagiaire":  0.00,
    "secretaire": 0.05,
    "employe":    0.10,
    "manager":    0.20,
    "directeur":  0.30,
    "pdg":        0.35,
}

# Rôles autorisés à déclencher /versersalaires
PAYROLL_ROLES = ("pdg",)

# Rôles de direction (candidatures, recrutement, licenciement)
DIRECTION_ROLES = ("pdg", "directeur")

# Rôles pouvant nommer des employés
MANAGEMENT_ROLES = ("pdg", "directeur", "manager")

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

def _max_employees(company) -> int:
    """Capacité réelle = capacité du niveau + places supplémentaires achetées."""
    _, _, _, _, base_max = LEVELS.get(company.level, LEVELS[1])
    return base_max + (company.extra_slots or 0)


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
        ).order_by(CompanyEmployee.id.desc())  # plus récent en premier
        .limit(1)  # jamais de crash sur doublon
    )
    return r.scalar_one_or_none()


async def _get_user_company(session, user_id: int) -> Optional[tuple[Company, CompanyEmployee]]:
    """Retourne (company, employee) si le user est dans une entreprise active.
    
    Priorité : PDG > directeur > autres rôles.
    Cela évite qu'un PDG membre d'une autre boite en tant qu'employé
    récupère la mauvaise entreprise (bug "ça marche pour certains").
    """
    r = await session.execute(
        select(CompanyEmployee, Company).join(
            Company, Company.id == CompanyEmployee.company_id
        ).where(
            CompanyEmployee.user_id == user_id,
            CompanyEmployee.left_at == None,
            Company.is_active == True,
        ).order_by(
            # Tri par importance du rôle : pdg > directeur > manager > … 
            # ROLES_ORDER = ["stagiaire","secretaire","employe","manager","directeur","pdg"]
            # On utilise case() pour mapper l'ordre sans dépendre de l'ordre alphabétique.
            CompanyEmployee.role.in_(["pdg"]).desc(),
            CompanyEmployee.role.in_(["directeur"]).desc(),
            CompanyEmployee.role.in_(["manager"]).desc(),
        )
    )
    row = r.first()
    if row:
        return row[1], row[0]
    return None, None


async def _add_log(session, company_id: int, event_type: str, description: str, amount: int = None):
    try:
        from sqlalchemy import text as _text
        await session.execute(
            _text(
                "INSERT INTO company_logs (company_id, event_type, description, amount, created_at) "
                "VALUES (:cid, :etype, :desc, :amt, NOW())"
            ),
            {"cid": company_id, "etype": event_type, "desc": description[:500], "amt": amount},
        )
    except Exception:
        pass


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
    # Importer les systèmes sectoriels (bonus diplôme + contrats)
    try:
        from handlers.company_sector import get_diploma_bonus, get_all_active_contracts, get_contract_bonus
        active_contracts = None  # chargé une seule fois ci-dessous
    except ImportError:
        get_diploma_bonus = None
        active_contracts = None

    async with AsyncSessionLocal() as session:
        companies = (await session.execute(
            select(Company).where(Company.is_active == True)
        )).scalars().all()

        # Charger tous les contrats actifs une seule fois
        if get_diploma_bonus:
            try:
                from handlers.company_sector import get_all_active_contracts, get_contract_bonus
                active_contracts = await get_all_active_contracts(session)
            except Exception:
                active_contracts = []

        for company in companies:
            _, _, _, monthly_rate, _ = _level_info(company.level)
            base_revenue = int(company.value * monthly_rate) // 30  # versement journalier = taux mensuel ÷ 30
            if base_revenue <= 0:
                continue

            # ── Bonus diplôme PDG ──────────────────────────────────────────
            diploma_bonus_rate = 0.0
            if get_diploma_bonus and not company.is_bot_company:
                pdg_user = await session.get(User, company.owner_id)
                if pdg_user:
                    diploma_bonus_rate = get_diploma_bonus(pdg_user, company.sector)

            # ── Bonus contrats actifs ──────────────────────────────────────
            contract_bonus_rate = 0.0
            if active_contracts is not None and not company.is_bot_company:
                contract_bonus_rate = get_contract_bonus(company.id, active_contracts)

            # ── Revenu final avec tous les bonus ──────────────────────────
            total_bonus_rate = diploma_bonus_rate + contract_bonus_rate
            revenue = int(base_revenue * (1 + total_bonus_rate))

            # Bot company : pas d'accumulation en trésorerie — vider automatiquement
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
                # Bot company avec employés : payer directement les employés (pas via trésorerie)
                # pour éviter que la caisse gonfle sans PDG pour la vider
                emps = (await session.execute(
                    select(CompanyEmployee).where(
                        CompanyEmployee.company_id == company.id,
                        CompanyEmployee.left_at == None,
                    )
                )).scalars().all()
                for e in emps:
                    share = ROLE_SHARE.get(e.role, 0)
                    if share <= 0:
                        continue
                    emp_pay = int(revenue * share)
                    emp_user = await session.get(User, e.user_id)
                    if emp_user:
                        emp_user.coins += emp_pay
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

            # Fix 9 : Morale des employés — perte de valeur si PDG ne paie pas depuis 3 jours
            if not company.is_bot_company:
                last_pay = company.last_payroll
                if last_pay:
                    days_since_pay = (datetime.utcnow() - last_pay).days
                    if days_since_pay >= 3:
                        # -0.5% de valeur par jour sans paie (après 3 jours de grâce)
                        morale_penalty = 0.005 * (days_since_pay - 2)
                        morale_penalty = min(morale_penalty, 0.05)  # max -5% par jour
                        company.value = int(company.value * (1 - morale_penalty))
                        if company.reputation > 1.0:
                            company.reputation = max(1.0, company.reputation - 0.05)

                # ── Réserve légale : 10% du revenu brut bloqué automatiquement ──
                legal_share = int(revenue * 0.10)
                net_revenue = revenue - legal_share
                company.legal_reserve = (company.legal_reserve or 0) + legal_share

                # ── Remboursement automatique du prêt actif ──────────────────────
                try:
                    from database.models import CompanyLoan
                    loan_res = await session.execute(
                        select(CompanyLoan).where(
                            CompanyLoan.company_id == company.id,
                            CompanyLoan.status == "active",
                        )
                    )
                    active_loan = loan_res.scalar_one_or_none()
                    if active_loan:
                        payment = min(active_loan.daily_payment, active_loan.remaining)
                        if company.treasury >= payment:
                            company.treasury -= payment
                            company.value = max(LEVELS[1][2], company.value - payment)
                            active_loan.remaining -= payment
                            active_loan.missed_days = 0
                            if active_loan.remaining <= 0:
                                active_loan.status = "repaid"
                                await _add_log(session, company.id, "pret",
                                               "✅ Prêt bancaire entièrement remboursé !")
                        else:
                            # Trésorerie insuffisante : pénalité
                            active_loan.missed_days = (active_loan.missed_days or 0) + 1
                            penalty = int(company.value * 0.01)  # -1% valeur
                            company.value = max(1_000_000, company.value - penalty)
                            if company.reputation > 1.0:
                                company.reputation = max(1.0, company.reputation - 0.1)
                            await _add_log(session, company.id, "pret",
                                           f"⚠️ Remboursement prêt impossible (trésorerie insuffisante) — pénalité appliquée")
                except Exception:
                    pass

                # ── Revenus nets → trésorerie ─────────────────────────────────
                company.treasury += net_revenue
                company.weekly_revenue = (company.weekly_revenue or 0) + net_revenue
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

REVENUE_PER_CMD = 10_000  # 1 commande employé = 10 000 $ en trésorerie

# Throttle : on ne met à jour qu'une fois par 60s par user pour éviter de saturer le pool DB
_company_activity_last: dict[int, float] = {}
_ACTIVITY_THROTTLE = 30  # secondes entre deux mises à jour par user

async def update_company_activity(user_id: int):
    """Appelé à chaque commande — throttlé à 1 fois/30s par user pour ne pas épuiser le pool."""
    import time
    now = time.monotonic()
    last = _company_activity_last.get(user_id, 0)
    if now - last < _ACTIVITY_THROTTLE:
        return
    _company_activity_last[user_id] = now

    try:
        from sqlalchemy import text as _text
        async with AsyncSessionLocal() as session:
            # Incrémenter command_count et activity_since_payroll en SQL pur
            await session.execute(_text("""
                UPDATE company_employees
                SET command_count = COALESCE(command_count, 0) + 1,
                    activity_since_payroll = COALESCE(activity_since_payroll, 0) + 1
                WHERE user_id = :uid AND left_at IS NULL
                  AND company_id IN (SELECT id FROM companies WHERE is_active = TRUE)
            """), {"uid": user_id})

            # Mettre à jour last_active des entreprises concernées
            await session.execute(_text("""
                UPDATE companies SET last_active = NOW()
                WHERE id IN (
                    SELECT company_id FROM company_employees
                    WHERE user_id = :uid AND left_at IS NULL
                ) AND is_active = TRUE
            """), {"uid": user_id})

            # Promotion stagiaire → employé si 50 commandes et bac validé
            await session.execute(_text("""
                UPDATE company_employees ce
                SET role = 'employe'
                FROM users u
                WHERE ce.user_id = :uid
                  AND ce.role = 'stagiaire'
                  AND ce.command_count >= 50
                  AND u.user_id = ce.user_id
                  AND u.diplome_bac = TRUE
                  AND ce.left_at IS NULL
            """), {"uid": user_id})

            # Incrémenter cmds_done des contrats bureau actifs
            await session.execute(_text("""
                UPDATE bureau_contrats bc
                SET cmds_done = COALESCE(cmds_done, 0) + 1
                WHERE bc.status = 'active'
                  AND bc.company_id IN (
                      SELECT company_id FROM company_employees
                      WHERE user_id = :uid AND left_at IS NULL
                  )
            """), {"uid": user_id})

            await session.commit()
    except Exception:
        pass  # Ne jamais bloquer une commande à cause de l'activité

        await session.commit()


# ─── INCREMENT CONTRATS : appelé à chaque commande, sans throttle ────────────

async def increment_contract_progress(user_id: int):
    """Incrémente cmds_done des contrats actifs de l'entreprise de l'employé.
    Léger : 1 SELECT + 1 UPDATE atomique. Pas de throttle."""
    try:
        async with AsyncSessionLocal() as session:
            # Trouver les entreprises actives où le user est employé
            rows = (await session.execute(
                select(CompanyEmployee.company_id).where(
                    CompanyEmployee.user_id == user_id,
                    CompanyEmployee.left_at == None,
                )
            )).scalars().all()
            if not rows:
                return
            # UPDATE atomique en SQL pur — pas de race condition
            for company_id in rows:
                await session.execute(
                    sa_text("""
                        UPDATE bureau_contrats 
                        SET cmds_done = COALESCE(cmds_done, 0) + 1
                        WHERE company_id = :cid AND status = 'active'
                    """),
                    {"cid": company_id}
                )
                await session.execute(
                    sa_text("""
                        UPDATE company_auto_contracts 
                        SET cmds_done = COALESCE(cmds_done, 0) + 1
                        WHERE company_id = :cid AND status = 'active'
                    """),
                    {"cid": company_id}
                )
            await session.commit()
    except Exception:
        pass  # Ne jamais bloquer une commande


# ─── COMMANDE : /listeboites ──────────────────────────────────────────────────

PAGE_SIZE = 8  # entreprises par page

def _build_listeboites_page(companies: list, page: int, total: int) -> tuple[str, InlineKeyboardMarkup]:
    """Construit le message et les boutons de navigation pour une page du classement."""
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_companies = companies[start:end]
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

    lvl_icons = {1: "🏪", 2: "🏢", 3: "🏬", 4: "🏦", 5: "👑"}
    rank_icons = {1: "🥇", 2: "🥈", 3: "🥉"}

    lines = [
        f"🏢 <b>ANNUAIRE DES ENTREPRISES</b>",
        f"<i>Page {page + 1}/{total_pages} · {total} entreprises actives</i>",
        "─────────────────────────────",
    ]

    for i, c in enumerate(page_companies, start + 1):
        sec_emoji, sec_name = SECTORS.get(c.sector, ("🏢", c.sector))
        lvl_emoji = lvl_icons.get(c.level, "🏢")
        bot_tag = " 🤖" if c.is_bot_company else ""
        rank_icon = rank_icons.get(i, f"<b>{i}.</b>")

        lines.append(
            f"{rank_icon} {sec_emoji} <b>{c.name}</b>{bot_tag}\n"
            f"    {lvl_emoji} · 💰 {_fmt(c.treasury)} $ · ⭐ {c.reputation:.1f}/5"
        )

    lines.append("─────────────────────────────")
    lines.append("💡 <code>/infoboite [nom]</code> · <code>/postuler [nom]</code>")

    # Boutons navigation
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀ Précédent", callback_data=f"lb:{page - 1}"))
    if end < total:
        nav_buttons.append(InlineKeyboardButton("Suivant ▶", callback_data=f"lb:{page + 1}"))

    keyboard = []
    if nav_buttons:
        keyboard.append(nav_buttons)

    # Boutons secteurs (filtre rapide)
    sector_row1 = [
        InlineKeyboardButton("💻", callback_data="lb_sec:tech:0"),
        InlineKeyboardButton("📈", callback_data="lb_sec:finance:0"),
        InlineKeyboardButton("🛒", callback_data="lb_sec:commerce:0"),
        InlineKeyboardButton("🌾", callback_data="lb_sec:agriculture:0"),
    ]
    sector_row2 = [
        InlineKeyboardButton("🏥", callback_data="lb_sec:sante:0"),
        InlineKeyboardButton("⚖️", callback_data="lb_sec:droit:0"),
        InlineKeyboardButton("🛡️", callback_data="lb_sec:securite:0"),
        InlineKeyboardButton("🏗️", callback_data="lb_sec:immobilier:0"),
    ]
    keyboard.append(sector_row1)
    keyboard.append(sector_row2)
    if "lb_sec:" in "".join(b.callback_data for row in keyboard for b in row):
        keyboard.append([InlineKeyboardButton("🔄 Tous les secteurs", callback_data="lb:0")])

    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def listeboites_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncSessionLocal() as session:
        companies = (await session.execute(
            select(Company).where(Company.is_active == True).order_by(Company.treasury.desc())
        )).scalars().all()

        if not companies:
            await update.message.reply_text("❌ Aucune entreprise active.")
            return

        text, markup = _build_listeboites_page(companies, 0, len(companies))
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def listeboites_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère la pagination et le filtre secteur de /listeboites."""
    query = update.callback_query
    await query.answer()
    data = query.data  # "lb:2" ou "lb_sec:tech:1"

    async with AsyncSessionLocal() as session:
        if data.startswith("lb_sec:"):
            _, sector, page_str = data.split(":")
            page = int(page_str)
            companies = (await session.execute(
                select(Company).where(
                    Company.is_active == True,
                    Company.sector == sector,
                ).order_by(Company.treasury.desc())
            )).scalars().all()

            if not companies:
                await query.edit_message_text(f"❌ Aucune entreprise dans ce secteur.", parse_mode="HTML")
                return

            sec_emoji, sec_name = SECTORS.get(sector, ("🏢", sector))
            total = len(companies)
            start = page * PAGE_SIZE
            end = start + PAGE_SIZE
            page_companies = companies[start:end]
            total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
            lvl_icons = {1: "🏪", 2: "🏢", 3: "🏬", 4: "🏦", 5: "👑"}
            rank_icons = {1: "🥇", 2: "🥈", 3: "🥉"}

            lines = [
                f"{sec_emoji} <b>ENTREPRISES — {sec_name.upper()}</b>",
                f"<i>Page {page + 1}/{total_pages} · {total} entreprises</i>",
                "─────────────────────────────",
            ]
            for i, c in enumerate(page_companies, start + 1):
                lvl_emoji = lvl_icons.get(c.level, "🏢")
                bot_tag = " 🤖" if c.is_bot_company else ""
                rank_icon = rank_icons.get(i, f"<b>{i}.</b>")
                lines.append(
                    f"{rank_icon} <b>{c.name}</b>{bot_tag}\n"
                    f"    {lvl_emoji} · 💰 {_fmt(c.treasury)} $ · ⭐ {c.reputation:.1f}/5"
                )
            lines.append("─────────────────────────────")
            lines.append("💡 <code>/infoboite [nom]</code> · <code>/postuler [nom]</code>")

            nav_buttons = []
            if page > 0:
                nav_buttons.append(InlineKeyboardButton("◀", callback_data=f"lb_sec:{sector}:{page - 1}"))
            if end < total:
                nav_buttons.append(InlineKeyboardButton("▶", callback_data=f"lb_sec:{sector}:{page + 1}"))

            keyboard = []
            if nav_buttons:
                keyboard.append(nav_buttons)
            keyboard.append([InlineKeyboardButton("🔄 Tous les secteurs", callback_data="lb:0")])

            await query.edit_message_text(
                "\n".join(lines),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        else:  # "lb:page"
            page = int(data.split(":")[1])
            companies = (await session.execute(
                select(Company).where(Company.is_active == True).order_by(Company.treasury.desc())
            )).scalars().all()

            text, markup = _build_listeboites_page(companies, page, len(companies))
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)


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
        lvl_emoji, lvl_name, _, daily_rate, _ = _level_info(company.level)
        max_emp = _max_employees(company)

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
            f"📈 <b>Revenus/jour</b> : {_fmt(int(company.value * daily_rate) // 30)} <i>(→ trésorerie, versés via /versersalaires)</i>\n"
            f"⭐ <b>Réputation</b> : {company.reputation:.1f}/5\n"
            f"💎 <b>PDG / PDG</b> : {owner_name}\n"
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
        if company and emp.role == "pdg":
            await update.message.reply_text(
                f"❌ Tu es déjà PDG de <b>{company.name}</b>.\n"
                f"Un PDG ne peut pas créer une deuxième entreprise.",
                parse_mode="HTML"
            )
            return
        # Si employé (non-PDG) : autorisé à créer sa propre boite en parallèle

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

        # Le fondateur devient PDG (rang maximum, fondateur de l'entreprise)
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

        try:
            await _add_log(session, new_company.id, "creation",
                           f"Entreprise créée par {user.first_name}")
            await session.commit()
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
            # Fallback SQL pur — garantit que company + PDG sont bien en DB
            from sqlalchemy import text as _txt
            async with AsyncSessionLocal() as s2:
                try:
                    await s2.execute(_txt(
                        "INSERT INTO company_employees (company_id, user_id, role) "
                        "VALUES (:c, :u, 'pdg') ON CONFLICT DO NOTHING"
                    ), {"c": new_company.id, "u": user.id})
                    await s2.execute(_txt(
                        "INSERT INTO company_shares (company_id, owner_id, quantity) "
                        "VALUES (:c, :u, 100) ON CONFLICT DO NOTHING"
                    ), {"c": new_company.id, "u": user.id})
                    await s2.commit()
                except Exception:
                    pass

        sec_emoji, sec_name = SECTORS[sector]
        await log_event("company_created", owner=user.first_name, name=new_company.name, sector=sec_name)
        await update.message.reply_text(
            f"✅ <b>{new_company.name}</b> est fondée !\n\n"
            f"{sec_emoji} Secteur : <b>{sec_name}</b>\n"
            f"💰 Capital initial : <b>50 000 000 $</b>\n"
            f"📦 Parts : <b>100/100</b> (tu détiens tout)\n"
            f"💎 Tu es le <b>PDG</b> (fondateur)\n\n"
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
    if lvl >= 3: return "manager"   # Master ou MBA → manager max (jamais directeur auto)
    if lvl >= 2: return "employe"   # Licence → employé
    return "stagiaire"              # Bac → stagiaire


# ─── COMMANDE : /postuler [nom entreprise] ───────────────────────────────────

async def postuler_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user)
    if not context.args:
        await update.message.reply_text("❌ Usage : <code>/postuler [nom de l'entreprise]</code>", parse_mode="HTML")
        return

    name = " ".join(context.args)
    async with AsyncSessionLocal() as session:
        db_user = await get_user(session, user.id)

        # Déjà dans une ou deux boites ?
        all_emps = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.user_id == user.id,
                CompanyEmployee.left_at == None,
            )
        )).scalars().all()

        if len(all_emps) >= 2:
            noms = []
            for e in all_emps:
                c = await session.get(Company, e.company_id)
                if c:
                    noms.append(f"<b>{c.name}</b>")
            await update.message.reply_text(
                f"❌ Tu es déjà dans 2 entreprises ({' & '.join(noms)}).\n"
                f"Maximum 2 entreprises simultanées.",
                parse_mode="HTML"
            )
            return

        # Vérifier si PDG (pour forcer le rôle employé)
        own_company = (await session.execute(
            select(Company).where(
                Company.owner_id == user.id,
                Company.is_active == True,
                Company.is_bot_company == False,
            )
        )).scalar_one_or_none()
        is_pdg_elsewhere = own_company is not None

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
                if days_passed < 3:
                    jours = 3 - days_passed
                    await update.message.reply_text(
                        f"⏳ Tu dois attendre encore <b>{jours} jour(s)</b> avant de rejoindre une nouvelle entreprise.\n"
                        f"💡 Tape <code>/skipattente</code> pour payer et ignorer ce délai.",
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
        max_emp = _max_employees(target)
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

        pdg_note = (
            f"\n⚠️ Tu es PDG de <b>{own_company.name}</b>. Si tu es recruté, tu seras considéré comme <b>Employé</b> dans cette entreprise."
            if is_pdg_elsewhere else ""
        )
        await update.message.reply_text(
            f"📩 Ta candidature pour <b>{target.name}</b> a été envoyée !\n"
            f"Le PDG ou les Directeurs vont l'examiner.{pdg_note}",
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
        if not company or emp.role not in DIRECTION_ROLES:
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
        if not company or emp.role not in DIRECTION_ROLES:
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

        # Vérifier capacité avant d'accepter
        max_emp = _max_employees(company)
        nb_emp = (await session.execute(
            select(func.count()).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.left_at == None,
            )
        )).scalar()
        if nb_emp >= max_emp:
            await update.message.reply_text(
                f"❌ <b>{company.name}</b> est au complet ({nb_emp}/{max_emp} employés).\n"
                f"Améliore la réputation de l'entreprise pour augmenter la capacité.",
                parse_mode="HTML"
            )
            return

        # Rôle selon diplôme (cohérent avec ROLE_DIPLOMA)
        role = "stagiaire"
        if candidate and candidate.diplome_mba:      role = "directeur"
        elif candidate and candidate.diplome_master: role = "manager"   # Master → manager (directeur réservé au MBA)
        elif candidate and candidate.diplome_licence:role = "employe"   # Licence → employe
        elif candidate and candidate.diplome_bac:    role = "employe"

        # Si le candidat est PDG d'une autre entreprise → forcer employe
        is_pdg_elsewhere = False
        if candidate:
            own_co = (await session.execute(
                select(Company).where(
                    Company.owner_id == candidate.user_id,
                    Company.is_active == True,
                    Company.is_bot_company == False,
                )
            )).scalar_one_or_none()
            if own_co:
                is_pdg_elsewhere = True
                role = "employe"

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
        pdg_note = f"\n⚠️ PDG d'une autre entreprise — rôle limité à <b>Employé</b>." if is_pdg_elsewhere else ""
        await update.message.reply_text(
            f"✅ <b>{candidate.first_name if candidate else target_id}</b> a rejoint <b>{company.name}</b> "
            f"en tant que {role_emoji} <b>{role.capitalize()}</b> !{pdg_note}",
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
        if not company or emp.role not in DIRECTION_ROLES:
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
    """
    /recruter @pseudo [poste] [salaire_jour] [prime_optionnelle]
    Le PDG invite un utilisateur et peut lui proposer directement un contrat.
    Exemples :
      /recruter @jean employe 50000         → invite + propose 50 000 $/jour
      /recruter @jean employe 50000 10000   → invite + 50 000 $/jour + prime 10 000 $
      /recruter @jean employe               → invite sans contrat (l'employé rejoint comme stagiaire/poste)
    """
    user = update.effective_user
    if not context.args:
        await update.message.reply_text(
            "❌ Usage : <code>/recruter @pseudo [poste] [salaire/jour] [prime]</code>\n\n"
            "Exemples :\n"
            "• <code>/recruter @jean employe 50000</code> — invite + contrat 50k$/j\n"
            "• <code>/recruter @jean employe 50000 10000</code> — avec prime de 10k$",
            parse_mode="HTML"
        )
        return

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company or emp.role not in MANAGEMENT_ROLES:
            await update.message.reply_text("❌ Tu dois être au moins Manager pour recruter.")
            return

        # Parser les arguments
        mention = context.args[0].lstrip("@")
        role_arg = context.args[1].lower() if len(context.args) > 1 else "employe"
        if role_arg not in ROLES_ORDER or role_arg == "pdg":
            role_arg = "employe"

        proposed_salary = 0
        proposed_bonus = 0
        if len(context.args) > 2:
            try:
                proposed_salary = int(context.args[2].replace("_", "").replace(" ", ""))
            except ValueError:
                await update.message.reply_text("❌ Salaire invalide. Exemple : <code>/recruter @jean employe 50000</code>", parse_mode="HTML")
                return
        if len(context.args) > 3:
            try:
                proposed_bonus = int(context.args[3].replace("_", "").replace(" ", ""))
            except ValueError:
                proposed_bonus = 0

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

        # Créer l'invitation (avec salaire proposé si renseigné)
        invite = CompanyInvite(
            company_id=company.id,
            target_id=target.user_id,
            role=role_arg,
            invited_by=user.id,
            status="pending",
            expires_at=datetime.utcnow() + timedelta(hours=48),
        )
        # Stocker la proposition de contrat dans l'invite via des attributs dynamiques
        # On encode salaire/bonus dans le champ role étendu via un séparateur
        if proposed_salary > 0:
            invite.role = f"{role_arg}|{proposed_salary}|{proposed_bonus}"
        session.add(invite)
        await session.commit()

        role_emoji = ROLE_EMOJI.get(role_arg, "👤")

        # Construire le message de l'invitation
        contrat_info = ""
        if proposed_salary > 0:
            contrat_info = (
                f"\n\n📄 <b>Contrat proposé :</b>\n"
                f"   💰 Salaire : <b>{_fmt(proposed_salary)} $/jour</b>\n"
            )
            if proposed_bonus > 0:
                contrat_info += f"   🎁 Prime à la signature : <b>{_fmt(proposed_bonus)} $</b>\n"
            contrat_info += "\nIl peut <b>accepter</b>, <b>refuser</b> ou <b>contre-proposer</b> un salaire."

        await update.message.reply_text(
            f"📩 Invitation envoyée à <b>{target.first_name}</b> pour rejoindre <b>{company.name}</b> "
            f"en tant que {role_emoji} <b>{role_arg.capitalize()}</b>.{contrat_info}\n\n"
            f"Il peut accepter avec <code>/rejoindre {company.name}</code>",
            parse_mode="HTML"
        )
        # ── Notifier la cible en DM ──
        try:
            dm_contrat = ""
            if proposed_salary > 0:
                dm_contrat = (
                    f"\n\n📄 <b>Contrat proposé :</b>\n"
                    f"   💰 Salaire : <b>{_fmt(proposed_salary)} $/jour</b>\n"
                )
                if proposed_bonus > 0:
                    dm_contrat += f"   🎁 Prime à la signature : <b>{_fmt(proposed_bonus)} $</b>\n"
                dm_contrat += (
                    f"\n✅ Accepter : <code>/rejoindre {company.name}</code>\n"
                    f"❌ Refuser : ignore ou attends l'expiration\n"
                    f"💬 Contre-proposer : <code>/rejoindre {company.name} [ton_salaire]</code>"
                )
            else:
                dm_contrat = f"\n\n✅ Accepter : <code>/rejoindre {company.name}</code>\n⏳ L'invitation expire dans <b>48h</b>."

            await context.bot.send_message(
                chat_id=target.user_id,
                text=(
                    f"📩 <b>Tu as reçu une invitation !</b>\n\n"
                    f"🏢 <b>{company.name}</b> t'invite à les rejoindre "
                    f"en tant que {role_emoji} <b>{role_arg.capitalize()}</b>.{dm_contrat}"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass


# ─── COMMANDE : /rejoindre [entreprise] ──────────────────────────────────────

async def rejoindre_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /rejoindre [nom entreprise]              → accepte le contrat proposé
    /rejoindre [nom entreprise] [mon_salaire] → contre-propose un salaire
    """
    user = update.effective_user
    await ensure_user(user)
    if not context.args:
        await update.message.reply_text("❌ Usage : <code>/rejoindre [nom entreprise]</code>", parse_mode="HTML")
        return

    # Détecter si le dernier arg est un nombre (contre-proposition)
    counter_salary = 0
    args_list = list(context.args)
    if args_list and args_list[-1].replace("_", "").isdigit():
        counter_salary = int(args_list.pop().replace("_", ""))
    name = " ".join(args_list)

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
                if days_passed < 3:
                    jours = 3 - days_passed
                    await update.message.reply_text(
                        f"⏳ Cooldown : encore {jours} jour(s) avant de rejoindre une entreprise.\n"
                        f"💡 Tape <code>/skipattente</code> pour payer et ignorer ce délai.",
                        parse_mode="HTML"
                    )
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

        # Décoder role et contrat de l'invite (format: "role|salaire|bonus" ou juste "role")
        invite_parts = invite.role.split("|")
        real_role = invite_parts[0]
        proposed_salary = int(invite_parts[1]) if len(invite_parts) > 1 else 0
        proposed_bonus  = int(invite_parts[2]) if len(invite_parts) > 2 else 0

        # Vérifier capacité avant de rejoindre
        max_emp = _max_employees(target)
        nb_emp = (await session.execute(
            select(func.count()).where(
                CompanyEmployee.company_id == target.id,
                CompanyEmployee.left_at == None,
            )
        )).scalar()
        if nb_emp >= max_emp:
            await update.message.reply_text(
                f"❌ <b>{target.name}</b> est malheureusement au complet ({nb_emp}/{max_emp}).\n"
                f"L'invitation reste valide, réessaie si une place se libère.",
                parse_mode="HTML"
            )
            return

        # ── CAS 1 : Contre-proposition de salaire ──────────────────────────
        if counter_salary > 0 and proposed_salary > 0:
            # L'employé contre-propose → on notifie le PDG et on attend sa réponse
            invite.status = "counter"
            # Stocker la contre-proposition dans le role field
            invite.role = f"{real_role}|{counter_salary}|{proposed_bonus}|counter"
            await session.commit()

            role_emoji = ROLE_EMOJI.get(real_role, "👤")
            await update.message.reply_text(
                f"💬 <b>Contre-proposition envoyée à {target.name} !</b>\n\n"
                f"📄 Ta demande : <b>{_fmt(counter_salary)} $/jour</b>\n"
                f"⏳ En attente de la réponse du PDG...\n\n"
                f"💡 Le PDG peut accepter ou refuser avec <code>/acceptercandidature @{user.username or user.first_name}</code>",
                parse_mode="HTML"
            )

            # Notifier le PDG
            pdg_emp = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == target.id,
                    CompanyEmployee.role == "pdg",
                    CompanyEmployee.left_at == None,
                )
            )).scalar_one_or_none()
            if pdg_emp:
                try:
                    await context.bot.send_message(
                        chat_id=pdg_emp.user_id,
                        text=(
                            f"💬 <b>Contre-proposition reçue !</b>\n\n"
                            f"👤 <b>{user.first_name}</b> (@{user.username or '?'}) refuse le salaire proposé ({_fmt(proposed_salary)} $/j)\n"
                            f"📄 Il demande : <b>{_fmt(counter_salary)} $/jour</b>\n\n"
                            f"✅ Accepter : <code>/negociercontrat @{user.username or user.first_name} {counter_salary}</code>\n"
                            f"❌ Refuser : <code>/negociercontrat @{user.username or user.first_name} refuser</code>"
                        ),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
            return

        # ── CAS 2 : Acceptation directe ────────────────────────────────────
        invite.status = "accepted"
        new_emp = CompanyEmployee(
            company_id=target.id,
            user_id=user.id,
            role=real_role,
        )

        # Appliquer le contrat si un salaire était proposé
        if proposed_salary > 0:
            new_emp.daily_salary = proposed_salary
            new_emp.contract_status = "signed"
        else:
            new_emp.contract_status = "none"

        session.add(new_emp)
        await session.flush()

        # Verser la prime si applicable
        if proposed_bonus > 0 and proposed_salary > 0:
            target_user = await session.get(User, user.id)
            if target_user:
                target_user.coins += proposed_bonus
            if target.treasury >= proposed_bonus:
                target.treasury -= proposed_bonus
                target.value = max(LEVELS[1][2], target.value - proposed_bonus)
            else:
                proposed_bonus = 0  # Pas assez en trésorerie, pas de prime

        await _add_log(session, target.id, "recrutement",
                       f"{user.first_name} a rejoint l'entreprise ({real_role})"
                       + (f" — contrat {_fmt(proposed_salary)} $/j" if proposed_salary > 0 else ""))
        await session.commit()

        role_emoji = ROLE_EMOJI.get(real_role, "👤")
        contrat_msg = ""
        if proposed_salary > 0:
            contrat_msg = f"\n\n📄 Contrat signé : <b>{_fmt(proposed_salary)} $/jour</b>"
            if proposed_bonus > 0:
                contrat_msg += f" + prime de <b>{_fmt(proposed_bonus)} $</b> versée !"
        await update.message.reply_text(
            f"✅ Bienvenue dans <b>{target.name}</b> ! {role_emoji} Tu es <b>{real_role.capitalize()}</b>.{contrat_msg}",
            parse_mode="HTML"
        )


# ─── COMMANDE : /demissionner [nom_entreprise] ────────────────────────────────

async def demissionner_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with AsyncSessionLocal() as session:

        # ── Récupérer TOUTES les entreprises où le joueur est actif ──────────
        rows = (await session.execute(
            select(CompanyEmployee, Company).join(
                Company, Company.id == CompanyEmployee.company_id
            ).where(
                CompanyEmployee.user_id == user.id,
                CompanyEmployee.left_at == None,
                # Pas de filtre is_active : permet de quitter même une entreprise dissoute
            )
        )).all()

        if not rows:
            await update.message.reply_text("❌ Tu ne fais partie d'aucune entreprise.")
            return

        # ── Si plusieurs entreprises : demander laquelle cibler ──────────────
        if len(rows) > 1 and not context.args:
            liste = "\n".join(
                f"• <code>/demissionner {row[1].name}</code> — {row[0].role.capitalize()}"
                for row in rows
            )
            await update.message.reply_text(
                f"⚠️ Tu es membre de plusieurs entreprises. Précise laquelle :\n\n{liste}",
                parse_mode="HTML"
            )
            return

        # ── Sélectionner la bonne entreprise ─────────────────────────────────
        if context.args:
            target_name = " ".join(context.args).strip().lower()
            match = next(
                (row for row in rows if row[1].name.lower() == target_name),
                None
            )
            if not match:
                await update.message.reply_text(
                    f"❌ Entreprise <b>{' '.join(context.args)}</b> introuvable parmi tes appartenances.",
                    parse_mode="HTML"
                )
                return
            emp, company = match[0], match[1]
        else:
            emp, company = rows[0][0], rows[0][1]

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
            # Vérifier que le directeur a le MBA requis pour devenir PDG
            director_user = await session.get(User, director.user_id)
            if director_user and not _has_diploma(director_user, "mba"):
                await update.message.reply_text(
                    "❌ Le Directeur n'a pas le <b>MBA</b> requis pour reprendre la direction.\n"
                    "Forme-le ou nomme un autre Directeur qualifié avant de partir.",
                    parse_mode="HTML"
                )
                return
            # Transfert → le directeur devient PDG (pas PDG, le titre PDG part avec le fondateur)
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
            "Postes : secretaire | employe | manager | directeur",
            parse_mode="HTML"
        )
        return

    mention = context.args[0].lstrip("@")
    new_role = context.args[1].lower()
    if new_role not in ROLES_ORDER or new_role in ("pdg", "stagiaire"):
        await update.message.reply_text("❌ Poste invalide. Choix : secretaire | employe | manager | directeur")
        return

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company or emp.role not in MANAGEMENT_ROLES:
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
        lvl_emoji, lvl_name, _, daily_rate, _ = _level_info(company.level)
        max_emp = _max_employees(company)
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
            role_label = "👑 PDG" if emp.role == "pdg" else "👑 PDG"
            personal_rev_line = (
                f"  ╰┈➤  {role_label} — {_fmt(personal_revenue)} $/paie (via <code>/versersalaires</code>)\n"
                f"  ╰┈➤  💡 <code>/retraitboite</code> pour un retrait ponctuel\n"
            )
        elif personal_revenue > 0:
            # Fix 7 : estimation basée sur l'activité réelle, pas "X$/jour automatique"
            activity = getattr(emp, "activity_since_payroll", 0) or 0
            activity_bonus = min(0.5, activity / 40)
            estimated = int(personal_revenue * (1 + activity_bonus))
            personal_rev_line = (
                f"  ╰┈➤  💵 Prochaine paie estimée : ~<b>{_fmt(estimated)} $</b>\n"
                f"  ╰┈➤  📊 {activity} cmds depuis la dernière paie\n"
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

    from sqlalchemy import text as _text
    async with AsyncSessionLocal() as session:
        # Vérifications
        row = await session.execute(
            _text("""
                SELECT ce.role, c.id, c.name, c.treasury, u.coins
                FROM company_employees ce
                JOIN companies c ON c.id = ce.company_id
                JOIN users u ON u.user_id = :uid
                WHERE ce.user_id = :uid AND ce.left_at IS NULL
                  AND c.is_active = TRUE AND ce.role IN ('pdg','directeur')
                ORDER BY ce.role DESC LIMIT 1
            """),
            {"uid": user.id}
        )
        res = row.first()
        if not res:
            await update.message.reply_text("❌ Réservé au PDG et Directeur.")
            return

        role, company_id, company_name, treasury, coins = res

        if coins < amount:
            await update.message.reply_text(f"❌ Tu n'as pas assez. Ton solde : {_fmt(coins)} $")
            return

        # Écriture dans la même session
        await session.execute(
            _text("UPDATE users SET coins = coins - :amt WHERE user_id = :uid"),
            {"amt": amount, "uid": user.id}
        )
        await session.execute(
            _text("UPDATE companies SET treasury = COALESCE(treasury,0) + :amt WHERE id = :cid"),
            {"amt": amount, "cid": company_id}
        )
        await _add_log(session, company_id, "depot",
                       f"Dépôt de {user.first_name}", amount=amount)
        await session.commit()

        # Lire la vraie valeur après commit
        async with AsyncSessionLocal() as session2:
            row2 = await session2.execute(
                _text("SELECT treasury FROM companies WHERE id = :cid"),
                {"cid": company_id}
            )
            new_treasury = int(row2.scalar() or (treasury + amount))

        await update.message.reply_text(
            f"✅ <b>{_fmt(amount)} $</b> déposé dans la trésorerie de <b>{company_name}</b>.\n"
            f"🏦 Trésorerie : {_fmt(new_treasury)} $\n"
            f"📈 Valeur de l'entreprise : {_fmt(new_treasury)} $",
            parse_mode="HTML"
        )


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

        # Chercher explicitement la boite où l'user est PDG
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
            await update.message.reply_text("❌ Réservé au PDG.")
            return
        company, emp = row[1], row[0]
        if company.is_bot_company:
            await update.message.reply_text("❌ Tu ne peux pas retirer de fonds d'une entreprise officielle.")
            return
        if company.treasury_frozen:
            await update.message.reply_text(
                f"🔒 <b>Trésorerie gelée par l'Agence Fiscale.</b>\n\n"
                f"💳 Paie au moins 50% de ta dette fiscale pour débloquer :\n"
                f"<code>/payerimpots [montant]</code>",
                parse_mode="HTML"
            )
            return
        if company.treasury < amount:
            await update.message.reply_text(f"❌ Trésorerie insuffisante. Disponible : {_fmt(company.treasury)} $")
            return

        # Limite : max 30% de la valeur par retrait

        # ── Limite 2 retraits par jour calendaire (sans restriction horaire) ──
        today = datetime.utcnow().date()
        retraits_today = (await session.execute(
            select(func.count()).select_from(CompanyLog).where(
                CompanyLog.company_id == company.id,
                CompanyLog.event_type == "retrait",
                func.date(CompanyLog.created_at) == today,
            )
        )).scalar() or 0
        if retraits_today >= 3:
            await update.message.reply_text(
                f"❌ Tu as déjà effectué <b>3 retraits aujourd'hui</b>.\n"
                f"⏳ Reviens demain pour retirer à nouveau.",
                parse_mode="HTML"
            )
            return

        max_retrait = int(company.value * 0.30)
        if amount > max_retrait:
            await update.message.reply_text(
                f"❌ Tu ne peux pas retirer plus de <b>30%</b> de la valeur de l'entreprise par transaction.\n"
                f"Max autorisé : {_fmt(max_retrait)} $",
                parse_mode="HTML"
            )
            return

        # Protection salariale : on garantit que la trésorerie peut couvrir
        # au moins une paie complète pour tous les employés (hors PDG et stagiaires).
        emps_actifs = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.left_at == None,
            )
        )).scalars().all()
        _, _, _, monthly_rate, _ = _level_info(company.level)
        base_rev = int(company.value * monthly_rate) // 30
        reserve_salaires = sum(
            int(base_rev * ROLE_SHARE.get(e.role, 0))
            for e in emps_actifs
            if e.role not in ("stagiaire", "pdg")
        )
        # La réserve légale est intouchable
        reserve_legale = company.legal_reserve or 0
        montant_disponible = max(0, company.treasury - reserve_legale - reserve_salaires)
        if amount > montant_disponible:
            await update.message.reply_text(
                f"❌ Ce retrait dépasse les fonds disponibles après réserves.\n\n"
                f"🏦 Trésorerie : <b>{_fmt(company.treasury)} $</b>\n"
                f"🔒 Réserve légale (intouchable) : <b>{_fmt(reserve_legale)} $</b>\n"
                f"👷 Réserve salaires employés : <b>{_fmt(reserve_salaires)} $</b>\n"
                f"💸 Tu peux retirer au maximum : <b>{_fmt(montant_disponible)} $</b>\n\n"
                f"💡 Utilise <code>/versersalaires</code> pour libérer la réserve salariale.",
                parse_mode="HTML"
            )
            return

        from sqlalchemy import text as _text
        company_id = company.id
        company_name = company.name
        current_treasury = company.treasury
        user_coins = db_user.coins
        company_name = company.name
        current_treasury = company.treasury
        user_coins = db_user.coins

    # Session séparée pour les écritures SQL pures — décrément atomique
    from sqlalchemy import text as _text
    async with AsyncSessionLocal() as session:
        await session.execute(
            _text("UPDATE companies SET treasury = treasury - :amt, value = treasury - :amt, last_retrait_pdg = NOW() WHERE id = :cid"),
            {"amt": amount, "cid": company_id}
        )
        await session.execute(
            _text("UPDATE users SET coins = coins + :amt WHERE user_id = :uid"),
            {"amt": amount, "uid": user.id}
        )
        # Lire la nouvelle trésorerie pour l'affichage
        row = await session.execute(
            _text("SELECT treasury, (SELECT coins FROM users WHERE user_id = :uid) FROM companies WHERE id = :cid"),
            {"uid": user.id, "cid": company_id}
        )
        res = row.first()
        new_treasury = res[0] if res else (current_treasury - amount)
        new_coins = res[1] if res else (user_coins + amount)
        await _add_log(session, company_id, "retrait",
                       f"Retrait PDG ({user.first_name})", amount=amount)
        await session.commit()

        await update.message.reply_text(
            f"✅ <b>{_fmt(amount)} $</b> retiré de <b>{company_name}</b>.\n"
            f"🏦 Trésorerie restante : {_fmt(new_treasury)} $\n"
            f"💰 Ton solde : {_fmt(new_coins)} $",
            parse_mode="HTML"
        )


# ─── COMMANDE : /logsboite ────────────────────────────────────────────────────

async def logsboite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company or emp.role not in DIRECTION_ROLES:
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
        if not company.is_bot_company:
            lines.append(f"\n💡 <code>/vendreparts [nb] [nom entreprise]</code> pour vendre (prix auto)")
            lines.append(f"💡 <code>/acheterparts [nb] [nom entreprise]</code> pour acheter au PDG")
        else:
            lines.append(f"\nℹ️ <i>Entreprise officielle — les parts ne sont pas cessibles.</i>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── HELPER : vérifier et mettre à jour le PDG selon les parts ───────────────

async def _check_and_update_pdg(session, company, context=None) -> str | None:
    """
    Vérifie tous les actionnaires et attribue le titre PDG au top détenteur.
    - Le PDG n'est jamais détrôné par cette fonction (titre fondateur).
    - En cas d'égalité, le PDG actuel conserve son titre.
    """
    # Récupérer tous les actionnaires avec leurs parts
    all_shares = (await session.execute(
        select(CompanyShare).where(
            CompanyShare.company_id == company.id,
            CompanyShare.quantity > 0,
        )
    )).scalars().all()

    if not all_shares:
        return None

    # Vérifier si le owner actuel est PDG → intouchable par les parts
    current_owner_emp = await _get_employee(session, company.id, company.owner_id)
    if current_owner_emp and current_owner_emp.role == "pdg":
        return None

    # Trouver le max de parts
    top_share = max(all_shares, key=lambda s: s.quantity)
    top_user_id = top_share.owner_id

    # Si c'est déjà le owner actuel, rien à faire
    if company.owner_id == top_user_id:
        return None

    # En cas d'égalité : le PDG actuel garde son titre
    current_shares = next((s.quantity for s in all_shares if s.owner_id == company.owner_id), 0)
    if current_shares == top_share.quantity:
        return None

    # Vérifier que le nouveau top n'est pas le PDG (il a déjà son titre)
    top_emp = await _get_employee(session, company.id, top_user_id)
    if top_emp and top_emp.role == "pdg":
        # Le PDG reprend le owner_id
        company.owner_id = top_user_id
        company.owner_shares = top_share.quantity
        return None

    # Transfert du titre PDG
    old_pdg_id = company.owner_id

    # Ancien PDG → directeur
    old_emp = await _get_employee(session, company.id, old_pdg_id)
    if old_emp and old_emp.role == "pdg":
        old_emp.role = "directeur"

    # Nouveau PDG
    if top_emp:
        top_emp.role = "pdg"
    else:
        session.add(CompanyEmployee(company_id=company.id, user_id=top_user_id, role="pdg"))

    company.owner_id = top_user_id
    company.owner_shares = top_share.quantity

    await _add_log(session, company.id, "changement_pdg",
                   f"👑 Nouveau PDG : uid {top_user_id} ({top_share.quantity} parts)")

    if context:
        try:
            await context.bot.send_message(
                chat_id=top_user_id,
                text=(
                    f"👑 <b>Tu es le nouveau PDG de {company.name} !</b>\n\n"
                    f"Tu détiens <b>{top_share.quantity}/{company.total_shares}</b> parts, "
                    f"plus que tout autre actionnaire."
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass
        try:
            await context.bot.send_message(
                chat_id=old_pdg_id,
                text=(
                    f"📉 <b>Tu n'es plus PDG de {company.name}.</b>\n\n"
                    f"Un autre actionnaire détient désormais plus de parts que toi.\n"
                    f"Tu es rétrogradé au rang de <b>Directeur</b>."
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass

    return (
        f"\n👑 <b>Changement de PDG !</b> Le nouveau PDG de <b>{company.name}</b> "
        f"est l'actionnaire avec <b>{top_share.quantity}</b> parts."
    )


# ─── COMMANDE : /vendreparts [nb] [nom entreprise] ───────────────────────────

async def vendreparts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Usage : <code>/vendreparts [nombre de parts] [nom entreprise]</code>",
            parse_mode="HTML"
        )
        return
    try:
        qty = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Quantité invalide. Exemple : <code>/vendreparts 5 MonEntreprise</code>", parse_mode="HTML")
        return

    company_name = " ".join(context.args[1:])

    async with AsyncSessionLocal() as session:
        # Chercher l'entreprise par nom
        company = await _get_company_by_name(session, company_name)
        if not company:
            await update.message.reply_text(f"❌ Entreprise <b>{company_name}</b> introuvable.", parse_mode="HTML")
            return

        # Récupérer le rôle dans l'entreprise (peut être None si l'user n'est pas employé)
        emp = await _get_employee(session, company.id, user.id)

        # ── Entreprise du bot : vente de parts interdite ──────────────────────
        if company.is_bot_company:
            await update.message.reply_text(
                "🚫 <b>Vente impossible.</b>\n\n"
                f"<b>{company.name}</b> est une entreprise officielle gérée par le système.\n"
                "Les parts de ces entreprises ne sont pas cessibles.",
                parse_mode="HTML"
            )
            return

        # Prix calculé depuis la trésorerie (value = treasury)
        price_each = company.treasury // company.total_shares if company.total_shares > 0 else 1

        my_shares = await _get_shares(session, company.id, user.id)
        if my_shares <= 0:
            await update.message.reply_text(
                f"❌ Tu ne détiens aucune part dans <b>{company.name}</b>.\n"
                f"💡 Utilise <code>/parts {company.name}</code> pour voir les actionnaires.",
                parse_mode="HTML"
            )
            return

        # Le PDG doit conserver au moins 51 parts (contrôle majoritaire).
        # Cela empêche l'exploit : créer → déposer → vendre 99 parts → dissolution.
        if emp and emp.role == "pdg":
            can_sell = max(0, my_shares - 51)
            if can_sell == 0:
                await update.message.reply_text(
                    f"❌ En tant que PDG, tu dois conserver au moins <b>51 parts</b> "
                    f"(contrôle majoritaire).\n"
                    f"Tu as <b>{my_shares} parts</b> — impossible d\'en vendre.",
                    parse_mode="HTML"
                )
                return
        else:
            can_sell = my_shares

        if qty <= 0:
            await update.message.reply_text("❌ La quantité doit être supérieure à 0.")
            return

        if qty > can_sell:
            if emp and emp.role == "pdg":
                await update.message.reply_text(
                    f"❌ En tant que PDG tu dois garder <b>51 parts</b> minimum.\n"
                    f"Tu peux vendre au maximum <b>{can_sell} parts</b>.",
                    parse_mode="HTML"
                )
            else:
                await update.message.reply_text(f"❌ Tu n'as que {my_shares} parts dans cette entreprise.")
            return

        total = qty * price_each
        db_user = await get_user(session, user.id)

        # ── RACHAT PAR LE MARCHÉ ────────────────────────────────────────────
        db_user.coins += total
        company.treasury -= total
        company.value = company.treasury
        company.total_shares = max(1, company.total_shares - qty)
        await _update_level(session, company)

        # Mettre à jour les parts du vendeur
        share_row = (await session.execute(
            select(CompanyShare).where(
                CompanyShare.company_id == company.id,
                CompanyShare.owner_id == user.id,
            )
        )).scalar_one_or_none()
        if share_row:
            share_row.quantity = max(0, share_row.quantity - qty)
            # Sync owner_shares depuis la table (source de vérité)
            if emp and emp.role == "pdg":
                company.owner_shares = share_row.quantity

        await _add_log(session, company.id, "vente_parts",
                       f"{user.first_name} a vendu {qty} parts au marché à {_fmt(price_each)}/part",
                       amount=total)

        # Vérifier et mettre à jour le PDG selon les parts restantes
        pdg_msg = await _check_and_update_pdg(session, company, context)

        await session.commit()

        await update.message.reply_text(
            f"✅ <b>{qty} parts</b> vendues au marché pour <b>{_fmt(total)} $</b> "
            f"(<b>{_fmt(price_each)} $/part</b>) !\n\n"
            f"💰 Ton solde : <b>{_fmt(db_user.coins)} $</b>\n"
            f"📉 Valeur de <b>{company.name}</b> : {_fmt(company.value)} $"
            f"{pdg_msg or ''}",
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
    if qty <= 0:
        await update.message.reply_text("❌ La quantité doit être supérieure à 0.")
        return

    name = " ".join(context.args[1:])

    async with AsyncSessionLocal() as session:
        company = await _get_company_by_name(session, name)
        if not company:
            await update.message.reply_text(f"❌ Entreprise <b>{name}</b> introuvable.", parse_mode="HTML")
            return

        # Parts disponibles à la vente = toutes les parts que le PDG détient
        available = company.owner_shares
        if available <= 0:
            await update.message.reply_text(
                "❌ Le PDG ne détient plus de parts à vendre."
            )
            return

        if qty > available:
            await update.message.reply_text(f"❌ Seulement <b>{available}</b> parts disponibles à l'achat.", parse_mode="HTML")
            return

        price_per = company.treasury // company.total_shares if company.total_shares > 0 else 1
        total = qty * price_per
        db_user = await get_user(session, user.id)

        if db_user.coins < total:
            await update.message.reply_text(
                f"❌ Tu n'as pas assez. Prix total : <b>{_fmt(total)} $</b>\n"
                f"Ton solde : <b>{_fmt(db_user.coins)} $</b>",
                parse_mode="HTML"
            )
            return

        # ── Entreprise du bot : achat de parts interdit ───────────────────────
        if company.is_bot_company:
            await update.message.reply_text(
                f"🚫 <b>Achat impossible.</b>\n\n"
                f"<b>{company.name}</b> est une entreprise officielle gérée par le système.\n"
                f"Les parts de ces entreprises ne sont pas cessibles.",
                parse_mode="HTML"
            )
            return

        # ── Entreprise d'un joueur : offre soumise au PDG ────────────────────

        # Vérifier qu'une offre n'est pas déjà en cours de ce buyer
        existing_offer = (await session.execute(
            select(CompanyShareOffer).where(
                CompanyShareOffer.company_id == company.id,
                CompanyShareOffer.buyer_id == user.id,
                CompanyShareOffer.status == "pending",
            )
        )).scalar_one_or_none()
        if existing_offer:
            await update.message.reply_text(
                f"⏳ Tu as déjà une offre en attente sur <b>{company.name}</b>.\n"
                f"Attends la réponse du PDG ou qu'elle expire (48h).",
                parse_mode="HTML"
            )
            return

        # Bloquer les fonds immédiatement (escrow)
        db_user.coins -= total

        offer = CompanyShareOffer(
            company_id=company.id,
            buyer_id=user.id,
            quantity=qty,
            price_each=price_per,
            total_price=total,
            status="pending",
            expires_at=datetime.utcnow() + timedelta(hours=48),
        )
        session.add(offer)
        await session.flush()

        await _add_log(session, company.id, "offre_parts",
                       f"{user.first_name} a soumis une offre pour {qty} parts", amount=total)
        await session.commit()

        await update.message.reply_text(
            f"📩 <b>Offre envoyée au PDG de {company.name} !</b>\n\n"
            f"📦 Parts demandées : <b>{qty}</b>\n"
            f"💰 Prix total : <b>{_fmt(total)} $</b> (bloqués sur ton compte)\n"
            f"⏳ Expire dans <b>48h</b> si aucune réponse.\n\n"
            f"Tu seras remboursé automatiquement en cas de refus.",
            parse_mode="HTML"
        )

        # Notifier le PDG en DM
        try:
            await context.bot.send_message(
                chat_id=company.owner_id,
                text=(
                    f"💼 <b>Nouvelle offre d'achat de parts !</b>\n\n"
                    f"👤 <b>{user.first_name}</b> veut acheter <b>{qty} parts</b> de <b>{company.name}</b>\n"
                    f"💰 Offre : <b>{_fmt(total)} $</b> ({_fmt(price_per)} $/part)\n\n"
                    f"✅ Accepter : <code>/accepteroffre {offer.id}</code>\n"
                    f"❌ Refuser : <code>/refuseroffre {offer.id}</code>\n"
                    f"⏳ Expire dans 48h."
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass


# ─── COMMANDE : /accepteroffre [offer_id] ────────────────────────────────────

async def accepteroffre_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("❌ Usage : <code>/accepteroffre [id_offre]</code>", parse_mode="HTML")
        return
    try:
        offer_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID invalide.")
        return

    async with AsyncSessionLocal() as session:
        offer = await session.get(CompanyShareOffer, offer_id)
        if not offer or offer.status != "pending":
            await update.message.reply_text("❌ Offre introuvable ou déjà traitée.")
            return

        company = await session.get(Company, offer.company_id)
        if not company or company.owner_id != user.id:
            await update.message.reply_text("❌ Seul le PDG de cette entreprise peut accepter l'offre.")
            return

        # Vérifier expiration
        if datetime.utcnow() > offer.expires_at:
            offer.status = "expired"
            # Rembourser l'acheteur
            buyer = await session.get(User, offer.buyer_id)
            if buyer:
                buyer.coins += offer.total_price
            await session.commit()
            await update.message.reply_text("❌ Cette offre a expiré. L'acheteur a été remboursé.")
            return

        # Vérifier que les parts sont toujours disponibles
        available = company.owner_shares
        if offer.quantity > available:
            offer.status = "rejected"
            buyer = await session.get(User, offer.buyer_id)
            if buyer:
                buyer.coins += offer.total_price
            await session.commit()
            await update.message.reply_text(
                f"❌ Plus assez de parts disponibles ({available} dispo). Offre annulée, acheteur remboursé.",
                parse_mode="HTML"
            )
            return

        # ── Vérification limite 51 parts PDG (anti-exploit dissolution) ─────
        # Même règle que /vendreparts : le PDG doit garder au minimum 51 parts.
        # Sans cette vérification, un complice peut faire /acheterparts 99
        # et le PDG accepte via /accepteroffre → contournement total du fix.
        pdg_shares_row = (await session.execute(
            select(CompanyShare).where(
                CompanyShare.company_id == company.id,
                CompanyShare.owner_id == user.id,
            )
        )).scalar_one_or_none()
        pdg_shares_qty = pdg_shares_row.quantity if pdg_shares_row else 0
        can_sell_pdg = max(0, pdg_shares_qty - 51)
        if offer.quantity > can_sell_pdg:
            offer.status = "rejected"
            buyer = await session.get(User, offer.buyer_id)
            if buyer:
                buyer.coins += offer.total_price
            await session.commit()
            await update.message.reply_text(
                f"❌ Offre refusée automatiquement.\n\n"
                f"En tant que PDG tu dois conserver au moins <b>51 parts</b>.\n"
                f"Tu détiens <b>{pdg_shares_qty} parts</b> et peux en vendre au max <b>{can_sell_pdg}</b>.\n"
                f"L'acheteur a été remboursé.",
                parse_mode="HTML"
            )
            return

        # ── Transaction ──────────────────────────────────────────────────────
        offer.status = "accepted"
        qty = offer.quantity
        total = offer.total_price

        # Le PDG reçoit l'argent
        pdg_user = await get_user(session, user.id)
        pdg_user.coins += total

        # Mise à jour de la CompanyShare de l'acheteur
        buyer_share = (await session.execute(
            select(CompanyShare).where(
                CompanyShare.company_id == company.id,
                CompanyShare.owner_id == offer.buyer_id,
            )
        )).scalar_one_or_none()
        if buyer_share:
            buyer_share.quantity += qty
        else:
            session.add(CompanyShare(company_id=company.id, owner_id=offer.buyer_id, quantity=qty))

        # Mise à jour de la CompanyShare du PDG + sync owner_shares (une seule fois)
        pdg_share = (await session.execute(
            select(CompanyShare).where(
                CompanyShare.company_id == company.id,
                CompanyShare.owner_id == user.id,
            )
        )).scalar_one_or_none()
        if pdg_share:
            pdg_share.quantity = max(0, pdg_share.quantity - qty)
            company.owner_shares = pdg_share.quantity
        else:
            company.owner_shares = 0

        await session.flush()

        # ── Vérifier changement de PDG (celui qui a le plus de parts prend le titre) ──
        pdg_msg = await _check_and_update_pdg(session, company, context)

        await _add_log(session, company.id, "achat_parts",
                       f"{offer.buyer_id} a acheté {qty} parts (accord PDG)", amount=total)
        await session.commit()

        await update.message.reply_text(
            f"✅ Offre acceptée ! <b>{qty} parts</b> vendues pour <b>{_fmt(total)} $</b>.{pdg_msg or ''}",
            parse_mode="HTML"
        )

        # Notifier l'acheteur
        try:
            await context.bot.send_message(
                chat_id=offer.buyer_id,
                text=(
                    f"🎉 <b>Ton offre a été acceptée !</b>\n\n"
                    f"📦 Tu as obtenu <b>{qty} parts</b> de <b>{company.name}</b>\n"
                    f"💰 Montant débité : <b>{_fmt(total)} $</b>"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass


# ─── COMMANDE : /refuseroffre [offer_id] ─────────────────────────────────────

async def refuseroffre_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("❌ Usage : <code>/refuseroffre [id_offre]</code>", parse_mode="HTML")
        return
    try:
        offer_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID invalide.")
        return

    async with AsyncSessionLocal() as session:
        offer = await session.get(CompanyShareOffer, offer_id)
        if not offer or offer.status != "pending":
            await update.message.reply_text("❌ Offre introuvable ou déjà traitée.")
            return

        company = await session.get(Company, offer.company_id)
        if not company or company.owner_id != user.id:
            await update.message.reply_text("❌ Seul le PDG de cette entreprise peut refuser l'offre.")
            return

        offer.status = "rejected"

        # Rembourser l'acheteur
        buyer = await session.get(User, offer.buyer_id)
        if buyer:
            buyer.coins += offer.total_price

        await _add_log(session, company.id, "offre_refusee",
                       f"Offre de {offer.buyer_id} refusée par le PDG ({offer.quantity} parts)")
        await session.commit()

        await update.message.reply_text(
            f"✅ Offre refusée. <b>{_fmt(offer.total_price)} $</b> remboursés à l'acheteur.",
            parse_mode="HTML"
        )

        try:
            await context.bot.send_message(
                chat_id=offer.buyer_id,
                text=(
                    f"😔 Ton offre d'achat de <b>{offer.quantity} parts</b> dans <b>{company.name}</b> "
                    f"a été <b>refusée</b> par le PDG.\n"
                    f"💰 <b>{_fmt(offer.total_price)} $</b> remboursés sur ton compte."
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass


# ─── COMMANDE : /cederentreprise @pseudo ─────────────────────────────────────

async def cederentreprise_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Permet au PDG de transférer son titre à un autre employé.
    L'ancien PDG devient Directeur. Le nouveau PDG doit être dans l'entreprise.
    """
    user = update.effective_user
    if not context.args:
        await update.message.reply_text(
            "❌ Usage : <code>/cederentreprise @pseudo</code>\n"
            "Transfère ton titre de PDG à un autre employé.",
            parse_mode="HTML"
        )
        return

    mention = context.args[0].lstrip("@")

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company or emp.role not in ("pdg",):
            await update.message.reply_text("❌ Seul le PDG peut transférer ce titre.")
            return

        if company.is_bot_company:
            await update.message.reply_text("❌ Impossible sur une entreprise officielle.")
            return

        target = (await session.execute(
            select(User).where(User.username == mention)
        )).scalar_one_or_none()
        if not target:
            await update.message.reply_text(f"❌ @{mention} introuvable.")
            return

        if target.user_id == user.id:
            await update.message.reply_text("❌ Tu es déjà PDG.")
            return

        target_emp = await _get_employee(session, company.id, target.user_id)
        if not target_emp:
            await update.message.reply_text(f"❌ {target.first_name} ne fait pas partie de {company.name}.")
            return

        # Vérifier que le nouveau PDG a le MBA
        if not _has_diploma(target, "mba"):
            await update.message.reply_text(
                f"❌ {target.first_name} doit avoir un <b>MBA</b> pour devenir PDG.",
                parse_mode="HTML"
            )
            return

        # Transfert : ancien PDG → Directeur
        emp.role = "directeur"

        # Nouveau PDG
        target_emp.role = "pdg"

        # Mettre à jour owner_id
        company.owner_id = target.user_id

        # ── Transfert des parts ──────────────────────────────────────────────
        # L'ancien PDG cède TOUTES ses parts au nouveau PDG.
        # Sans ça : l'ancien PDG garde ses parts → peut dissoudre via un complice
        # ou continuer à toucher des dividendes sans contrôle.
        old_share = (await session.execute(
            select(CompanyShare).where(
                CompanyShare.company_id == company.id,
                CompanyShare.owner_id == user.id,
            )
        )).scalar_one_or_none()

        new_share = (await session.execute(
            select(CompanyShare).where(
                CompanyShare.company_id == company.id,
                CompanyShare.owner_id == target.user_id,
            )
        )).scalar_one_or_none()

        parts_cedees = 0
        if old_share and old_share.quantity > 0:
            parts_cedees = old_share.quantity
            if new_share:
                new_share.quantity += parts_cedees
            else:
                session.add(CompanyShare(
                    company_id=company.id,
                    owner_id=target.user_id,
                    quantity=parts_cedees,
                ))
            old_share.quantity = 0
            company.owner_shares = parts_cedees

        await _add_log(session, company.id, "cession_pdg",
                       f"👑 PDG transféré : {user.first_name} → {target.first_name} ({parts_cedees} parts cédées)")
        await session.commit()

        await update.message.reply_text(
            f"👑 <b>Titre PDG transféré !</b>\n\n"
            f"👤 <b>{target.first_name}</b> est le nouveau <b>PDG de {company.name}</b>.\n"
            f"📦 <b>{parts_cedees} parts</b> transférées au nouveau PDG.\n"
            f"Tu restes dans l'entreprise en tant que <b>Directeur</b> (sans parts).",
            parse_mode="HTML"
        )

        try:
            await context.bot.send_message(
                chat_id=target.user_id,
                text=(
                    f"👑 <b>Tu es le nouveau PDG de {company.name} !</b>\n\n"
                    f"{user.first_name} t'a transféré le titre de fondateur.\n"
                    f"Tu as désormais le rang le plus élevé de l'entreprise."
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass


# ─── JOB : EXPIRATION DES OFFRES DE PARTS ────────────────────────────────────

async def job_expire_share_offers(context: ContextTypes.DEFAULT_TYPE):
    """Expire les offres de parts non répondues après 48h et rembourse les acheteurs."""
    async with AsyncSessionLocal() as session:
        expired_offers = (await session.execute(
            select(CompanyShareOffer).where(
                CompanyShareOffer.status == "pending",
                CompanyShareOffer.expires_at <= datetime.utcnow(),
            )
        )).scalars().all()

        for offer in expired_offers:
            offer.status = "expired"
            buyer = await session.get(User, offer.buyer_id)
            if buyer:
                buyer.coins += offer.total_price
            company = await session.get(Company, offer.company_id)
            company_name = company.name if company else "?"

            try:
                await context.bot.send_message(
                    chat_id=offer.buyer_id,
                    text=(
                        f"⏰ Ton offre d'achat de <b>{offer.quantity} parts</b> dans <b>{company_name}</b> "
                        f"a expiré sans réponse.\n"
                        f"💰 <b>{_fmt(offer.total_price)} $</b> remboursés sur ton compte."
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass

        if expired_offers:
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
        lvl_emoji, lvl_name, _, _, _ = _level_info(company.level)
        max_emp = _max_employees(company)

        # Regrouper par rôle
        by_role: dict[str, list] = {r: [] for r in ROLES_ORDER[::-1]}
        for emp, u in rows:
            by_role.setdefault(emp.role, []).append((emp, u))

        lines = [
            f"「 {sec_emoji} 」<b>{company.name}</b>  ·  {lvl_emoji} {lvl_name}",
            f"👥 <b>{len(rows)}/{max_emp} employés</b>",
            "◈━━━━━━━━━━━━━━━━━━━━━━━━◈",
        ]

        role_order = ["pdg", "directeur", "manager", "employe", "secretaire", "stagiaire"]
        role_labels = {
            "pdg":        "👑 PDG",
            "pdg":        "👑 PDG",
            "directeur":  "🏦 Directeurs",
            "manager":    "💼 Managers",
            "employe":    "👷 Employés",
            "secretaire": "🗂️ Secrétaires",
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
        if not company or emp.role not in DIRECTION_ROLES:
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
            _, _, _, monthly_rate, _ = _level_info(company.level)
            max_emp = _max_employees(company)
            revenue = int(company.value * monthly_rate) // 30
            lvl_emoji, lvl_name, _, _, _ = _level_info(company.level)
            sec_emoji, sec_name = SECTORS.get(company.sector, ("🏢", company.sector))

            # Candidatures en attente
            pending = (await session.execute(
                select(func.count()).where(
                    CompanyApplication.company_id == company.id,
                    CompanyApplication.status == "pending",
                )
            )).scalar()

            # Fix 10 : Notification trésorerie suffisante pour payer
            last_pay = company.last_payroll
            can_pay_now = company.treasury > 0
            treasury_hint = ""
            if can_pay_now and (not last_pay or (datetime.utcnow() - last_pay).total_seconds() >= 12 * 3600):
                treasury_hint = f"\n💡 <b>Tu peux verser les salaires maintenant !</b> (<code>/versersalaires</code>)"

            # Fix 12 : Contrats actifs et leurs bonus
            contracts_line = ""
            try:
                from handlers.company_sector import get_all_active_contracts, get_contract_bonus
                active_contracts = await get_all_active_contracts(session)
                contract_bonus = get_contract_bonus(company.id, active_contracts)
                nb_contracts = len([c for c in active_contracts
                                    if c.company_a_id == company.id or c.company_b_id == company.id])
                if nb_contracts > 0:
                    bonus_pct = int(contract_bonus * 100)
                    contracts_line = f"\n🤝 Contrats actifs : <b>{nb_contracts}</b> (+{bonus_pct}% revenus)"
            except Exception:
                pass

            pending_line = (
                f"\n📩 <b>{pending} candidature(s) en attente</b> — <code>/candidatures</code>"
                if pending > 0 else ""
            )

            # Fix 3 : Indiquer clairement que les revenus vont en trésorerie
            last_pay_str = last_pay.strftime("%d/%m %H:%M") if last_pay else "Jamais"

            rapport = (
                f"📊 <b>Rapport quotidien — {company.name}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n\n"
                f"{sec_emoji} Secteur : <b>{sec_name}</b>\n"
                f"{lvl_emoji} Niveau : <b>{lvl_name}</b>\n\n"
                f"💰 Valeur : <b>{_fmt(company.value)} $</b>\n"
                f"🏦 Trésorerie : <b>{_fmt(company.treasury)} $</b>\n"
                f"📈 Revenus/jour : <b>{_fmt(revenue)} $</b> <i>(→ trésorerie, PDG inclus dans la paie)</i>\n"
                f"🕒 Dernière paie : <b>{last_pay_str}</b>"
                f"{contracts_line}\n\n"
                f"👥 Employés : <b>{nb_emps}/{max_emp}</b>\n"
                f"⭐ Réputation : <b>{company.reputation:.1f}/5.0</b>"
                f"{pending_line}"
                f"{treasury_hint}"
            )

            try:
                await context.bot.send_message(
                    chat_id=company.owner_id,
                    text=rapport,
                    parse_mode="HTML"
                )
            except Exception:
                pass
            await asyncio.sleep(0.5)  # anti-flood

# ─── COMMANDE : /mesparts ─────────────────────────────────────────────────────

async def mesparts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /mesparts — Affiche toutes les parts détenues dans toutes les entreprises.
    Permet de voir et vendre des parts même sans être employé.
    """
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        # Chercher toutes les parts de l'utilisateur (qty > 0)
        all_shares = (await session.execute(
            select(CompanyShare).where(
                CompanyShare.owner_id == user.id,
                CompanyShare.quantity > 0,
            )
        )).scalars().all()

        if not all_shares:
            await update.message.reply_text(
                "📭 Tu ne détiens aucune part dans une entreprise.\n\n"
                "💡 Achète des parts avec <code>/acheterparts [nb] [nom entreprise]</code>",
                parse_mode="HTML"
            )
            return

        total_value = 0
        lines = [
            "📦 <b>MES PARTS D'ENTREPRISE</b>",
            "─────────────────────────────",
        ]

        for s in all_shares:
            company = await session.get(Company, s.company_id)
            if not company or not company.is_active:
                continue
            sec_emoji, sec_name = SECTORS.get(company.sector, ("🏢", company.sector))
            price_per = company.treasury // company.total_shares if company.total_shares > 0 else 0
            val = s.quantity * price_per
            total_value += val
            pct = (s.quantity / company.total_shares) * 100

            # Statut dans l'entreprise
            emp = await _get_employee(session, company.id, user.id)
            role_tag = f" · {ROLE_EMOJI.get(emp.role, '')} {emp.role.capitalize()}" if emp else " · 🔗 Actionnaire ext."

            lines.append(
                f"\n{sec_emoji} <b>{company.name}</b>{role_tag}\n"
                f"   📦 {s.quantity} parts ({pct:.1f}%) · 💰 {_fmt(val)} $\n"
                f"   📊 Prix/part : {_fmt(price_per)} $ · Total parts : {company.total_shares}\n"
                f"   💡 <code>/vendreparts {s.quantity} {company.name}</code>"
            )

        lines.append("\n─────────────────────────────")
        lines.append(f"💼 Valeur totale de ton portefeuille : <b>{_fmt(total_value)} $</b>")
        lines.append("ℹ️ La vente de parts ne nécessite pas d'être employé dans l'entreprise.")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── COMMANDE : /offresparts ─────────────────────────────────────────────────

async def offresparts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /offresparts — Le PDG voit toutes les offres de rachat de parts en attente.
    Évite les offres silencieuses si le PDG a les notifs coupées.
    """
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company or emp.role not in ("pdg",):
            await update.message.reply_text("❌ Réservé au PDG de ton entreprise.")
            return

        offers = (await session.execute(
            select(CompanyShareOffer).where(
                CompanyShareOffer.company_id == company.id,
                CompanyShareOffer.status == "pending",
            ).order_by(CompanyShareOffer.created_at.desc())
        )).scalars().all()

        if not offers:
            await update.message.reply_text(
                f"📭 <b>Aucune offre en attente</b> sur <b>{company.name}</b>.\n\n"
                f"💡 Les acheteurs utilisent <code>/acheterparts nb {company.name}</code>",
                parse_mode="HTML"
            )
            return

        price_per = company.treasury // company.total_shares if company.total_shares > 0 else 0
        lines = [
            f"💼 <b>OFFRES DE PARTS — {company.name}</b>",
            f"<i>{len(offers)} offre(s) en attente · Prix actuel : {_fmt(price_per)} $/part</i>",
            "─────────────────────────────",
        ]

        for offer in offers:
            buyer = await session.get(User, offer.buyer_id)
            buyer_name = buyer.first_name if buyer else f"uid:{offer.buyer_id}"
            expires_in = offer.expires_at - datetime.utcnow()
            h = int(expires_in.total_seconds() // 3600)
            lines.append(
                f"👤 <b>{buyer_name}</b> — {offer.quantity} parts · {_fmt(offer.total_price)} $\n"
                f"   ⏳ Expire dans {h}h\n"
                f"   ✅ <code>/accepteroffre {offer.id}</code>  ❌ <code>/refuseroffre {offer.id}</code>"
            )

        lines.append("─────────────────────────────")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── COMMANDE : /dissoudreboite ───────────────────────────────────────────────

async def dissoudreboite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company or emp.role not in ("pdg",):
            await update.message.reply_text("❌ Seul le PDG peut dissoudre son entreprise.")
            return

        if company.is_bot_company:
            await update.message.reply_text("❌ Tu ne peux pas dissoudre une entreprise officielle.")
            return

        # Confirmation requise : /dissoudreboite CONFIRMER
        if not context.args or context.args[0].upper() != "CONFIRMER":
            # Calculer la répartition prévisionnelle par parts
            shares_list = (await session.execute(
                select(CompanyShare).where(
                    CompanyShare.company_id == company.id,
                    CompanyShare.quantity > 0,
                )
            )).scalars().all()
            total_shares = company.total_shares or 100
            preview_lines = []
            for s in shares_list:
                sh_user = await session.get(User, s.owner_id)
                name_s = sh_user.first_name if sh_user else "?"
                pct = s.quantity / total_shares
                montant = int(company.treasury * pct)
                preview_lines.append(f"  · {name_s} ({s.quantity} parts) → {_fmt(montant)} $")
            preview = "\n".join(preview_lines) if preview_lines else "  · Aucun actionnaire"

            await update.message.reply_text(
                f"⚠️ <b>Tu es sur le point de dissoudre {company.name} !</b>\n\n"
                f"💰 Valeur actuelle : <b>{_fmt(company.value)} $</b>\n"
                f"🏦 Trésorerie : <b>{_fmt(company.treasury)} $</b>\n\n"
                f"📊 <b>Distribution de la trésorerie aux actionnaires :</b>\n{preview}\n\n"
                f"Tous les employés seront libérés <b>sans cooldown</b>.\n\n"
                f"Pour confirmer : <code>/dissoudreboite CONFIRMER</code>",
                parse_mode="HTML"
            )
            return

        # Récupérer les actionnaires
        shares_list = (await session.execute(
            select(CompanyShare).where(
                CompanyShare.company_id == company.id,
                CompanyShare.quantity > 0,
            )
        )).scalars().all()
        total_shares = company.total_shares or 100
        # Distribution directe de la trésorerie (value = treasury, pas de double dip possible)
        treasury = company.treasury

        # 🏛️ Si trésorerie gelée → impôts prélevés en premier
        tax_preleve = 0
        if company.treasury_frozen and company.tax_debt > 0:
            from database.models import StateCaisse
            tax_preleve = min(company.tax_debt, treasury)
            treasury -= tax_preleve
            caisse = (await session.execute(select(StateCaisse))).scalar_one_or_none()
            if not caisse:
                caisse = StateCaisse(total=0)
                session.add(caisse)
            caisse.total += tax_preleve
            company.tax_debt = 0
            company.treasury_frozen = False

        # Récupérer les employés pour les libérer
        emps = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.left_at == None,
            )
        )).scalars().all()

        # Libérer tous les employés SANS cooldown
        bypass_date = datetime.utcnow() - timedelta(days=8)
        for e in emps:
            if e.user_id != user.id:
                e.left_at = bypass_date
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

        # Distribuer la trésorerie proportionnellement aux parts
        total_distribue = 0
        for s in shares_list:
            if s.quantity <= 0:
                continue
            pct = s.quantity / total_shares
            montant = int(treasury * pct)
            if montant <= 0:
                continue
            sh_user = await session.get(User, s.owner_id)
            if sh_user:
                sh_user.coins += montant
                total_distribue += montant
                try:
                    await context.bot.send_message(
                        chat_id=s.owner_id,
                        text=(
                            f"🏚️ <b>{company.name}</b> a été dissoute.\n\n"
                            f"📊 Tu détenais <b>{s.quantity} parts</b> ({int(pct*100)}%)\n"
                            f"💰 Tu reçois : <b>+{_fmt(montant)} $</b>"
                        ),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

        # Fermer l'entreprise
        company.is_active = False
        company.treasury = 0

        # Marquer le PDG aussi
        pdg_emp = await _get_employee(session, company.id, user.id)
        if pdg_emp:
            pdg_emp.left_at = datetime.utcnow()

        await _add_log(session, company.id, "dissolution",
                       f"Entreprise dissoute par {user.first_name} — {_fmt(total_distribue)} $ distribués aux actionnaires")
        await session.commit()

        await update.message.reply_text(
            f"🏚️ <b>{company.name}</b> a été dissoute.\n\n"
            + (f"🏛️ <b>{_fmt(tax_preleve)} $</b> prélevés par l'Agence Fiscale (impôts impayés).\n" if tax_preleve > 0 else "")
            + f"💰 <b>{_fmt(total_distribue)} $</b> distribués aux actionnaires selon leurs parts.\n"
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
            role_label = "👑 PDG" if emp.role == "pdg" else "👑 PDG"
            salaire_line = (
                f"  ╰┈➤  {role_label} — <b>~{_fmt(personal_revenue)} $/paie</b>\n"
                f"  ╰┈➤  Déclenche <code>/versersalaires</code> pour te payer\n"
                f"  ╰┈➤  ou <code>/retraitboite</code> pour un retrait ponctuel\n"
            )
        elif personal_revenue > 0:
            # Fix 7 : affichage basé sur l'activité, plus "X$/jour automatique"
            activity = getattr(emp, "activity_since_payroll", 0) or 0
            activity_bonus = min(0.5, activity / 40)
            estimated = int(personal_revenue * (1 + activity_bonus))
            salaire_line = (
                f"  ╰┈➤  💵 Estimation prochaine paie : <b>~{_fmt(estimated)} $</b>\n"
                f"  ╰┈➤  📊 Activité comptabilisée : <b>{activity} commandes</b>\n"
                f"  ╰┈➤  💡 Plus t'es actif, plus tu touches à la prochaine paie !\n"
            )
        else:
            salaire_line = (
                f"  ╰┈➤  Stagiaire — pas de salaire direct\n"
                f"  ╰┈➤  Passe ton <code>/diplome</code> pour être payé !\n"
            )

        # Si args : /salaireinfo transfert [montant]
        if context.args and context.args[0].lower() == "transfert":
            if emp.role in ("stagiaire",):
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


# ─── COMMANDE : /presences ────────────────────────────────────────────────────

async def presences_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /presences — Direction voit l'activité + salaire contractuel de chaque employé.
    Affiche TOUTES les entreprises où le user est en direction.
    """
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        # Toutes les entreprises où le user est direction
        all_rows = (await session.execute(
            select(CompanyEmployee, Company).join(
                Company, Company.id == CompanyEmployee.company_id
            ).where(
                CompanyEmployee.user_id == user.id,
                CompanyEmployee.left_at == None,
                Company.is_active == True,
                CompanyEmployee.role.in_(DIRECTION_ROLES),
            )
        )).all()

        if not all_rows:
            company, emp = await _get_user_company(session, user.id)
            if not company:
                await update.message.reply_text("❌ Tu ne fais partie d'aucune entreprise.", parse_mode="HTML")
            else:
                await update.message.reply_text("❌ Réservé à la direction (Directeur, PDG).", parse_mode="HTML")
            return

        for emp, company in all_rows:
            emps = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == company.id,
                    CompanyEmployee.left_at == None,
                )
            )).scalars().all()

            last_pay_str = company.last_payroll.strftime("%d/%m %H:%M") if company.last_payroll else "Jamais"
            is_pdg = emp.role == "pdg"

            sans_contrat = [
                e for e in emps
                if e.role not in ("pdg", "stagiaire") and (e.contract_status or "none") != "signed"
            ]

            lines = [
                f"📊 <b>PRÉSENCES — {company.name}</b>",
                f"<i>Dernière paie : {last_pay_str}</i>",
                f"🏦 Trésorerie : <b>{_fmt(company.treasury)} $</b>",
            ]
            if sans_contrat and is_pdg:
                lines.append(f"⚠️ <b>{len(sans_contrat)} employé(s) sans contrat signé</b>")
            lines.append("─────────────────────────────")

            sorted_emps = sorted(
                emps,
                key=lambda x: ROLES_ORDER.index(x.role) if x.role in ROLES_ORDER else 0,
                reverse=True
            )

            total_masse = 0
            for e in sorted_emps:
                emp_user = await session.get(User, e.user_id)
                name = emp_user.first_name if emp_user else "?"
                uname = emp_user.username if emp_user and emp_user.username else None
                role_emoji = ROLE_EMOJI.get(e.role, "👤")
                activity = e.activity_since_payroll or 0
                daily = e.daily_salary or 0
                status = e.contract_status or "none"

                bar_full = min(10, activity // 5 if activity > 10 else activity)
                bar = "█" * bar_full + "░" * (10 - bar_full)

                if e.role == "pdg":
                    lines.append(
                        f"{role_emoji} <b>{name}</b> [pdg]\n"
                        f"   {bar} {activity} cmd"
                    )
                    continue

                if e.role == "stagiaire":
                    lines.append(
                        f"{role_emoji} <b>{name}</b> [stagiaire]\n"
                        f"   {bar} {activity} cmd · pas de salaire"
                    )
                    continue

                total_masse += daily

                if status == "signed":
                    contrat_line = f"📄 Contrat : <b>{_fmt(daily)} $/jour</b>"
                elif status == "pending_employee":
                    contrat_line = f"⏳ En attente de réponse employé ({_fmt(e.pending_salary or 0)} $/j proposé)"
                elif status == "pending_pdg":
                    contrat_line = f"💬 Contre-prop employé : <b>{_fmt(e.pending_salary or 0)} $/j</b>"
                else:
                    contrat_line = "❌ <b>Pas de contrat</b>"

                hint = ""
                if is_pdg and status != "signed" and uname:
                    hint = f"\n   👉 <code>/negociercontrat @{uname} [salaire]</code>"
                elif is_pdg and status == "pending_pdg" and uname:
                    pending = e.pending_salary or 0
                    hint = f"\n   👉 <code>/negociercontrat @{uname} {pending}</code> pour accepter"

                tag = f"· @{uname}" if uname else ""
                lines.append(
                    f"{role_emoji} <b>{name}</b> [{e.role}] {tag}\n"
                    f"   {bar} {activity} cmd\n"
                    f"   {contrat_line}{hint}"
                )

            lines.append("─────────────────────────────")
            lines.append(f"💰 Masse salariale/jour : <b>{_fmt(total_masse)} $</b>")

            if is_pdg:
                if sans_contrat:
                    lines.append(
                        f"\n⚠️ <b>{len(sans_contrat)} employé(s) sans contrat</b> — utilise "
                        f"<code>/negociercontrat @pseudo [salaire]</code> pour les régulariser."
                    )
                lines.append("\n💡 <code>/versersalaires payer</code> pour verser les salaires du jour.")
            else:
                lines.append("💡 Le PDG déclenche la paie avec <code>/versersalaires payer</code>.")

            await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── COMMANDE : /versersalaires ──────────────────────────────────────────────

PAYROLL_COOLDOWN_HOURS = 20   # Le PDG ne peut payer qu'une fois toutes les 20h (≈ quotidien)

async def versersalaires_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /versersalaires                    → affiche les salaires contractuels + stats activité
    /versersalaires payer              → verse les daily_salary signés à tous les employés
    /versersalaires modifier @pseudo [montant] → modifier le salaire d'un employé et sauvegarder
    """
    user = update.effective_user
    args = context.args or []
    subcmd = args[0].lower() if args else ""

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)

        if not company:
            await update.message.reply_text("❌ Tu ne fais partie d'aucune entreprise.")
            return
        if emp.role != "pdg":
            await update.message.reply_text("❌ Seul le PDG peut gérer les salaires.")
            return

        # ── MODIFIER LE SALAIRE D'UN EMPLOYÉ ─────────────────────────────
        if subcmd == "modifier":
            if len(args) < 3:
                await update.message.reply_text(
                    "❌ Usage : <code>/versersalaires modifier @pseudo [montant]</code>",
                    parse_mode="HTML"
                )
                return
            mention = args[1].lstrip("@")
            try:
                new_salary = int(args[2].replace("_", "").replace(" ", ""))
            except ValueError:
                await update.message.reply_text("❌ Montant invalide.", parse_mode="HTML")
                return

            target = (await session.execute(
                select(User).where(User.username == mention)
            )).scalar_one_or_none()
            if not target:
                await update.message.reply_text(f"❌ @{mention} introuvable.")
                return

            target_emp = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == company.id,
                    CompanyEmployee.user_id == target.user_id,
                    CompanyEmployee.left_at == None,
                )
            )).scalar_one_or_none()
            if not target_emp:
                await update.message.reply_text(f"❌ {target.first_name} n'est pas dans ton entreprise.")
                return

            old_salary = target_emp.daily_salary or 0
            target_emp.daily_salary = new_salary
            target_emp.contract_status = "signed"
            await session.commit()

            await update.message.reply_text(
                f"✅ Salaire de <b>{target.first_name}</b> modifié :\n"
                f"   {_fmt(old_salary)} $ → <b>{_fmt(new_salary)} $/jour</b>",
                parse_mode="HTML"
            )
            try:
                await context.bot.send_message(
                    chat_id=target.user_id,
                    text=(
                        f"📄 <b>Modification de contrat</b>\n\n"
                        f"🏢 <b>{company.name}</b> a mis à jour ton salaire :\n"
                        f"   {_fmt(old_salary)} $ → <b>{_fmt(new_salary)} $/jour</b>"
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass
            return

        # Récupérer tous les employés actifs (hors PDG)
        emps = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.left_at == None,
                CompanyEmployee.role != "pdg",
            )
        )).scalars().all()

        # ── AFFICHAGE RÉCAPITULATIF ───────────────────────────────────────
        if subcmd not in ("payer",):
            cooldown_info = ""
            if company.last_payroll:
                delta = datetime.utcnow() - company.last_payroll
                remaining = timedelta(hours=PAYROLL_COOLDOWN_HOURS) - delta
                if remaining.total_seconds() > 0:
                    h = int(remaining.total_seconds() // 3600)
                    m = int((remaining.total_seconds() % 3600) // 60)
                    cooldown_info = f"\n⏳ Prochaine paie dans : <b>{h}h{m:02d}m</b>"

            total_daily = 0
            lines = [
                f"💼 <b>SALAIRES — {company.name}</b>",
                f"🏦 Trésorerie : <b>{_fmt(company.treasury)} $</b>",
                "─────────────────────────────",
            ]
            for e in emps:
                emp_user = await session.get(User, e.user_id)
                name_emp = emp_user.first_name if emp_user else "?"
                role_emoji = ROLE_EMOJI.get(e.role, "👤")
                salary = e.daily_salary or 0
                activity = e.activity_since_payroll or 0
                status_icon = "✅" if e.contract_status == "signed" else "⏳"
                lines.append(
                    f"{role_emoji} <b>{name_emp}</b> [{e.role}]\n"
                    f"   {status_icon} Salaire : <b>{_fmt(salary)} $/j</b> · 📊 {activity} cmds"
                )
                total_daily += salary

            lines += [
                "─────────────────────────────",
                f"💰 Total à verser : <b>{_fmt(total_daily)} $</b>",
                f"{'✅ Trésorerie suffisante' if company.treasury >= total_daily else '⚠️ Trésorerie insuffisante'}",
                cooldown_info,
                "",
                "📌 <b>Options :</b>",
                "• <code>/versersalaires payer</code> — verser les salaires",
                "• <code>/versersalaires modifier @pseudo [montant]</code> — changer un salaire",
            ]
            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
            return

        # ── PAYER LES SALAIRES ────────────────────────────────────────────
        if company.last_payroll:
            delta = datetime.utcnow() - company.last_payroll
            remaining = timedelta(hours=PAYROLL_COOLDOWN_HOURS) - delta
            if remaining.total_seconds() > 0:
                h = int(remaining.total_seconds() // 3600)
                m = int((remaining.total_seconds() % 3600) // 60)
                await update.message.reply_text(
                    f"⏳ Prochaine paie disponible dans <b>{h}h{m:02d}m</b>.",
                    parse_mode="HTML"
                )
                return

        # Filtrer les employés avec un contrat signé
        eligible = [(e,) for e in emps if (e.daily_salary or 0) > 0 and e.contract_status == "signed"]

        if not eligible:
            await update.message.reply_text(
                "❌ Aucun employé avec un contrat signé (salaire > 0).\n"
                "Utilise <code>/versersalaires modifier @pseudo [montant]</code> pour définir les salaires.",
                parse_mode="HTML"
            )
            return

        total_to_pay = sum(e.daily_salary for (e,) in eligible)

        if company.treasury < total_to_pay:
            ratio = company.treasury / total_to_pay if total_to_pay > 0 else 0
        else:
            ratio = 1.0

        result_lines = [
            f"💼 <b>PAIE — {company.name}</b>",
            f"<i>{datetime.utcnow().strftime('%d/%m/%Y %H:%M')}</i>",
            "─────────────────────────────",
        ]

        from sqlalchemy import text as _text
        total_paid = 0
        for (e,) in eligible:
            amount = int(e.daily_salary * ratio)
            if amount <= 0:
                continue
            emp_user = await session.get(User, e.user_id)
            # SQL pur atomique pour garantir l'écriture en DB
            await session.execute(
                _text("UPDATE users SET coins = coins + :amt WHERE user_id = :uid"),
                {"amt": amount, "uid": e.user_id}
            )
            role_emoji = ROLE_EMOJI.get(e.role, "👤")
            name_emp = emp_user.first_name if emp_user else "?"
            activity = e.activity_since_payroll or 0
            result_lines.append(
                f"{role_emoji} <b>{name_emp}</b> — +{_fmt(amount)} $ <i>({activity} cmds)</i>"
            )
            try:
                await context.bot.send_message(
                    chat_id=e.user_id,
                    text=(
                        f"💵 <b>Salaire reçu !</b>\n\n"
                        f"🏢 <b>{company.name}</b> t'a versé <b>{_fmt(amount)} $</b>\n"
                        f"📊 {activity} commandes effectuées\n"
                        f"📄 Salaire contractuel : {_fmt(e.daily_salary)} $/jour"
                        + (f"\n⚠️ Versement partiel ({int(ratio*100)}%) — trésorerie insuffisante" if ratio < 1.0 else "")
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass
            e.activity_since_payroll = 0
            total_paid += amount

        from sqlalchemy import text as _text2
        await session.execute(
            _text2("UPDATE companies SET treasury = GREATEST(0, treasury - :amt), value = GREATEST(:minv, value - :amt), last_payroll = NOW() WHERE id = :cid"),
            {"amt": total_paid, "minv": LEVELS[1][2], "cid": company.id}
        )

        await _add_log(session, company.id, "paie",
                       f"Paie versée par {user.first_name} (PDG) — {_fmt(total_paid)} $ distribués",
                       amount=total_paid)
        await log_event("company_payroll", pdg=user.first_name, company=company.name, total=_fmt(total_paid))
        await session.commit()

        result_lines.append("─────────────────────────────")
        result_lines.append(f"💰 Total distribué : <b>{_fmt(total_paid)} $</b>")
        result_lines.append(f"🏦 Trésorerie restante : <b>{_fmt(company.treasury)} $</b>")
        result_lines.append(f"⏳ Prochaine paie dans <b>{PAYROLL_COOLDOWN_HOURS}h</b>")
        if ratio < 1.0:
            result_lines.append(f"⚠️ Versement partiel ({int(ratio*100)}%) — trésorerie insuffisante")
        await update.message.reply_text("\n".join(result_lines), parse_mode="HTML")


# ─── COMMANDE : /negociercontrat ─────────────────────────────────────────────

async def negociercontrat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    PDG ouvre/modifie une négociation de contrat avec un employé déjà en poste.

    Usage :
      /negociercontrat @pseudo [salaire] [prime_optionnelle]
        → PDG propose un nouveau salaire à un employé existant

      /negociercontrat @pseudo refuser
        → PDG refuse la contre-proposition de l'employé

    Côté employé — répondre à une proposition du PDG :
      /negociercontrat accepter
        → accepte le contrat en cours proposé par le PDG
      /negociercontrat refuser
        → refuse la proposition du PDG
      /negociercontrat [montant]
        → contre-propose un salaire au PDG
    """
    user = update.effective_user
    args = context.args or []

    if not args:
        await update.message.reply_text(
            "❌ Usage PDG : <code>/negociercontrat @pseudo [salaire] [prime]</code>\n"
            "❌ Usage Employé : <code>/negociercontrat accepter</code> / <code>refuser</code> / <code>[montant]</code>",
            parse_mode="HTML"
        )
        return

    async with AsyncSessionLocal() as session:
        # Détecter si c'est un PDG qui négocie (arg[0] = @pseudo) ou un employé qui répond
        first_arg = (args[0] if args else "").lower()
        is_pdg_action = first_arg.startswith("@") or (
            first_arg not in ("accepter", "refuser") and not first_arg.isdigit() == False
        )

        # Si premier arg = @pseudo → c'est forcément un PDG qui initie
        # On cherche la boite où il est PDG
        if args and args[0].startswith("@"):
            pdg_rows = (await session.execute(
                select(CompanyEmployee, Company).join(
                    Company, Company.id == CompanyEmployee.company_id
                ).where(
                    CompanyEmployee.user_id == user.id,
                    CompanyEmployee.role == "pdg",
                    CompanyEmployee.left_at == None,
                    Company.is_active == True,
                )
            )).all()
            if not pdg_rows:
                await update.message.reply_text("❌ Tu n'es PDG d'aucune entreprise.")
                return
            # Si PDG de plusieurs boites, chercher dans laquelle est la cible
            target_username = args[0].lstrip("@").lower()
            target_user = (await session.execute(
                select(User).where(func.lower(User.username) == target_username)
            )).scalar_one_or_none()
            if not target_user:
                await update.message.reply_text(f"❌ Utilisateur @{target_username} introuvable.")
                return
            company, emp = None, None
            for _emp, _company in pdg_rows:
                # Vérifier si la cible est dans cette boite
                _target_emp = (await session.execute(
                    select(CompanyEmployee).where(
                        CompanyEmployee.company_id == _company.id,
                        CompanyEmployee.user_id == target_user.user_id,
                        CompanyEmployee.left_at == None,
                    )
                )).scalar_one_or_none()
                if _target_emp:
                    company, emp = _company, _emp
                    break
            if not company:
                await update.message.reply_text(
                    f"❌ @{target_username} ne fait partie d'aucune de tes entreprises."
                )
                return
        else:
            # Chercher d'abord une entreprise où l'user a une proposition en attente
            pending_row = (await session.execute(
                select(CompanyEmployee, Company).join(
                    Company, Company.id == CompanyEmployee.company_id
                ).where(
                    CompanyEmployee.user_id == user.id,
                    CompanyEmployee.left_at == None,
                    Company.is_active == True,
                    CompanyEmployee.contract_status.in_(["pending_employee", "pending_pdg"]),
                )
            )).first()
            if pending_row:
                emp, company = pending_row
            else:
                company, emp = await _get_user_company(session, user.id)
            if not company:
                await update.message.reply_text("❌ Tu ne fais partie d'aucune entreprise.")
                return

        # ── CAS EMPLOYÉ : répondre à une proposition du PDG ───────────────
        if emp.role != "pdg":
            subcmd = args[0].lower()

            if emp.contract_status != "pending_employee":
                await update.message.reply_text(
                    "❌ Tu n'as pas de proposition de contrat en attente.\n"
                    "Attends que ton PDG t'envoie une proposition avec <code>/negociercontrat</code>.",
                    parse_mode="HTML"
                )
                return

            pending_sal = emp.pending_salary or 0
            pending_bon = emp.pending_bonus or 0

            # Trouver le PDG pour notifier
            pdg_emp = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == company.id,
                    CompanyEmployee.role == "pdg",
                    CompanyEmployee.left_at == None,
                )
            )).scalar_one_or_none()

            if subcmd == "accepter":
                emp.daily_salary = pending_sal
                emp.contract_status = "signed"
                emp.pending_salary = 0
                emp.pending_bonus = 0
                await session.commit()

                await update.message.reply_text(
                    f"✅ <b>Contrat accepté !</b>\n\n"
                    f"📄 Ton salaire : <b>{_fmt(pending_sal)} $/jour</b>"
                    + (f"\n🎁 Prime : <b>{_fmt(pending_bon)} $</b>" if pending_bon > 0 else ""),
                    parse_mode="HTML"
                )
                if pdg_emp:
                    try:
                        await context.bot.send_message(
                            chat_id=pdg_emp.user_id,
                            text=(
                                f"✅ <b>{user.first_name}</b> a accepté le contrat !\n"
                                f"📄 Salaire signé : <b>{_fmt(pending_sal)} $/jour</b>"
                            ),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

            elif subcmd == "refuser":
                emp.contract_status = "none"
                emp.pending_salary = 0
                emp.pending_bonus = 0
                await session.commit()

                await update.message.reply_text("❌ Tu as refusé la proposition de contrat.")
                if pdg_emp:
                    try:
                        await context.bot.send_message(
                            chat_id=pdg_emp.user_id,
                            text=f"❌ <b>{user.first_name}</b> a refusé ta proposition de contrat."
                        )
                    except Exception:
                        pass

            else:
                # Contre-proposition
                try:
                    counter = int(subcmd.replace("_", ""))
                except ValueError:
                    await update.message.reply_text("❌ Commande invalide. Tape <code>/negociercontrat accepter</code>, <code>refuser</code> ou un montant.", parse_mode="HTML")
                    return

                emp.contract_status = "pending_pdg"
                emp.pending_salary = counter
                await session.commit()

                await update.message.reply_text(
                    f"💬 Contre-proposition envoyée : <b>{_fmt(counter)} $/jour</b>\n"
                    f"⏳ En attente de la réponse du PDG...",
                    parse_mode="HTML"
                )
                if pdg_emp:
                    try:
                        await context.bot.send_message(
                            chat_id=pdg_emp.user_id,
                            text=(
                                f"💬 <b>Contre-proposition de {user.first_name} !</b>\n\n"
                                f"Il refuse {_fmt(pending_sal)} $/j et demande : <b>{_fmt(counter)} $/jour</b>\n\n"
                                f"✅ Accepter : <code>/negociercontrat @{user.username or user.first_name} {counter}</code>\n"
                                f"❌ Refuser : <code>/negociercontrat @{user.username or user.first_name} refuser</code>"
                            ),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
            return

        # ── CAS PDG : proposer ou répondre à une contre-proposition ───────
        if emp.role != "pdg":
            await update.message.reply_text("❌ Seul le PDG peut initier une négociation.")
            return

        mention = args[0].lstrip("@")
        subcmd_pdg = args[1].lower() if len(args) > 1 else ""

        target_user = (await session.execute(
            select(User).where(User.username == mention)
        )).scalar_one_or_none()
        if not target_user:
            await update.message.reply_text(f"❌ @{mention} introuvable.")
            return

        target_emp = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.user_id == target_user.user_id,
                CompanyEmployee.left_at == None,
            )
        )).scalar_one_or_none()
        if not target_emp:
            await update.message.reply_text(f"❌ {target_user.first_name} n'est pas dans ton entreprise.")
            return

        # PDG refuse une contre-proposition
        if subcmd_pdg == "refuser":
            target_emp.contract_status = "none"
            target_emp.pending_salary = 0
            target_emp.pending_bonus = 0
            await session.commit()

            await update.message.reply_text(f"❌ Tu as refusé la contre-proposition de {target_user.first_name}.")
            try:
                await context.bot.send_message(
                    chat_id=target_user.user_id,
                    text=f"❌ <b>{company.name}</b> a refusé ta contre-proposition."
                )
            except Exception:
                pass
            return

        # PDG propose / accepte avec un montant
        try:
            proposed = int(subcmd_pdg.replace("_", "")) if subcmd_pdg else 0
        except ValueError:
            await update.message.reply_text("❌ Montant invalide.", parse_mode="HTML")
            return

        proposed_bonus_pdg = 0
        if len(args) > 2:
            try:
                proposed_bonus_pdg = int(args[2].replace("_", ""))
            except ValueError:
                pass

        if proposed <= 0:
            await update.message.reply_text(
                "❌ Usage : <code>/negociercontrat @pseudo [salaire] [prime]</code>",
                parse_mode="HTML"
            )
            return

        # Si l'employé a une contre-proposition en attente et le PDG accepte son montant
        if target_emp.contract_status == "pending_pdg" and proposed == target_emp.pending_salary:
            target_emp.daily_salary = proposed
            target_emp.contract_status = "signed"
            target_emp.pending_salary = 0
            target_emp.pending_bonus = 0
            await session.commit()

            await update.message.reply_text(
                f"✅ Contre-proposition acceptée ! Contrat signé : <b>{_fmt(proposed)} $/jour</b>",
                parse_mode="HTML"
            )
            try:
                await context.bot.send_message(
                    chat_id=target_user.user_id,
                    text=(
                        f"✅ <b>Contrat signé !</b>\n\n"
                        f"🏢 <b>{company.name}</b> a accepté ta demande.\n"
                        f"📄 Salaire : <b>{_fmt(proposed)} $/jour</b>"
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            # Nouvelle proposition du PDG
            target_emp.contract_status = "pending_employee"
            target_emp.pending_salary = proposed
            target_emp.pending_bonus = proposed_bonus_pdg
            await session.commit()

            bonus_txt = f"\n🎁 Prime proposée : <b>{_fmt(proposed_bonus_pdg)} $</b>" if proposed_bonus_pdg > 0 else ""
            await update.message.reply_text(
                f"📩 Proposition envoyée à <b>{target_user.first_name}</b> :\n"
                f"   💰 Salaire : <b>{_fmt(proposed)} $/jour</b>{bonus_txt}\n\n"
                f"⏳ En attente de sa réponse...",
                parse_mode="HTML"
            )
            try:
                await context.bot.send_message(
                    chat_id=target_user.user_id,
                    text=(
                        f"📄 <b>Proposition de contrat — {company.name}</b>\n\n"
                        f"💰 Salaire proposé : <b>{_fmt(proposed)} $/jour</b>{bonus_txt}\n\n"
                        f"✅ Accepter : <code>/negociercontrat accepter</code>\n"
                        f"❌ Refuser : <code>/negociercontrat refuser</code>\n"
                        f"💬 Contre-proposer : <code>/negociercontrat [ton_montant]</code>"
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass


# ─── COMMANDE : /payeremploye @pseudo [montant] ───────────────────────────────

async def payeremploye_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /payeremploye @pseudo [montant] — Réservé au PDG.
    Le PDG choisit librement le montant à verser à un employé depuis la trésorerie.
    Pas de cooldown global, mais un minimum de 1 $ et un plafond = trésorerie disponible.
    Le compteur d'activité de l'employé est reset après paiement.
    """
    user = update.effective_user

    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Usage : <code>/payeremploye @pseudo [montant]</code>\n\n"
            "💡 Consulte <code>/presences</code> pour voir les travaux de chacun.",
            parse_mode="HTML"
        )
        return

    mention = context.args[0].lstrip("@")
    try:
        amount = int(context.args[1].replace("_", "").replace(" ", ""))
    except ValueError:
        await update.message.reply_text("❌ Montant invalide. Exemple : <code>/payeremploye @jean 500000</code>", parse_mode="HTML")
        return

    if amount <= 0:
        await update.message.reply_text("❌ Le montant doit être supérieur à 0.")
        return

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)

        if not company:
            await update.message.reply_text("❌ Tu ne fais partie d'aucune entreprise.")
            return
        if emp.role not in ("pdg",):
            await update.message.reply_text(
                "❌ Seul le <b>PDG</b> peut utiliser cette commande.\n"
                "Le PDG utilise <code>/versersalaires</code> pour la paie automatique.",
                parse_mode="HTML"
            )
            return
        if company.is_bot_company:
            await update.message.reply_text("❌ Impossible sur une entreprise officielle.")
            return

        # Trouver la cible
        target = (await session.execute(
            select(User).where(User.username == mention)
        )).scalar_one_or_none()
        if not target:
            await update.message.reply_text(f"❌ @{mention} introuvable.")
            return

        if target.user_id == user.id:
            await update.message.reply_text(
                "❌ Tu ne peux pas te payer toi-même ici.\n"
                "Utilise <code>/retraitboite [montant]</code> pour retirer ta part.",
                parse_mode="HTML"
            )
            return

        target_emp = await _get_employee(session, company.id, target.user_id)
        if not target_emp or target_emp.left_at is not None:
            await update.message.reply_text(f"❌ {target.first_name} ne fait pas partie de {company.name}.")
            return

        if target_emp.role == "stagiaire":
            await update.message.reply_text(
                f"❌ Les stagiaires ne reçoivent pas de salaire.\n"
                f"Promus-le d'abord avec <code>/nommer @{mention} secretaire</code> ou supérieur.",
                parse_mode="HTML"
            )
            return

        # Vérifier trésorerie
        if company.treasury <= 0:
            await update.message.reply_text(
                "❌ La trésorerie est vide. Attends que les revenus s'accumulent.",
                parse_mode="HTML"
            )
            return

        if amount > company.treasury:
            await update.message.reply_text(
                f"❌ Trésorerie insuffisante.\n"
                f"🏦 Disponible : <b>{_fmt(company.treasury)} $</b>\n"
                f"💸 Demandé : <b>{_fmt(amount)} $</b>",
                parse_mode="HTML"
            )
            return

        # Paiement
        activity = target_emp.activity_since_payroll or 0
        target_user = await get_user(session, target.user_id)
        target_user.coins += amount
        company.treasury -= amount
        company.value = max(LEVELS[1][2], company.value - amount)
        target_emp.activity_since_payroll = 0   # Reset le compteur après paiement

        role_emoji = ROLE_EMOJI.get(target_emp.role, "👤")

        await _add_log(session, company.id, "paie_pdg",
                       f"PDG {user.first_name} → {target.first_name} ({target_emp.role}) : {_fmt(amount)} $ "
                       f"({activity} cmds depuis dernière paie)",
                       amount=amount)
        await session.commit()

        await update.message.reply_text(
            f"✅ <b>Salaire versé !</b>\n\n"
            f"{role_emoji} <b>{target.first_name}</b> [{target_emp.role}]\n"
            f"💰 Montant : <b>{_fmt(amount)} $</b>\n"
            f"📊 Activité depuis dernière paie : <b>{activity} commandes</b>\n\n"
            f"🏦 Trésorerie restante : <b>{_fmt(company.treasury)} $</b>",
            parse_mode="HTML"
        )

        # Notifier l'employé
        try:
            await context.bot.send_message(
                chat_id=target.user_id,
                text=(
                    f"💵 <b>Salaire reçu !</b>\n\n"
                    f"🏢 <b>{company.name}</b>\n"
                    f"💎 Le PDG t'a versé <b>{_fmt(amount)} $</b>\n"
                    f"📊 Activité comptabilisée : {activity} commandes"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass


# ─── COMMANDE : /skipattente ────────────────────────────────────────────────────

SKIP_COMPANY_COST = 500_000  # 500K pour ignorer le cooldown démission

async def skipattente_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    async with AsyncSessionLocal() as session:
        from sqlalchemy import text
        # Chercher le dernier départ non-bot
        last_left_row = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.user_id == user.id,
                CompanyEmployee.left_at != None,
            ).order_by(CompanyEmployee.left_at.desc()).limit(1)
        )).scalar_one_or_none()

        if not last_left_row or not last_left_row.left_at:
            await update.message.reply_text("✅ Tu n'as aucun cooldown de démission actif !")
            return

        last_company = await session.get(Company, last_left_row.company_id)
        is_bot = last_company.is_bot_company if last_company else False

        if is_bot:
            await update.message.reply_text("✅ Tu n'as aucun cooldown actif (dernière boîte = bot company).")
            return

        days_passed = (datetime.utcnow() - last_left_row.left_at).days
        if days_passed >= 3:
            await update.message.reply_text("✅ Ton cooldown de démission est déjà terminé !")
            return

        # Vérifier les coins
        u = await get_user(session, user.id)
        if u.coins < SKIP_COMPANY_COST:
            await update.message.reply_text(
                f"❌ Il te faut <b>{_fmt(SKIP_COMPANY_COST)} 💰</b> pour ignorer l'attente.\n"
                f"Ton solde : <b>{_fmt(u.coins)} 💰</b>",
                parse_mode="HTML",
            )
            return

        # Déduire les coins et effacer la date de départ (simule la fin du cooldown)
        await session.execute(
            text("UPDATE company_employees SET left_at = :old_date WHERE id = :eid"),
            {"old_date": datetime.utcnow() - timedelta(days=4), "eid": last_left_row.id},
        )
        await session.execute(
            text("UPDATE users SET coins = GREATEST(0, CAST(coins AS BIGINT) - CAST(:cost AS BIGINT)) WHERE user_id = :uid AND coins >= :cost"),
            {"cost": SKIP_COMPANY_COST, "uid": user.id},
        )
        await session.commit()
        jours_restants = 3 - days_passed  # calculé ici avant la fermeture du bloc

    try:
        await update.message.reply_text(
            f"⚡ <b>Cooldown ignoré !</b>\n\n"
            f"💸 <b>{_fmt(SKIP_COMPANY_COST)} 💰</b> déduits.\n"
            f"(Il te restait <b>{jours_restants} jour(s)</b>)\n\n"
            f"Tu peux maintenant postuler dans une entreprise avec <code>/postuler [nom]</code>.",
            parse_mode="HTML",
        )
    except Exception:
        # Transaction déjà commitée — fallback court si Telegram rate-limite
        try:
            await update.message.reply_text("⚡ Cooldown ignoré ! 💸 Coins déduits.")
        except Exception:
            pass


# ─── /annoncerecrutement — Annonce PDG broadcast dans tous les groupes ─────────

ANNONCE_COOLDOWN_DAYS = 7  # 1 annonce par semaine

ANNONCE_TYPES = {
    "recrutement": "📢 Recrutement",
    "partenariat": "🤝 Partenariat",
    "promotion":   "🎉 Promotion interne",
    "flexing":     "💰 Palmarès",
}


def _build_annonce(type_key: str, company: "Company", rank: int, total: int) -> str:
    sec_emoji, sec_name = SECTORS.get(company.sector, ("🏢", company.sector))
    lvl_emoji, lvl_name, *_ = LEVELS.get(company.level, LEVELS[1])
    val_str = _fmt(company.value)
    rep = company.reputation

    if type_key == "recrutement":
        # Estimation salaire mensuel
        daily_rate = 0.001 * (1 + (company.level - 1) * 0.002)
        est_salary = _fmt(int(company.value * daily_rate) // 30)
        return (
            f"📢 <b>{company.name} recrute !</b>\n\n"
            f"{sec_emoji} Secteur : <b>{sec_name}</b> | {lvl_emoji} Niveau : <b>{lvl_name}</b>\n"
            f"⭐ Réputation : <b>{rep:.1f}/5</b>\n"
            f"💵 Salaire estimé : <b>~{est_salary} $ / semaine</b>\n\n"
            f"👉 <code>/postuler {company.name}</code>"
        )
    elif type_key == "partenariat":
        return (
            f"🤝 <b>{company.name} cherche un partenaire !</b>\n\n"
            f"{sec_emoji} Secteur : <b>{sec_name}</b>\n"
            f"⭐ Réputation : <b>{rep:.1f}/5</b> | 💰 Valeur : <b>{val_str} $</b>\n\n"
            f"Proposez un contrat inter-secteurs :\n"
            f"👉 <code>/proposercontrat {company.name}</code>"
        )
    elif type_key == "promotion":
        return (
            f"🎉 <b>{company.name}</b> vient de promouvoir un membre de son équipe !\n\n"
            f"{sec_emoji} Secteur : <b>{sec_name}</b> | {lvl_emoji} <b>{lvl_name}</b>\n"
            f"L'équipe s'agrandit et se renforce. Rejoignez l'aventure !\n\n"
            f"👉 <code>/postuler {company.name}</code>"
        )
    elif type_key == "flexing":
        return (
            f"💰 <b>{company.name}</b> — #{rank} sur {total} entreprises\n\n"
            f"Valeur : <b>{val_str} $</b> | {lvl_emoji} <b>{lvl_name}</b>\n"
            f"⭐ Réputation : <b>{rep:.1f}/5</b>\n\n"
            f"On recrute les meilleurs. Vous êtes à la hauteur ?\n"
            f"👉 <code>/postuler {company.name}</code>"
        )
    return ""


async def annoncerecrutement_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /annoncerecrutement — Le PDG choisit un type d'annonce (boutons), le bot l'envoie dans tous les groupes.
    Cooldown 24h par entreprise.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    user = update.effective_user

    async with AsyncSessionLocal() as session:
        result = await _get_user_company(session, user.id)
        if not result:
            return await update.message.reply_text("❌ Tu ne fais partie d'aucune entreprise.")
        company, emp = result
        if emp.role not in DIRECTION_ROLES:
            return await update.message.reply_text("❌ Seul le PDG ou Directeur peut envoyer une annonce.")

        # Vérifier cooldown (persisté en base)
        if company.last_annonce:
            delta = datetime.utcnow() - company.last_annonce
            if delta.days < ANNONCE_COOLDOWN_DAYS:
                reste = ANNONCE_COOLDOWN_DAYS - delta.days
                return await update.message.reply_text(
                    f"⏳ Annonce déjà envoyée cette semaine.\n"
                    f"Prochaine annonce disponible dans <b>{reste} jour(s)</b>.",
                    parse_mode="HTML"
                )

    # Afficher le menu de choix
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Recrutement",     callback_data=f"annonce:{company.id}:recrutement")],
        [InlineKeyboardButton("🤝 Partenariat",     callback_data=f"annonce:{company.id}:partenariat")],
        [InlineKeyboardButton("🎉 Promotion interne", callback_data=f"annonce:{company.id}:promotion")],
        [InlineKeyboardButton("💰 Palmarès",        callback_data=f"annonce:{company.id}:flexing")],
    ])
    await update.message.reply_text(
        f"📣 <b>Quelle annonce veux-tu diffuser pour <i>{company.name}</i> ?</b>\n\n"
        f"Elle sera envoyée dans tous les groupes actifs du bot.",
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def annoncerecrutement_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback quand le PDG choisit son type d'annonce."""
    from database.db import get_all_groups
    query = update.callback_query
    await query.answer()

    _, company_id_str, type_key = query.data.split(":")
    company_id = int(company_id_str)

    # Vérifier autorisation + cooldown + marquer l'annonce
    user_id = query.from_user.id
    rank = 0
    total = 0
    async with AsyncSessionLocal() as session:
        emp = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == company_id,
                CompanyEmployee.user_id == user_id,
                CompanyEmployee.left_at == None,
                CompanyEmployee.role.in_(["pdg", "directeur"]),
            )
        )).scalar_one_or_none()
        if not emp:
            return await query.edit_message_text("❌ Tu n'es plus autorisé à faire cette action.")

        company = await session.get(Company, company_id)
        if not company or not company.is_active:
            return await query.edit_message_text("❌ Entreprise introuvable.")

        # Cooldown 7 jours
        if company.last_annonce:
            delta = datetime.utcnow() - company.last_annonce
            if delta.days < ANNONCE_COOLDOWN_DAYS:
                reste = ANNONCE_COOLDOWN_DAYS - delta.days
                return await query.edit_message_text(
                    f"⏳ Annonce déjà envoyée cette semaine. Réessaie dans <b>{reste} jour(s)</b>.",
                    parse_mode="HTML"
                )

        # Rang de l'entreprise
        all_companies = (await session.execute(
            select(Company).where(Company.is_active == True).order_by(Company.treasury.desc())
        )).scalars().all()
        total = len(all_companies)
        rank = next((i + 1 for i, c in enumerate(all_companies) if c.id == company_id), total)

        company.last_annonce = datetime.utcnow()
        await session.commit()

    msg = _build_annonce(type_key, company, rank, total)
    groups = await get_all_groups(active_only=True)

    sent_ok = 0
    sent_err = 0
    current_chat = query.message.chat_id
    for grp in groups:
        gid = grp[0]
        if gid == current_chat:
            continue
        try:
            await context.bot.send_message(chat_id=gid, text=msg, parse_mode="HTML")
            sent_ok += 1
        except Exception:
            sent_err += 1

    label = ANNONCE_TYPES.get(type_key, type_key)
    await query.edit_message_text(
        f"✅ <b>Annonce « {label} » diffusée !</b>\n\n"
        f"📡 Envoyée dans <b>{sent_ok}</b> groupe(s)"
        + (f" — {sent_err} échec(s)" if sent_err else "") + ".\n"
        f"⏳ Prochaine annonce disponible dans <b>7 jours</b>.",
        parse_mode="HTML"
    )


# ─── COMMANDE : /renommerboite [nouveau nom] ────────────────────────────────

RENAME_COST = 10_000_000  # 10 millions $
RENAME_COOLDOWN_DAYS = 30  # un renommage tous les 30 jours

async def renommerboite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not context.args:
        await update.message.reply_text(
            f"❌ Usage : <code>/renommerboite [nouveau nom]</code>\n"
            f"💸 Coût : <b>{_fmt(RENAME_COST)} $</b> (depuis ta trésorerie)\n"
            f"⏳ Cooldown : <b>{RENAME_COOLDOWN_DAYS} jours</b>\n"
            f"🔒 Réservé au <b>PDG</b>",
            parse_mode="HTML"
        )
        return

    new_name = " ".join(context.args).strip()

    if len(new_name) < 2:
        await update.message.reply_text("❌ Le nom doit contenir au moins 2 caractères.")
        return

    if len(new_name) > 40:
        await update.message.reply_text("❌ Le nom ne peut pas dépasser 40 caractères.")
        return

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)

        if not company:
            await update.message.reply_text("❌ Tu ne fais partie d'aucune entreprise.")
            return

        if emp.role not in ("pdg",):
            await update.message.reply_text(
                "❌ Seul le <b>PDG</b> 💎 peut renommer l'entreprise.",
                parse_mode="HTML"
            )
            return

        if company.is_bot_company:
            await update.message.reply_text("❌ Les entreprises du bot ne peuvent pas être renommées.")
            return

        # Vérifier cooldown
        if company.last_rename:
            delta = datetime.utcnow() - company.last_rename
            if delta.days < RENAME_COOLDOWN_DAYS:
                reste = RENAME_COOLDOWN_DAYS - delta.days
                await update.message.reply_text(
                    f"⏳ Tu as déjà renommé cette entreprise récemment.\n"
                    f"Prochain renommage disponible dans <b>{reste} jour(s)</b>.",
                    parse_mode="HTML"
                )
                return

        # Vérifier que le nouveau nom n'existe pas déjà
        name_conflict = (await session.execute(
            select(Company).where(Company.name.ilike(new_name))
        )).scalar_one_or_none()
        if name_conflict:
            await update.message.reply_text(
                f"❌ Une entreprise nommée <b>{new_name}</b> existe déjà (ou a existé).\n"
                f"Choisis un autre nom.",
                parse_mode="HTML"
            )
            return

        # Vérifier fonds en trésorerie
        if company.treasury < RENAME_COST:
            await update.message.reply_text(
                f"❌ La trésorerie de <b>{company.name}</b> est insuffisante.\n"
                f"💸 Coût du renommage : <b>{_fmt(RENAME_COST)} $</b>\n"
                f"🏦 Trésorerie actuelle : <b>{_fmt(company.treasury)} $</b>\n\n"
                f"Alimente la trésorerie avec <code>/depotboite [montant]</code>",
                parse_mode="HTML"
            )
            return

        old_name = company.name
        company.treasury -= RENAME_COST
        company.value = max(LEVELS[1][2], company.value - RENAME_COST)
        company.name = new_name
        company.last_rename = datetime.utcnow()

        await _add_log(
            session, company.id, "renommage",
            f"Entreprise renommée de « {old_name} » en « {new_name} » par {user.first_name} "
            f"(coût : {_fmt(RENAME_COST)} $)"
        )
        await session.commit()

    await update.message.reply_text(
        f"✅ Entreprise renommée avec succès !\n\n"
        f"📛 Ancien nom : <b>{old_name}</b>\n"
        f"🆕 Nouveau nom : <b>{new_name}</b>\n"
        f"💸 Coût débité de la trésorerie : <b>{_fmt(RENAME_COST)} $</b>\n"
        f"⏳ Prochain renommage possible dans <b>{RENAME_COOLDOWN_DAYS} jours</b>",
        parse_mode="HTML"
    )


# ─── COMMANDE : /acheterpla [quantite] ──────────────────────────────────────

# Prix par place supplémentaire selon le niveau de l'entreprise
SLOT_PRICES = {
    1: 5_000_000,     # Startup       → 5M$ / place
    2: 15_000_000,    # PME           → 15M$ / place
    3: 50_000_000,    # Société       → 50M$ / place
    4: 150_000_000,   # Corporation   → 150M$ / place
    5: 500_000_000,   # Holding       → 500M$ / place
}
MAX_EXTRA_SLOTS = 20  # maximum de places bonus cumulables

async def acheterpla_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /acheterpla [quantite]
    Permet au PDG d'acheter des places supplémentaires pour son entreprise.
    Payé depuis la trésorerie. Max 20 places bonus cumulables.
    """
    user = update.effective_user

    if not context.args:
        await update.message.reply_text(
            "❌ <b>Usage :</b> <code>/acheterpla [quantité]</code>\n\n"
            "💡 Achète des places supplémentaires pour ton entreprise.\n"
            "💸 Payé depuis la <b>trésorerie</b>.\n"
            f"📦 Maximum <b>{MAX_EXTRA_SLOTS} places bonus</b> cumulables.\n\n"
            "Prix selon le niveau :\n"
            "🏪 Startup      → 5 000 000 $ / place\n"
            "🏢 PME          → 15 000 000 $ / place\n"
            "🏬 Société      → 50 000 000 $ / place\n"
            "🏦 Corporation  → 150 000 000 $ / place\n"
            "👑 Holding      → 500 000 000 $ / place",
            parse_mode="HTML"
        )
        return

    try:
        qty = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Quantité invalide.")
        return

    if qty <= 0:
        await update.message.reply_text("❌ La quantité doit être positive.")
        return

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)

        if not company:
            await update.message.reply_text("❌ Tu ne fais partie d'aucune entreprise.")
            return

        if emp.role not in ("pdg",):
            await update.message.reply_text(
                "❌ Seul le <b>PDG</b> 👑 peut acheter des places supplémentaires.",
                parse_mode="HTML"
            )
            return

        if company.is_bot_company:
            await update.message.reply_text("❌ Impossible sur une entreprise officielle.")
            return

        current_extra = company.extra_slots or 0
        if current_extra >= MAX_EXTRA_SLOTS:
            await update.message.reply_text(
                f"❌ Tu as déjà atteint le maximum de <b>{MAX_EXTRA_SLOTS} places bonus</b>.\n"
                f"Monte de niveau pour augmenter la capacité de base.",
                parse_mode="HTML"
            )
            return

        # Ajuster la quantité si elle dépasse le max
        qty_possible = min(qty, MAX_EXTRA_SLOTS - current_extra)
        if qty_possible < qty:
            await update.message.reply_text(
                f"⚠️ Tu ne peux acheter que <b>{qty_possible} place(s)</b> "
                f"(limite de {MAX_EXTRA_SLOTS} bonus atteinte après ça).\n"
                f"Achat ramené à <b>{qty_possible}</b>.",
                parse_mode="HTML"
            )
            qty = qty_possible

        prix_unitaire = SLOT_PRICES.get(company.level, SLOT_PRICES[1])
        cout_total = prix_unitaire * qty

        _, _, _, _, base_cap = _level_info(company.level)
        nouvelle_cap = base_cap + current_extra + qty

        if company.treasury < cout_total:
            await update.message.reply_text(
                f"❌ Trésorerie insuffisante.\n\n"
                f"💸 Coût : <b>{_fmt(cout_total)} $</b> ({qty} × {_fmt(prix_unitaire)} $)\n"
                f"🏦 Trésorerie : <b>{_fmt(company.treasury)} $</b>\n\n"
                f"Alimente la trésorerie avec <code>/depotboite [montant]</code>",
                parse_mode="HTML"
            )
            return

        # Déduire de la trésorerie et créditer les places
        company.treasury -= cout_total
        company.value = max(LEVELS[1][2], company.value - cout_total)
        company.extra_slots = current_extra + qty

        await _add_log(
            session, company.id, "achat_places",
            f"👤 {user.first_name} a acheté {qty} place(s) supplémentaire(s) "
            f"(coût : {_fmt(cout_total)} $) — capacité : {base_cap + current_extra} → {nouvelle_cap}"
        )
        await session.commit()

    await update.message.reply_text(
        f"✅ <b>Achat de places confirmé !</b>\n\n"
        f"📦 Places achetées : <b>+{qty}</b>\n"
        f"💸 Coût débité : <b>{_fmt(cout_total)} $</b>\n"
        f"👥 Nouvelle capacité : <b>{nouvelle_cap} employés</b>\n"
        f"📊 Places bonus restantes disponibles : <b>{MAX_EXTRA_SLOTS - company.extra_slots}</b>",
        parse_mode="HTML"
    )
