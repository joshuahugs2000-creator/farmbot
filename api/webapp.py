"""
api/webapp.py — Routes API pour la Mini App Telegram
"""
import hmac, hashlib, json
from datetime import datetime
from aiohttp import web
from sqlalchemy import select, text, func
from database.db import AsyncSessionLocal
from database.models import (
    User, BankAccount, Loan, Investment,
    CompanyEmployee, Company, CompanyShare, CompanyLog,
    CompanyApplication, CompanyShareOffer
)
from config import BOT_TOKEN, CURRENCY

# ── Constantes entreprise (miroir de handlers/company.py) ────────────────────
LEVELS = {
    1: ("🏪", "Startup",      50_000_000,    0.04, 5),
    2: ("🏢", "PME",          200_000_000,   0.06, 10),
    3: ("🏬", "Société",      500_000_000,   0.08, 50),
    4: ("🏦", "Corporation", 2_000_000_000,  0.10, 100),
    5: ("👑", "Holding",    10_000_000_000,  0.12, 200),
}
ROLE_EMOJI = {
    "stagiaire": "👷", "employe": "👷",
    "manager": "💼", "directeur": "🏦", "pdg": "👑",
}
ROLE_SHARE = {
    "stagiaire": 0.00, "employe": 0.10,
    "manager": 0.20, "directeur": 0.30, "pdg": 0.35,
}
SECTORS = {
    "tech": ("💻", "Technologie"), "finance": ("📈", "Finance"),
    "commerce": ("🛒", "Commerce"), "droit": ("⚖️", "Droit"),
    "agriculture": ("🌾", "Agriculture"), "securite": ("🛡️", "Sécurité"),
    "immobilier": ("🏗️", "Immobilier"), "sante": ("🏥", "Santé"),
}

def _level_info(lvl):
    return LEVELS.get(lvl, LEVELS[1])

# ── Accès restreint ──────────────────────────────────────────────────────────
WEBAPP_WHITELIST = {
    6227863810,   # Admin 1
    # Ajoute d'autres IDs ici
}


def _is_allowed(user_id: int) -> bool:
    return user_id in WEBAPP_WHITELIST
    """Vérifie la signature Telegram initData."""
    if not init_data:
        return False
    try:
        pairs = dict(p.split('=', 1) for p in init_data.split('&') if '=' in p)
        check_hash = pairs.pop('hash', '')
        data_check = '\n'.join(f'{k}={v}' for k, v in sorted(pairs.items()))
        secret = hmac.new(b'WebAppData', BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, check_hash)
    except Exception:
        return False


def _fmt(n):
    if not n:
        return 0
    return int(n)


async def webapp_user(request: web.Request) -> web.Response:
    """GET /api/webapp/user?user_id=xxx&init_data=xxx"""
    user_id   = request.rel_url.query.get('user_id')
    init_data = request.rel_url.query.get('init_data', '')

    if not user_id:
        return web.json_response({'error': 'missing user_id'}, status=400)

    try:
        uid = int(user_id)
    except ValueError:
        return web.json_response({'error': 'invalid user_id'}, status=400)

    if not _is_allowed(uid):
        return web.json_response({'error': 'access denied'}, status=403)

    async with AsyncSessionLocal() as session:
        # ── User ──
        user = (await session.execute(
            select(User).where(User.user_id == uid)
        )).scalar_one_or_none()

        if not user:
            return web.json_response({'error': 'user not found'}, status=404)

        # ── Banque ──
        banks = (await session.execute(
            select(BankAccount).where(BankAccount.user_id == uid)
        )).scalars().all()
        bank_total = sum(b.balance or 0 for b in banks)

        # ── Dettes ──
        loans = (await session.execute(
            select(Loan).where(Loan.user_id == uid, Loan.status == 'active')
        )).scalars().all()
        loans_total = sum(l.remaining or 0 for l in loans)

        # ── Fortune ──
        fortune_totale = (user.coins or 0) + bank_total - loans_total

        # ── Classement ──
        all_fortunes = (await session.execute(
            text("""
                SELECT u.user_id,
                       COALESCE(u.coins,0)
                       + COALESCE((SELECT SUM(b.balance) FROM bank_accounts b WHERE b.user_id=u.user_id),0)
                       - COALESCE((SELECT SUM(l.remaining) FROM loans l WHERE l.user_id=u.user_id AND l.status='active'),0)
                       AS fortune
                FROM users u
                ORDER BY fortune DESC
            """)
        )).fetchall()
        total_players = len(all_fortunes)
        rank = next((i+1 for i,r in enumerate(all_fortunes) if r[0]==uid), total_players)

        # ── Portfolio ──
        investments = (await session.execute(
            select(Investment).where(Investment.user_id == uid, Investment.status == 'active')
        )).scalars().all()
        invested = sum((i.buy_price or 0) * (i.quantity or 0) for i in investments)
        # Prix actuel approximatif (on garde buy_price comme proxy)
        current  = sum((i.buy_price or 0) * (i.quantity or 0) * 1.05 for i in investments)

        # ── Entreprise ──
        emp = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.user_id == uid,
                CompanyEmployee.left_at == None
            )
        )).scalar_one_or_none()

        company_data = {}
        if emp:
            company = await session.get(Company, emp.company_id)
            if company and company.is_active:
                # Nombre employés
                nb_emp = (await session.execute(
                    select(func.count()).where(
                        CompanyEmployee.company_id == company.id,
                        CompanyEmployee.left_at == None
                    )
                )).scalar()

                # PDG
                pdg_emp = (await session.execute(
                    select(CompanyEmployee).where(
                        CompanyEmployee.company_id == company.id,
                        CompanyEmployee.role == 'pdg',
                        CompanyEmployee.left_at == None
                    )
                )).scalar_one_or_none()
                pdg_user = None
                if pdg_emp:
                    pdg_user = await session.get(User, pdg_emp.user_id)

                daily_rate = 0.001 * (1 + (company.level - 1) * 0.002)
                rev_day    = int(company.value * daily_rate) // 30

                LEVELS = {1:('⭐','Startup'),2:('⭐⭐','PME'),3:('⭐⭐⭐','ETI'),4:('⭐⭐⭐⭐','Grande Entreprise'),5:('⭐⭐⭐⭐⭐','Multinationale')}
                lvl_emoji, lvl_name = LEVELS.get(company.level, ('⭐','—'))

                company_data = {
                    'company':            company.name,
                    'company_level':      f"{lvl_emoji} {lvl_name}",
                    'company_rep':        company.reputation,
                    'company_value':      _fmt(company.value),
                    'company_treasury':   _fmt(company.treasury),
                    'company_revenue_day': _fmt(rev_day),
                    'company_employees':  f"{nb_emp} employé(s)",
                    'company_owner':      pdg_user.first_name if pdg_user else '—',
                    'role':               emp.role.upper(),
                }

        # ── Diplômes ──
        diplomes_raw = getattr(user, 'diplomes', None) or ''
        diplomes_str = diplomes_raw if diplomes_raw else '—'

        # ── Titre ──
        from config import TITLES
        titre = '👤 Citoyen'
        family_size = 0
        karma = user.karma or 0
        for min_fam, min_karma, label in TITLES:
            if family_size >= min_fam and karma >= min_karma:
                titre = label

        payload = {
            'name':          user.first_name or '—',
            'username':      user.username or '',
            'title':         titre,
            'coins':         _fmt(user.coins),
            'bank_total':    _fmt(bank_total),
            'loans_total':   _fmt(loans_total),
            'fortune_totale':_fmt(fortune_totale),
            'karma':         karma,
            'rank':          rank,
            'total_players': total_players,
            'diplomes':      diplomes_str,
            'avatar_data':   user.avatar_data or None,
            'portfolio': {
                'invested': _fmt(invested),
                'current':  _fmt(current),
                'pnl':      _fmt(current - invested),
            },
            **company_data,
        }

    return web.json_response(payload)


async def webapp_save_avatar(request: web.Request) -> web.Response:
    """POST /api/webapp/avatar — Sauvegarde l'avatar en base de données."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid JSON'}, status=400)

    user_id_raw = body.get('user_id')
    avatar_data = body.get('avatar_data')

    if not user_id_raw or not avatar_data:
        return web.json_response({'error': 'missing user_id or avatar_data'}, status=400)

    try:
        uid = int(user_id_raw)
    except (ValueError, TypeError):
        return web.json_response({'error': 'invalid user_id'}, status=400)

    if not _is_allowed(uid):
        return web.json_response({'error': 'access denied'}, status=403)

    # Valider que avatar_data est bien du JSON sérialisable
    try:
        json_str = json.dumps(avatar_data)
    except Exception:
        return web.json_response({'error': 'invalid avatar_data'}, status=400)

    async with AsyncSessionLocal() as session:
        user = (await session.execute(
            select(User).where(User.user_id == uid)
        )).scalar_one_or_none()

        if not user:
            return web.json_response({'error': 'user not found'}, status=404)

        user.avatar_data = json_str
        await session.commit()

    return web.json_response({'ok': True})


# ── Helpers ──────────────────────────────────────────────────────────────────
async def _get_company_of_user(session, uid):
    """Retourne (Company, CompanyEmployee) ou (None, None)."""
    emp = (await session.execute(
        select(CompanyEmployee).where(
            CompanyEmployee.user_id == uid,
            CompanyEmployee.left_at == None
        )
    )).scalar_one_or_none()
    if not emp:
        return None, None
    company = await session.get(Company, emp.company_id)
    if not company or not company.is_active:
        return None, None
    return company, emp


async def webapp_company(request: web.Request) -> web.Response:
    """GET /api/webapp/company?user_id=xxx — Données complètes de l'entreprise."""
    user_id = request.rel_url.query.get('user_id')
    if not user_id:
        return web.json_response({'error': 'missing user_id'}, status=400)
    try:
        uid = int(user_id)
    except ValueError:
        return web.json_response({'error': 'invalid user_id'}, status=400)
    if not _is_allowed(uid):
        return web.json_response({'error': 'access denied'}, status=403)

    async with AsyncSessionLocal() as session:
        company, emp = await _get_company_of_user(session, uid)
        if not company:
            return web.json_response({'error': 'no_company'}, status=404)

        lvl_emoji, lvl_name, _, monthly_rate, max_emp = _level_info(company.level)
        sec_emoji, sec_name = SECTORS.get(company.sector, ("🏢", company.sector))
        daily_rev = int(company.value * monthly_rate) // 30

        # Employés
        emps_rows = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.left_at == None
            )
        )).scalars().all()

        employees = []
        for e in emps_rows:
            u = await session.get(User, e.user_id)
            activity = e.activity_since_payroll or 0
            share = ROLE_SHARE.get(e.role, 0)
            if e.role == "stagiaire" or share == 0:
                salary_est = 0
            else:
                bonus = min(0.5, activity / 40)
                salary_est = int(daily_rev * share * (1 + bonus))
            days = (datetime.utcnow() - e.joined_at).days if e.joined_at else 0
            employees.append({
                'user_id':    e.user_id,
                'name':       u.first_name if u else '?',
                'role':       e.role,
                'role_emoji': ROLE_EMOJI.get(e.role, '👤'),
                'activity':   activity,
                'salary_est': salary_est,
                'days':       days,
                'is_me':      e.user_id == uid,
            })
        employees.sort(key=lambda x: ['pdg','directeur','manager','employe','stagiaire'].index(x['role']) if x['role'] in ['pdg','directeur','manager','employe','stagiaire'] else 99)

        # Parts
        shares_rows = (await session.execute(
            select(CompanyShare).where(CompanyShare.company_id == company.id)
        )).scalars().all()
        share_price = company.value // max(company.total_shares, 1)
        my_shares = 0
        shares_list = []
        for s in shares_rows:
            u = await session.get(User, s.owner_id)
            pct = round(s.quantity / max(company.total_shares, 1) * 100, 1)
            val = s.quantity * share_price
            if s.owner_id == uid:
                my_shares = s.quantity
            shares_list.append({
                'name': u.first_name if u else '?',
                'quantity': s.quantity,
                'pct': pct,
                'value': val,
                'is_me': s.owner_id == uid,
            })
        shares_list.sort(key=lambda x: x['quantity'], reverse=True)

        # Logs (20 derniers)
        logs_rows = (await session.execute(
            select(CompanyLog)
            .where(CompanyLog.company_id == company.id)
            .order_by(CompanyLog.created_at.desc())
            .limit(20)
        )).scalars().all()
        logs = [{
            'type': l.event_type,
            'desc': l.description,
            'amount': _fmt(l.amount) if l.amount else None,
            'date': l.created_at.strftime('%d/%m %H:%M') if l.created_at else '',
        } for l in logs_rows]

        # Candidatures en attente (PDG/directeur)
        pending_apps = []
        if emp.role in ('pdg', 'directeur', 'manager'):
            apps = (await session.execute(
                select(CompanyApplication).where(
                    CompanyApplication.company_id == company.id,
                    CompanyApplication.status == 'pending'
                )
            )).scalars().all()
            for a in apps:
                u = await session.get(User, a.user_id)
                pending_apps.append({
                    'id': a.id,
                    'user_id': a.user_id,
                    'name': u.first_name if u else '?',
                    'date': a.created_at.strftime('%d/%m') if a.created_at else '',
                })

        # Offres de parts en attente (PDG)
        pending_offers = []
        if emp.role == 'pdg':
            offers = (await session.execute(
                select(CompanyShareOffer).where(
                    CompanyShareOffer.company_id == company.id,
                    CompanyShareOffer.status == 'pending'
                )
            )).scalars().all()
            for o in offers:
                u = await session.get(User, o.buyer_id)
                pending_offers.append({
                    'id': o.id,
                    'buyer_id': o.buyer_id,
                    'name': u.first_name if u else '?',
                    'quantity': o.quantity,
                    'total': _fmt(o.total_price),
                    'expires': o.expires_at.strftime('%d/%m') if o.expires_at else '',
                })

        # Mon salaire estimé
        my_share_pct = ROLE_SHARE.get(emp.role, 0)
        my_activity = emp.activity_since_payroll or 0
        if my_share_pct > 0 and emp.role != 'stagiaire':
            my_bonus = min(0.5, my_activity / 40)
            my_salary_est = int(daily_rev * my_share_pct * (1 + my_bonus))
        else:
            my_salary_est = 0

        last_pay = company.last_payroll.strftime('%d/%m à %H:%M') if company.last_payroll else 'Jamais'

        return web.json_response({
            'company': {
                'id':           company.id,
                'name':         company.name,
                'sector':       company.sector,
                'sector_emoji': sec_emoji,
                'sector_name':  sec_name,
                'level':        company.level,
                'level_emoji':  lvl_emoji,
                'level_name':   lvl_name,
                'reputation':   company.reputation,
                'value':        company.value,
                'treasury':     company.treasury,
                'daily_rev':    daily_rev,
                'share_price':  share_price,
                'total_shares': company.total_shares,
                'max_emp':      max_emp,
                'last_payroll': last_pay,
                'description':  company.description or '',
            },
            'me': {
                'role':         emp.role,
                'role_emoji':   ROLE_EMOJI.get(emp.role, '👤'),
                'activity':     my_activity,
                'salary_est':   my_salary_est,
                'my_shares':    my_shares,
                'days':         (datetime.utcnow() - emp.joined_at).days if emp.joined_at else 0,
            },
            'employees':      employees,
            'shares':         shares_list,
            'logs':           logs,
            'pending_apps':   pending_apps,
            'pending_offers': pending_offers,
        })


async def webapp_company_action(request: web.Request) -> web.Response:
    """POST /api/webapp/company/action — Actions entreprise."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid JSON'}, status=400)

    user_id_raw = body.get('user_id')
    action      = body.get('action')

    if not user_id_raw or not action:
        return web.json_response({'error': 'missing fields'}, status=400)
    try:
        uid = int(user_id_raw)
    except (ValueError, TypeError):
        return web.json_response({'error': 'invalid user_id'}, status=400)
    if not _is_allowed(uid):
        return web.json_response({'error': 'access denied'}, status=403)

    async with AsyncSessionLocal() as session:
        company, emp = await _get_company_of_user(session, uid)
        if not company:
            return web.json_response({'error': 'Vous ne faites partie d\'aucune entreprise.'}, status=400)

        # ── DÉPÔT ──────────────────────────────────────────────────────────────
        if action == 'depot':
            if emp.role not in ('pdg', 'directeur', 'manager'):
                return web.json_response({'error': 'Accès réservé au PDG / Directeur / Manager.'}, status=403)
            amount = int(body.get('amount', 0))
            if amount <= 0:
                return web.json_response({'error': 'Montant invalide.'}, status=400)
            user = await session.get(User, uid)
            if user.coins < amount:
                return web.json_response({'error': f'Solde insuffisant ({_fmt(user.coins)} $).'}, status=400)
            user.coins -= amount
            company.treasury += amount
            from database.models import CompanyLog
            session.add(CompanyLog(
                company_id=company.id, event_type='depot',
                description=f'{user.first_name} a déposé {_fmt(amount)} $ en caisse.',
                amount=amount
            ))
            await session.commit()
            return web.json_response({'ok': True, 'msg': f'✅ {_fmt(amount)} $ déposés en caisse !'})

        # ── RETRAIT ─────────────────────────────────────────────────────────────
        elif action == 'retrait':
            if emp.role != 'pdg':
                return web.json_response({'error': 'Réservé au PDG.'}, status=403)
            amount = int(body.get('amount', 0))
            if amount <= 0:
                return web.json_response({'error': 'Montant invalide.'}, status=400)
            if company.treasury < amount:
                return web.json_response({'error': f'Trésorerie insuffisante ({_fmt(company.treasury)} $).'}, status=400)
            user = await session.get(User, uid)
            company.treasury -= amount
            user.coins += amount
            session.add(CompanyLog(
                company_id=company.id, event_type='retrait',
                description=f'{user.first_name} (PDG) a retiré {_fmt(amount)} $ de la caisse.',
                amount=amount
            ))
            await session.commit()
            return web.json_response({'ok': True, 'msg': f'✅ {_fmt(amount)} $ retirés !'})

        # ── VERSER SALAIRES ──────────────────────────────────────────────────────
        elif action == 'versersalaires':
            if emp.role != 'pdg':
                return web.json_response({'error': 'Réservé au PDG.'}, status=403)
            # Cooldown 12h
            if company.last_payroll:
                delta = (datetime.utcnow() - company.last_payroll).total_seconds() / 3600
                if delta < 12:
                    reste = round(12 - delta, 1)
                    return web.json_response({'error': f'Cooldown : encore {reste}h avant la prochaine paie.'}, status=400)

            lvl_emoji, lvl_name, _, monthly_rate, max_emp = _level_info(company.level)
            daily_rev = int(company.value * monthly_rate) // 30
            emps_rows = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == company.id,
                    CompanyEmployee.left_at == None
                )
            )).scalars().all()

            total_needed = 0
            payroll = []
            for e in emps_rows:
                share = ROLE_SHARE.get(e.role, 0)
                if share <= 0:
                    continue
                activity = e.activity_since_payroll or 0
                bonus = min(0.5, activity / 40)
                amount = int(daily_rev * share * (1 + bonus))
                total_needed += amount
                payroll.append((e, amount))

            if company.treasury < total_needed:
                return web.json_response({'error': f'Trésorerie insuffisante. Besoin : {_fmt(total_needed)} $ / Dispo : {_fmt(company.treasury)} $.'}, status=400)

            for e, amount in payroll:
                u = await session.get(User, e.user_id)
                if u:
                    u.coins += amount
                e.activity_since_payroll = 0
            company.treasury -= total_needed
            company.last_payroll = datetime.utcnow()
            pdg_user = await session.get(User, uid)
            session.add(CompanyLog(
                company_id=company.id, event_type='versersalaires',
                description=f'{pdg_user.first_name} a versé les salaires ({_fmt(total_needed)} $ répartis sur {len(payroll)} employés).',
                amount=total_needed
            ))
            await session.commit()
            return web.json_response({'ok': True, 'msg': f'✅ Salaires versés ! Total : {_fmt(total_needed)} $ pour {len(payroll)} employés.'})

        # ── ACCEPTER CANDIDATURE ────────────────────────────────────────────────
        elif action == 'accept_app':
            if emp.role not in ('pdg', 'directeur', 'manager'):
                return web.json_response({'error': 'Accès refusé.'}, status=403)
            app_id = body.get('app_id')
            app = await session.get(CompanyApplication, app_id)
            if not app or app.company_id != company.id or app.status != 'pending':
                return web.json_response({'error': 'Candidature introuvable.'}, status=404)
            # Vérifier qu'il n'est pas déjà dans une boite
            existing = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.user_id == app.user_id,
                    CompanyEmployee.left_at == None
                )
            )).scalar_one_or_none()
            if existing:
                app.status = 'rejected'
                await session.commit()
                return web.json_response({'error': 'Ce joueur est déjà dans une entreprise.'}, status=400)
            app.status = 'accepted'
            session.add(CompanyEmployee(
                company_id=company.id, user_id=app.user_id, role='stagiaire'
            ))
            candidate = await session.get(User, app.user_id)
            pdg_user = await session.get(User, uid)
            session.add(CompanyLog(
                company_id=company.id, event_type='recrutement',
                description=f'{pdg_user.first_name} a accepté la candidature de {candidate.first_name if candidate else "?"}.'
            ))
            await session.commit()
            return web.json_response({'ok': True, 'msg': f'✅ Candidature acceptée !'})

        # ── REFUSER CANDIDATURE ─────────────────────────────────────────────────
        elif action == 'reject_app':
            if emp.role not in ('pdg', 'directeur', 'manager'):
                return web.json_response({'error': 'Accès refusé.'}, status=403)
            app_id = body.get('app_id')
            app = await session.get(CompanyApplication, app_id)
            if not app or app.company_id != company.id or app.status != 'pending':
                return web.json_response({'error': 'Candidature introuvable.'}, status=404)
            app.status = 'rejected'
            await session.commit()
            return web.json_response({'ok': True, 'msg': '❌ Candidature refusée.'})

        # ── LICENCIER ───────────────────────────────────────────────────────────
        elif action == 'fire':
            if emp.role not in ('pdg', 'directeur'):
                return web.json_response({'error': 'Réservé au PDG / Directeur.'}, status=403)
            target_id = int(body.get('target_id', 0))
            if target_id == uid:
                return web.json_response({'error': 'Vous ne pouvez pas vous licencier vous-même.'}, status=400)
            target_emp = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == company.id,
                    CompanyEmployee.user_id == target_id,
                    CompanyEmployee.left_at == None
                )
            )).scalar_one_or_none()
            if not target_emp:
                return web.json_response({'error': 'Employé introuvable.'}, status=404)
            if target_emp.role == 'pdg':
                return web.json_response({'error': 'Impossible de licencier le PDG.'}, status=400)
            target_emp.left_at = datetime.utcnow()
            target_user = await session.get(User, target_id)
            me_user = await session.get(User, uid)
            session.add(CompanyLog(
                company_id=company.id, event_type='licenciement',
                description=f'{me_user.first_name} a licencié {target_user.first_name if target_user else "?"}.'
            ))
            await session.commit()
            return web.json_response({'ok': True, 'msg': f'✅ Employé licencié.'})

        # ── PROMOUVOIR ──────────────────────────────────────────────────────────
        elif action == 'promote':
            if emp.role not in ('pdg',):
                return web.json_response({'error': 'Réservé au PDG.'}, status=403)
            target_id = int(body.get('target_id', 0))
            new_role = body.get('new_role', '')
            ROLES_ORDER = ['stagiaire', 'employe', 'manager', 'directeur']
            if new_role not in ROLES_ORDER:
                return web.json_response({'error': 'Rôle invalide.'}, status=400)
            target_emp = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == company.id,
                    CompanyEmployee.user_id == target_id,
                    CompanyEmployee.left_at == None
                )
            )).scalar_one_or_none()
            if not target_emp:
                return web.json_response({'error': 'Employé introuvable.'}, status=404)
            target_emp.role = new_role
            target_user = await session.get(User, target_id)
            me_user = await session.get(User, uid)
            session.add(CompanyLog(
                company_id=company.id, event_type='promotion',
                description=f'{me_user.first_name} a nommé {target_user.first_name if target_user else "?"} au poste de {new_role}.'
            ))
            await session.commit()
            return web.json_response({'ok': True, 'msg': f'✅ Promu {new_role} !'})

        # ── DÉMISSIONNER ────────────────────────────────────────────────────────
        elif action == 'quit':
            if emp.role == 'pdg':
                return web.json_response({'error': 'Le PDG ne peut pas démissionner sans dissoudre l\'entreprise.'}, status=400)
            emp.left_at = datetime.utcnow()
            me_user = await session.get(User, uid)
            session.add(CompanyLog(
                company_id=company.id, event_type='demission',
                description=f'{me_user.first_name} a démissionné.'
            ))
            await session.commit()
            return web.json_response({'ok': True, 'msg': '✅ Vous avez démissionné.'})

        # ── ACCEPTER OFFRE DE PARTS ─────────────────────────────────────────────
        elif action == 'accept_offer':
            if emp.role != 'pdg':
                return web.json_response({'error': 'Réservé au PDG.'}, status=403)
            offer_id = body.get('offer_id')
            offer = await session.get(CompanyShareOffer, offer_id)
            if not offer or offer.company_id != company.id or offer.status != 'pending':
                return web.json_response({'error': 'Offre introuvable.'}, status=404)
            offer.status = 'accepted'
            # Transférer les parts (depuis le PDG / owner)
            buyer_share = (await session.execute(
                select(CompanyShare).where(
                    CompanyShare.company_id == company.id,
                    CompanyShare.owner_id == offer.buyer_id
                )
            )).scalar_one_or_none()
            if buyer_share:
                buyer_share.quantity += offer.quantity
            else:
                session.add(CompanyShare(
                    company_id=company.id, owner_id=offer.buyer_id, quantity=offer.quantity
                ))
            # Retirer les parts au PDG
            pdg_share = (await session.execute(
                select(CompanyShare).where(
                    CompanyShare.company_id == company.id,
                    CompanyShare.owner_id == uid
                )
            )).scalar_one_or_none()
            if pdg_share:
                pdg_share.quantity = max(0, pdg_share.quantity - offer.quantity)
            # Payer le PDG
            pdg_user = await session.get(User, uid)
            if pdg_user:
                pdg_user.coins += offer.total_price
            # Rembourser l'escrow (déjà bloqué — on ne rembourse rien, c'est le paiement)
            buyer_user = await session.get(User, offer.buyer_id)
            session.add(CompanyLog(
                company_id=company.id, event_type='cession_parts',
                description=f'{pdg_user.first_name if pdg_user else "PDG"} a cédé {offer.quantity} parts à {buyer_user.first_name if buyer_user else "?"}.',
                amount=offer.total_price
            ))
            await session.commit()
            return web.json_response({'ok': True, 'msg': f'✅ {offer.quantity} parts cédées !'})

        # ── REFUSER OFFRE DE PARTS ──────────────────────────────────────────────
        elif action == 'reject_offer':
            if emp.role != 'pdg':
                return web.json_response({'error': 'Réservé au PDG.'}, status=403)
            offer_id = body.get('offer_id')
            offer = await session.get(CompanyShareOffer, offer_id)
            if not offer or offer.company_id != company.id or offer.status != 'pending':
                return web.json_response({'error': 'Offre introuvable.'}, status=404)
            offer.status = 'rejected'
            # Rembourser l'acheteur
            buyer_user = await session.get(User, offer.buyer_id)
            if buyer_user:
                buyer_user.coins += offer.total_price
            await session.commit()
            return web.json_response({'ok': True, 'msg': '❌ Offre refusée, montant remboursé à l\'acheteur.'})

        else:
            return web.json_response({'error': f'Action inconnue : {action}'}, status=400)


async def webapp_index(request: web.Request) -> web.Response:
    """Sert la Mini App HTML — accès restreint."""
    # Récupérer l'user_id depuis les query params (passé par Telegram initData)
    user_id_str = request.rel_url.query.get('user_id')
    if user_id_str:
        try:
            if not _is_allowed(int(user_id_str)):
                return web.Response(text="⛔ Accès refusé. Mini App en cours de développement.", status=403)
        except ValueError:
            return web.Response(text="⛔ Accès refusé.", status=403)

    import os
    path = os.path.join(os.path.dirname(__file__), '..', 'webapp', 'index.html')
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    return web.Response(text=content, content_type='text/html')


def setup_webapp_routes(app: web.Application):
    """Enregistre les routes de la Mini App."""
    app.router.add_get('/',                           webapp_index)
    app.router.add_get('/webapp',                     webapp_index)
    app.router.add_get('/api/webapp/user',            webapp_user)
    app.router.add_post('/api/webapp/avatar',         webapp_save_avatar)
    app.router.add_get('/api/webapp/company',         webapp_company)
    app.router.add_post('/api/webapp/company/action', webapp_company_action)
