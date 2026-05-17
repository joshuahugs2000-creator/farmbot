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


import random as _random_game
import asyncio as _asyncio_game

# ── Routes Jeux ──────────────────────────────────────────────────────────────

async def webapp_game(request: web.Request) -> web.Response:
    """POST /api/webapp/game — Jouer à un jeu depuis la webapp."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid json'}, status=400)

    uid = int(body.get('user_id', 0))
    if not _is_allowed(uid):
        return web.json_response({'error': 'unauthorized'}, status=403)

    game = body.get('game')  # 'crash_start', 'crash_cashout', 'roue', 'mines_start', 'mines_reveal', 'mines_cashout'
    mise = int(body.get('mise', 0))

    async with AsyncSessionLocal() as session:
        user_result = await session.execute(select(User).where(User.user_id == uid))
        user = user_result.scalar_one_or_none()
        if not user:
            return web.json_response({'error': 'user not found'}, status=404)

        # ── ROUE DE FORTUNE ──────────────────────────────────────────────────
        if game == 'roue':
            if mise < 1000:
                return web.json_response({'error': 'Mise minimum 1 000 $'})
            if mise > 6_600_000:
                return web.json_response({'error': 'Mise maximum 6 600 000 $'})
            if user.coins < mise:
                return web.json_response({'error': f'Fonds insuffisants (solde: {int(user.coins):,} $)'})

            WHEEL_SEGMENTS = [
                ("💀 Ruine totale",        "ruine",   0,          6),
                ("☠️ x0.1",               "mult",    0.1,        10),
                ("😭 x0.2",               "mult",    0.2,         9),
                ("😞 x0.3",               "mult",    0.3,        10),
                ("💸 x0.4",               "mult",    0.4,         9),
                ("😐 x0.5",               "mult",    0.5,         9),
                ("🔄 IDEM",               "idem",    0,          10),
                ("🙂 x0.8",               "mult",    0.8,         9),
                ("💵 +50 000 $",          "fixed",   50_000,      6),
                ("💰 x1.2",               "mult",    1.2,         8),
                ("💵 +200 000 $",         "fixed",   200_000,     6),
                ("💰 x1.5",               "mult",    1.5,        10),
                ("🎁 +500 000 $",         "fixed",   500_000,     4),
                ("🤑 x2.0",               "mult",    2.0,         7),
                ("🎯 x3.0",               "mult",    3.0,         5),
                ("💵 +1 000 000 $",       "fixed",   1_000_000,   3),
                ("⭐ x5.0",               "mult",    5.0,         3),
                ("🔥 x10.0",              "mult",    10.0,        2),
                ("🌟 MÉGA CHANCE x15.0",  "mult",    15.0,        1),
                ("💎 JACKPOT x25.0",      "mult",    25.0,        1),
            ]

            total_w = sum(s[3] for s in WHEEL_SEGMENTS)
            r = _random_game.uniform(0, total_w)
            cum = 0
            label, kind, val = WHEEL_SEGMENTS[-1][:3]
            for seg in WHEEL_SEGMENTS:
                cum += seg[3]
                if r <= cum:
                    label, kind, val = seg[0], seg[1], seg[2]
                    break

            # Trouver l'index du segment pour l'animation
            seg_index = next(i for i, s in enumerate(WHEEL_SEGMENTS) if s[0] == label)

            if kind == 'ruine':
                gain = 0
            elif kind == 'idem':
                gain = mise
            elif kind == 'fixed':
                gain = min(int(val), 100_000_000)
            else:
                gain = min(int(mise * val), 100_000_000)

            user.coins -= mise
            user.coins += gain
            await session.commit()

            profit = gain - mise
            return web.json_response({
                'ok': True,
                'label': label,
                'kind': kind,
                'val': val,
                'gain': gain,
                'profit': profit,
                'seg_index': seg_index,
                'total_segs': len(WHEEL_SEGMENTS),
            })

        # ── CRASH ─────────────────────────────────────────────────────────────
        if game == 'crash_start':
            if mise < 1000:
                return web.json_response({'error': 'Mise minimum 1 000 $'})
            if user.coins < mise:
                return web.json_response({'error': f'Fonds insuffisants'})

            # Générer le crash point côté serveur, le cacher
            r = _random_game.random()
            if r < 0.05:
                crash_point = 1.0
            else:
                crash_point = round(min(0.99 / (1 - r * 0.95), 100.0), 2)

            user.coins -= mise
            await session.commit()

            # Stocker en mémoire (clé = uid)
            _CRASH_SESSIONS[uid] = {'mise': mise, 'crash_point': crash_point}

            return web.json_response({'ok': True, 'crash_point': crash_point})

        if game == 'crash_cashout':
            mult = float(body.get('mult', 1.0))
            sess = _CRASH_SESSIONS.pop(uid, None)
            if not sess:
                return web.json_response({'error': 'Aucune partie en cours'})
            if mult >= sess['crash_point']:
                return web.json_response({'error': 'Trop tard — crash déjà survenu !', 'crashed': True})

            gain = int(sess['mise'] * mult)
            user.coins += gain
            await session.commit()
            return web.json_response({'ok': True, 'gain': gain, 'profit': gain - sess['mise']})

        # ── MINES ─────────────────────────────────────────────────────────────
        if game == 'mines_start':
            nb_mines = int(body.get('nb_mines', 3))
            if nb_mines < 1 or nb_mines > 24:
                return web.json_response({'error': 'Mines : 1-24'})
            if mise < 100:
                return web.json_response({'error': 'Mise minimum 100 $'})
            if user.coins < mise:
                return web.json_response({'error': 'Fonds insuffisants'})

            grid = ['safe'] * 25
            for pos in _random_game.sample(range(25), nb_mines):
                grid[pos] = 'mine'

            user.coins -= mise
            await session.commit()
            _MINES_SESSIONS[uid] = {'mise': mise, 'nb_mines': nb_mines, 'grid': grid, 'revealed': []}
            return web.json_response({'ok': True})

        if game == 'mines_reveal':
            idx = int(body.get('idx', 0))
            sess = _MINES_SESSIONS.get(uid)
            if not sess:
                return web.json_response({'error': 'Aucune partie'})
            if idx in sess['revealed']:
                return web.json_response({'error': 'Déjà révélé'})

            sess['revealed'].append(idx)
            is_mine = sess['grid'][idx] == 'mine'

            if is_mine:
                full_grid = sess['grid']
                _MINES_SESSIONS.pop(uid, None)
                return web.json_response({'ok': True, 'mine': True, 'grid': full_grid})

            nb_revealed = len(sess['revealed'])
            nb_mines = sess['nb_mines']
            safe = 25 - nb_mines
            try:
                prob = 1.0
                for i in range(nb_revealed):
                    prob *= (safe - i) / (25 - i)
                mult = round(0.97 / prob, 2) if prob > 0 else 1.0
            except Exception:
                mult = 1.0

            gain_now = int(sess['mise'] * mult)
            all_safe = nb_revealed >= safe
            if all_safe:
                _MINES_SESSIONS.pop(uid, None)
                user.coins += gain_now
                await session.commit()

            return web.json_response({'ok': True, 'mine': False, 'mult': mult, 'gain_now': gain_now, 'all_safe': all_safe})

        if game == 'mines_cashout':
            sess = _MINES_SESSIONS.pop(uid, None)
            if not sess:
                return web.json_response({'error': 'Aucune partie'})
            nb_revealed = len(sess['revealed'])
            nb_mines = sess['nb_mines']
            safe = 25 - nb_mines
            try:
                prob = 1.0
                for i in range(nb_revealed):
                    prob *= (safe - i) / (25 - i)
                mult = round(0.97 / prob, 2) if prob > 0 else 1.0
            except Exception:
                mult = 1.0
            gain = int(sess['mise'] * mult)
            user.coins += gain
            await session.commit()
            return web.json_response({'ok': True, 'gain': gain, 'profit': gain - sess['mise'], 'mult': mult})

        # ── APPLE OF FORTUNE ──────────────────────────────────────────────────
        if game == 'apple_start':
            if mise < _APPLE_MIN:
                return web.json_response({'error': f'Mise minimum {_APPLE_MIN:,} $'})
            if user.coins < mise:
                return web.json_response({'error': 'Fonds insuffisants'})
            user.coins -= mise
            await session.commit()
            row = _apple_gen_row(1)
            _APPLE_SESSIONS[uid] = {'mise': mise, 'level': 1, 'row': row}
            return web.json_response({'ok': True, 'level': 1, 'bombs': _apple_bombs(1)})

        if game == 'apple_pick':
            col = int(body.get('col', 0))
            sess = _APPLE_SESSIONS.get(uid)
            if not sess:
                return web.json_response({'error': 'Aucune partie'})
            row = sess['row']
            is_bomb = row[col]
            if is_bomb:
                _APPLE_SESSIONS.pop(uid, None)
                return web.json_response({'ok': True, 'bomb': True, 'row': row})
            # Safe — avance au niveau suivant
            level = sess['level']
            mult = _APPLE_MULTS.get(level, 1.0)
            gain_now = int(sess['mise'] * mult)
            if level >= 10:
                # Gagné tout !
                _APPLE_SESSIONS.pop(uid, None)
                user.coins += gain_now
                await session.commit()
                return web.json_response({'ok': True, 'bomb': False, 'won': True, 'mult': mult, 'gain': gain_now, 'row': row})
            # Prochain niveau
            new_level = level + 1
            new_row = _apple_gen_row(new_level)
            sess['level'] = new_level
            sess['row'] = new_row
            next_mult = _APPLE_MULTS.get(new_level, 1.0)
            return web.json_response({'ok': True, 'bomb': False, 'won': False, 'level': new_level, 'mult': mult, 'gain_now': gain_now, 'next_mult': next_mult, 'bombs_next': _apple_bombs(new_level), 'row': row})

        if game == 'apple_cashout':
            sess = _APPLE_SESSIONS.pop(uid, None)
            if not sess:
                return web.json_response({'error': 'Aucune partie'})
            level = sess['level'] - 1  # le level passé
            mult = _APPLE_MULTS.get(level, 1.0)
            gain = int(sess['mise'] * mult)
            user.coins += gain
            await session.commit()
            return web.json_response({'ok': True, 'gain': gain, 'profit': gain - sess['mise'], 'mult': mult})

        # ── REBET — Quitte ou Double ──────────────────────────────────────────
        if game == 'rebet_start':
            MIN_REBET = 5000
            if mise < MIN_REBET:
                return web.json_response({'error': f'Mise minimum {MIN_REBET:,} $'})
            if user.coins < mise:
                return web.json_response({'error': 'Fonds insuffisants'})
            user.coins -= mise
            await session.commit()
            _REBET_SESSIONS[uid] = {'mise': mise, 'gains': mise, 'round': 1}
            return web.json_response({'ok': True, 'gains': mise, 'round': 1})

        if game == 'rebet_action':
            action = body.get('action')  # 'cash' ou 'double'
            sess = _REBET_SESSIONS.get(uid)
            if not sess:
                return web.json_response({'error': 'Aucune partie'})
            if action == 'cash':
                gains = sess['gains']
                _REBET_SESSIONS.pop(uid, None)
                user.coins += gains
                await session.commit()
                return web.json_response({'ok': True, 'gained': gains, 'profit': gains - sess['mise']})
            elif action == 'double':
                won = _random_game.random() < 0.5
                if won:
                    sess['gains'] *= 2
                    sess['round'] += 1
                    return web.json_response({'ok': True, 'won': True, 'gains': sess['gains'], 'round': sess['round']})
                else:
                    _REBET_SESSIONS.pop(uid, None)
                    return web.json_response({'ok': True, 'won': False, 'lost': sess['gains']})
            return web.json_response({'error': 'Action invalide'})

    return web.json_response({'error': 'Jeu inconnu'}, status=400)


_CRASH_SESSIONS: dict = {}
_MINES_SESSIONS: dict = {}
_APPLE_SESSIONS: dict = {}
_REBET_SESSIONS: dict = {}

# ── Apple of Fortune constants ────────────────────────────────────────────────
_APPLE_MULTS = {1:1.50,2:2.10,3:3.20,4:4.80,5:7.00,6:12.00,7:22.00,8:45.00,9:100.00,10:500.00}
_APPLE_MIN   = 50_000

def _apple_bombs(level: int) -> int:
    if level <= 2: return 2
    if level <= 8: return 3
    return 4

def _apple_gen_row(level: int) -> list:
    import random as _r
    n = _apple_bombs(level)
    row = [False]*5
    for pos in _r.sample(range(5), n):
        row[pos] = True
    return row  # True = bombe


def setup_webapp_routes(app: web.Application):
    """Enregistre les routes de la Mini App."""
    app.router.add_get('/',                       webapp_index)
    app.router.add_get('/webapp',                 webapp_index)
    app.router.add_get('/api/webapp/user',        webapp_user)
    app.router.add_post('/api/webapp/avatar',     webapp_save_avatar)
    app.router.add_get('/api/webapp/market',      webapp_market_catalog)
    app.router.add_get('/api/webapp/portfolio',   webapp_market_portfolio)
    app.router.add_post('/api/webapp/market/action', webapp_market_action)
    app.router.add_post('/api/webapp/game',       webapp_game)
