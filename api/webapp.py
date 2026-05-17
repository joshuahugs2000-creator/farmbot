"""
api/webapp.py — Routes API pour la Mini App Telegram
"""
import hmac, hashlib, json
from aiohttp import web
from sqlalchemy import select, text, func
from database.db import AsyncSessionLocal
from database.models import User, BankAccount, Loan, Investment, CompanyEmployee, Company
from config import BOT_TOKEN, CURRENCY
from handlers.invest import ASSETS, CATEGORIES, _current_price, _risk_emoji

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



# ── Route : Catalogue marché ─────────────────────────────────────────────────
async def webapp_market_catalog(request: web.Request) -> web.Response:
    uid = int(request.rel_url.query.get('user_id', 0))
    if not _is_allowed(uid):
        return web.json_response({'error': 'unauthorized'}, status=403)

    catalog = {}
    for cat in CATEGORIES:
        items = []
        for asset_id, a in ASSETS.items():
            if a['category'] != cat:
                continue
            price = _current_price(asset_id)
            items.append({
                'id': asset_id,
                'name': a['name'],
                'emoji': a['emoji'],
                'category': cat,
                'risk': a['risk'],
                'risk_emoji': _risk_emoji(a['risk']),
                'desc': a['desc'],
                'price': price,
                'price_fmt': _fmt(price),
                'base_price': a['base_price'],
                'volatility': int(a['volatility'] * 100),
            })
        catalog[cat] = items

    return web.json_response({'catalog': catalog, 'categories': CATEGORIES})


# ── Route : Portfolio détaillé ───────────────────────────────────────────────
async def webapp_market_portfolio(request: web.Request) -> web.Response:
    uid = int(request.rel_url.query.get('user_id', 0))
    if not _is_allowed(uid):
        return web.json_response({'error': 'unauthorized'}, status=403)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Investment).where(Investment.user_id == uid, Investment.status == 'active')
        )
        investments = result.scalars().all()

        positions = []
        total_invested = 0
        total_current = 0
        for inv in investments:
            a = ASSETS.get(inv.asset_id, {})
            if not a:
                continue
            buy_total = inv.buy_price * inv.quantity
            cur_price = _current_price(inv.asset_id)
            cur_total = cur_price * inv.quantity
            pnl = cur_total - buy_total
            pnl_pct = round((pnl / buy_total) * 100, 1) if buy_total else 0
            total_invested += buy_total
            total_current += cur_total
            positions.append({
                'id': inv.id,
                'asset_id': inv.asset_id,
                'name': a.get('name', inv.asset_id),
                'emoji': a.get('emoji', '📊'),
                'risk': a.get('risk', 'medium'),
                'risk_emoji': _risk_emoji(a.get('risk', 'medium')),
                'quantity': inv.quantity,
                'buy_price': inv.buy_price,
                'buy_price_fmt': _fmt(inv.buy_price),
                'cur_price': cur_price,
                'cur_price_fmt': _fmt(cur_price),
                'buy_total_fmt': _fmt(buy_total),
                'cur_total_fmt': _fmt(cur_total),
                'pnl': pnl,
                'pnl_fmt': ('+' if pnl >= 0 else '') + _fmt(abs(pnl)),
                'pnl_pct': pnl_pct,
                'pnl_positive': pnl >= 0,
                'bought_at': inv.bought_at.strftime('%d/%m/%Y') if inv.bought_at else '—',
            })

        total_pnl = total_current - total_invested
        return web.json_response({
            'positions': positions,
            'summary': {
                'invested': _fmt(total_invested),
                'current': _fmt(total_current),
                'pnl': ('+' if total_pnl >= 0 else '') + _fmt(abs(total_pnl)),
                'pnl_positive': total_pnl >= 0,
                'count': len(positions),
            }
        })


# ── Route : Acheter/Vendre ────────────────────────────────────────────────────
async def webapp_market_action(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid json'}, status=400)

    uid = int(body.get('user_id', 0))
    if not _is_allowed(uid):
        return web.json_response({'error': 'unauthorized'}, status=403)

    action = body.get('action')  # 'buy' ou 'sell'
    asset_id = body.get('asset_id', '')
    qty = int(body.get('quantity', 1))

    if asset_id not in ASSETS:
        return web.json_response({'error': 'Asset inconnu'}, status=400)
    if qty < 1:
        return web.json_response({'error': 'Quantité invalide'}, status=400)

    a = ASSETS[asset_id]
    price = _current_price(asset_id)

    async with AsyncSessionLocal() as session:
        user_result = await session.execute(select(User).where(User.user_id == uid))
        user = user_result.scalar_one_or_none()
        if not user:
            return web.json_response({'error': 'Utilisateur introuvable'}, status=404)

        if action == 'buy':
            total_cost = price * qty
            if user.coins < total_cost:
                return web.json_response({'error': f'Fonds insuffisants — il te faut {_fmt(total_cost)} {CURRENCY}'})
            user.coins -= total_cost
            inv = Investment(user_id=uid, asset_id=asset_id, quantity=qty, buy_price=price)
            session.add(inv)
            await session.commit()
            return web.json_response({
                'ok': True,
                'msg': f"✅ Acheté {qty}x {a['emoji']} {a['name']} pour {_fmt(total_cost)} {CURRENCY}"
            })

        elif action == 'sell':
            inv_id = body.get('inv_id')
            if inv_id:
                result = await session.execute(
                    select(Investment).where(Investment.id == inv_id, Investment.user_id == uid, Investment.status == 'active')
                )
                inv = result.scalar_one_or_none()
                if not inv:
                    return web.json_response({'error': 'Position introuvable'})
                sell_price = _current_price(inv.asset_id)
                proceeds = sell_price * inv.quantity
                user.coins += proceeds
                inv.status = 'sold'
                inv.sell_price = sell_price
                from datetime import datetime
                inv.sold_at = datetime.utcnow()
                await session.commit()
                pnl = proceeds - inv.buy_price * inv.quantity
                return web.json_response({
                    'ok': True,
                    'msg': f"{'✅' if pnl>=0 else '⚠️'} Vendu pour {_fmt(proceeds)} {CURRENCY} (PnL: {'+' if pnl>=0 else ''}{_fmt(pnl)})"
                })
            else:
                return web.json_response({'error': 'inv_id requis pour vendre'})

        return web.json_response({'error': 'Action invalide'}, status=400)


def setup_webapp_routes(app: web.Application):
    """Enregistre les routes de la Mini App."""
    app.router.add_get('/',                       webapp_index)
    app.router.add_get('/webapp',                 webapp_index)
    app.router.add_get('/api/webapp/user',        webapp_user)
    app.router.add_post('/api/webapp/avatar',     webapp_save_avatar)
    app.router.add_get('/api/webapp/market',      webapp_market_catalog)
    app.router.add_get('/api/webapp/portfolio',   webapp_market_portfolio)
    app.router.add_post('/api/webapp/market/action', webapp_market_action)
