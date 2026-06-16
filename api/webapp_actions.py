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
    try:
        return int(request.rel_url.query.get(key, 0))
    except Exception:
        return 0


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
    # Import dynamique pour ne pas créer de dépendance circulaire
    try:
        from api.webapp import _is_allowed
        return _is_allowed(uid)
    except Exception:
        return uid > 0  # fallback si webapp.py non dispo


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
                "sector":        c.sector,
                "sec_emoji":     sec_emoji,
                "sec_name":      sec_name,
                "level":         c.level or 1,
                "lvl_emoji":     lvl_emoji,
                "lvl_name":      lvl_name,
                "value":         _fmt(c.value),
                "value_raw":     c.value or 0,
                "treasury":      _fmt(c.treasury),
                "reputation":    round(c.reputation or 0, 1),
                "nb_emp":        nb_emp,
                "max_emp":       max_emp,
                "is_bot":        c.is_bot_company,
                "can_apply":     not already_in and not already_applied and nb_emp < max_emp,
                "already_applied": bool(already_applied),
                "already_in":    bool(already_in),
            })

    return web.json_response({"items": items, "total": total, "page": page, "pages": (total + PAGE - 1) // PAGE})


async def webapp_companies_search(request: web.Request) -> web.Response:
    """GET /api/webapp/companies/search?q=nom&user_id=xxx"""
    uid = _parse_uid(request)
    if not _auth(uid):
        return _err("unauthorized", 403)

    q = request.rel_url.query.get("q", "").strip()
    if len(q) < 2:
        return _err("Tape au moins 2 caractères")

    async with AsyncSessionLocal() as session:
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
            items.append({
                "id":        c.id,
                "name":      c.name,
                "sec_emoji": sec_emoji,
                "sec_name":  sec_name,
                "lvl_emoji": lvl_emoji,
                "lvl_name":  lvl_name,
                "value":     _fmt(c.value),
                "reputation": round(c.reputation or 0, 1),
                "nb_emp":    nb_emp,
                "max_emp":   _max_employees(c),
                "is_bot":    c.is_bot_company,
            })

    return web.json_response({"items": items})


# ═════════════════════════════════════════════════════════════════════════════
#  POSTULER
# ═════════════════════════════════════════════════════════════════════════════

async def webapp_postuler(request: web.Request) -> web.Response:
    """POST /api/webapp/postuler — body: {user_id, company_id}"""
    body = await _body(request)
    uid  = int(body.get("user_id", 0))
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
    uid  = int(body.get("user_id", 0))
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
    uid       = int(body.get("user_id", 0))
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
    uid       = int(body.get("user_id", 0))
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
    uid       = int(body.get("user_id", 0))
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
    uid       = int(body.get("user_id", 0))
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
    uid       = int(body.get("user_id", 0))
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
    uid  = int(body.get("user_id", 0))

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
    uid       = int(body.get("user_id", 0))
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
    uid    = int(body.get("user_id", 0))
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
    uid  = int(body.get("user_id", 0))
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
    uid  = int(body.get("user_id", 0))
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
    uid      = int(body.get("user_id", 0))
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
    uid      = int(body.get("user_id", 0))
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
    uid       = int(body.get("user_id", 0))
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
        await session.commit()

        await _notify(target_id,
            f"💸 <b>{sender.first_name}</b> t'a envoyé <b>{_fmt(amount)} $</b> !")

        # Notif persistante
        try:
            from api.webapp import push_db_notif as _pn
            await _pn(target_id, "💸", "Paiement reçu",
                      f"{sender.first_name} t'a envoyé {_fmt(amount)} $")
        except Exception:
            pass

    return _ok(
        f"✅ {_fmt(amount)} $ envoyés à {target.first_name} !",
        new_balance=_fmt(sender.coins),
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

        # Total cmds équipe
        total_cmds = (await session.execute(
            select(func.sum(CompanyEmployee.command_count)).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.left_at == None,
            )
        )).scalar() or 0

        now = datetime.utcnow()
        items = []
        for c in contrats:
            cmds_done = getattr(c, "cmds_done", None)
            if cmds_done is None:
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
                CompanyEmployee.left_at == None,
            )
        )).scalar() or 0

        now = datetime.utcnow()
        items = []
        for ac in rows:
            cmds_done = getattr(ac, "cmds_done", None)
            if cmds_done is None:
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
                "days_left":  max(0, deadline_left),
                "deadline":   ac.deadline_at.strftime("%d/%m à %H:%M") if ac.deadline_at else "—",
                "is_pdg":     emp.role == "pdg",
            })

    return web.json_response({"contrats": items, "company": company.name})


# ═════════════════════════════════════════════════════════════════════════════
#  SKIP ATTENTE (cooldown démission)
# ═════════════════════════════════════════════════════════════════════════════

async def webapp_skipattente(request: web.Request) -> web.Response:
    """POST /api/webapp/skipattente — body: {user_id}"""
    body = await _body(request)
    uid  = int(body.get("user_id", 0))

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
            from api.webapp import push_db_notif as _push  # noqa — import pour déclencher _ensure
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
        except Exception:
            pass  # table pas encore créée ou erreur non bloquante

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

    # Misc
    app.router.add_post("/api/webapp/skipattente",         webapp_skipattente)

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


# ═════════════════════════════════════════════════════════════════════════════
#  MARCHÉ DES OBJETS (auction_inventory)
# ═════════════════════════════════════════════════════════════════════════════

async def webapp_item_sell_expert(request: web.Request) -> web.Response:
    """POST /api/webapp/items/sell_expert — Revendre un objet au bot (50% valeur)"""
    body = await _body(request)
    uid     = int(body.get("user_id", 0))
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
    uid     = int(body.get("user_id", 0))
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
    uid     = int(body.get("user_id", 0))
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
    uid     = int(body.get("user_id", 0))
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
    uid     = int(body.get("user_id", 0))
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
