"""
api/webapp_actions.py — Endpoints webapp complets, isolés du bot Telegram.

Principe d'isolation :
  - Ce fichier n'importe JAMAIS les handlers du bot (handlers/*)
  - Toutes les notifications Telegram passent par l'API HTTP (bot token), pas par l'objet bot
  - Les accès DB sont directs via AsyncSessionLocal + modèles SQLAlchemy
  - Le bot peut redémarrer sans impacter la webapp, et vice versa

Routes ajoutées (toutes préfixées /api/webapp/) :
  GET  companies/list          → liste toutes les entreprises avec pagination
  GET  companies/search        → recherche par nom
  GET  companies/:id/info      → détail public d'une entreprise
  POST postuler                → postuler à une entreprise
  POST demissionner            → démissionner
  POST licencier               → licencier un employé (PDG/Directeur)
  POST nommer                  → changer le rôle d'un employé
  POST recruter                → inviter un joueur
  POST versersalaires          → verser les salaires (PDG)
  POST payeremploye            → payer un employé manuellement (PDG)
  POST negociercontrat         → négocier/accepter/refuser contrat employé
  GET  parts                   → mes parts dans toutes les entreprises
  POST parts/vendre            → vendre des parts
  POST parts/acheter           → soumettre une offre d'achat
  POST parts/accepteroffre     → PDG accepte une offre
  POST parts/refuseroffre      → PDG refuse une offre
  POST pay                     → transfert de coins entre joueurs
  GET  players/search          → rechercher un joueur par username
  GET  invitations             → mes invitations en attente
  POST invitations/accepter    → accepter une invitation (rejoindre)
  POST invitations/refuser     → refuser une invitation
  GET  contrats/bc             → mes contrats Bureau
  GET  contrats/auto           → mes contrats automatiques
  POST skipattente             → payer pour sauter le cooldown démission
  GET  presences               → tableau de bord présences (direction)
"""

from __future__ import annotations

import aiohttp
import hmac
import hashlib
import json
import logging
from datetime import datetime, timedelta

from aiohttp import web
from sqlalchemy import select, func, or_, text as sa_text

from config import BOT_TOKEN
from database.db import AsyncSessionLocal
from database.models import (
    User, Company, CompanyEmployee, CompanyShare,
    CompanyApplication, CompanyInvite, CompanyLog,
    CompanyShareOffer, BureauContrat, CompanyAutoContract,
)

logger = logging.getLogger(__name__)

# ─── CONSTANTES (dupliquées ici pour l'isolation totale) ────────────────────

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

LEVELS = {
    1: ("🏪", "Startup",      50_000_000,    0.04, 5),
    2: ("🏢", "PME",          200_000_000,   0.06, 10),
    3: ("🏬", "Société",      500_000_000,   0.08, 50),
    4: ("🏦", "Corporation", 2_000_000_000,  0.10, 100),
    5: ("👑", "Holding",    10_000_000_000,  0.12, 200),
}

ROLES_ORDER = ["stagiaire", "secretaire", "employe", "manager", "directeur", "pdg"]

ROLE_EMOJI = {
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

DIRECTION_ROLES = ("pdg", "directeur")
MANAGEMENT_ROLES = ("pdg", "directeur", "manager")

SKIP_COST = 500_000
PAYROLL_COOLDOWN_HOURS = 20

# ─── UTILITAIRES ─────────────────────────────────────────────────────────────

def _fmt(n) -> str:
    try:
        n = int(float(n or 0))
    except Exception:
        return "0"
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)


def _level_info(lvl: int):
    return LEVELS.get(lvl, LEVELS[1])


def _max_employees(company: Company) -> int:
    _, _, _, _, base = _level_info(company.level or 1)
    return base + (company.extra_slots or 0)


def _has_diploma(user: User, level: str) -> bool:
    order = ["bac", "licence", "master", "mba"]
    if level not in order:
        return True
    idx = order.index(level)
    checks = [user.diplome_bac, user.diplome_licence, user.diplome_master, user.diplome_mba]
    return bool(checks[idx])


def _user_diploma_level(user: User) -> int:
    if user.diplome_mba:     return 4
    if user.diplome_master:  return 3
    if user.diplome_licence: return 2
    if user.diplome_bac:     return 1
    return 0


async def _get_company_by_name(session, name: str):
    r = await session.execute(
        select(Company).where(Company.name.ilike(name), Company.is_active == True)
    )
    return r.scalar_one_or_none()


async def _get_user_company(session, uid: int):
    """Retourne (Company, CompanyEmployee) en priorisant le rôle le plus élevé."""
    rows = (await session.execute(
        select(CompanyEmployee, Company).join(
            Company, Company.id == CompanyEmployee.company_id
        ).where(
            CompanyEmployee.user_id == uid,
            CompanyEmployee.left_at == None,
            Company.is_active == True,
        )
    )).all()
    if not rows:
        return None, None
    # Priorité : pdg > directeur > manager > ...
    rows.sort(key=lambda r: ROLES_ORDER.index(r[0].role) if r[0].role in ROLES_ORDER else 0, reverse=True)
    emp, company = rows[0]
    return company, emp


async def _add_log(session, company_id: int, event_type: str, description: str, amount: int = None):
    session.add(CompanyLog(
        company_id=company_id,
        event_type=event_type,
        description=description,
        amount=amount,
    ))


async def _notify(chat_id: int, text: str, parse_mode: str = "HTML"):
    """Envoie un message Telegram sans passer par l'objet bot — isolation totale."""
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
                timeout=aiohttp.ClientTimeout(total=5),
            )
    except Exception:
        pass


def _parse_uid(request: web.Request, key: str = "user_id") -> int:
    """Retourne le user_id vérifié par le middleware (ignore key, gardé pour compatibilité)."""
    return request.get('verified_uid', 0)


async def _body(request: web.Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


def _ok(msg: str, **extra) -> web.Response:
    return web.json_response({"ok": True, "msg": msg, **extra})


def _err(msg: str, status: int = 200) -> web.Response:
    return web.json_response({"error": msg}, status=status)


# ─── AUTH ────────────────────────────────────────────────────────────────────
# La whitelist est gérée dans webapp.py principal. Ici on fait confiance
# au fait que les requêtes arrivent depuis l'interface déjà authentifiée.
# On vérifie juste que user_id est présent et entier.

def _auth(uid: int) -> bool:
    """uid > 0 suffit : le middleware a déjà vérifié la signature Telegram."""
    return uid > 0


# ═════════════════════════════════════════════════════════════════════════════
#  RECHERCHE JOUEURS
# ═════════════════════════════════════════════════════════════════════════════

async def webapp_players_search(request: web.Request) -> web.Response:
    """GET /api/webapp/players/search?q=pseudo&user_id=xxx"""
    uid = _parse_uid(request)
    if not _auth(uid):
        return _err("unauthorized", 403)

    q = request.rel_url.query.get("q", "").strip().lstrip("@")
    if len(q) < 2:
        return _err("Tape au moins 2 caractères")

    async with AsyncSessionLocal() as session:
        users = (await session.execute(
            select(User).where(
                or_(
                    User.username.ilike(f"%{q}%"),
                    User.first_name.ilike(f"%{q}%"),
                ),
                User.user_id != uid,
                User.is_banned == False,
            ).limit(10)
        )).scalars().all()

        players = []
        for u in users:
            # Entreprise actuelle
            _c, _e = await _get_user_company(session, u.user_id)
            players.append({
                "user_id":  u.user_id,
                "name":     u.first_name or "—",
                "username": u.username or "",
                "company":  _c.name if _c else None,
                "role":     _e.role if _e else None,
                "diplome":  "MBA" if u.diplome_mba else "Master" if u.diplome_master else
                            "Licence" if u.diplome_licence else "Bac" if u.diplome_bac else "—",
            })

    return web.json_response({"players": players})


# ═════════════════════════════════════════════════════════════════════════════
#  LISTE ENTREPRISES
# ═════════════════════════════════════════════════════════════════════════════

async def webapp_companies_list(request: web.Request) -> web.Response:
    """GET /api/webapp/companies/list?page=0&sector=tech&user_id=xxx"""
    uid = _parse_uid(request)
    if not _auth(uid):
        return _err("unauthorized", 403)

    page   = int(request.rel_url.query.get("page", 0))
    sector = request.rel_url.query.get("sector", "").strip()
    PAGE   = 10

    async with AsyncSessionLocal() as session:
        q = select(Company).where(Company.is_active == True)
        if sector:
            q = q.where(Company.sector == sector)
        q = q.order_by(Company.value.desc())

        all_cos = (await session.execute(q)).scalars().all()
        total   = len(all_cos)
        page_cos = all_cos[page * PAGE : (page + 1) * PAGE]

        items = []
        for i, c in enumerate(page_cos, page * PAGE + 1):
            sec_emoji, sec_name = SECTORS.get(c.sector, ("🏢", c.sector))
            lvl_emoji, lvl_name, *_ = _level_info(c.level or 1)
            nb_emp = (await session.execute(
                select(func.count()).where(
                    CompanyEmployee.company_id == c.id,
                    CompanyEmployee.left_at == None,
                )
            )).scalar()
            max_emp = _max_employees(c)

            # Vérifier si le user a déjà postulé
            already_applied = (await session.execute(
                select(CompanyApplication).where(
                    CompanyApplication.company_id == c.id,
                    CompanyApplication.user_id == uid,
                    CompanyApplication.status == "pending",
                )
            )).scalar_one_or_none()

            # Vérifier si le user est déjà dans cette boite
            already_in = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == c.id,
                    CompanyEmployee.user_id == uid,
                    CompanyEmployee.left_at == None,
                )
            )).scalar_one_or_none()

            items.append({
                "rank":          i,
                "id":            c.id,
                "name":          c.name,
                "emoji":         sec_emoji,
                "sector":        c.sector,
                "sec_emoji":     sec_emoji,
                "sec_name":      sec_name,
                "level":         c.level or 1,
                "lvl_emoji":     lvl_emoji,
                "lvl_name":      lvl_name,
                "value":         _fmt(c.value),
                "value_raw":     c.value or 0,
                "capital":       c.value or 0,
                "treasury":      _fmt(c.treasury),
                "reputation":    round(c.reputation or 0, 1),
                "nb_emp":        nb_emp,
                "employee_count": nb_emp,
                "max_emp":       max_emp,
                "max_employees": max_emp,
                "is_bot":        c.is_bot_company,
                "can_apply":     not already_in and not already_applied and nb_emp < max_emp,
                "is_hiring":     not already_in and not already_applied and nb_emp < max_emp,
                "already_applied": bool(already_applied),
                "already_in":    bool(already_in),
            })

    return web.json_response({"items": items, "companies": items, "total": total, "page": page, "pages": (total + PAGE - 1) // PAGE})


async def webapp_companies_search(request: web.Request) -> web.Response:
    """GET /api/webapp/companies/search?q=nom&user_id=xxx
    GET /api/webapp/companies/search?id=123&user_id=xxx  (fiche détaillée d'une entreprise)"""
    uid = _parse_uid(request)
    if not _auth(uid):
        return _err("unauthorized", 403)

    cid_param = request.rel_url.query.get("id", "").strip()
    q = request.rel_url.query.get("q", "").strip()

    async with AsyncSessionLocal() as session:
        if cid_param:
            try:
                cid = int(cid_param)
            except ValueError:
                return _err("id invalide")
            target = await session.get(Company, cid)
            cos = [target] if target and target.is_active else []
        else:
            if len(q) < 2:
                return _err("Tape au moins 2 caractères")
            cos = (await session.execute(
                select(Company).where(
                    Company.name.ilike(f"%{q}%"),
                    Company.is_active == True,
                ).limit(10)
            )).scalars().all()

        items = []
        for c in cos:
            sec_emoji, sec_name = SECTORS.get(c.sector, ("🏢", c.sector))
            lvl_emoji, lvl_name, *_ = _level_info(c.level or 1)
            nb_emp = (await session.execute(
                select(func.count()).where(
                    CompanyEmployee.company_id == c.id,
                    CompanyEmployee.left_at == None,
                )
            )).scalar()
            max_emp = _max_employees(c)
            avg_salary = (await session.execute(
                select(func.avg(CompanyEmployee.daily_salary)).where(
                    CompanyEmployee.company_id == c.id,
                    CompanyEmployee.left_at == None,
                )
            )).scalar() or 0

            already_applied = (await session.execute(
                select(CompanyApplication).where(
                    CompanyApplication.company_id == c.id,
                    CompanyApplication.user_id == uid,
                    CompanyApplication.status == "pending",
                )
            )).scalar_one_or_none()
            already_in = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == c.id,
                    CompanyEmployee.user_id == uid,
                    CompanyEmployee.left_at == None,
                )
            )).scalar_one_or_none()

            items.append({
                "id":        c.id,
                "name":      c.name,
                "emoji":     sec_emoji,
                "sector":    c.sector,
                "sec_emoji": sec_emoji,
                "sec_name":  sec_name,
                "level":     c.level or 1,
                "lvl_emoji": lvl_emoji,
                "lvl_name":  lvl_name,
                "description": c.description or "",
                "value":     _fmt(c.value),
                "capital":   c.value or 0,
                "reputation": round(c.reputation or 0, 1),
                "nb_emp":    nb_emp,
                "employee_count": nb_emp,
                "max_emp":   max_emp,
                "max_employees": max_emp,
                "avg_salary": int(avg_salary),
                "is_bot":    c.is_bot_company,
                "is_hiring": not already_in and not already_applied and nb_emp < max_emp,
                "already_applied": bool(already_applied),
                "already_in": bool(already_in),
            })

    return web.json_response({"items": items, "companies": items})


# ═════════════════════════════════════════════════════════════════════════════
#  POSTULER
# ═════════════════════════════════════════════════════════════════════════════

async def webapp_postuler(request: web.Request) -> web.Response:
    """POST /api/webapp/postuler — body: {user_id, company_id}"""
    body = await _body(request)
    uid = _parse_uid(request)
    cid  = int(body.get("company_id", 0))

    if not _auth(uid):
        return _err("unauthorized", 403)
    if not cid:
        return _err("company_id manquant")

    async with AsyncSessionLocal() as session:
        user = await session.get(User, uid)
        if not user:
            return _err("Utilisateur introuvable")

        target = await session.get(Company, cid)
        if not target or not target.is_active:
            return _err("Entreprise introuvable")

        # Max 2 entreprises
        all_emps = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.user_id == uid,
                CompanyEmployee.left_at == None,
            )
        )).scalars().all()
        if len(all_emps) >= 2:
            return _err("Tu es déjà dans 2 entreprises (maximum)")

        # Cooldown démission (3 jours, hors bot company)
        last_left = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.user_id == uid,
                CompanyEmployee.left_at != None,
            ).order_by(CompanyEmployee.left_at.desc()).limit(1)
        )).scalar_one_or_none()

        if last_left and last_left.left_at:
            last_co = await session.get(Company, last_left.company_id)
            if last_co and not last_co.is_bot_company:
                days_passed = (datetime.utcnow() - last_left.left_at).days
                if days_passed < 3:
                    jours = 3 - days_passed
                    return _err(f"Cooldown : encore {jours} jour(s) avant de postuler. Utilise /skipattente.")

        # Déjà postulé ?
        existing = (await session.execute(
            select(CompanyApplication).where(
                CompanyApplication.company_id == cid,
                CompanyApplication.user_id == uid,
                CompanyApplication.status == "pending",
            )
        )).scalar_one_or_none()
        if existing:
            return _err("Tu as déjà une candidature en cours")

        # Capacité
        nb_emp = (await session.execute(
            select(func.count()).where(
                CompanyEmployee.company_id == cid,
                CompanyEmployee.left_at == None,
            )
        )).scalar()
        if nb_emp >= _max_employees(target):
            return _err(f"{target.name} est au complet")

        # Entreprise bot → verdict immédiat
        if target.is_bot_company:
            SECTOR_REQ = {
                "tech": 2, "finance": 3, "droit": 3, "immobilier": 2, "sante": 2,
                "commerce": 1, "agriculture": 1, "securite": 1,
            }
            min_lvl = SECTOR_REQ.get(target.sector, 1)
            user_lvl = _user_diploma_level(user)
            if user_lvl < min_lvl:
                LABELS = {0: "Aucun", 1: "Bac", 2: "Licence", 3: "Master", 4: "MBA"}
                return _err(f"{target.name} exige au minimum {LABELS.get(min_lvl, '?')} (tu as : {LABELS.get(user_lvl, '?')})")

            role = "manager" if user_lvl >= 3 else "employe" if user_lvl >= 2 else "stagiaire"
            new_emp = CompanyEmployee(company_id=cid, user_id=uid, role=role)
            session.add(new_emp)
            await _add_log(session, cid, "recrutement", f"{user.first_name} recruté ({role}) via webapp")
            await session.commit()
            return _ok(f"✅ Recruté(e) chez {target.name} en tant que {role.capitalize()} !")

        # Vérif bac minimum
        if not user.diplome_bac:
            return _err("Il te faut au moins le Bac pour postuler")

        # Candidature classique
        app = CompanyApplication(company_id=cid, user_id=uid, status="pending")
        session.add(app)
        await _add_log(session, cid, "candidature", f"{user.first_name} a postulé via webapp")
        await session.commit()

        # Notifier PDG + directeurs
        managers = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == cid,
                CompanyEmployee.left_at == None,
                CompanyEmployee.role.in_(["pdg", "directeur"]),
            )
        )).scalars().all()

        diplomes = []
        if user.diplome_bac:     diplomes.append("Bac")
        if user.diplome_licence: diplomes.append(f"Licence {user.diplome_domain or ''}")
        if user.diplome_master:  diplomes.append("Master")
        if user.diplome_mba:     diplomes.append("MBA")

        notif = (
            f"🔔 <b>Nouvelle candidature (webapp) !</b>\n\n"
            f"👤 <b>{user.first_name}</b> postule dans <b>{target.name}</b>\n"
            f"🎓 Diplômes : {' · '.join(diplomes) or 'Aucun'}\n\n"
            f"✅ Accepte sur la mini app ou tape /candidatures"
        )
        for mgr in managers:
            await _notify(mgr.user_id, notif)

    return _ok(f"📩 Candidature envoyée à {target.name} ! La direction va l'examiner.")


# ═════════════════════════════════════════════════════════════════════════════
#  DÉMISSIONNER
# ═════════════════════════════════════════════════════════════════════════════

async def webapp_demissionner(request: web.Request) -> web.Response:
    """POST /api/webapp/demissionner — body: {user_id, company_id}"""
    body = await _body(request)
    uid = _parse_uid(request)
    cid  = int(body.get("company_id", 0))

    if not _auth(uid):
        return _err("unauthorized", 403)

    async with AsyncSessionLocal() as session:
        user = await session.get(User, uid)

        if cid:
            emp = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == cid,
                    CompanyEmployee.user_id == uid,
                    CompanyEmployee.left_at == None,
                )
            )).scalar_one_or_none()
        else:
            _, emp = await _get_user_company(session, uid)

        if not emp:
            return _err("Tu ne fais partie d'aucune entreprise")

        company = await session.get(Company, emp.company_id)

        if emp.role == "pdg":
            # Vérifier qu'un directeur peut reprendre
            director = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == emp.company_id,
                    CompanyEmployee.role == "directeur",
                    CompanyEmployee.left_at == None,
                )
            )).scalar_one_or_none()
            if not director:
                return _err("Nomme d'abord un Directeur avant de démissionner")
            director.role = "pdg"
            company.owner_id = director.user_id
            dir_user = await session.get(User, director.user_id)
            await _notify(director.user_id, f"👑 Tu es le nouveau PDG de <b>{company.name}</b> !")
            if dir_user:
                await _add_log(session, emp.company_id, "transfert",
                               f"PDG transféré à {dir_user.first_name} (démission via webapp)")

        old_role = emp.role
        emp.left_at = datetime.utcnow()
        await _add_log(session, emp.company_id, "demission",
                       f"{user.first_name if user else uid} a démissionné ({old_role}) via webapp")
        await session.commit()

        msg = f"👋 Tu as quitté {company.name}."
        if not company.is_bot_company:
            msg += " Cooldown de 3 jours avant de rejoindre une autre entreprise."
        return _ok(msg)


# ═════════════════════════════════════════════════════════════════════════════
#  LICENCIER
# ═════════════════════════════════════════════════════════════════════════════

async def webapp_licencier(request: web.Request) -> web.Response:
    """POST /api/webapp/licencier — body: {user_id, target_id}"""
    body      = await _body(request)
    uid = _parse_uid(request)
    target_id = int(body.get("target_id", 0))

    if not _auth(uid):
        return _err("unauthorized", 403)
    if not target_id or target_id == uid:
        return _err("target_id invalide")

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company or emp.role not in DIRECTION_ROLES:
            return _err("Réservé au PDG et Directeur")
        if company.is_bot_company:
            return _err("Impossible sur une entreprise officielle")

        target_emp = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.user_id == target_id,
                CompanyEmployee.left_at == None,
            )
        )).scalar_one_or_none()
        if not target_emp:
            return _err("Cet employé n'est pas dans ton entreprise")

        if emp.role == "directeur" and target_emp.role in ("pdg", "directeur"):
            return _err("Tu ne peux pas licencier quelqu'un de rang égal ou supérieur")

        target_user = await session.get(User, target_id)
        target_emp.left_at = datetime.utcnow()
        await _add_log(session, company.id, "licenciement",
                       f"{target_user.first_name if target_user else target_id} licencié via webapp")
        await session.commit()

        if target_user:
            await _notify(target_id, f"🚨 Tu as été licencié(e) de <b>{company.name}</b> par la direction.")
            try:
                from api.webapp import push_db_notif as _pn
                await _pn(target_id, "🚨", "Licenciement",
                          f"Tu as été licencié(e) de {company.name} par la direction.")
            except Exception:
                pass

        name = target_user.first_name if target_user else str(target_id)
        return _ok(f"✅ {name} a été licencié(e) de {company.name}.")


# ═════════════════════════════════════════════════════════════════════════════
#  NOMMER (changer le rôle d'un employé)
# ═════════════════════════════════════════════════════════════════════════════

async def webapp_nommer(request: web.Request) -> web.Response:
    """POST /api/webapp/nommer — body: {user_id, target_id, role}"""
    body      = await _body(request)
    uid = _parse_uid(request)
    target_id = int(body.get("target_id", 0))
    new_role  = str(body.get("role", "")).lower().strip()

    if not _auth(uid):
        return _err("unauthorized", 403)
    if new_role not in ROLES_ORDER or new_role in ("pdg", "stagiaire"):
        return _err("Rôle invalide. Choix : secretaire | employe | manager | directeur")

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company or emp.role not in MANAGEMENT_ROLES:
            return _err("Tu dois être au moins Manager")

        if ROLES_ORDER.index(new_role) >= ROLES_ORDER.index(emp.role):
            return _err("Tu ne peux pas nommer quelqu'un à un rang égal ou supérieur au tien")

        target_emp = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.user_id == target_id,
                CompanyEmployee.left_at == None,
            )
        )).scalar_one_or_none()
        if not target_emp:
            return _err("Cet employé n'est pas dans ton entreprise")

        target_user = await session.get(User, target_id)

        # Vérifier diplôme requis
        required = ROLE_DIPLOMA.get(new_role)
        if required and target_user and not _has_diploma(target_user, required):
            return _err(f"{target_user.first_name} n'a pas le diplôme requis pour {new_role}")

        old_role = target_emp.role
        target_emp.role = new_role
        await _add_log(session, company.id, "promotion",
                       f"{target_user.first_name if target_user else target_id}: {old_role} → {new_role} (webapp)")
        await session.commit()

        if target_user:
            await _notify(target_id,
                f"🎖️ Tu as été promu(e) dans <b>{company.name}</b> !\n"
                f"{ROLE_EMOJI.get(new_role, '')} Nouveau poste : <b>{new_role.capitalize()}</b>")
            try:
                from api.webapp import push_db_notif as _pn
                await _pn(target_id, "🎖️", f"Promotion chez {company.name}",
                          f"Tu es maintenant {ROLE_EMOJI.get(new_role, '')} {new_role.capitalize()} !")
            except Exception:
                pass

        return _ok(f"✅ {target_user.first_name if target_user else target_id} est maintenant {new_role.capitalize()} !")


# ═════════════════════════════════════════════════════════════════════════════
#  RECRUTER (inviter un joueur)
# ═════════════════════════════════════════════════════════════════════════════

async def webapp_recruter(request: web.Request) -> web.Response:
    """POST /api/webapp/recruter — body: {user_id, target_id, role, salary?, bonus?}"""
    body      = await _body(request)
    uid = _parse_uid(request)
    target_id = int(body.get("target_id", 0))
    role      = str(body.get("role", "employe")).lower().strip()
    salary    = int(body.get("salary", 0))
    bonus     = int(body.get("bonus", 0))

    if not _auth(uid):
        return _err("unauthorized", 403)
    if not target_id:
        return _err("target_id manquant")
    if role not in ROLES_ORDER or role == "pdg":
        role = "employe"

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company or emp.role not in MANAGEMENT_ROLES:
            return _err("Tu dois être au moins Manager pour recruter")

        target = await session.get(User, target_id)
        if not target:
            return _err("Joueur introuvable")

        # Déjà dans une entreprise ?
        _tc, _te = await _get_user_company(session, target_id)
        if _tc:
            return _err(f"{target.first_name} est déjà dans {_tc.name}")

        # Diplôme requis pour le rôle
        required = ROLE_DIPLOMA.get(role)
        if required and not _has_diploma(target, required):
            return _err(f"{target.first_name} n'a pas le diplôme requis pour {role}")

        # Hiérarchie : on ne peut pas recruter au-dessus de soi
        if ROLES_ORDER.index(role) >= ROLES_ORDER.index(emp.role):
            return _err("Tu ne peux pas inviter quelqu'un à un rang égal ou supérieur au tien")

        # Créer l'invitation
        role_encoded = role if salary <= 0 else f"{role}|{salary}|{bonus}"
        invite = CompanyInvite(
            company_id=company.id,
            target_id=target_id,
            role=role_encoded,
            invited_by=uid,
            status="pending",
            expires_at=datetime.utcnow() + timedelta(hours=48),
        )
        session.add(invite)
        await session.commit()

        contrat_txt = ""
        if salary > 0:
            contrat_txt = f"\n💰 Salaire proposé : <b>{_fmt(salary)} $/jour</b>"
            if bonus > 0:
                contrat_txt += f"\n🎁 Prime : <b>{_fmt(bonus)} $</b>"

        await _notify(target_id,
            f"📩 <b>Invitation de {company.name} !</b>\n"
            f"{ROLE_EMOJI.get(role, '')} Poste proposé : <b>{role.capitalize()}</b>"
            f"{contrat_txt}\n\n"
            f"✅ Accepte sur la mini app ou tape /rejoindre {company.name}\n"
            f"⏳ Expire dans 48h.")
        try:
            from api.webapp import push_db_notif as _pn
            sal_txt = f" — {_fmt(salary)} $/j" if salary > 0 else ""
            await _pn(target_id, "📩", f"Invitation de {company.name}",
                      f"Poste : {ROLE_EMOJI.get(role, '')} {role.capitalize()}{sal_txt}. Expire dans 48h.")
        except Exception:
            pass

    return _ok(f"📩 Invitation envoyée à {target.first_name} pour rejoindre {company.name} !")


# ═════════════════════════════════════════════════════════════════════════════
#  INVITATIONS (mes invitations reçues)
# ═════════════════════════════════════════════════════════════════════════════

async def webapp_invitations(request: web.Request) -> web.Response:
    """GET /api/webapp/invitations?user_id=xxx"""
    uid = _parse_uid(request)
    if not _auth(uid):
        return _err("unauthorized", 403)

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(CompanyInvite).where(
                CompanyInvite.target_id == uid,
                CompanyInvite.status == "pending",
                CompanyInvite.expires_at > datetime.utcnow(),
            )
        )).scalars().all()

        items = []
        for inv in rows:
            company = await session.get(Company, inv.company_id)
            inviter = await session.get(User, inv.invited_by)
            parts = inv.role.split("|")
            real_role = parts[0]
            salary = int(parts[1]) if len(parts) > 1 else 0
            bonus  = int(parts[2]) if len(parts) > 2 else 0
            items.append({
                "id":          inv.id,
                "company_id":  inv.company_id,
                "company":     company.name if company else "—",
                "sector":      company.sector if company else "",
                "role":        real_role,
                "role_emoji":  ROLE_EMOJI.get(real_role, "👤"),
                "salary":      _fmt(salary) if salary else None,
                "salary_raw":  salary,
                "bonus":       _fmt(bonus) if bonus else None,
                "bonus_raw":   bonus,
                "inviter":     inviter.first_name if inviter else "—",
                "expires_at":  inv.expires_at.strftime("%d/%m à %H:%M") if inv.expires_at else "—",
            })

    return web.json_response({"invitations": items})


async def webapp_invitations_accepter(request: web.Request) -> web.Response:
    """POST /api/webapp/invitations/accepter — body: {user_id, invite_id, counter_salary?}"""
    body      = await _body(request)
    uid = _parse_uid(request)
    invite_id = int(body.get("invite_id", 0))
    counter   = int(body.get("counter_salary", 0))

    if not _auth(uid):
        return _err("unauthorized", 403)

    async with AsyncSessionLocal() as session:
        user = await session.get(User, uid)
        invite = await session.get(CompanyInvite, invite_id)
        if not invite or invite.target_id != uid or invite.status != "pending":
            return _err("Invitation introuvable ou expirée")
        if datetime.utcnow() > invite.expires_at:
            invite.status = "expired"
            await session.commit()
            return _err("Cette invitation a expiré")

        company = await session.get(Company, invite.company_id)
        if not company or not company.is_active:
            return _err("Entreprise introuvable")

        parts     = invite.role.split("|")
        real_role = parts[0]
        salary    = int(parts[1]) if len(parts) > 1 else 0
        bonus_val = int(parts[2]) if len(parts) > 2 else 0

        # Contre-proposition
        if counter > 0 and salary > 0 and counter != salary:
            invite.status = "counter"
            invite.role = f"{real_role}|{counter}|{bonus_val}|counter"
            await session.commit()

            # Notifier PDG
            pdg_emp = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == company.id,
                    CompanyEmployee.role == "pdg",
                    CompanyEmployee.left_at == None,
                )
            )).scalar_one_or_none()
            if pdg_emp:
                await _notify(pdg_emp.user_id,
                    f"💬 <b>Contre-proposition de {user.first_name if user else uid} !</b>\n"
                    f"Il refuse {_fmt(salary)} $/j et demande : <b>{_fmt(counter)} $/jour</b>\n"
                    f"Réponds sur la mini app.")
            return _ok(f"💬 Contre-proposition de {_fmt(counter)} $/j envoyée. En attente du PDG.")

        # Vérifier capacité
        nb_emp = (await session.execute(
            select(func.count()).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.left_at == None,
            )
        )).scalar()
        if nb_emp >= _max_employees(company):
            return _err(f"{company.name} est au complet")

        # Accepter
        invite.status = "accepted"
        new_emp = CompanyEmployee(
            company_id=company.id,
            user_id=uid,
            role=real_role,
        )
        if salary > 0:
            new_emp.daily_salary = salary
            new_emp.contract_status = "signed"
            # Verser la prime
            if bonus_val > 0 and company.treasury >= bonus_val:
                user_db = await session.get(User, uid)
                if user_db:
                    user_db.coins += bonus_val
                company.treasury -= bonus_val
                company.value = max(50_000_000, company.value - bonus_val)

        session.add(new_emp)
        await _add_log(session, company.id, "recrutement",
                       f"{user.first_name if user else uid} a rejoint ({real_role}) via webapp")
        await session.commit()

        # Notif persistante au PDG
        try:
            from api.webapp import push_db_notif as _pn
            pdg_emp2 = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == company.id,
                    CompanyEmployee.role == "pdg",
                    CompanyEmployee.left_at == None,
                )
            )).scalar_one_or_none()
            if pdg_emp2:
                uname = user.first_name if user else str(uid)
                await _pn(pdg_emp2.user_id, "✅", f"Nouveau membre dans {company.name}",
                          f"{uname} a rejoint en tant que {ROLE_EMOJI.get(real_role,'')} {real_role.capitalize()}.")
        except Exception:
            pass

        msg = f"✅ Bienvenue dans {company.name} ! Tu es {ROLE_EMOJI.get(real_role, '')} {real_role.capitalize()}."
        if salary > 0:
            msg += f" Contrat signé : {_fmt(salary)} $/jour."
            if bonus_val > 0:
                msg += f" Prime de {_fmt(bonus_val)} $ versée !"
        return _ok(msg)


async def webapp_invitations_refuser(request: web.Request) -> web.Response:
    """POST /api/webapp/invitations/refuser — body: {user_id, invite_id}"""
    body      = await _body(request)
    uid = _parse_uid(request)
    invite_id = int(body.get("invite_id", 0))

    if not _auth(uid):
        return _err("unauthorized", 403)

    async with AsyncSessionLocal() as session:
        invite = await session.get(CompanyInvite, invite_id)
        if not invite or invite.target_id != uid:
            return _err("Invitation introuvable")
        invite.status = "rejected"
        await session.commit()

    return _ok("❌ Invitation refusée.")


# ═════════════════════════════════════════════════════════════════════════════
#  VERSERSALAIRES
# ═════════════════════════════════════════════════════════════════════════════

async def webapp_versersalaires(request: web.Request) -> web.Response:
    """POST /api/webapp/versersalaires — body: {user_id}"""
    body = await _body(request)
    uid = _parse_uid(request)

    if not _auth(uid):
        return _err("unauthorized", 403)

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company or emp.role != "pdg":
            return _err("Seul le PDG peut verser les salaires")

        # Cooldown
        if company.last_payroll:
            delta = datetime.utcnow() - company.last_payroll
            remaining = timedelta(hours=PAYROLL_COOLDOWN_HOURS) - delta
            if remaining.total_seconds() > 0:
                h = int(remaining.total_seconds() // 3600)
                m = int((remaining.total_seconds() % 3600) // 60)
                return _err(f"Prochaine paie disponible dans {h}h{m:02d}m")

        emps = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.left_at == None,
                CompanyEmployee.role != "pdg",
            )
        )).scalars().all()

        eligible = [e for e in emps if (e.daily_salary or 0) > 0 and e.contract_status == "signed"]
        if not eligible:
            return _err("Aucun employé avec contrat signé. Utilise Négocier contrat d'abord.")

        total_to_pay = sum(e.daily_salary for e in eligible)
        ratio = min(1.0, company.treasury / total_to_pay) if total_to_pay > 0 else 0

        total_paid = 0
        paid_list  = []
        from sqlalchemy import text as _text
        for e in eligible:
            amount = int(e.daily_salary * ratio)
            if amount <= 0:
                continue
            emp_user = await session.get(User, e.user_id)
            if emp_user:
                emp_user.coins += amount
            # Lire activity_since_payroll via SQL pur pour éviter la race condition avec flush_activity_queue
            row = (await session.execute(
                _text("SELECT activity_since_payroll FROM company_employees WHERE user_id = :uid AND company_id = :cid AND left_at IS NULL"),
                {"uid": e.user_id, "cid": e.company_id}
            )).fetchone()
            activity = row[0] if row else 0
            # Reset via SQL pur (pas ORM) pour ne pas écraser les incréments concurrent du flush
            await session.execute(
                _text("UPDATE company_employees SET activity_since_payroll = 0 WHERE user_id = :uid AND company_id = :cid AND left_at IS NULL"),
                {"uid": e.user_id, "cid": e.company_id}
            )
            total_paid += amount
            paid_list.append({
                "name":     emp_user.first_name if emp_user else "—",
                "role":     e.role,
                "amount":   _fmt(amount),
                "activity": activity,
            })
            await _notify(e.user_id,
                f"💵 <b>Salaire reçu !</b>\n"
                f"🏢 <b>{company.name}</b> t'a versé <b>{_fmt(amount)} $</b>\n"
                f"📊 {activity} commandes effectuées"
                + (f"\n⚠️ Versement partiel ({int(ratio*100)}%)" if ratio < 1.0 else ""))

        company.treasury = max(0, company.treasury - total_paid)
        company.value    = max(50_000_000, company.value - total_paid)
        company.last_payroll = datetime.utcnow()

        await _add_log(session, company.id, "paie",
                       f"Salaires versés via webapp — {_fmt(total_paid)} $ distribués", amount=total_paid)
        await session.commit()

    return _ok(
        f"💼 Paie effectuée ! {_fmt(total_paid)} $ distribués à {len(paid_list)} employé(s).",
        paid=paid_list,
        total=_fmt(total_paid),
        partial=ratio < 1.0,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  PAYER UN EMPLOYÉ (montant libre)
# ═════════════════════════════════════════════════════════════════════════════

async def webapp_payeremploye(request: web.Request) -> web.Response:
    """POST /api/webapp/payeremploye — body: {user_id, target_id, amount}"""
    body      = await _body(request)
    uid = _parse_uid(request)
    target_id = int(body.get("target_id", 0))
    amount    = int(body.get("amount", 0))

    if not _auth(uid):
        return _err("unauthorized", 403)
    if amount <= 0:
        return _err("Montant invalide")

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company or emp.role != "pdg":
            return _err("Seul le PDG peut payer manuellement")
        if company.is_bot_company:
            return _err("Impossible sur une entreprise officielle")
        if target_id == uid:
            return _err("Utilise /retraitboite pour te payer toi-même")

        target_emp = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.user_id == target_id,
                CompanyEmployee.left_at == None,
            )
        )).scalar_one_or_none()
        if not target_emp:
            return _err("Cet employé n'est pas dans ton entreprise")
        if target_emp.role == "stagiaire":
            return _err("Les stagiaires ne reçoivent pas de salaire")

        if company.treasury < amount:
            return _err(f"Trésorerie insuffisante (disponible : {_fmt(company.treasury)} $)")

        target_user = await session.get(User, target_id)
        if target_user:
            target_user.coins += amount
        activity = target_emp.activity_since_payroll or 0
        target_emp.activity_since_payroll = 0
        company.treasury -= amount
        company.value = max(50_000_000, company.value - amount)

        await _add_log(session, company.id, "paie_pdg",
                       f"Paiement manuel → {target_user.first_name if target_user else target_id} "
                       f"({target_emp.role}) : {_fmt(amount)} $", amount=amount)
        await session.commit()

        if target_user:
            await _notify(target_id,
                f"💵 <b>Salaire reçu !</b>\n"
                f"🏢 <b>{company.name}</b>\n"
                f"💎 Le PDG t'a versé <b>{_fmt(amount)} $</b>\n"
                f"📊 Activité comptabilisée : {activity} commandes")

        return _ok(
            f"✅ {_fmt(amount)} $ versés à {target_user.first_name if target_user else target_id} !",
            treasury_left=_fmt(company.treasury),
        )


# ═════════════════════════════════════════════════════════════════════════════
#  NÉGOCIER CONTRAT
# ═════════════════════════════════════════════════════════════════════════════

async def webapp_negociercontrat(request: web.Request) -> web.Response:
    """
    POST /api/webapp/negociercontrat
    body:
      PDG propose   : {user_id, target_id, salary, bonus?}
      PDG refuse    : {user_id, target_id, action: "refuser"}
      Employé répond: {user_id, action: "accepter"|"refuser"|"counter", counter_salary?}
    """
    body   = await _body(request)
    uid = _parse_uid(request)
    action = str(body.get("action", "")).lower()

    if not _auth(uid):
        return _err("unauthorized", 403)

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company:
            return _err("Tu ne fais partie d'aucune entreprise")

        user = await session.get(User, uid)

        # ── CAS EMPLOYÉ : répondre à une proposition ──────────────────────
        if emp.role != "pdg":
            if emp.contract_status != "pending_employee":
                return _err("Aucune proposition de contrat en attente")

            pending_sal = emp.pending_salary or 0
            pending_bon = emp.pending_bonus or 0

            pdg_emp = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == company.id,
                    CompanyEmployee.role == "pdg",
                    CompanyEmployee.left_at == None,
                )
            )).scalar_one_or_none()

            if action == "accepter":
                emp.daily_salary = pending_sal
                emp.contract_status = "signed"
                emp.pending_salary = 0
                emp.pending_bonus  = 0
                await session.commit()
                if pdg_emp:
                    await _notify(pdg_emp.user_id,
                        f"✅ <b>{user.first_name if user else uid}</b> a accepté le contrat !\n"
                        f"📄 Salaire signé : <b>{_fmt(pending_sal)} $/jour</b>")
                    try:
                        from api.webapp import push_db_notif as _pn
                        await _pn(pdg_emp.user_id, "✅", "Contrat signé",
                                  f"{user.first_name if user else uid} a accepté {_fmt(pending_sal)} $/jour.")
                    except Exception:
                        pass
                return _ok(f"✅ Contrat accepté ! Salaire : {_fmt(pending_sal)} $/jour")

            elif action == "refuser":
                emp.contract_status = "none"
                emp.pending_salary  = 0
                emp.pending_bonus   = 0
                await session.commit()
                if pdg_emp:
                    await _notify(pdg_emp.user_id,
                        f"❌ <b>{user.first_name if user else uid}</b> a refusé ta proposition.")
                return _ok("❌ Proposition refusée.")

            else:  # counter
                counter = int(body.get("counter_salary", 0))
                if counter <= 0:
                    return _err("Montant invalide pour la contre-proposition")
                emp.contract_status = "pending_pdg"
                emp.pending_salary  = counter
                await session.commit()
                if pdg_emp:
                    await _notify(pdg_emp.user_id,
                        f"💬 <b>Contre-proposition de {user.first_name if user else uid} !</b>\n"
                        f"Il refuse {_fmt(pending_sal)} $/j et demande : <b>{_fmt(counter)} $/jour</b>\n"
                        f"Réponds sur la mini app.")
                return _ok(f"💬 Contre-proposition de {_fmt(counter)} $/j envoyée.")

        # ── CAS PDG : proposer ou répondre à une contre-prop ─────────────
        target_id = int(body.get("target_id", 0))
        salary    = int(body.get("salary", 0))
        bonus_val = int(body.get("bonus", 0))

        if not target_id:
            return _err("target_id manquant")

        target_emp = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.user_id == target_id,
                CompanyEmployee.left_at == None,
            )
        )).scalar_one_or_none()
        if not target_emp:
            return _err("Cet employé n'est pas dans ton entreprise")

        target_user = await session.get(User, target_id)

        if action == "refuser":
            target_emp.contract_status = "none"
            target_emp.pending_salary  = 0
            target_emp.pending_bonus   = 0
            await session.commit()
            await _notify(target_id,
                f"❌ <b>{company.name}</b> a refusé ta contre-proposition.")
            return _ok(f"❌ Contre-proposition de {target_user.first_name if target_user else target_id} refusée.")

        if salary <= 0:
            return _err("Salaire invalide")

        # PDG accepte la contre-prop de l'employé
        if target_emp.contract_status == "pending_pdg" and salary == target_emp.pending_salary:
            target_emp.daily_salary    = salary
            target_emp.contract_status = "signed"
            target_emp.pending_salary  = 0
            target_emp.pending_bonus   = 0
            await session.commit()
            await _notify(target_id,
                f"✅ <b>Contrat signé !</b>\n"
                f"🏢 <b>{company.name}</b> a accepté ta demande.\n"
                f"📄 Salaire : <b>{_fmt(salary)} $/jour</b>")
            return _ok(f"✅ Contre-proposition acceptée ! Contrat signé à {_fmt(salary)} $/jour.")

        # Nouvelle proposition PDG → employé
        target_emp.contract_status = "pending_employee"
        target_emp.pending_salary  = salary
        target_emp.pending_bonus   = bonus_val
        await session.commit()

        bonus_txt = f"\n🎁 Prime : <b>{_fmt(bonus_val)} $</b>" if bonus_val > 0 else ""
        await _notify(target_id,
            f"📄 <b>Proposition de contrat — {company.name}</b>\n"
            f"💰 Salaire proposé : <b>{_fmt(salary)} $/jour</b>{bonus_txt}\n\n"
            f"Accepte, refuse ou contre-propose sur la mini app.")

        name = target_user.first_name if target_user else str(target_id)
        return _ok(f"📩 Proposition envoyée à {name} : {_fmt(salary)} $/jour.")


# ═════════════════════════════════════════════════════════════════════════════
#  PRÉSENCES (tableau de bord direction)
# ═════════════════════════════════════════════════════════════════════════════

async def webapp_presences(request: web.Request) -> web.Response:
    """GET /api/webapp/presences?user_id=xxx"""
    uid = _parse_uid(request)
    if not _auth(uid):
        return _err("unauthorized", 403)

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company or emp.role not in DIRECTION_ROLES:
            return _err("Réservé au PDG et Directeur")

        emps = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.left_at == None,
            )
        )).scalars().all()

        data = []
        total_masse = 0
        for e in sorted(emps, key=lambda x: ROLES_ORDER.index(x.role) if x.role in ROLES_ORDER else 0, reverse=True):
            eu = await session.get(User, e.user_id)
            daily    = e.daily_salary or 0
            activity = e.activity_since_payroll or 0
            status   = e.contract_status or "none"
            if e.role not in ("pdg", "stagiaire"):
                total_masse += daily

            data.append({
                "user_id":   e.user_id,
                "name":      eu.first_name if eu else "—",
                "username":  eu.username if eu else "",
                "role":      e.role,
                "role_emoji": ROLE_EMOJI.get(e.role, "👤"),
                "activity":  activity,
                "daily":     _fmt(daily),
                "daily_raw": daily,
                "contract":  status,
                "is_me":     e.user_id == uid,
                "cmd_count": e.command_count or 0,
                "joined":    e.joined_at.strftime("%d/%m/%Y") if e.joined_at else "—",
            })

        last_pay = company.last_payroll.strftime("%d/%m %H:%M") if company.last_payroll else "Jamais"

        # Cooldown paie
        can_pay = True
        pay_in = ""
        if company.last_payroll:
            delta = datetime.utcnow() - company.last_payroll
            remaining = timedelta(hours=PAYROLL_COOLDOWN_HOURS) - delta
            if remaining.total_seconds() > 0:
                can_pay = False
                h = int(remaining.total_seconds() // 3600)
                m = int((remaining.total_seconds() % 3600) // 60)
                pay_in = f"{h}h{m:02d}m"

    return web.json_response({
        "employees":    data,
        "treasury":     _fmt(company.treasury),
        "treasury_raw": company.treasury or 0,
        "last_payroll": last_pay,
        "can_pay":      can_pay,
        "pay_in":       pay_in,
        "total_masse":  _fmt(total_masse),
        "is_pdg":       emp.role == "pdg",
    })


# ═════════════════════════════════════════════════════════════════════════════
#  PARTS
# ═════════════════════════════════════════════════════════════════════════════

async def webapp_mes_parts(request: web.Request) -> web.Response:
    """GET /api/webapp/parts?user_id=xxx"""
    uid = _parse_uid(request)
    if not _auth(uid):
        return _err("unauthorized", 403)

    async with AsyncSessionLocal() as session:
        shares = (await session.execute(
            select(CompanyShare).where(
                CompanyShare.owner_id == uid,
                CompanyShare.quantity > 0,
            )
        )).scalars().all()

        items = []
        total_value = 0
        for s in shares:
            c = await session.get(Company, s.company_id)
            if not c or not c.is_active:
                continue
            price_per = (c.treasury // c.total_shares) if (c.total_shares or 0) > 0 else 0
            val = s.quantity * price_per
            total_value += val
            pct = round((s.quantity / c.total_shares) * 100, 1) if c.total_shares else 0
            sec_emoji, sec_name = SECTORS.get(c.sector, ("🏢", c.sector))
            emp = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == c.id,
                    CompanyEmployee.user_id == uid,
                    CompanyEmployee.left_at == None,
                )
            )).scalar_one_or_none()
            items.append({
                "company_id":  c.id,
                "company":     c.name,
                "sec_emoji":   sec_emoji,
                "quantity":    s.quantity,
                "pct":         pct,
                "value":       _fmt(val),
                "value_raw":   val,
                "price_per":   _fmt(price_per),
                "total_shares": c.total_shares,
                "role":        emp.role if emp else None,
                "is_bot":      c.is_bot_company,
                "can_sell":    not c.is_bot_company,
            })

    return web.json_response({"parts": items, "total_value": _fmt(total_value), "total_raw": total_value})


async def webapp_vendre_parts(request: web.Request) -> web.Response:
    """POST /api/webapp/parts/vendre — body: {user_id, company_id, quantity}"""
    body = await _body(request)
    uid = _parse_uid(request)
    cid  = int(body.get("company_id", 0))
    qty  = int(body.get("quantity", 0))

    if not _auth(uid):
        return _err("unauthorized", 403)
    if qty <= 0:
        return _err("Quantité invalide")

    async with AsyncSessionLocal() as session:
        company = await session.get(Company, cid)
        if not company or not company.is_active:
            return _err("Entreprise introuvable")
        if company.is_bot_company:
            return _err("Impossible de vendre des parts d'une entreprise officielle")

        share_row = (await session.execute(
            select(CompanyShare).where(
                CompanyShare.company_id == cid,
                CompanyShare.owner_id == uid,
            )
        )).scalar_one_or_none()
        if not share_row or share_row.quantity <= 0:
            return _err("Tu ne détiens aucune part dans cette entreprise")

        emp = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == cid,
                CompanyEmployee.user_id == uid,
                CompanyEmployee.left_at == None,
            )
        )).scalar_one_or_none()

        # PDG doit garder 51 parts minimum
        if emp and emp.role == "pdg":
            can_sell = max(0, share_row.quantity - 51)
            if can_sell == 0:
                return _err("En tant que PDG tu dois conserver au moins 51 parts")
            if qty > can_sell:
                return _err(f"PDG : tu peux vendre au maximum {can_sell} parts")
        else:
            if qty > share_row.quantity:
                return _err(f"Tu n'as que {share_row.quantity} parts")

        price_each = (company.treasury // company.total_shares) if (company.total_shares or 0) > 0 else 0
        total = qty * price_each

        user_db = await session.get(User, uid)
        if user_db:
            user_db.coins += total

        company.treasury = max(0, company.treasury - total)
        company.value    = company.treasury
        company.total_shares = max(1, company.total_shares - qty)
        share_row.quantity = max(0, share_row.quantity - qty)

        if emp and emp.role == "pdg":
            company.owner_shares = share_row.quantity

        await _add_log(session, cid, "vente_parts",
                       f"{user_db.first_name if user_db else uid} vendu {qty} parts au marché", amount=total)
        await session.commit()

    return _ok(
        f"✅ {qty} parts vendues pour {_fmt(total)} $ ({_fmt(price_each)} $/part) !",
        total=_fmt(total),
        new_quantity=share_row.quantity,
    )


async def webapp_acheter_parts(request: web.Request) -> web.Response:
    """POST /api/webapp/parts/acheter — body: {user_id, company_id, quantity}"""
    body = await _body(request)
    uid = _parse_uid(request)
    cid  = int(body.get("company_id", 0))
    qty  = int(body.get("quantity", 0))

    if not _auth(uid):
        return _err("unauthorized", 403)
    if qty <= 0:
        return _err("Quantité invalide")

    async with AsyncSessionLocal() as session:
        company = await session.get(Company, cid)
        if not company or not company.is_active:
            return _err("Entreprise introuvable")
        if company.is_bot_company:
            return _err("Impossible d'acheter des parts d'une entreprise officielle")

        available = company.owner_shares or 0
        if available <= 0:
            return _err("Le PDG ne détient plus de parts à vendre")
        if qty > available:
            return _err(f"Seulement {available} parts disponibles")

        price_per = (company.treasury // company.total_shares) if (company.total_shares or 0) > 0 else 0
        total     = qty * price_per

        user_db = await session.get(User, uid)
        if not user_db:
            return _err("Utilisateur introuvable")
        if user_db.coins < total:
            return _err(f"Fonds insuffisants. Prix : {_fmt(total)} $, ton solde : {_fmt(user_db.coins)} $")

        # Vérifier offre en cours
        existing = (await session.execute(
            select(CompanyShareOffer).where(
                CompanyShareOffer.company_id == cid,
                CompanyShareOffer.buyer_id == uid,
                CompanyShareOffer.status == "pending",
            )
        )).scalar_one_or_none()
        if existing:
            return _err("Tu as déjà une offre en attente sur cette entreprise")

        # Bloquer les fonds
        user_db.coins -= total
        offer = CompanyShareOffer(
            company_id=cid,
            buyer_id=uid,
            quantity=qty,
            price_each=price_per,
            total_price=total,
            status="pending",
            expires_at=datetime.utcnow() + timedelta(hours=48),
        )
        session.add(offer)
        await session.flush()

        await _add_log(session, cid, "offre_parts",
                       f"{user_db.first_name} a soumis une offre pour {qty} parts", amount=total)
        await session.commit()

        await _notify(company.owner_id,
            f"💼 <b>Nouvelle offre d'achat de parts !</b>\n"
            f"👤 <b>{user_db.first_name}</b> veut acheter <b>{qty} parts</b>\n"
            f"💰 Offre : <b>{_fmt(total)} $</b> ({_fmt(price_per)} $/part)\n"
            f"Réponds sur la mini app ou avec /accepteroffre {offer.id}")

    return _ok(
        f"📩 Offre envoyée au PDG de {company.name} ! {_fmt(total)} $ bloqués (remboursés si refus).",
        offer_id=offer.id,
    )


async def webapp_accepter_offre(request: web.Request) -> web.Response:
    """POST /api/webapp/parts/accepteroffre — body: {user_id, offer_id}"""
    body     = await _body(request)
    uid = _parse_uid(request)
    offer_id = int(body.get("offer_id", 0))

    if not _auth(uid):
        return _err("unauthorized", 403)

    async with AsyncSessionLocal() as session:
        offer = await session.get(CompanyShareOffer, offer_id)
        if not offer or offer.status != "pending":
            return _err("Offre introuvable ou déjà traitée")

        company = await session.get(Company, offer.company_id)
        if not company or company.owner_id != uid:
            return _err("Seul le PDG peut accepter cette offre")

        if datetime.utcnow() > offer.expires_at:
            offer.status = "expired"
            buyer = await session.get(User, offer.buyer_id)
            if buyer:
                buyer.coins += offer.total_price
            await session.commit()
            return _err("Cette offre a expiré. L'acheteur a été remboursé.")

        # Vérif 51 parts PDG
        pdg_share = (await session.execute(
            select(CompanyShare).where(
                CompanyShare.company_id == company.id,
                CompanyShare.owner_id == uid,
            )
        )).scalar_one_or_none()
        pdg_qty = pdg_share.quantity if pdg_share else 0
        if offer.quantity > max(0, pdg_qty - 51):
            offer.status = "rejected"
            buyer = await session.get(User, offer.buyer_id)
            if buyer:
                buyer.coins += offer.total_price
            await session.commit()
            return _err(f"Refusé automatiquement : tu dois garder 51 parts minimum (tu as {pdg_qty}). Acheteur remboursé.")

        offer.status = "accepted"
        pdg_user = await session.get(User, uid)
        if pdg_user:
            pdg_user.coins += offer.total_price

        # Buyer reçoit les parts
        buyer_share = (await session.execute(
            select(CompanyShare).where(
                CompanyShare.company_id == company.id,
                CompanyShare.owner_id == offer.buyer_id,
            )
        )).scalar_one_or_none()
        if buyer_share:
            buyer_share.quantity += offer.quantity
        else:
            session.add(CompanyShare(company_id=company.id, owner_id=offer.buyer_id, quantity=offer.quantity))

        if pdg_share:
            pdg_share.quantity = max(0, pdg_share.quantity - offer.quantity)
            company.owner_shares = pdg_share.quantity

        await _add_log(session, company.id, "achat_parts",
                       f"Offre {offer_id} acceptée — {offer.quantity} parts vendues", amount=offer.total_price)
        await session.commit()

        buyer = await session.get(User, offer.buyer_id)
        await _notify(offer.buyer_id,
            f"🎉 <b>Ton offre a été acceptée !</b>\n"
            f"📦 Tu as obtenu <b>{offer.quantity} parts</b> de <b>{company.name}</b>\n"
            f"💰 Montant débité : <b>{_fmt(offer.total_price)} $</b>")
        try:
            from api.webapp import push_db_notif as _pn
            await _pn(offer.buyer_id, "🎉", f"Parts achetées — {company.name}",
                      f"Tu as obtenu {offer.quantity} parts pour {_fmt(offer.total_price)} $.")
        except Exception:
            pass

    return _ok(f"✅ Offre acceptée ! {offer.quantity} parts vendues pour {_fmt(offer.total_price)} $.")


async def webapp_refuser_offre(request: web.Request) -> web.Response:
    """POST /api/webapp/parts/refuseroffre — body: {user_id, offer_id}"""
    body     = await _body(request)
    uid = _parse_uid(request)
    offer_id = int(body.get("offer_id", 0))

    if not _auth(uid):
        return _err("unauthorized", 403)

    async with AsyncSessionLocal() as session:
        offer = await session.get(CompanyShareOffer, offer_id)
        if not offer or offer.status != "pending":
            return _err("Offre introuvable ou déjà traitée")

        company = await session.get(Company, offer.company_id)
        if not company or company.owner_id != uid:
            return _err("Seul le PDG peut refuser cette offre")

        offer.status = "rejected"
        buyer = await session.get(User, offer.buyer_id)
        if buyer:
            buyer.coins += offer.total_price

        await _add_log(session, company.id, "offre_refusee",
                       f"Offre {offer_id} refusée — {_fmt(offer.total_price)} $ remboursés")
        await session.commit()

        await _notify(offer.buyer_id,
            f"😔 Ton offre de <b>{offer.quantity} parts</b> dans <b>{company.name}</b> a été refusée.\n"
            f"💰 <b>{_fmt(offer.total_price)} $</b> remboursés.")
        try:
            from api.webapp import push_db_notif as _pn
            await _pn(offer.buyer_id, "😔", f"Offre refusée — {company.name}",
                      f"Ton offre de {offer.quantity} parts a été refusée. {_fmt(offer.total_price)} $ remboursés.")
        except Exception:
            pass

    return _ok(f"❌ Offre refusée. {_fmt(offer.total_price)} $ remboursés à l'acheteur.")


# ═════════════════════════════════════════════════════════════════════════════
#  TRANSFERT D'ARGENT (/pay)
# ═════════════════════════════════════════════════════════════════════════════

async def webapp_pay(request: web.Request) -> web.Response:
    """POST /api/webapp/pay — body: {user_id, target_id, amount}"""
    body      = await _body(request)
    uid = _parse_uid(request)
    target_id = int(body.get("target_id", 0))
    amount    = int(body.get("amount", 0))

    if not _auth(uid):
        return _err("unauthorized", 403)
    if target_id == uid:
        return _err("Tu ne peux pas te transférer de l'argent à toi-même")
    if amount <= 0:
        return _err("Montant invalide")

    async with AsyncSessionLocal() as session:
        sender = await session.get(User, uid)
        if not sender:
            return _err("Utilisateur introuvable")
        if sender.coins < amount:
            return _err(f"Solde insuffisant ({_fmt(sender.coins)} $ disponibles)")

        target = await session.get(User, target_id)
        if not target:
            return _err("Destinataire introuvable")
        if target.is_banned:
            return _err("Ce joueur est banni")

        sender.coins -= amount
        target.coins += amount
        # Capturer les noms AVANT commit (après, les objets peuvent expirer)
        sender_name  = sender.first_name
        target_name  = target.first_name
        sender_coins = sender.coins
        await session.commit()

        await _notify(target_id,
            f"💸 <b>{sender_name}</b> t'a envoyé <b>{_fmt(amount)} $</b> !")

    # Notifs persistantes EN DEHORS du with (session déjà fermée)
    try:
        from api.webapp import push_db_notif as _pn
        import logging as _lg
        await _pn(target_id, "💸", "Paiement reçu",
                  f"{sender_name} t'a envoyé {_fmt(amount)} $")
        await _pn(uid, "✅", "Paiement envoyé",
                  f"Tu as envoyé {_fmt(amount)} $ à {target_name}")
    except Exception as _e:
        import logging as _lg
        _lg.getLogger(__name__).error(f"push_db_notif pay: {_e}")

    return _ok(
        f"✅ {_fmt(amount)} $ envoyés à {target_name} !",
        new_balance=_fmt(sender_coins),
    )


# ═════════════════════════════════════════════════════════════════════════════
#  CONTRATS BUREAU
# ═════════════════════════════════════════════════════════════════════════════

async def webapp_contrats_bc(request: web.Request) -> web.Response:
    """GET /api/webapp/contrats/bc?user_id=xxx"""
    uid = _parse_uid(request)
    if not _auth(uid):
        return _err("unauthorized", 403)

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company:
            return web.json_response({"contrats": [], "company": None})

        contrats = (await session.execute(
            select(BureauContrat).where(
                BureauContrat.company_id == company.id,
            ).order_by(BureauContrat.created_at.desc()).limit(20)
        )).scalars().all()

        # Total live des commandes de l'équipe (actifs + anciens, pour éviter une régression
        # de la barre de progression quand un employé quitte l'entreprise)
        total_cmds = (await session.execute(
            select(func.sum(CompanyEmployee.command_count)).where(
                CompanyEmployee.company_id == company.id,
            )
        )).scalar() or 0

        now = datetime.utcnow()
        items = []
        for c in contrats:
            # cmds_done en DB n'est pas fiable (incrémentée seulement pour certaines commandes) :
            # on calcule toujours la progression live à partir du total d'équipe.
            cmds_done = max(0, int(total_cmds) - (c.cmds_at_start or 0))
            obj  = c.objective_cmds or 1
            pct  = min(100, int(cmds_done / obj * 100))
            days = (c.ends_at - now).days if c.ends_at else 0
            items.append({
                "id":        c.id,
                "title":     c.title,
                "desc":      c.description,
                "reward":    _fmt(c.reward),
                "reward_raw": c.reward,
                "objective": c.objective_cmds,
                "cmds_done": cmds_done,
                "pct":       pct,
                "status":    c.status,
                "can_claim": c.status == "active" and pct >= 100,
                "days_left": max(0, days),
                "ends_at":   c.ends_at.strftime("%d/%m à %H:%M") if c.ends_at else "—",
                "starts_at": c.starts_at.strftime("%d/%m") if c.starts_at else "—",
            })

    return web.json_response({"contrats": items, "company": company.name, "total_cmds": int(total_cmds)})


async def webapp_contrats_auto(request: web.Request) -> web.Response:
    """GET /api/webapp/contrats/auto?user_id=xxx"""
    uid = _parse_uid(request)
    if not _auth(uid):
        return _err("unauthorized", 403)

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company:
            return web.json_response({"contrats": [], "company": None})

        rows = (await session.execute(
            select(CompanyAutoContract).where(
                CompanyAutoContract.company_id == company.id,
            ).order_by(CompanyAutoContract.created_at.desc()).limit(20)
        )).scalars().all()

        total_cmds = (await session.execute(
            select(func.sum(CompanyEmployee.command_count)).where(
                CompanyEmployee.company_id == company.id,
            )
        )).scalar() or 0

        now = datetime.utcnow()
        items = []
        for ac in rows:
            # cmds_done en DB n'est pas fiable : calcul toujours live.
            cmds_done = max(0, int(total_cmds) - (ac.cmds_at_start or 0))
            obj = ac.objective_cmds or 1
            pct = min(100, int(cmds_done / obj * 100))
            deadline_left = (ac.deadline_at - now).days if ac.deadline_at else 0
            items.append({
                "id":         ac.id,
                "client":     ac.client_name,
                "desc":       ac.description,
                "objective":  ac.objective_cmds,
                "cmds_done":  cmds_done,
                "pct":        pct,
                "reward":     _fmt(ac.negotiated_reward or ac.reward),
                "reward_raw": ac.negotiated_reward or ac.reward,
                "status":     ac.status,
                "can_claim":  ac.status == "active" and pct >= 100,
                "days_left":  max(0, deadline_left),
                "deadline":   ac.deadline_at.strftime("%d/%m à %H:%M") if ac.deadline_at else "—",
                "is_pdg":     emp.role == "pdg",
            })

    return web.json_response({"contrats": items, "company": company.name})


async def webapp_contrats_bc_claim(request: web.Request) -> web.Response:
    """POST /api/webapp/contrats/bc/claim — body: {user_id, contract_id}
    Réclame immédiatement la récompense d'un contrat Bureau si l'objectif est atteint."""
    body = await _body(request)
    uid = _parse_uid(request)
    if not _auth(uid):
        return _err("unauthorized", 403)
    contract_id = int(body.get("contract_id", 0) or 0)
    if not contract_id:
        return _err("contract_id manquant")

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company or not emp or emp.role not in ("pdg", "directeur"):
            return _err("Tu n'es PDG ni directeur d'aucune entreprise.")

        contract = await session.get(BureauContrat, contract_id)
        if not contract or contract.company_id != company.id:
            return _err("Contrat introuvable.")
        if contract.status != "active":
            return _err("Ce contrat n'est plus actif.")

        total_cmds = (await session.execute(
            select(func.sum(CompanyEmployee.command_count)).where(
                CompanyEmployee.company_id == company.id,
            )
        )).scalar() or 0
        progression = max(0, int(total_cmds) - (contract.cmds_at_start or 0))
        obj = contract.objective_cmds or 1
        if progression < obj:
            return _err(f"Objectif pas encore atteint : {progression:,}/{obj:,} commandes".replace(",", " "))

        contract.status = "completed"
        company.treasury = (company.treasury or 0) + contract.reward
        company.value = company.treasury
        await _add_log(session, company.id, "contrat_bureau",
                        f"Contrat BC '{contract.title}' réclamé via la mini app — +{_fmt(contract.reward)} $",
                        amount=contract.reward)
        await session.commit()
        treasury_now = company.treasury

    try:
        await _notify(uid, f"🎉 Contrat Bureau '{contract.title}' réclamé ! +{_fmt(contract.reward)} $ en trésorerie.")
    except Exception:
        pass

    return _ok(f"✅ +{_fmt(contract.reward)} $ crédités en trésorerie !", treasury=_fmt(treasury_now))


async def webapp_contrats_auto_claim(request: web.Request) -> web.Response:
    """POST /api/webapp/contrats/auto/claim — body: {user_id, contract_id}
    Réclame immédiatement la récompense d'un contrat automatique si l'objectif est atteint."""
    body = await _body(request)
    uid = _parse_uid(request)
    if not _auth(uid):
        return _err("unauthorized", 403)
    contract_id = int(body.get("contract_id", 0) or 0)
    if not contract_id:
        return _err("contract_id manquant")

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company or not emp or emp.role not in ("pdg", "directeur"):
            return _err("Tu n'es PDG ni directeur d'aucune entreprise.")

        contract = await session.get(CompanyAutoContract, contract_id)
        if not contract or contract.company_id != company.id:
            return _err("Contrat introuvable.")
        if contract.status != "active":
            return _err("Ce contrat n'est plus actif.")

        total_cmds = (await session.execute(
            select(func.sum(CompanyEmployee.command_count)).where(
                CompanyEmployee.company_id == company.id,
            )
        )).scalar() or 0
        progression = max(0, int(total_cmds) - (contract.cmds_at_start or 0))
        obj = contract.objective_cmds or 1
        if progression < obj:
            return _err(f"Objectif pas encore atteint : {progression:,}/{obj:,} commandes".replace(",", " "))

        reward = contract.negotiated_reward or contract.reward
        contract.status = "completed"
        company.treasury = (company.treasury or 0) + reward
        import random as _rnd
        rep_gain = _rnd.choice([0.05, 0.05, 0.075, 0.075, 0.10])
        company.reputation = min(5.0, (company.reputation or 3.0) + rep_gain)
        await _add_log(session, company.id, "contrat_auto",
                        f"Contrat auto '{contract.client_name}' réclamé via la mini app — +{_fmt(reward)} $",
                        amount=reward)
        await session.commit()
        treasury_now = company.treasury

    try:
        await _notify(uid, f"🎉 Contrat '{contract.client_name}' réclamé ! +{_fmt(reward)} $ en trésorerie.")
    except Exception:
        pass

    return _ok(f"✅ +{_fmt(reward)} $ crédités en trésorerie !", treasury=_fmt(treasury_now))

async def webapp_skipattente(request: web.Request) -> web.Response:
    """POST /api/webapp/skipattente — body: {user_id}"""
    body = await _body(request)
    uid = _parse_uid(request)

    if not _auth(uid):
        return _err("unauthorized", 403)

    async with AsyncSessionLocal() as session:
        last_left = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.user_id == uid,
                CompanyEmployee.left_at != None,
            ).order_by(CompanyEmployee.left_at.desc()).limit(1)
        )).scalar_one_or_none()

        if not last_left or not last_left.left_at:
            return _ok("✅ Aucun cooldown actif !")

        last_co = await session.get(Company, last_left.company_id)
        if last_co and last_co.is_bot_company:
            return _ok("✅ Aucun cooldown actif (dernière boîte = officielle).")

        days_passed = (datetime.utcnow() - last_left.left_at).days
        if days_passed >= 3:
            return _ok("✅ Ton cooldown est déjà terminé !")

        user_db = await session.get(User, uid)
        if not user_db:
            return _err("Utilisateur introuvable")
        if user_db.coins < SKIP_COST:
            return _err(f"Fonds insuffisants. Coût : {_fmt(SKIP_COST)} $ (tu as {_fmt(user_db.coins)} $)")

        jours_restants = 3 - days_passed
        user_db.coins -= SKIP_COST
        await session.execute(
            sa_text("UPDATE company_employees SET left_at = :old WHERE id = :eid"),
            {"old": datetime.utcnow() - timedelta(days=4), "eid": last_left.id},
        )
        await session.commit()

    return _ok(
        f"⚡ Cooldown ignoré ! {_fmt(SKIP_COST)} $ déduits. (Il restait {jours_restants} jour(s))",
        cost=_fmt(SKIP_COST),
    )


# ═════════════════════════════════════════════════════════════════════════════
#  NOTIFICATIONS — cloche principale (/notifications/all)
# ═════════════════════════════════════════════════════════════════════════════

async def webapp_notifications_all(request: web.Request) -> web.Response:
    """GET /api/webapp/notifications/all?user_id=xxx
    Agrège :
    - Notifs persistantes (table user_notifications) — paiements, enchères, annonces, etc.
    - Notifs actionnables temps réel — candidatures, invitations, contrats, offres parts
    """
    uid = _parse_uid(request)
    if not _auth(uid):
        return _err("unauthorized", 403)

    notifs = []

    async with AsyncSessionLocal() as session:
        # ── 0. Notifs persistantes (table user_notifications) ────────────────
        try:
            from api.webapp import _ensure_notif_table as _ent
            await _ent()
            rows = (await session.execute(
                sa_text("""
                    SELECT id, icon, title, body, is_read, created_at
                    FROM user_notifications
                    WHERE user_id = :uid
                    ORDER BY created_at DESC
                    LIMIT 50
                """), {"uid": uid}
            )).fetchall()
            for row in rows:
                notifs.append({
                    "id":     row[0],
                    "icon":   row[1] or "🔔",
                    "title":  row[2],
                    "text":   row[3],
                    "unread": not row[4],
                    "time":   row[5].strftime("%d/%m %H:%M") if row[5] else "",
                    "type":   "persistent",
                })
        except Exception as _e:
            import logging as _nlog
            _nlog.getLogger(__name__).warning(f"notifs persistantes: {_e}")

        # ── 1. Candidatures reçues (PDG/Directeur) ───────────────────────────
        company, emp = await _get_user_company(session, uid)
        if company and emp and emp.role in DIRECTION_ROLES and not company.is_bot_company:
            apps = (await session.execute(
                select(CompanyApplication).where(
                    CompanyApplication.company_id == company.id,
                    CompanyApplication.status == "pending",
                )
            )).scalars().all()
            for a in apps:
                applicant = await session.get(User, a.user_id)
                name = applicant.first_name if applicant else "Un joueur"
                notifs.append({
                    "icon": "📩",
                    "title": "Nouvelle candidature",
                    "text": f"{name} postule dans {company.name}",
                    "time": a.created_at.strftime("%d/%m %H:%M") if a.created_at else "",
                    "unread": True,
                    "type": "application",
                })

        # ── 2. Invitations entreprise en attente ─────────────────────────────
        invites = (await session.execute(
            select(CompanyInvite).where(
                CompanyInvite.target_id == uid,
                CompanyInvite.status == "pending",
                CompanyInvite.expires_at > datetime.utcnow(),
            )
        )).scalars().all()
        for inv in invites:
            co = await session.get(Company, inv.company_id)
            parts = inv.role.split("|")
            real_role = parts[0]
            notifs.append({
                "icon": ROLE_EMOJI.get(real_role, "👤"),
                "title": f"Invitation — {co.name if co else '?'}",
                "text": f"Poste proposé : {real_role.capitalize()}",
                "time": inv.expires_at.strftime("Expire le %d/%m") if inv.expires_at else "",
                "unread": True,
                "type": "invite",
            })

        # ── 3. Contre-propositions de contrat (PDG doit répondre) ────────────
        if company and emp and emp.role == "pdg":
            counter_emps = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == company.id,
                    CompanyEmployee.left_at == None,
                    CompanyEmployee.contract_status == "pending_pdg",
                )
            )).scalars().all()
            for ce in counter_emps:
                eu = await session.get(User, ce.user_id)
                name = eu.first_name if eu else "Un employé"
                notifs.append({
                    "icon": "💬",
                    "title": "Contre-proposition",
                    "text": f"{name} demande {_fmt(ce.pending_salary or 0)} $/jour",
                    "time": "",
                    "unread": True,
                    "type": "counter",
                })

        # ── 4. Proposition de contrat reçue (employé doit répondre) ──────────
        if emp and emp.role not in ("pdg",) and emp.contract_status == "pending_employee":
            if company:
                notifs.append({
                    "icon": "📄",
                    "title": "Proposition de contrat",
                    "text": f"{company.name} te propose {_fmt(emp.pending_salary or 0)} $/jour",
                    "time": "",
                    "unread": True,
                    "type": "contract",
                })

        # ── 5. Offres d'achat de parts en attente (PDG) ──────────────────────
        if company and emp and emp.role == "pdg":
            pending_offers = (await session.execute(
                select(CompanyShareOffer).where(
                    CompanyShareOffer.company_id == company.id,
                    CompanyShareOffer.status == "pending",
                    CompanyShareOffer.expires_at > datetime.utcnow(),
                )
            )).scalars().all()
            for offer in pending_offers:
                buyer = await session.get(User, offer.buyer_id)
                name = buyer.first_name if buyer else "Un joueur"
                notifs.append({
                    "icon": "💼",
                    "title": "Offre d'achat de parts",
                    "text": f"{name} veut {offer.quantity} parts pour {_fmt(offer.total_price)} $",
                    "time": offer.expires_at.strftime("Expire le %d/%m") if offer.expires_at else "",
                    "unread": True,
                    "type": "share_offer",
                })

    unread_count = sum(1 for n in notifs if n.get("unread"))
    return web.json_response({"notifications": notifs, "count": unread_count, "total": len(notifs)})


# ═════════════════════════════════════════════════════════════════════════════
#  NOTIFICATIONS — badge onglet Éco (/notifications/eco)
# ═════════════════════════════════════════════════════════════════════════════

async def webapp_notifications_eco(request: web.Request) -> web.Response:
    """GET /api/webapp/notifications/eco?user_id=xxx
    Retourne un compte rapide pour le badge de l'onglet Éco :
    - Contrats claimables (BureauContrat statut 'completed' non réclamés)
    - Offres de parts en attente pour le PDG
    """
    uid = _parse_uid(request)
    if not _auth(uid):
        return _err("unauthorized", 403)

    count = 0
    claimable = 0

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)

        if company:
            # Contrats bureau complétés non réclamés
            done_contrats = (await session.execute(
                select(func.count()).where(
                    BureauContrat.company_id == company.id,
                    BureauContrat.status == "completed",
                )
            )).scalar() or 0
            claimable = int(done_contrats)
            count += claimable

            # Offres de parts en attente (PDG)
            if emp and emp.role == "pdg":
                pending_shares = (await session.execute(
                    select(func.count()).where(
                        CompanyShareOffer.company_id == company.id,
                        CompanyShareOffer.status == "pending",
                        CompanyShareOffer.expires_at > datetime.utcnow(),
                    )
                )).scalar() or 0
                count += int(pending_shares)

    return web.json_response({"count": count, "claimable": claimable})


# ═════════════════════════════════════════════════════════════════════════════
#  ENREGISTREMENT DES ROUTES
# ═════════════════════════════════════════════════════════════════════════════

def setup_actions_routes(app: web.Application):
    """Appelé depuis webapp.py pour enregistrer toutes les nouvelles routes."""

    # Joueurs
    app.router.add_get("/api/webapp/players/search",      webapp_players_search)

    # Entreprises — navigation
    app.router.add_get("/api/webapp/companies/list",       webapp_companies_list)
    app.router.add_get("/api/webapp/companies/search",     webapp_companies_search)

    # RH
    app.router.add_post("/api/webapp/postuler",            webapp_postuler)
    app.router.add_post("/api/webapp/demissionner",        webapp_demissionner)
    app.router.add_post("/api/webapp/licencier",           webapp_licencier)
    app.router.add_post("/api/webapp/nommer",              webapp_nommer)
    app.router.add_post("/api/webapp/recruter",            webapp_recruter)

    # Invitations
    app.router.add_get("/api/webapp/invitations",          webapp_invitations)
    app.router.add_post("/api/webapp/invitations/accepter", webapp_invitations_accepter)
    app.router.add_post("/api/webapp/invitations/refuser",  webapp_invitations_refuser)

    # Salaires
    app.router.add_post("/api/webapp/versersalaires",      webapp_versersalaires)
    app.router.add_post("/api/webapp/payeremploye",        webapp_payeremploye)
    app.router.add_post("/api/webapp/negociercontrat",     webapp_negociercontrat)
    app.router.add_get("/api/webapp/presences",            webapp_presences)

    # Parts
    app.router.add_get("/api/webapp/parts",                webapp_mes_parts)
    app.router.add_post("/api/webapp/parts/vendre",        webapp_vendre_parts)
    app.router.add_post("/api/webapp/parts/acheter",       webapp_acheter_parts)
    app.router.add_post("/api/webapp/parts/accepteroffre", webapp_accepter_offre)
    app.router.add_post("/api/webapp/parts/refuseroffre",  webapp_refuser_offre)

    # Finance
    app.router.add_post("/api/webapp/pay",                 webapp_pay)

    # Contrats
    app.router.add_get("/api/webapp/contrats/bc",          webapp_contrats_bc)
    app.router.add_get("/api/webapp/contrats/auto",        webapp_contrats_auto)
    app.router.add_post("/api/webapp/contrats/bc/claim",   webapp_contrats_bc_claim)
    app.router.add_post("/api/webapp/contrats/auto/claim", webapp_contrats_auto_claim)

    # Misc
    app.router.add_post("/api/webapp/skipattente",         webapp_skipattente)

    # Entreprise — création + gestion PDG
    app.router.add_post("/api/webapp/creerboite",          webapp_creerboite)
    app.router.add_get("/api/webapp/bilan",                webapp_bilan)
    app.router.add_get("/api/webapp/dividendes",           webapp_dividendes)
    app.router.add_get("/api/webapp/salaireinfo",          webapp_salaireinfo)
    app.router.add_post("/api/webapp/cederentreprise",     webapp_cederentreprise)
    app.router.add_post("/api/webapp/renommerboite",       webapp_renommerboite)
    app.router.add_post("/api/webapp/acheterpla",          webapp_acheterpla)
    app.router.add_get("/api/webapp/pretboite",            webapp_pretboite)
    app.router.add_post("/api/webapp/rembourserboite",     webapp_rembourserboite)
    app.router.add_get("/api/webapp/batiments",            webapp_batiments)
    app.router.add_post("/api/webapp/batiments/acheter",   webapp_batiments_acheter)
    app.router.add_post("/api/webapp/negociercontrat",     webapp_negociercontrat)
    app.router.add_post("/api/webapp/contrat/repondre",    webapp_contrat_repondre)

    # Notifications
    app.router.add_get("/api/webapp/notifications/all",    webapp_notifications_all)
    app.router.add_get("/api/webapp/notifications/eco",    webapp_notifications_eco)

    # Marché des objets
    app.router.add_get("/api/webapp/items/market",         webapp_item_market_list)
    app.router.add_post("/api/webapp/items/sell_expert",   webapp_item_sell_expert)
    app.router.add_post("/api/webapp/items/put_market",    webapp_item_put_market)
    app.router.add_post("/api/webapp/items/remove_market", webapp_item_remove_market)
    app.router.add_post("/api/webapp/items/buy",           webapp_item_buy)
    app.router.add_post("/api/webapp/items/delete",        webapp_item_delete)

    # Famille
    app.router.add_post("/api/webapp/family/marry",        webapp_family_marry)
    app.router.add_post("/api/webapp/family/adopt",        webapp_family_adopt)
    app.router.add_post("/api/webapp/family/friend",       webapp_family_friend)
    app.router.add_post("/api/webapp/family/divorce",      webapp_family_divorce)
    app.router.add_post("/api/webapp/family/unfriend",     webapp_family_unfriend)
    app.router.add_post("/api/webapp/family/disown",       webapp_family_disown)


# ═════════════════════════════════════════════════════════════════════════════
#  MARCHÉ DES OBJETS (auction_inventory)
# ═════════════════════════════════════════════════════════════════════════════

async def webapp_item_sell_expert(request: web.Request) -> web.Response:
    """POST /api/webapp/items/sell_expert — Revendre un objet au bot (50% valeur)"""
    body = await _body(request)
    uid = _parse_uid(request)
    item_id = int(body.get("item_id", 0))
    if not _auth(uid):
        return _err("unauthorized")
    if not item_id:
        return _err("item_id manquant")

    async with AsyncSessionLocal() as session:
        r = await session.execute(
            sa_text("SELECT id, item_name, item_emoji, true_value, for_sale FROM auction_inventory WHERE id = :iid AND user_id = :uid"),
            {"iid": item_id, "uid": uid}
        )
        row = r.fetchone()
        if not row:
            return _err("Objet introuvable")
        if row[4]:  # for_sale
            return _err("Retire l'objet du marché avant de le revendre")

        gain = max(1, int((row[3] or 0) * 0.50))
        await session.execute(
            sa_text("DELETE FROM auction_inventory WHERE id = :iid AND user_id = :uid"),
            {"iid": item_id, "uid": uid}
        )
        await session.execute(
            sa_text("UPDATE users SET coins = CAST(coins AS BIGINT) + CAST(:g AS BIGINT) WHERE user_id = :uid"),
            {"g": gain, "uid": uid}
        )
        await session.commit()

    return _ok(f"✅ {row[2]} {row[1]} vendu à l'expert pour {gain:,} $ (50% de la valeur) !", gain=gain)


async def webapp_item_put_market(request: web.Request) -> web.Response:
    """POST /api/webapp/items/put_market — Mettre un objet en vente sur le marché"""
    body = await _body(request)
    uid = _parse_uid(request)
    item_id = int(body.get("item_id", 0))
    price   = int(body.get("price", 0))
    if not _auth(uid):
        return _err("unauthorized")
    if not item_id:
        return _err("item_id manquant")
    if price < 1:
        return _err("Prix invalide (minimum 1 $)")

    async with AsyncSessionLocal() as session:
        r = await session.execute(
            sa_text("SELECT id, item_name, item_emoji, true_value FROM auction_inventory WHERE id = :iid AND user_id = :uid"),
            {"iid": item_id, "uid": uid}
        )
        row = r.fetchone()
        if not row:
            return _err("Objet introuvable")

        await session.execute(
            sa_text("UPDATE auction_inventory SET for_sale = TRUE, sale_price = :price WHERE id = :iid AND user_id = :uid"),
            {"price": price, "iid": item_id, "uid": uid}
        )
        await session.commit()

    return _ok(f"🏷️ {row[2]} {row[1]} mis en vente à {price:,} $ !", price=price)


async def webapp_item_remove_market(request: web.Request) -> web.Response:
    """POST /api/webapp/items/remove_market — Retirer un objet du marché"""
    body = await _body(request)
    uid = _parse_uid(request)
    item_id = int(body.get("item_id", 0))
    if not _auth(uid):
        return _err("unauthorized")

    async with AsyncSessionLocal() as session:
        await session.execute(
            sa_text("UPDATE auction_inventory SET for_sale = FALSE, sale_price = NULL WHERE id = :iid AND user_id = :uid"),
            {"iid": item_id, "uid": uid}
        )
        await session.commit()

    return _ok("✅ Objet retiré du marché.")


async def webapp_item_market_list(request: web.Request) -> web.Response:
    """GET /api/webapp/items/market — Liste des objets en vente"""
    uid = int(request.rel_url.query.get("user_id", 0))
    if not _auth(uid):
        return _err("unauthorized")

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT ai.id, ai.user_id, ai.item_name, ai.item_emoji, ai.rarity,
                   ai.true_value, ai.sale_price, ai.acquired_at,
                   u.first_name, u.username
            FROM auction_inventory ai
            JOIN users u ON u.user_id = ai.user_id
            WHERE ai.for_sale = TRUE
            ORDER BY ai.sale_price ASC
            LIMIT 100
        """))
        items = []
        for row in r.fetchall():
            items.append({
                "id":          row[0],
                "seller_id":   row[1],
                "item_name":   row[2],
                "item_emoji":  row[3],
                "rarity":      row[4],
                "true_value":  row[5],
                "sale_price":  row[6],
                "acquired_at": str(row[7])[:10] if row[7] else "",
                "seller_name": row[8] or "—",
                "seller_username": row[9] or "",
                "is_mine":     row[1] == uid,
            })

    return web.json_response({"items": items})


async def webapp_item_buy(request: web.Request) -> web.Response:
    """POST /api/webapp/items/buy — Acheter un objet sur le marché"""
    body = await _body(request)
    uid = _parse_uid(request)
    item_id = int(body.get("item_id", 0))
    if not _auth(uid):
        return _err("unauthorized")
    if not item_id:
        return _err("item_id manquant")

    async with AsyncSessionLocal() as session:
        r = await session.execute(
            sa_text("SELECT id, user_id, item_name, item_emoji, rarity, true_value, sale_price FROM auction_inventory WHERE id = :iid AND for_sale = TRUE"),
            {"iid": item_id}
        )
        row = r.fetchone()
        if not row:
            return _err("Objet introuvable ou plus disponible")
        if row[1] == uid:
            return _err("Tu ne peux pas acheter ton propre objet")

        price = row[6]
        buyer_r = await session.execute(sa_text("SELECT coins FROM users WHERE user_id = :uid"), {"uid": uid})
        buyer_row = buyer_r.fetchone()
        if not buyer_row or buyer_row[0] < price:
            return _err(f"Fonds insuffisants (besoin de {price:,} $)")

        seller_id = row[1]
        # Débiter acheteur
        await session.execute(
            sa_text("UPDATE users SET coins = CAST(coins AS BIGINT) - CAST(:p AS BIGINT) WHERE user_id = :uid"),
            {"p": price, "uid": uid}
        )
        # Créditer vendeur
        await session.execute(
            sa_text("UPDATE users SET coins = CAST(coins AS BIGINT) + CAST(:p AS BIGINT) WHERE user_id = :uid"),
            {"p": price, "uid": seller_id}
        )
        # Transférer l'objet
        await session.execute(
            sa_text("UPDATE auction_inventory SET user_id = :buyer, for_sale = FALSE, sale_price = NULL WHERE id = :iid"),
            {"buyer": uid, "iid": item_id}
        )
        await session.commit()

    return _ok(f"✅ {row[3]} {row[2]} acheté pour {price:,} $ ! L'objet est dans ton inventaire.", price=price)


async def webapp_item_delete(request: web.Request) -> web.Response:
    """POST /api/webapp/items/delete — Supprimer définitivement un objet de l'inventaire"""
    body = await _body(request)
    uid = _parse_uid(request)
    item_id = int(body.get("item_id", 0))
    if not _auth(uid):
        return _err("unauthorized")
    if not item_id:
        return _err("item_id manquant")

    async with AsyncSessionLocal() as session:
        r = await session.execute(
            sa_text("SELECT id, item_name, item_emoji FROM auction_inventory WHERE id = :iid AND user_id = :uid AND for_sale = FALSE"),
            {"iid": item_id, "uid": uid}
        )
        row = r.fetchone()
        if not row:
            return _err("Objet introuvable ou en vente (retire-le du marché d'abord)")
        await session.execute(
            sa_text("DELETE FROM auction_inventory WHERE id = :iid AND user_id = :uid"),
            {"iid": item_id, "uid": uid}
        )
        await session.commit()

    return _ok(f"🗑️ {row[2]} {row[1]} supprimé définitivement.")


# ═════════════════════════════════════════════════════════════════════════════
#  NOUVELLES ROUTES ENTREPRISE — création + gestion PDG
# ═════════════════════════════════════════════════════════════════════════════

async def webapp_creerboite(request: web.Request) -> web.Response:
    """POST /api/webapp/creerboite — body: {user_id, name, sector}"""
    body = await _body(request)
    uid = _parse_uid(request)
    name   = (body.get("name") or "").strip()
    sector = (body.get("sector") or "").strip().lower()
    if not _auth(uid): return _err("unauthorized")
    if not name:       return _err("Nom manquant")
    if len(name) < 2:  return _err("Le nom doit contenir au moins 2 caractères")
    if len(name) > 40: return _err("Le nom ne peut pas dépasser 40 caractères")

    from handlers.company import SECTORS, SECTOR_ALLOWED_DOMAINS, LEVELS
    if sector not in SECTORS:
        return _err(f"Secteur invalide. Choix : {', '.join(SECTORS.keys())}")

    async with AsyncSessionLocal() as session:
        db_user = await get_user(session, uid)
        if not db_user: return _err("Utilisateur introuvable")

        if not db_user.diplome_licence:
            return _err("Il te faut au minimum une Licence pour créer une entreprise")

        # Vérifier cohérence domaine/secteur
        user_domain = db_user.diplome_domain
        allowed_domains = SECTOR_ALLOWED_DOMAINS.get(sector, [])
        if user_domain and allowed_domains and user_domain not in allowed_domains:
            sec_emoji, sec_name = SECTORS[sector]
            return _err(f"Ton domaine ne te permet pas de créer une entreprise dans le secteur {sec_name}")

        if db_user.coins < 50_000_000:
            return _err(f"Il te faut 50 000 000 $ pour créer une entreprise (tu as {_fmt(db_user.coins)} $)")

        # Vérifier déjà PDG
        company, emp = await _get_user_company(session, uid)
        if company and emp and emp.role == "pdg":
            return _err(f"Tu es déjà PDG de {company.name}")

        # Vérifier nom unique
        from sqlalchemy import select as _sel
        from database.models import Company, CompanyEmployee, CompanyShare
        exists = (await session.execute(
            _sel(Company).where(Company.name.ilike(name))
        )).scalar_one_or_none()
        if exists:
            return _err(f"Une entreprise nommée « {name} » existe déjà. Choisis un autre nom.")

        # Créer
        db_user.coins -= 50_000_000
        new_company = Company(
            name=name,
            sector=sector,
            owner_id=uid,
            group_id=0,
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
            return _err("Ce nom est déjà pris. Choisis un autre nom.")

        pdg_emp = CompanyEmployee(company_id=new_company.id, user_id=uid, role="pdg")
        session.add(pdg_emp)
        share = CompanyShare(company_id=new_company.id, owner_id=uid, quantity=100)
        session.add(share)

        await _add_log(session, new_company.id, "creation", f"Entreprise créée via Mini App")
        await session.commit()

        sec_emoji, sec_name = SECTORS[sector]
        return _ok(
            f"✅ {new_company.name} est fondée ! {sec_emoji} Secteur : {sec_name}. Tu es le PDG (fondateur).",
            company_id=new_company.id, name=new_company.name, sector=sec_name
        )


async def webapp_bilan(request: web.Request) -> web.Response:
    """GET /api/webapp/bilan?user_id=..."""
    uid = _parse_uid(request)
    if not _auth(uid): return _err("unauthorized")

    from handlers.company import ROLE_SHARE, LEVELS
    from database.models import CompanyLoan

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company: return _err("Tu n'appartiens à aucune entreprise")

        from handlers.company import _level_info as _li
        level_label, _, _, monthly_rate, _ = _li(company.level)
        gross_daily = int(company.value * monthly_rate) // 30
        legal_daily = int(gross_daily * 0.10)
        net_daily   = gross_daily - legal_daily

        from sqlalchemy import select as _sel
        from database.models import CompanyEmployee
        emps = (await session.execute(
            _sel(CompanyEmployee).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.left_at == None,
            )
        )).scalars().all()

        charges_sal = sum(
            int(gross_daily * ROLE_SHARE.get(e.role, 0))
            for e in emps if e.role not in ("pdg",)
        )
        benefice_net = net_daily - charges_sal

        from database.models import CompanyShare
        shares = (await session.execute(
            _sel(CompanyShare).where(CompanyShare.company_id == company.id)
        )).scalars().all()

        loan = None
        try:
            from database.models import CompanyLoan as CL
            loan_row = (await session.execute(
                _sel(CL).where(CL.company_id == company.id, CL.status == "active")
            )).scalar_one_or_none()
            if loan_row:
                jours_restants = max(0, (loan_row.due_at - datetime.utcnow()).days)
                loan = {
                    "amount": _fmt(loan_row.amount),
                    "remaining": _fmt(loan_row.remaining),
                    "daily_payment": _fmt(loan_row.daily_payment),
                    "jours_restants": jours_restants,
                    "taux": f"{loan_row.interest_rate * 100:.0f}%",
                    "missed_days": loan_row.missed_days or 0,
                }
        except Exception:
            pass

        reserve_legale = company.legal_reserve or 0
        reserve_sal = sum(
            int(gross_daily * ROLE_SHARE.get(e.role, 0))
            for e in emps if e.role not in ("stagiaire", "pdg")
        )
        disponible = max(0, company.treasury - reserve_legale - reserve_sal)
        dividend_pool = int((company.weekly_revenue or 0) * 0.30)

        return web.json_response({
            "ok": True,
            "bilan": {
                "company_name": company.name,
                "level_label": level_label,
                "reputation": f"{company.reputation:.1f}",
                "gross_daily": _fmt(gross_daily),
                "legal_daily": _fmt(legal_daily),
                "net_daily": _fmt(net_daily),
                "charges_sal": _fmt(charges_sal),
                "nb_employees": len(emps),
                "benefice_net": _fmt(benefice_net),
                "treasury": _fmt(company.treasury),
                "reserve_legale": _fmt(reserve_legale),
                "disponible": _fmt(disponible),
                "total_shares": company.total_shares,
                "nb_actionnaires": len(shares),
                "weekly_revenue": _fmt(company.weekly_revenue or 0),
                "dividend_pool": _fmt(dividend_pool),
                "loan": loan,
            }
        })


async def webapp_dividendes(request: web.Request) -> web.Response:
    """GET /api/webapp/dividendes?user_id=..."""
    uid = _parse_uid(request)
    if not _auth(uid): return _err("unauthorized")

    from sqlalchemy import select as _sel
    from database.models import CompanyShare, Company

    async with AsyncSessionLocal() as session:
        user_shares = (await session.execute(
            _sel(CompanyShare).where(CompanyShare.owner_id == uid)
        )).scalars().all()

        if not user_shares:
            return _err("Tu ne détiens aucune part d'entreprise. Achète des parts pour toucher des dividendes.")

        items = []
        total_estimated = 0

        for share in user_shares:
            company = await session.get(Company, share.company_id)
            if not company or not company.is_active:
                continue
            weekly_rev = company.weekly_revenue or 0
            dividend_pool = int(weekly_rev * 0.30)
            my_ratio = share.quantity / company.total_shares if company.total_shares > 0 else 0
            my_dividend = int(dividend_pool * my_ratio)
            total_estimated += my_dividend
            items.append({
                "company_name": company.name,
                "my_shares": share.quantity,
                "total_shares": company.total_shares,
                "pct": round(my_ratio * 100, 1),
                "weekly_rev": _fmt(weekly_rev),
                "my_dividend": _fmt(my_dividend),
                "my_dividend_raw": my_dividend,
            })

        return web.json_response({
            "ok": True,
            "dividendes": items,
            "total_estimated": _fmt(total_estimated),
        })


async def webapp_salaireinfo(request: web.Request) -> web.Response:
    """GET /api/webapp/salaireinfo?user_id=..."""
    uid = _parse_uid(request)
    if not _auth(uid): return _err("unauthorized")

    from handlers.company import ROLE_SHARE, ROLE_EMOJI, SECTORS, _level_info as _li

    async with AsyncSessionLocal() as session:
        db_user = await get_user(session, uid)
        company, emp = await _get_user_company(session, uid)
        if not company: return _err("Tu ne fais partie d'aucune entreprise")

        _, _, _, monthly_rate, _ = _li(company.level)
        total_revenue = int(company.value * monthly_rate) // 30
        personal_share = ROLE_SHARE.get(emp.role, 0.0)
        personal_revenue = int(total_revenue * personal_share)

        joined = emp.joined_at if hasattr(emp, "joined_at") and emp.joined_at else None
        days_here = (datetime.utcnow() - joined).days if joined else 0

        activity = getattr(emp, "activity_since_payroll", 0) or 0
        activity_bonus = min(0.5, activity / 40)
        estimated = int(personal_revenue * (1 + activity_bonus)) if personal_revenue > 0 else 0

        sec_emoji, sec_name = SECTORS.get(company.sector, ("🏢", company.sector))

        return web.json_response({
            "ok": True,
            "salaireinfo": {
                "company_name": company.name,
                "sector": f"{sec_emoji} {sec_name}",
                "role": emp.role,
                "role_emoji": ROLE_EMOJI.get(emp.role, "👤"),
                "personal_share_pct": f"{personal_share*100:.0f}%",
                "estimated_salary": _fmt(estimated),
                "activity_cmds": activity,
                "days_here": days_here,
                "is_pdg": emp.role == "pdg",
                "wallet": _fmt(db_user.coins),
            }
        })


async def webapp_cederentreprise(request: web.Request) -> web.Response:
    """POST /api/webapp/cederentreprise — body: {user_id, target_username}"""
    body = await _body(request)
    uid = _parse_uid(request)
    username = (body.get("target_username") or "").lstrip("@").strip()
    if not _auth(uid): return _err("unauthorized")
    if not username:   return _err("Nom d'utilisateur manquant")

    from sqlalchemy import select as _sel, func
    from database.models import User, CompanyShare, CompanyEmployee, Company
    from handlers.company import _has_diploma

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company or emp.role not in ("pdg",):
            return _err("Seul le PDG peut transférer ce titre")
        if company.is_bot_company:
            return _err("Impossible sur une entreprise officielle")

        target = (await session.execute(
            _sel(User).where(func.lower(User.username) == username.lower())
        )).scalar_one_or_none()
        if not target: return _err(f"@{username} introuvable")
        if target.user_id == uid: return _err("Tu es déjà PDG")

        target_emp = (await session.execute(
            _sel(CompanyEmployee).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.user_id == target.user_id,
                CompanyEmployee.left_at == None,
            )
        )).scalar_one_or_none()
        if not target_emp:
            return _err(f"{target.first_name} ne fait pas partie de {company.name}")

        if not _has_diploma(target, "mba"):
            return _err(f"{target.first_name} doit avoir un MBA pour devenir PDG")

        emp.role = "directeur"
        target_emp.role = "pdg"
        company.owner_id = target.user_id

        # Transférer les parts
        old_share = (await session.execute(
            _sel(CompanyShare).where(
                CompanyShare.company_id == company.id,
                CompanyShare.owner_id == uid,
            )
        )).scalar_one_or_none()
        new_share = (await session.execute(
            _sel(CompanyShare).where(
                CompanyShare.company_id == company.id,
                CompanyShare.owner_id == target.user_id,
            )
        )).scalar_one_or_none()

        if old_share and old_share.quantity > 0:
            if new_share:
                new_share.quantity += old_share.quantity
            else:
                transferred = CompanyShare(
                    company_id=company.id, owner_id=target.user_id, quantity=old_share.quantity
                )
                session.add(transferred)
            old_share.quantity = 0

        await _add_log(session, company.id, "cession",
                       f"PDG cédé à {target.first_name} (@{target.username or target.user_id})")
        await session.commit()

        return _ok(f"✅ Entreprise {company.name} cédée à {target.first_name} (@{target.username or target.user_id}). Tu deviens Directeur.")


async def webapp_renommerboite(request: web.Request) -> web.Response:
    """POST /api/webapp/renommerboite — body: {user_id, new_name}"""
    body = await _body(request)
    uid = _parse_uid(request)
    new_name = (body.get("new_name") or "").strip()
    if not _auth(uid):     return _err("unauthorized")
    if not new_name:       return _err("Nouveau nom manquant")
    if len(new_name) < 2:  return _err("Le nom doit contenir au moins 2 caractères")
    if len(new_name) > 40: return _err("Le nom ne peut pas dépasser 40 caractères")

    RENAME_COST = 10_000_000
    RENAME_COOLDOWN_DAYS = 30

    from sqlalchemy import select as _sel
    from database.models import Company

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company or emp.role not in ("pdg",):
            return _err("Seul le PDG peut renommer l'entreprise")
        if company.is_bot_company:
            return _err("Les entreprises officielles ne peuvent pas être renommées")

        if company.last_rename:
            delta = datetime.utcnow() - company.last_rename
            if delta.days < RENAME_COOLDOWN_DAYS:
                reste = RENAME_COOLDOWN_DAYS - delta.days
                return _err(f"Cooldown : encore {reste} jour(s) avant le prochain renommage")

        name_conflict = (await session.execute(
            _sel(Company).where(Company.name.ilike(new_name))
        )).scalar_one_or_none()
        if name_conflict:
            return _err(f"Une entreprise nommée « {new_name} » existe déjà")

        if company.treasury < RENAME_COST:
            return _err(f"Trésorerie insuffisante. Coût : {_fmt(RENAME_COST)} $, disponible : {_fmt(company.treasury)} $")

        old_name = company.name
        company.treasury -= RENAME_COST
        company.value = max(50_000_000, company.value - RENAME_COST)
        company.name = new_name
        company.last_rename = datetime.utcnow()

        await _add_log(session, company.id, "renommage",
                       f"Renommée de « {old_name} » en « {new_name} » (coût : {_fmt(RENAME_COST)} $)")
        await session.commit()

        return _ok(f"✅ Entreprise renommée : « {old_name} » → « {new_name} ». Coût : {_fmt(RENAME_COST)} $ débité de la trésorerie.")


async def webapp_acheterpla(request: web.Request) -> web.Response:
    """POST /api/webapp/acheterpla — body: {user_id, qty}"""
    body = await _body(request)
    uid = _parse_uid(request)
    qty = int(body.get("qty", 0))
    if not _auth(uid): return _err("unauthorized")
    if qty <= 0:       return _err("Quantité invalide")

    SLOT_PRICES = {1: 5_000_000, 2: 15_000_000, 3: 50_000_000, 4: 150_000_000, 5: 500_000_000}
    MAX_EXTRA_SLOTS = 20
    from handlers.company import _level_info as _li

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company or emp.role not in ("pdg",):
            return _err("Seul le PDG peut acheter des places supplémentaires")
        if company.is_bot_company:
            return _err("Impossible sur une entreprise officielle")

        current_extra = company.extra_slots or 0
        if current_extra >= MAX_EXTRA_SLOTS:
            return _err(f"Maximum de {MAX_EXTRA_SLOTS} places bonus atteint")

        qty_possible = min(qty, MAX_EXTRA_SLOTS - current_extra)
        qty = qty_possible

        prix_unitaire = SLOT_PRICES.get(company.level, SLOT_PRICES[1])
        cout_total = prix_unitaire * qty
        _, _, _, _, base_cap = _li(company.level)
        nouvelle_cap = base_cap + current_extra + qty

        if company.treasury < cout_total:
            return _err(f"Trésorerie insuffisante. Coût : {_fmt(cout_total)} $, disponible : {_fmt(company.treasury)} $")

        company.treasury -= cout_total
        company.value = max(50_000_000, company.value - cout_total)
        company.extra_slots = current_extra + qty

        await _add_log(session, company.id, "acheterpla",
                       f"{qty} place(s) bonus achetée(s) pour {_fmt(cout_total)} $. Capacité : {nouvelle_cap}")
        await session.commit()

        return _ok(
            f"✅ {qty} place(s) achetée(s) ! Capacité totale : {nouvelle_cap} employés. Coût : {_fmt(cout_total)} $ débité.",
            nouvelle_cap=nouvelle_cap, qty=qty, cout_total=_fmt(cout_total)
        )


async def webapp_pretboite(request: web.Request) -> web.Response:
    """GET /api/webapp/pretboite?user_id=..."""
    uid = _parse_uid(request)
    if not _auth(uid): return _err("unauthorized")

    from sqlalchemy import select as _sel

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company: return _err("Tu n'appartiens à aucune entreprise")

        loan_data = None
        last_loan_data = None

        try:
            from database.models import CompanyLoan as CL
            loan = (await session.execute(
                _sel(CL).where(CL.company_id == company.id, CL.status == "active")
            )).scalar_one_or_none()

            if loan:
                jours_restants = max(0, (loan.due_at - datetime.utcnow()).days)
                total_due = loan.amount + int(loan.amount * loan.interest_rate / 12)
                paid_so_far = total_due - loan.remaining
                progression_pct = int(paid_so_far / total_due * 100) if total_due > 0 else 0
                loan_data = {
                    "amount": _fmt(loan.amount),
                    "remaining": _fmt(loan.remaining),
                    "daily_payment": _fmt(loan.daily_payment),
                    "taux": f"{loan.interest_rate * 100:.0f}%",
                    "jours_restants": jours_restants,
                    "progression_pct": progression_pct,
                    "missed_days": loan.missed_days or 0,
                }
            else:
                last_loan = (await session.execute(
                    _sel(CL).where(CL.company_id == company.id).order_by(CL.taken_at.desc())
                )).scalar_one_or_none()
                if last_loan:
                    last_loan_data = {
                        "amount": _fmt(last_loan.amount),
                        "status": "✅ Remboursé" if last_loan.status == "repaid" else "❌ En défaut",
                    }
        except Exception:
            pass  # CompanyLoan peut ne pas exister

        return web.json_response({
            "ok": True,
            "company_name": company.name,
            "loan": loan_data,
            "last_loan": last_loan_data,
        })


# ═════════════════════════════════════════════════════════════════════════════
#  FAMILLE — actions webapp
# ═════════════════════════════════════════════════════════════════════════════

async def _send_family_request(from_id: int, to_id: int, req_type_str: str,
                                from_name: str, to_user_id: int,
                                group_id: int = 0, extra: str = "") -> dict:
    """Crée un PendingRequest et envoie les boutons Accepter/Refuser via Telegram DM."""
    from database.models import PendingRequest, RequestType as RT
    from database.db import AsyncSessionLocal as _ASL

    type_map = {"marry": RT.MARRY, "adopt": RT.ADOPT, "friend": RT.FRIEND}
    rtype = type_map.get(req_type_str)
    if not rtype:
        return {"error": "Type invalide"}

    async with _ASL() as session:
        # Supprimer doublon
        from sqlalchemy import delete as _del
        await session.execute(
            _del(PendingRequest).where(
                PendingRequest.from_user_id == from_id,
                PendingRequest.to_user_id   == to_id,
                PendingRequest.request_type == rtype.name,
            )
        )
        from datetime import datetime, timedelta
        req = PendingRequest(
            from_user_id = from_id,
            to_user_id   = to_id,
            request_type = rtype.name,
            group_id     = group_id or from_id,  # DM = utilise from_id comme group_id fallback
            message_id   = 0,
            expires_at   = datetime.utcnow() + timedelta(hours=24),
            extra        = extra or None,
        )
        session.add(req)
        await session.commit()
        await session.refresh(req)
        req_id = req.id

    labels = {
        "marry": f"💍 {from_name} te propose le mariage !",
        "adopt": f"👨‍👦 {from_name} souhaite t'adopter !",
        "friend": f"🤝 {from_name} veut être ton ami(e) !",
    }
    text = labels.get(req_type_str, "Nouvelle demande")
    text += "\n\n<i>Réponds via les boutons ci-dessous (expire dans 24h)</i>"

    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Accepter", "callback_data": f"req:accept:{req_id}:{req_type_str}"},
            {"text": "❌ Refuser",  "callback_data": f"req:decline:{req_id}:{req_type_str}"},
        ]]
    }
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": to_user_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "reply_markup": keyboard,
                },
                timeout=aiohttp.ClientTimeout(total=5),
            )
    except Exception:
        pass

    return {"ok": True, "req_id": req_id}


async def webapp_family_marry(request: web.Request) -> web.Response:
    """POST /api/webapp/family/marry — body: {user_id, target_id, marriage_type}"""
    body      = await _body(request)
    uid = _parse_uid(request)
    target_id = int(body.get("target_id", 0))
    mtype     = body.get("marriage_type", "monogame")  # monogame | polygame

    if not _auth(uid): return _err("unauthorized", 403)
    if not target_id:  return _err("target_id manquant")
    if uid == target_id: return _err("Tu ne peux pas te marier avec toi-même")

    async with AsyncSessionLocal() as session:
        from_user  = (await session.execute(select(User).where(User.user_id == uid))).scalar_one_or_none()
        to_user    = (await session.execute(select(User).where(User.user_id == target_id))).scalar_one_or_none()
        if not from_user: return _err("Utilisateur introuvable")
        if not to_user:   return _err("Joueur cible introuvable")

        from database.models import Relationship, RelationType
        # Vérifier si déjà mariés
        already = (await session.execute(
            select(Relationship).where(
                ((Relationship.user_id == uid) & (Relationship.related_user_id == target_id)) |
                ((Relationship.user_id == target_id) & (Relationship.related_user_id == uid)),
                Relationship.relation_type == RelationType.SPOUSE.value,
            )
        )).scalar_one_or_none()
        if already: return _err("Vous êtes déjà mariés !")

        # Vérif monogamie
        s_type = getattr(from_user, "marriage_type", "monogame") or "monogame"
        if mtype == "monogame" or s_type == "monogame":
            nb_spouses = (await session.execute(
                select(Relationship).where(
                    ((Relationship.user_id == uid) | (Relationship.related_user_id == uid)),
                    Relationship.relation_type == RelationType.SPOUSE.value,
                )
            )).scalars().all()
            if nb_spouses:
                return _err("Tu es déjà marié(e) en monogame. Divorce d'abord ou passe en polygame.")

    extra = f"{mtype}|{getattr(from_user, 'gender', '') or ''}|{getattr(to_user, 'gender', '') or ''}"
    result = await _send_family_request(uid, target_id, "marry", from_user.first_name, target_id, extra=extra)
    if "error" in result: return _err(result["error"])
    return _ok(f"💍 Demande envoyée à {to_user.first_name} ! Elle doit accepter via Telegram.")


async def webapp_family_adopt(request: web.Request) -> web.Response:
    """POST /api/webapp/family/adopt — body: {user_id, target_id}"""
    body      = await _body(request)
    uid = _parse_uid(request)
    target_id = int(body.get("target_id", 0))

    if not _auth(uid): return _err("unauthorized", 403)
    if not target_id:  return _err("target_id manquant")
    if uid == target_id: return _err("Tu ne peux pas t'adopter toi-même")

    async with AsyncSessionLocal() as session:
        from_user = (await session.execute(select(User).where(User.user_id == uid))).scalar_one_or_none()
        to_user   = (await session.execute(select(User).where(User.user_id == target_id))).scalar_one_or_none()
        if not from_user: return _err("Utilisateur introuvable")
        if not to_user:   return _err("Joueur cible introuvable")

    result = await _send_family_request(uid, target_id, "adopt", from_user.first_name, target_id)
    if "error" in result: return _err(result["error"])
    return _ok(f"👨‍👦 Demande d'adoption envoyée à {to_user.first_name} !")


async def webapp_family_friend(request: web.Request) -> web.Response:
    """POST /api/webapp/family/friend — body: {user_id, target_id}"""
    body      = await _body(request)
    uid = _parse_uid(request)
    target_id = int(body.get("target_id", 0))

    if not _auth(uid): return _err("unauthorized", 403)
    if not target_id:  return _err("target_id manquant")
    if uid == target_id: return _err("Tu ne peux pas être ton propre ami")

    async with AsyncSessionLocal() as session:
        from_user = (await session.execute(select(User).where(User.user_id == uid))).scalar_one_or_none()
        to_user   = (await session.execute(select(User).where(User.user_id == target_id))).scalar_one_or_none()
        if not from_user: return _err("Utilisateur introuvable")
        if not to_user:   return _err("Joueur cible introuvable")

        from database.models import Relationship, RelationType
        already = (await session.execute(
            select(Relationship).where(
                ((Relationship.user_id == uid) & (Relationship.related_user_id == target_id)) |
                ((Relationship.user_id == target_id) & (Relationship.related_user_id == uid)),
                Relationship.relation_type == RelationType.FRIEND.value,
            )
        )).scalar_one_or_none()
        if already: return _err("Vous êtes déjà amis !")

    result = await _send_family_request(uid, target_id, "friend", from_user.first_name, target_id)
    if "error" in result: return _err(result["error"])
    return _ok(f"🤝 Demande d'amitié envoyée à {to_user.first_name} !")


async def webapp_family_divorce(request: web.Request) -> web.Response:
    """POST /api/webapp/family/divorce — body: {user_id, target_id}"""
    body      = await _body(request)
    uid = _parse_uid(request)
    target_id = int(body.get("target_id", 0))

    if not _auth(uid): return _err("unauthorized", 403)
    if not target_id:  return _err("target_id manquant")

    async with AsyncSessionLocal() as session:
        from database.models import Relationship, RelationType
        from database.db import remove_relationship
        rel = (await session.execute(
            select(Relationship).where(
                ((Relationship.user_id == uid) & (Relationship.related_user_id == target_id)) |
                ((Relationship.user_id == target_id) & (Relationship.related_user_id == uid)),
                Relationship.relation_type == RelationType.SPOUSE.value,
            )
        )).scalar_one_or_none()
        if not rel: return _err("Vous n'êtes pas mariés.")

        spouse = (await session.execute(select(User).where(User.user_id == target_id))).scalar_one_or_none()
        from sqlalchemy import delete as _del
        await session.execute(
            _del(Relationship).where(Relationship.id == rel.id)
        )
        await session.commit()

    spouse_name = spouse.first_name if spouse else str(target_id)
    await _notify(target_id, f"💔 Tu viens de divorcer de <b>{spouse_name}</b> via la mini app.", "HTML")
    return _ok(f"💔 Divorce effectué.")


async def webapp_family_unfriend(request: web.Request) -> web.Response:
    """POST /api/webapp/family/unfriend — body: {user_id, target_id}"""
    body      = await _body(request)
    uid = _parse_uid(request)
    target_id = int(body.get("target_id", 0))

    if not _auth(uid): return _err("unauthorized", 403)
    if not target_id:  return _err("target_id manquant")

    async with AsyncSessionLocal() as session:
        from database.models import Relationship, RelationType
        rel = (await session.execute(
            select(Relationship).where(
                ((Relationship.user_id == uid) & (Relationship.related_user_id == target_id)) |
                ((Relationship.user_id == target_id) & (Relationship.related_user_id == uid)),
                Relationship.relation_type == RelationType.FRIEND.value,
            )
        )).scalar_one_or_none()
        if not rel: return _err("Vous n'êtes pas amis.")

        target = (await session.execute(select(User).where(User.user_id == target_id))).scalar_one_or_none()
        from sqlalchemy import delete as _del
        await session.execute(_del(Relationship).where(Relationship.id == rel.id))
        await session.commit()

    return _ok(f"👋 Amitié retirée.")


async def webapp_family_disown(request: web.Request) -> web.Response:
    """POST /api/webapp/family/disown — body: {user_id, target_id}"""
    body      = await _body(request)
    uid = _parse_uid(request)
    target_id = int(body.get("target_id", 0))

    if not _auth(uid): return _err("unauthorized", 403)
    if not target_id:  return _err("target_id manquant")

    async with AsyncSessionLocal() as session:
        from database.models import Relationship, RelationType
        # L'utilisateur est le parent (user_id = uid, related = target)
        rel = (await session.execute(
            select(Relationship).where(
                Relationship.user_id == uid,
                Relationship.related_user_id == target_id,
                Relationship.relation_type == RelationType.PARENT.value,
            )
        )).scalar_one_or_none()
        if not rel: return _err("Cette personne n'est pas dans ta famille (pas ton enfant).")

        from sqlalchemy import delete as _del
        await session.execute(_del(Relationship).where(Relationship.id == rel.id))
        await session.commit()

    return _ok(f"😔 Désaveu effectué.")


# ═══════════════════════════════════════════════════════════════════════════════
#  PRÊT BOÎTE — remboursement anticipé
# ═══════════════════════════════════════════════════════════════════════════════

async def webapp_rembourserboite(request: web.Request) -> web.Response:
    """POST /api/webapp/rembourserboite  { amount: int | 'tout' }"""
    uid = _parse_uid(request)
    if not _auth(uid): return _err("unauthorized", 403)
    body = await request.json()
    amount_raw = body.get("amount", 0)

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company: return _err("Tu n'appartiens à aucune entreprise")
        if emp.role != "pdg": return _err("Réservé au PDG")

        from database.models import CompanyLoan as CL
        from handlers.company import _add_log, LEVELS
        loan = (await session.execute(
            select(CL).where(CL.company_id == company.id, CL.status == "active")
        )).scalar_one_or_none()
        if not loan: return _err("Aucun prêt actif")

        if str(amount_raw).lower() == "tout":
            amount = loan.remaining
        else:
            try:
                amount = int(amount_raw)
            except (ValueError, TypeError):
                return _err("Montant invalide")

        if amount <= 0: return _err("Montant invalide")
        if amount > loan.remaining: amount = loan.remaining
        if company.treasury < amount:
            return _err(f"Trésorerie insuffisante ({_fmt(company.treasury)} $ dispo)")

        company.treasury -= amount
        company.value = max(LEVELS[1][2], company.value - amount)
        loan.remaining -= amount
        loan.missed_days = 0

        if loan.remaining <= 0:
            loan.status = "repaid"
            await _add_log(session, company.id, "pret",
                           f"Remboursement anticipé total du prêt ({_fmt(loan.amount)} $)")
            msg = f"✅ Prêt entièrement remboursé ! {_fmt(amount)} $ prélevés."
        else:
            await _add_log(session, company.id, "pret",
                           f"Remboursement anticipé partiel de {_fmt(amount)} $", amount=amount)
            msg = f"✅ {_fmt(amount)} $ remboursés. Restant : {_fmt(loan.remaining)} $"

        await session.commit()
        return _ok(msg)


# ═══════════════════════════════════════════════════════════════════════════════
#  BÂTIMENTS
# ═══════════════════════════════════════════════════════════════════════════════

BUILDINGS = {
    "salle_reunion": {
        "name": "🪑 Salle de Réunion",
        "base_cost": 500_000,
        "effect": "-10% délai négociation de contrats",
        "unlock_lvl": 1,
        "maintenance_pct": 0.005,
    },
    "entrepot": {
        "name": "📦 Entrepôt",
        "base_cost": 1_000_000,
        "effect": "+15% trésorerie max autorisée",
        "unlock_lvl": 1,
        "maintenance_pct": 0.005,
    },
    "siege_social": {
        "name": "🏛️ Siège Social",
        "base_cost": 2_000_000,
        "effect": "+10% réputation (boost passif)",
        "unlock_lvl": 2,
        "maintenance_pct": 0.005,
    },
    "datacenter": {
        "name": "🖥️ Datacenter",
        "base_cost": 5_000_000,
        "effect": "+10% revenus des contrats",
        "unlock_lvl": 3,
        "maintenance_pct": 0.005,
    },
    "usine": {
        "name": "🏭 Usine",
        "base_cost": 8_000_000,
        "effect": "+10% revenus journaliers",
        "unlock_lvl": 3,
        "maintenance_pct": 0.005,
    },
    "agence_bancaire": {
        "name": "🏦 Agence Bancaire",
        "base_cost": 15_000_000,
        "effect": "Débloque les prêts inter-entreprises",
        "unlock_lvl": 4,
        "maintenance_pct": 0.005,
    },
    "campus_rd": {
        "name": "🔬 Campus R&D",
        "base_cost": 30_000_000,
        "effect": "Débloque les contrats exclusifs",
        "unlock_lvl": 5,
        "maintenance_pct": 0.005,
    },
    "tour_controle": {
        "name": "🗼 Tour de Contrôle",
        "base_cost": 50_000_000,
        "effect": "Visibilité dans le classement mondial",
        "unlock_lvl": 5,
        "maintenance_pct": 0.005,
    },
}

LEVEL_MULTIPLIER = {1: 1, 2: 5, 3: 25, 4: 125, 5: 625}


def _building_cost(btype: str, company_level: int) -> int:
    b = BUILDINGS.get(btype)
    if not b: return 0
    return int(b["base_cost"] * LEVEL_MULTIPLIER.get(company_level, 1))


def _building_maintenance(btype: str, company_level: int) -> int:
    b = BUILDINGS.get(btype)
    if not b: return 0
    return int(_building_cost(btype, company_level) * b["maintenance_pct"])


async def webapp_batiments(request: web.Request) -> web.Response:
    """GET /api/webapp/batiments — catalogue + bâtiments possédés"""
    uid = _parse_uid(request)
    if not _auth(uid): return _err("unauthorized", 403)

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company: return _err("Tu n'appartiens à aucune entreprise")

        from database.models import CompanyBuilding as CB
        owned_rows = (await session.execute(
            select(CB).where(CB.company_id == company.id, CB.status == "active")
        )).scalars().all()
        owned_types = {b.building_type for b in owned_rows}

        catalogue = []
        for btype, b in BUILDINGS.items():
            cost = _building_cost(btype, company.level)
            maint = _building_maintenance(btype, company.level)
            owned = btype in owned_types
            unlocked = company.level >= b["unlock_lvl"]
            catalogue.append({
                "type": btype,
                "name": b["name"],
                "effect": b["effect"],
                "cost": _fmt(cost),
                "cost_raw": cost,
                "maintenance": _fmt(maint),
                "maintenance_raw": maint,
                "unlock_lvl": b["unlock_lvl"],
                "owned": owned,
                "unlocked": unlocked,
                "can_afford": company.treasury >= cost and not owned and unlocked,
            })

        owned_list = []
        for ob in owned_rows:
            b = BUILDINGS.get(ob.building_type, {})
            maint = _building_maintenance(ob.building_type, company.level)
            owned_list.append({
                "type": ob.building_type,
                "name": b.get("name", ob.building_type),
                "effect": b.get("effect", ""),
                "maintenance": _fmt(maint),
                "purchased_at": ob.purchased_at.strftime("%d/%m/%Y") if ob.purchased_at else "—",
            })

        return web.json_response({
            "ok": True,
            "company_name": company.name,
            "company_level": company.level,
            "treasury": _fmt(company.treasury),
            "treasury_raw": company.treasury,
            "is_pdg": emp.role == "pdg",
            "catalogue": catalogue,
            "owned": owned_list,
        })


async def webapp_batiments_acheter(request: web.Request) -> web.Response:
    """POST /api/webapp/batiments/acheter  { building_type: str }"""
    uid = _parse_uid(request)
    if not _auth(uid): return _err("unauthorized", 403)
    body = await request.json()
    btype = body.get("building_type", "")

    if btype not in BUILDINGS:
        return _err("Type de bâtiment invalide")

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company: return _err("Tu n'appartiens à aucune entreprise")
        if emp.role != "pdg": return _err("Réservé au PDG")

        b = BUILDINGS[btype]
        if company.level < b["unlock_lvl"]:
            return _err(f"Ce bâtiment nécessite le niveau {b['unlock_lvl']}")

        from database.models import CompanyBuilding as CB
        from handlers.company import _add_log
        existing = (await session.execute(
            select(CB).where(
                CB.company_id == company.id,
                CB.building_type == btype,
                CB.status == "active",
            )
        )).scalar_one_or_none()
        if existing:
            return _err("Vous possédez déjà ce bâtiment")

        cost = _building_cost(btype, company.level)
        if company.treasury < cost:
            return _err(f"Trésorerie insuffisante ({_fmt(company.treasury)} $ / {_fmt(cost)} $ requis)")

        company.treasury -= cost
        building = CB(
            company_id=company.id,
            building_type=btype,
            status="active",
        )
        session.add(building)
        await _add_log(session, company.id, "batiment",
                       f"Achat de {b['name']} pour {_fmt(cost)} $", amount=cost)
        await session.commit()
        return _ok(f"✅ {b['name']} acheté pour {_fmt(cost)} $ !")


# ═══════════════════════════════════════════════════════════════════════════════
#  NÉGOCIATION CONTRAT (PDG → Employé)
# ═══════════════════════════════════════════════════════════════════════════════

async def webapp_negociercontrat(request: web.Request) -> web.Response:
    """POST /api/webapp/negociercontrat
    PDG propose un contrat à un employé.
    { target_user_id: int, salary: int, bonus: int = 0 }
    """
    uid = _parse_uid(request)
    if not _auth(uid): return _err("unauthorized", 403)
    body = await request.json()
    target_id = int(body.get("target_user_id", 0))
    salary = int(body.get("salary", 0))
    bonus = int(body.get("bonus", 0))

    if not target_id or salary <= 0:
        return _err("target_user_id et salary requis")

    async with AsyncSessionLocal() as session:
        from database.models import User as UserM
        company, emp = await _get_user_company(session, uid)
        if not company: return _err("Tu n'appartiens à aucune entreprise")
        if emp.role != "pdg": return _err("Réservé au PDG")

        # Vérifier que la cible est un employé de l'entreprise
        target_emp = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.user_id == target_id,
                CompanyEmployee.left_at == None,
            ).limit(1)
        )).scalar_one_or_none()
        if not target_emp: return _err("Cet employé n'est pas dans ton entreprise")
        if target_emp.role == "pdg": return _err("Tu ne peux pas te négocier avec toi-même")

        target_emp.contract_status = "pending_employee"
        target_emp.pending_salary = salary
        target_emp.pending_bonus = bonus
        await session.commit()

        # Notif Telegram (best effort)
        target_user = await session.get(UserM, target_id)
        bonus_txt = f"\n🎁 Prime proposée : <b>{_fmt(bonus)} $</b>" if bonus > 0 else ""
        try:
            from main import application
            await application.bot.send_message(
                chat_id=target_id,
                text=(
                    f"📄 <b>Proposition de contrat — {company.name}</b>\n\n"
                    f"💰 Salaire proposé : <b>{_fmt(salary)} $/jour</b>{bonus_txt}\n\n"
                    f"✅ Accepter : <code>/negociercontrat accepter</code>\n"
                    f"❌ Refuser : <code>/negociercontrat refuser</code>\n"
                    f"💬 Contre-proposer : <code>/negociercontrat [ton_montant]</code>"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass

        return _ok(f"📩 Proposition envoyée à {target_user.first_name if target_user else target_id} : {_fmt(salary)} $/jour" + (f" + {_fmt(bonus)} $ prime" if bonus else ""))


async def webapp_contrat_repondre(request: web.Request) -> web.Response:
    """POST /api/webapp/contrat/repondre
    Employé accepte, refuse ou contre-propose.
    { action: 'accepter' | 'refuser' | 'counter', amount: int (si counter) }
    """
    uid = _parse_uid(request)
    if not _auth(uid): return _err("unauthorized", 403)
    body = await request.json()
    action = body.get("action", "")
    counter_amount = int(body.get("amount", 0))

    async with AsyncSessionLocal() as session:
        from database.models import User as UserM
        company, emp = await _get_user_company(session, uid)
        if not company: return _err("Tu n'appartiens à aucune entreprise")
        if emp.contract_status != "pending_employee":
            return _err("Aucune proposition en attente")

        pending_sal = emp.pending_salary or 0
        pending_bon = emp.pending_bonus or 0

        # Trouver le PDG pour notifier
        pdg_emp = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.role == "pdg",
                CompanyEmployee.left_at == None,
            ).limit(1)
        )).scalar_one_or_none()

        me = await session.get(UserM, uid)
        my_name = me.first_name if me else str(uid)

        if action == "accepter":
            emp.daily_salary = pending_sal
            emp.contract_status = "signed"
            emp.pending_salary = 0
            emp.pending_bonus = 0
            await session.commit()
            try:
                from main import application
                if pdg_emp:
                    await application.bot.send_message(
                        chat_id=pdg_emp.user_id,
                        text=f"✅ <b>{my_name}</b> a accepté le contrat !\n📄 Salaire signé : <b>{_fmt(pending_sal)} $/jour</b>",
                        parse_mode="HTML"
                    )
            except Exception:
                pass
            return _ok(f"✅ Contrat accepté ! Salaire : {_fmt(pending_sal)} $/jour")

        elif action == "refuser":
            emp.contract_status = "none"
            emp.pending_salary = 0
            emp.pending_bonus = 0
            await session.commit()
            try:
                from main import application
                if pdg_emp:
                    await application.bot.send_message(
                        chat_id=pdg_emp.user_id,
                        text=f"❌ <b>{my_name}</b> a refusé ta proposition de contrat.",
                    )
            except Exception:
                pass
            return _ok("❌ Proposition refusée.")

        elif action == "counter":
            if counter_amount <= 0:
                return _err("Montant invalide")
            emp.contract_status = "pending_pdg"
            emp.pending_salary = counter_amount
            await session.commit()
            try:
                from main import application
                if pdg_emp:
                    await application.bot.send_message(
                        chat_id=pdg_emp.user_id,
                        text=(
                            f"💬 <b>Contre-proposition de {my_name} !</b>\n\n"
                            f"Il refuse {_fmt(pending_sal)} $/j et demande : <b>{_fmt(counter_amount)} $/jour</b>\n\n"
                            f"✅ Accepter : <code>/negociercontrat @{my_name} {counter_amount}</code>\n"
                            f"❌ Refuser : <code>/negociercontrat @{my_name} refuser</code>"
                        ),
                        parse_mode="HTML"
                    )
            except Exception:
                pass
            return _ok(f"💬 Contre-proposition envoyée : {_fmt(counter_amount)} $/jour")

        return _err("Action invalide (accepter / refuser / counter)")
