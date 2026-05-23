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
    # ── Banque ──────────────────────────────────────────────────────────────
    app.router.add_get('/api/webapp/bank',         webapp_bank_data)
    app.router.add_post('/api/webapp/bank/open',   webapp_bank_open)
    app.router.add_post('/api/webapp/bank/deposit',webapp_bank_deposit)
    app.router.add_post('/api/webapp/bank/withdraw',webapp_bank_withdraw)
    app.router.add_post('/api/webapp/bank/loan',   webapp_bank_loan)
    app.router.add_post('/api/webapp/bank/repay',  webapp_bank_repay)
    # ── Famille ─────────────────────────────────────────────────────────────
    app.router.add_get('/api/webapp/family', webapp_family)
    # ── Jardin ──────────────────────────────────────────────────────────────
    app.router.add_get('/api/webapp/garden',        webapp_garden)
    app.router.add_post('/api/webapp/garden/plant', webapp_garden_plant)
    app.router.add_post('/api/webapp/garden/harvest', webapp_garden_harvest)
    # ── Classement ──────────────────────────────────────────────────────────
    app.router.add_get('/api/webapp/ranking', webapp_ranking)
    # ── Crime ───────────────────────────────────────────────────────────────
    app.router.add_get('/api/webapp/crime',   webapp_crime)
    # ── Arena ───────────────────────────────────────────────────────────────
    app.router.add_get('/api/webapp/arena',   webapp_arena)
    # ── Diplômes ────────────────────────────────────────────────────────────
    app.router.add_get('/api/webapp/diplomes', webapp_diplomes)


# ══════════════════════════════════════════════════════════════════════════════
#  ÉTAPE 1 — BANQUE
# ══════════════════════════════════════════════════════════════════════════════

BANKS_DEF = {
    "bronze":   {"name":"🥉 Banque Bronze","emoji":"🥉","rank":1,"desc":"Banque populaire, accessible à tous","min_deposit":1_000,"max_deposit":100_000_000_000,"interest_rate":0.01,"max_loan":5_000_000,"loan_rate":0.08,"loan_days":7},
    "silver":   {"name":"🥈 Banque Silver","emoji":"🥈","rank":2,"desc":"Pour les épargnants sérieux","min_deposit":10_000,"max_deposit":100_000_000_000,"interest_rate":0.015,"max_loan":5_000_000,"loan_rate":0.06,"loan_days":14},
    "gold":     {"name":"🥇 Banque Gold","emoji":"🥇","rank":3,"desc":"Banque des investisseurs fortunés","min_deposit":100_000,"max_deposit":100_000_000_000,"interest_rate":0.02,"max_loan":5_000_000,"loan_rate":0.05,"loan_days":21},
    "platinum": {"name":"💠 Banque Platinum","emoji":"💠","rank":4,"desc":"Réservée aux élites financières","min_deposit":500_000,"max_deposit":100_000_000_000,"interest_rate":0.025,"max_loan":5_000_000,"loan_rate":0.04,"loan_days":30},
    "diamond":  {"name":"💎 Banque Diamond","emoji":"💎","rank":5,"desc":"La banque des milliardaires","min_deposit":2_000_000,"max_deposit":100_000_000_000,"interest_rate":0.03,"max_loan":5_000_000,"loan_rate":0.03,"loan_days":60},
}
BANK_KEYS_ORDER = ["bronze","silver","gold","platinum","diamond"]

from datetime import datetime as _dt


async def webapp_bank_data(request: web.Request) -> web.Response:
    """GET /api/webapp/bank — Comptes + prêts + catalogue banques."""
    uid = int(request.rel_url.query.get('user_id', 0))
    if not _is_allowed(uid):
        return web.json_response({'error': 'unauthorized'}, status=403)

    async with AsyncSessionLocal() as session:
        from database.models import BankAccount, Loan
        user = (await session.execute(select(User).where(User.user_id == uid))).scalar_one_or_none()
        if not user:
            return web.json_response({'error': 'user not found'}, status=404)

        accounts_raw = (await session.execute(
            select(BankAccount).where(BankAccount.user_id == uid)
        )).scalars().all()

        loans_raw = (await session.execute(
            select(Loan).where(Loan.user_id == uid, Loan.status == 'active')
        )).scalars().all()

        accounts = []
        account_ids = set()
        for acc in accounts_raw:
            b = BANKS_DEF.get(acc.bank_id, {})
            account_ids.add(acc.bank_id)
            accounts.append({
                'bank_id':       acc.bank_id,
                'name':          b.get('name', acc.bank_id),
                'emoji':         b.get('emoji', '🏦'),
                'balance':       _fmt(acc.balance or 0),
                'balance_raw':   int(acc.balance or 0),
                'interest_rate': b.get('interest_rate', 0) * 100,
                'max_loan':      _fmt(b.get('max_loan', 0)),
                'max_loan_raw':  b.get('max_loan', 0),
            })

        loans = []
        for loan in loans_raw:
            b = BANKS_DEF.get(loan.bank_id, {})
            overdue = _dt.utcnow() > loan.due_at if loan.due_at else False
            loans.append({
                'bank_id':   loan.bank_id,
                'name':      b.get('name', loan.bank_id),
                'emoji':     b.get('emoji', '🏦'),
                'remaining': _fmt(loan.remaining or 0),
                'remaining_raw': int(loan.remaining or 0),
                'due_at':    loan.due_at.strftime('%d/%m/%Y') if loan.due_at else '—',
                'overdue':   overdue,
            })

        # Banques disponibles (toutes, pour ouvrir un compte)
        all_banks = []
        for key in BANK_KEYS_ORDER:
            b = BANKS_DEF[key]
            all_banks.append({
                'bank_id':       key,
                'name':          b['name'],
                'emoji':         b['emoji'],
                'desc':          b['desc'],
                'rank':          b['rank'],
                'min_deposit':   _fmt(b['min_deposit']),
                'min_deposit_raw': b['min_deposit'],
                'interest_rate': b['interest_rate'] * 100,
                'loan_rate':     b['loan_rate'] * 100,
                'loan_days':     b['loan_days'],
                'max_loan':      _fmt(b['max_loan']),
                'has_account':   key in account_ids,
            })

        return web.json_response({
            'wallet':   _fmt(user.coins or 0),
            'wallet_raw': int(user.coins or 0),
            'accounts': accounts,
            'loans':    loans,
            'banks':    all_banks,
        })


async def webapp_bank_open(request: web.Request) -> web.Response:
    """POST /api/webapp/bank/open"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid json'}, status=400)

    uid     = int(body.get('user_id', 0))
    bank_id = body.get('bank_id', '')
    if not _is_allowed(uid):
        return web.json_response({'error': 'unauthorized'}, status=403)
    if bank_id not in BANKS_DEF:
        return web.json_response({'error': 'Banque inconnue'}, status=400)

    async with AsyncSessionLocal() as session:
        from database.models import BankAccount
        existing = (await session.execute(
            select(BankAccount).where(BankAccount.user_id == uid, BankAccount.bank_id == bank_id)
        )).scalar_one_or_none()
        if existing:
            return web.json_response({'error': 'Compte déjà ouvert dans cette banque'})

        acc = BankAccount(user_id=uid, bank_id=bank_id, balance=0, last_interest=_dt.utcnow())
        session.add(acc)
        await session.commit()

    b = BANKS_DEF[bank_id]
    return web.json_response({'ok': True, 'msg': f"✅ Compte ouvert à la {b['name']} !"})


async def webapp_bank_deposit(request: web.Request) -> web.Response:
    """POST /api/webapp/bank/deposit"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid json'}, status=400)

    uid     = int(body.get('user_id', 0))
    bank_id = body.get('bank_id', '')
    amount  = int(body.get('amount', 0))
    if not _is_allowed(uid):
        return web.json_response({'error': 'unauthorized'}, status=403)
    if bank_id not in BANKS_DEF:
        return web.json_response({'error': 'Banque inconnue'}, status=400)
    if amount <= 0:
        return web.json_response({'error': 'Montant invalide'})

    b = BANKS_DEF[bank_id]
    if amount < b['min_deposit']:
        return web.json_response({'error': f"Dépôt minimum : {_fmt(b['min_deposit'])} $"})

    async with AsyncSessionLocal() as session:
        from database.models import BankAccount
        acc = (await session.execute(
            select(BankAccount).where(BankAccount.user_id == uid, BankAccount.bank_id == bank_id)
        )).scalar_one_or_none()
        if not acc:
            return web.json_response({'error': 'Ouvre un compte d\'abord'})

        user = (await session.execute(select(User).where(User.user_id == uid))).scalar_one_or_none()
        if not user or user.coins < amount:
            return web.json_response({'error': 'Solde insuffisant'})

        if acc.balance + amount > b['max_deposit']:
            return web.json_response({'error': 'Plafond de dépôt atteint'})

        await session.execute(
            text("UPDATE users SET coins = CAST(coins AS BIGINT) - CAST(:amt AS BIGINT) WHERE user_id = :uid"),
            {"amt": amount, "uid": uid}
        )
        await session.execute(
            text("UPDATE bank_accounts SET balance = CAST(balance AS BIGINT) + CAST(:amt AS BIGINT) WHERE id = :aid"),
            {"amt": amount, "aid": acc.id}
        )
        await session.commit()

    return web.json_response({'ok': True, 'msg': f"✅ Dépôt de {_fmt(amount)} $ effectué !"})


async def webapp_bank_withdraw(request: web.Request) -> web.Response:
    """POST /api/webapp/bank/withdraw"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid json'}, status=400)

    uid     = int(body.get('user_id', 0))
    bank_id = body.get('bank_id', '')
    amount  = int(body.get('amount', 0))
    if not _is_allowed(uid):
        return web.json_response({'error': 'unauthorized'}, status=403)
    if bank_id not in BANKS_DEF:
        return web.json_response({'error': 'Banque inconnue'}, status=400)
    if amount <= 0:
        return web.json_response({'error': 'Montant invalide'})

    async with AsyncSessionLocal() as session:
        from database.models import BankAccount
        acc = (await session.execute(
            select(BankAccount).where(BankAccount.user_id == uid, BankAccount.bank_id == bank_id)
        )).scalar_one_or_none()
        if not acc:
            return web.json_response({'error': 'Compte introuvable'})
        if acc.balance < amount:
            return web.json_response({'error': f"Solde insuffisant ({_fmt(acc.balance)} $ disponible)"})

        await session.execute(
            text("UPDATE bank_accounts SET balance = CAST(balance AS BIGINT) - CAST(:amt AS BIGINT) WHERE id = :aid"),
            {"amt": amount, "aid": acc.id}
        )
        await session.execute(
            text("UPDATE users SET coins = CAST(coins AS BIGINT) + CAST(:amt AS BIGINT) WHERE user_id = :uid"),
            {"amt": amount, "uid": uid}
        )
        await session.commit()

    return web.json_response({'ok': True, 'msg': f"✅ Retrait de {_fmt(amount)} $ effectué !"})


async def webapp_bank_loan(request: web.Request) -> web.Response:
    """POST /api/webapp/bank/loan"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid json'}, status=400)

    uid     = int(body.get('user_id', 0))
    bank_id = body.get('bank_id', '')
    amount  = int(body.get('amount', 0))
    if not _is_allowed(uid):
        return web.json_response({'error': 'unauthorized'}, status=403)
    if bank_id not in BANKS_DEF:
        return web.json_response({'error': 'Banque inconnue'}, status=400)
    if amount <= 0:
        return web.json_response({'error': 'Montant invalide'})

    b = BANKS_DEF[bank_id]
    if amount > b['max_loan']:
        return web.json_response({'error': f"Prêt maximum : {_fmt(b['max_loan'])} $"})

    from datetime import timedelta
    async with AsyncSessionLocal() as session:
        from database.models import BankAccount, Loan

        acc = (await session.execute(
            select(BankAccount).where(BankAccount.user_id == uid, BankAccount.bank_id == bank_id)
        )).scalar_one_or_none()
        if not acc:
            return web.json_response({'error': 'Ouvre un compte dans cette banque d\'abord'})

        existing_loan = (await session.execute(
            select(Loan).where(Loan.user_id == uid, Loan.status == 'active')
        )).scalar_one_or_none()
        if existing_loan:
            return web.json_response({'error': 'Tu as déjà un prêt actif à rembourser'})

        required = int(amount * 0.25)
        if acc.balance < required:
            return web.json_response({'error': f"Garantie insuffisante (besoin de {_fmt(required)} $ dans ce compte)"})

        interest  = int(amount * b['loan_rate'])
        total_due = amount + interest
        due_at    = _dt.utcnow() + timedelta(days=b['loan_days'])

        loan = Loan(user_id=uid, bank_id=bank_id, amount=amount, remaining=total_due,
                    interest_rate=b['loan_rate'], due_at=due_at)
        session.add(loan)
        await session.execute(
            text("UPDATE users SET coins = CAST(coins AS BIGINT) + CAST(:amt AS BIGINT) WHERE user_id = :uid"),
            {"amt": amount, "uid": uid}
        )
        await session.commit()

    return web.json_response({'ok': True, 'msg': f"✅ Prêt de {_fmt(amount)} $ accordé ! À rembourser {_fmt(total_due)} $ avant le {due_at.strftime('%d/%m/%Y')}."})


async def webapp_bank_repay(request: web.Request) -> web.Response:
    """POST /api/webapp/bank/repay"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid json'}, status=400)

    uid     = int(body.get('user_id', 0))
    bank_id = body.get('bank_id', '')
    amount  = int(body.get('amount', 0))
    if not _is_allowed(uid):
        return web.json_response({'error': 'unauthorized'}, status=403)
    if amount <= 0:
        return web.json_response({'error': 'Montant invalide'})

    async with AsyncSessionLocal() as session:
        from database.models import Loan
        loan = (await session.execute(
            select(Loan).where(Loan.user_id == uid, Loan.bank_id == bank_id, Loan.status == 'active')
        )).scalar_one_or_none()
        if not loan:
            return web.json_response({'error': 'Aucun prêt actif dans cette banque'})

        user = (await session.execute(select(User).where(User.user_id == uid))).scalar_one_or_none()
        if not user or user.coins < amount:
            return web.json_response({'error': 'Solde insuffisant'})

        pay = min(amount, loan.remaining)
        await session.execute(
            text("UPDATE users SET coins = CAST(coins AS BIGINT) - CAST(:amt AS BIGINT) WHERE user_id = :uid"),
            {"amt": pay, "uid": uid}
        )
        loan.remaining -= pay
        if loan.remaining <= 0:
            loan.status = 'paid'
            msg = f"✅ Prêt entièrement remboursé ({_fmt(pay)} $) !"
        else:
            msg = f"✅ Remboursement de {_fmt(pay)} $. Reste : {_fmt(loan.remaining)} $."
        await session.commit()

    return web.json_response({'ok': True, 'msg': msg})



# ══════════════════════════════════════════════════════════════════════════════
#  ÉTAPE 2 — FAMILLE
# ══════════════════════════════════════════════════════════════════════════════

async def webapp_family(request: web.Request) -> web.Response:
    """GET /api/webapp/family?user_id=xxx"""
    uid = int(request.rel_url.query.get('user_id', 0))
    if not _is_allowed(uid):
        return web.json_response({'error': 'unauthorized'}, status=403)

    from database.models import Relationship, RelationType

    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.user_id == uid))).scalar_one_or_none()
        if not user:
            return web.json_response({'error': 'user not found'}, status=404)

        rels = (await session.execute(
            select(Relationship).where(Relationship.user_id == uid)
        )).scalars().all()

        spouses, parents, children, friends = [], [], [], []

        for rel in rels:
            ru = (await session.execute(
                select(User).where(User.user_id == rel.related_user_id)
            )).scalar_one_or_none()
            if not ru:
                continue
            member = {
                'user_id':  ru.user_id,
                'name':     ru.first_name,
                'username': ru.username or '',
                'karma':    ru.karma or 0,
                'gender':   ru.gender or '',
            }
            if rel.relation_type == RelationType.SPOUSE:
                spouses.append(member)
            elif rel.relation_type == RelationType.PARENT:
                # Si l'autre user est mon parent → je suis l'enfant
                # La relation PARENT dans user_id = moi signifie "je suis parent de related_user_id"
                children.append(member)
            elif rel.relation_type == RelationType.FRIEND:
                friends.append(member)

        # Parents = ceux qui ont une relation PARENT pointant vers moi
        parent_rels = (await session.execute(
            select(Relationship).where(
                Relationship.related_user_id == uid,
                Relationship.relation_type == RelationType.PARENT
            )
        )).scalars().all()
        for rel in parent_rels:
            pu = (await session.execute(
                select(User).where(User.user_id == rel.user_id)
            )).scalar_one_or_none()
            if pu:
                parents.append({
                    'user_id':  pu.user_id,
                    'name':     pu.first_name,
                    'username': pu.username or '',
                    'karma':    pu.karma or 0,
                    'gender':   pu.gender or '',
                })

    return web.json_response({
        'family_name': user.family_name or '',
        'spouses':  spouses,
        'parents':  parents,
        'children': children,
        'friends':  friends,
    })


# ══════════════════════════════════════════════════════════════════════════════
#  JARDIN
# ══════════════════════════════════════════════════════════════════════════════

async def webapp_garden(request: web.Request) -> web.Response:
    """GET /api/webapp/garden?user_id=xxx&group_id=xxx"""
    uid      = int(request.rel_url.query.get('user_id', 0))
    group_id = int(request.rel_url.query.get('group_id', 0))
    if not _is_allowed(uid):
        return web.json_response({'error': 'unauthorized'}, status=403)

    from config import PLANT_TYPES, GARDEN_SLOTS
    from database.models import Garden
    from datetime import timedelta

    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.user_id == uid))).scalar_one_or_none()
        if not user:
            return web.json_response({'error': 'user not found'}, status=404)

        # Récupérer les plantes du jardin
        if group_id:
            result = await session.execute(
                select(Garden).where(Garden.user_id == uid, Garden.group_id == group_id, Garden.harvested == False)
            )
        else:
            result = await session.execute(
                select(Garden).where(Garden.user_id == uid, Garden.harvested == False)
            )
        plants = list(result.scalars().all())

        slots = []
        for slot_i in range(GARDEN_SLOTS):
            g = next((p for p in plants if p.slot == slot_i), None)
            if g:
                pt        = PLANT_TYPES.get(g.plant_type, {})
                grow_time = pt.get('grow_time', 3600)
                ready_at  = g.planted_at + timedelta(seconds=grow_time)
                now       = datetime.utcnow()
                ready     = now >= ready_at
                remaining = max(0, int((ready_at - now).total_seconds() / 60))
                slots.append({
                    'slot':       slot_i,
                    'empty':      False,
                    'plant_type': g.plant_type,
                    'emoji':      pt.get('emoji', '🌱'),
                    'value':      pt.get('value', 0),
                    'ready':      ready,
                    'remaining_min': remaining,
                    'planted_at': g.planted_at.isoformat(),
                    'garden_id':  g.id,
                })
            else:
                slots.append({'slot': slot_i, 'empty': True})

        plant_catalog = [
            {'name': k, 'emoji': v['emoji'], 'grow_min': v['grow_time']//60, 'value': v['value']}
            for k, v in PLANT_TYPES.items()
        ]

    return web.json_response({
        'coins':       user.coins,
        'slots':       slots,
        'plant_types': plant_catalog,
        'group_id':    group_id,
    })


async def webapp_garden_plant(request: web.Request) -> web.Response:
    """POST /api/webapp/garden/plant  body: {user_id, group_id, slot, plant_type}"""
    data      = await request.json()
    uid       = int(data.get('user_id', 0))
    group_id  = int(data.get('group_id', 0))
    slot      = int(data.get('slot', 0))
    plant_type = data.get('plant_type', '')
    if not _is_allowed(uid):
        return web.json_response({'error': 'unauthorized'}, status=403)

    from config import PLANT_TYPES, GARDEN_SLOTS
    from database.models import Garden

    if plant_type not in PLANT_TYPES:
        return web.json_response({'error': 'Plante inconnue'}, status=400)
    if slot < 0 or slot >= GARDEN_SLOTS:
        return web.json_response({'error': 'Slot invalide'}, status=400)

    async with AsyncSessionLocal() as session:
        existing = (await session.execute(
            select(Garden).where(Garden.user_id == uid, Garden.slot == slot, Garden.harvested == False)
        )).scalar_one_or_none()
        if existing:
            return web.json_response({'error': 'Ce slot est déjà occupé'}, status=400)

        g = Garden(user_id=uid, group_id=group_id or 0, slot=slot, plant_type=plant_type)
        session.add(g)
        await session.commit()

    pt = PLANT_TYPES[plant_type]
    return web.json_response({'ok': True, 'emoji': pt['emoji'], 'grow_min': pt['grow_time']//60})


async def webapp_garden_harvest(request: web.Request) -> web.Response:
    """POST /api/webapp/garden/harvest  body: {user_id, garden_id}"""
    data      = await request.json()
    uid       = int(data.get('user_id', 0))
    garden_id = int(data.get('garden_id', 0))
    if not _is_allowed(uid):
        return web.json_response({'error': 'unauthorized'}, status=403)

    from config import PLANT_TYPES
    from database.models import Garden
    from datetime import timedelta

    async with AsyncSessionLocal() as session:
        g = (await session.execute(
            select(Garden).where(Garden.id == garden_id, Garden.user_id == uid, Garden.harvested == False)
        )).scalar_one_or_none()
        if not g:
            return web.json_response({'error': 'Plante introuvable'}, status=404)

        pt       = PLANT_TYPES.get(g.plant_type, {})
        grow_time = pt.get('grow_time', 3600)
        ready_at = g.planted_at + timedelta(seconds=grow_time)
        if datetime.utcnow() < ready_at:
            return web.json_response({'error': 'Pas encore prête'}, status=400)

        gain    = pt.get('value', 0) * 1000
        g.harvested = True

        user = (await session.execute(select(User).where(User.user_id == uid))).scalar_one_or_none()
        if user:
            user.coins = (user.coins or 0) + gain
        await session.commit()

    return web.json_response({'ok': True, 'gain': gain, 'emoji': pt.get('emoji', '🌱')})


# ══════════════════════════════════════════════════════════════════════════════
#  CLASSEMENT
# ══════════════════════════════════════════════════════════════════════════════

async def webapp_ranking(request: web.Request) -> web.Response:
    """GET /api/webapp/ranking?user_id=xxx"""
    uid = int(request.rel_url.query.get('user_id', 0))
    if not _is_allowed(uid):
        return web.json_response({'error': 'unauthorized'}, status=403)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.is_banned == False).order_by(User.coins.desc()).limit(20)
        )
        users = list(result.scalars().all())
        ranking = []
        for i, u in enumerate(users):
            ranking.append({
                'rank':      i + 1,
                'user_id':   u.user_id,
                'name':      u.first_name,
                'username':  u.username or '',
                'coins':     u.coins or 0,
                'karma':     u.karma or 0,
                'is_me':     u.user_id == uid,
            })

        # Position du joueur s'il n'est pas dans le top 20
        my_pos = None
        my_row = None
        if not any(r['is_me'] for r in ranking):
            count_r = await session.execute(
                select(User).where(User.is_banned == False, User.coins > (
                    (await session.execute(select(User.coins).where(User.user_id == uid))).scalar_one_or_none() or 0
                ))
            )
            my_pos = len(list(count_r.scalars().all())) + 1
            me = (await session.execute(select(User).where(User.user_id == uid))).scalar_one_or_none()
            if me:
                my_row = {'rank': my_pos, 'user_id': me.user_id, 'name': me.first_name,
                          'username': me.username or '', 'coins': me.coins or 0,
                          'karma': me.karma or 0, 'is_me': True}

    return web.json_response({'ranking': ranking, 'my_row': my_row})


# ══════════════════════════════════════════════════════════════════════════════
#  CRIME
# ══════════════════════════════════════════════════════════════════════════════

async def webapp_crime(request: web.Request) -> web.Response:
    """GET /api/webapp/crime?user_id=xxx"""
    uid = int(request.rel_url.query.get('user_id', 0))
    if not _is_allowed(uid):
        return web.json_response({'error': 'unauthorized'}, status=403)

    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.user_id == uid))).scalar_one_or_none()
        if not user:
            return web.json_response({'error': 'user not found'}, status=404)

        # Vérifier prison (table raw SQL)
        in_prison = False
        prison_data = None
        try:
            r = await session.execute(
                text("SELECT * FROM crime_prison WHERE user_id = :uid"),
                {'uid': uid}
            )
            row = r.fetchone()
            if row:
                released_at = row.released_at
                if isinstance(released_at, str):
                    released_at = datetime.fromisoformat(released_at)
                if datetime.utcnow() < released_at:
                    in_prison   = True
                    minutes_left = max(0, int((released_at - datetime.utcnow()).total_seconds() / 60))
                    prison_data = {
                        'bail_amount':   row.bail_amount,
                        'minutes_left':  minutes_left,
                        'released_at':   released_at.isoformat(),
                    }
        except Exception:
            pass

        # Historique crimes récents
        crimes = []
        try:
            cr = await session.execute(
                text("SELECT * FROM crime_log WHERE user_id = :uid ORDER BY committed_at DESC LIMIT 10"),
                {'uid': uid}
            )
            for row in cr.fetchall():
                crimes.append({
                    'type':         row.crime_type if hasattr(row, 'crime_type') else '?',
                    'result':       row.result if hasattr(row, 'result') else '?',
                    'amount':       row.amount if hasattr(row, 'amount') else 0,
                    'committed_at': str(row.committed_at) if hasattr(row, 'committed_at') else '',
                })
        except Exception:
            pass

        # Sécurité
        security = 0
        try:
            sr = await session.execute(
                text("SELECT level FROM crime_security WHERE user_id = :uid"),
                {'uid': uid}
            )
            srow = sr.fetchone()
            if srow:
                security = srow.level
        except Exception:
            pass

    return web.json_response({
        'coins':      user.coins or 0,
        'in_prison':  in_prison,
        'prison':     prison_data,
        'crimes':     crimes,
        'security':   security,
    })


# ══════════════════════════════════════════════════════════════════════════════
#  ARENA
# ══════════════════════════════════════════════════════════════════════════════

async def webapp_arena(request: web.Request) -> web.Response:
    """GET /api/webapp/arena?user_id=xxx"""
    uid = int(request.rel_url.query.get('user_id', 0))
    if not _is_allowed(uid):
        return web.json_response({'error': 'unauthorized'}, status=403)

    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.user_id == uid))).scalar_one_or_none()
        if not user:
            return web.json_response({'error': 'user not found'}, status=404)

        # Historique combats
        fights = []
        for table in ('arena_cockfight_log', 'arena_ppc_log', 'arena_lancer_log'):
            try:
                r = await session.execute(
                    text(f"SELECT * FROM {table} WHERE user_id = :uid ORDER BY played_at DESC LIMIT 5"),
                    {'uid': uid}
                )
                for row in r.fetchall():
                    fights.append({
                        'type':      table.replace('arena_','').replace('_log',''),
                        'result':    row.result if hasattr(row, 'result') else '?',
                        'gain':      row.gain if hasattr(row, 'gain') else 0,
                        'played_at': str(row.played_at) if hasattr(row, 'played_at') else '',
                    })
            except Exception:
                pass

        # Stats wins/losses
        stats = {'wins': 0, 'losses': 0, 'total_gain': 0}
        try:
            r = await session.execute(
                text("SELECT COUNT(*) FROM arena_cockfight_log WHERE user_id=:uid AND result='win'"), {'uid': uid}
            )
            stats['wins'] = r.scalar() or 0
        except Exception:
            pass

    return web.json_response({
        'coins':  user.coins or 0,
        'fights': fights,
        'stats':  stats,
    })


# ══════════════════════════════════════════════════════════════════════════════
#  DIPLÔMES
# ══════════════════════════════════════════════════════════════════════════════

async def webapp_diplomes(request: web.Request) -> web.Response:
    """GET /api/webapp/diplomes?user_id=xxx"""
    uid = int(request.rel_url.query.get('user_id', 0))
    if not _is_allowed(uid):
        return web.json_response({'error': 'unauthorized'}, status=403)

    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.user_id == uid))).scalar_one_or_none()
        if not user:
            return web.json_response({'error': 'user not found'}, status=404)

        cooldown_left = 0
        if user.exam_cooldown:
            cooldown_left = max(0, int((user.exam_cooldown - datetime.utcnow()).total_seconds() / 60))

    DIPLOMES_INFO = [
        {'key': 'bac',     'label': 'Baccalauréat', 'emoji': '📜', 'prérequis': None},
        {'key': 'licence', 'label': 'Licence',       'emoji': '🎓', 'prérequis': 'bac'},
        {'key': 'master',  'label': 'Master',         'emoji': '🏛️', 'prérequis': 'licence'},
        {'key': 'mba',     'label': 'MBA',             'emoji': '💼', 'prérequis': 'master'},
    ]

    diplomes = []
    for d in DIPLOMES_INFO:
        key     = d['key']
        field   = f'diplome_{key}'
        obtained = getattr(user, field, False) or False
        diplomes.append({
            'key':       key,
            'label':     d['label'],
            'emoji':     d['emoji'],
            'obtained':  obtained,
            'prérequis': d['prérequis'],
        })

    return web.json_response({
        'diplomes':      diplomes,
        'domain':        user.diplome_domain or '',
        'cooldown_left': cooldown_left,
        'coins':         user.coins or 0,
    })
