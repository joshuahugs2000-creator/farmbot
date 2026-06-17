"""
api/webapp.py — Routes API pour la Mini App Telegram
"""
import hmac, hashlib, json
from datetime import datetime, timedelta
from urllib.parse import unquote, parse_qsl
from aiohttp import web
from sqlalchemy import select, text, func
from database.db import AsyncSessionLocal
from database.models import User, BankAccount, Loan, Investment, CompanyEmployee, Company
from config import BOT_TOKEN, CURRENCY
from handlers.invest import ASSETS, CATEGORIES, _current_price, _risk_emoji

# ── Accès restreint ──────────────────────────────────────────────────────────
WEBAPP_ADMIN_IDS = {
    6227863810,   # Admin 1
    1782278519,   # Beta tester
}

# Mini App ouverte au public
WEBAPP_OPEN = True

# Routes qui n'ont pas besoin d'authentification
# /webhook DOIT être ici : c'est Telegram qui poste les updates, pas la Mini App.
# Sans ça le middleware renvoie 401 sur /webhook et le bot ne traite plus AUCUNE commande.
_PUBLIC_PATHS = {'/', '/webapp', '/webhook', '/health', '/api/webapp/load', '/api/webapp/photo'}

def _is_allowed(user_id: int) -> bool:
    """Vérifie que le user_id correspond à l'utilisateur authentifié via initData."""
    return user_id > 0

def _is_admin(user_id: int) -> bool:
    return user_id in WEBAPP_ADMIN_IDS


def _verify_init_data(init_data: str) -> tuple[bool, int]:
    """
    Vérifie la signature Telegram initData.
    Retourne (valide, user_id) — user_id=0 si invalide.
    """
    if not init_data:
        return False, 0
    try:
        # IMPORTANT : parse_qsl décode automatiquement les valeurs percent-encoded
        # (ex: %7B%22id%22... -> {"id"...). Telegram calcule le hash sur les valeurs
        # DÉCODÉES, donc utiliser le split('&')/split('=') brut ici cassait la
        # vérification pour 100% des utilisateurs, peu importe le BOT_TOKEN.
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        check_hash = pairs.pop('hash', '')
        data_check = '\n'.join(f'{k}={v}' for k, v in sorted(pairs.items()))
        secret = hmac.new(b'WebAppData', BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, check_hash):
            return False, 0
        # Extraire le user_id depuis le champ "user" du initData (déjà décodé par parse_qsl)
        user_str = pairs.get('user', '{}')
        user_obj = json.loads(user_str)
        uid = int(user_obj.get('id', 0))
        return uid > 0, uid
    except Exception:
        return False, 0


def _get_verified_uid(request: web.Request) -> int:
    """
    Retourne le user_id vérifié posé par le middleware.
    Retourne 0 si la requête n'est pas authentifiée (ne devrait pas arriver
    sur les routes protégées car le middleware bloque avant).
    """
    return request.get('verified_uid', 0)


@web.middleware
async def webapp_auth_middleware(request: web.Request, handler):
    """
    Middleware : vérifie le header X-Telegram-Init-Data sur toutes les routes
    sauf les routes publiques. Pose request['verified_uid'] pour les handlers.
    """
    if request.path in _PUBLIC_PATHS:
        return await handler(request)

    init_data = request.headers.get('X-Telegram-Init-Data', '')
    valid, uid = _verify_init_data(init_data)

    if not valid:
        return web.json_response({'error': 'unauthorized'}, status=401)

    request['verified_uid'] = uid
    return await handler(request)


def _fmt(n):
    try:
        if not n:
            return 0
        return int(float(n))
    except Exception:
        return 0


async def webapp_user(request: web.Request) -> web.Response:
    """GET /api/webapp/user"""
    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

    async with AsyncSessionLocal() as session:
        # ── User ──
        user = (await session.execute(
            select(User).where(User.user_id == uid)
        )).scalar_one_or_none()

        if not user:
            return web.json_response({'error': 'user not found'}, status=404)

        # Mettre à jour last_seen sans bloquer la lecture
        await session.execute(
            text("UPDATE users SET last_seen = NOW() WHERE user_id = :uid"),
            {'uid': uid}
        )
        await session.commit()

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
            'coins_raw':     int(user.coins or 0),
            'bank_total':    _fmt(bank_total),
            'loans_total':   _fmt(loans_total),
            'fortune_totale':_fmt(fortune_totale),
            'karma':         karma,
            'rank':          rank,
            'total_players': total_players,
            'diplomes':      diplomes_str,
            'avatar_data':   user.avatar_data or None,
            'photo_file_id': user.photo_file_id or None,
            'photo_file_type': user.photo_file_type or None,
            'customization': user.profile_color or None,
            'portfolio': {
                'invested': _fmt(invested),
                'current':  _fmt(current),
                'pnl':      _fmt(current - invested),
            },
            **company_data,
        }

    return web.json_response(payload)


async def webapp_photo_proxy(request: web.Request) -> web.Response:
    """GET /api/webapp/photo?user_id=xxx — Proxy vers la photo de profil Telegram ou photo custom. Route publique."""
    import aiohttp as _aiohttp
    import base64 as _b64
    user_id = request.rel_url.query.get('user_id')
    if not user_id:
        return web.Response(status=404)

    try:
        uid = int(user_id)
    except ValueError:
        return web.Response(status=400)

    async with AsyncSessionLocal() as session:
        user = (await session.execute(
            select(User).where(User.user_id == uid)
        )).scalar_one_or_none()

    if not user or not user.photo_file_id:
        return web.Response(status=404)

    # ── Photo custom (upload depuis la galerie) ──
    if user.photo_file_type == 'custom_b64':
        try:
            avatar_obj = json.loads(user.avatar_data or '{}') if user.avatar_data else {}
            b64data = avatar_obj.get('_custom_photo', '')
            if not b64data:
                return web.Response(status=404)
            img_bytes = _b64.b64decode(b64data)
            return web.Response(body=img_bytes, content_type='image/jpeg',
                                headers={'Cache-Control': 'no-store, no-cache, must-revalidate'})
        except Exception:
            return web.Response(status=500)

    try:
        async with _aiohttp.ClientSession() as s:
            # Récupérer le chemin du fichier
            r = await s.get(f'https://api.telegram.org/bot{BOT_TOKEN}/getFile',
                            params={'file_id': user.photo_file_id}, timeout=_aiohttp.ClientTimeout(total=5))
            data = await r.json()
            if not data.get('ok'):
                return web.Response(status=404)
            file_path = data['result']['file_path']
            # Streamer le fichier
            img_r = await s.get(f'https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}',
                                timeout=_aiohttp.ClientTimeout(total=10))
            img_bytes = await img_r.read()
            content_type = img_r.headers.get('Content-Type', 'image/jpeg')
            return web.Response(body=img_bytes, content_type=content_type,
                                headers={'Cache-Control': 'public, max-age=3600'})
    except Exception:
        return web.Response(status=502)


async def webapp_save_avatar(request: web.Request) -> web.Response:
    """POST /api/webapp/avatar — Sauvegarde l'avatar en base de données."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid JSON'}, status=400)

    uid = _get_verified_uid(request)
    avatar_data = body.get('avatar_data')

    if not uid or not avatar_data:
        return web.json_response({'error': 'missing avatar_data'}, status=400)

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
    """Sert un skeleton vide — le vrai HTML est livré par /api/webapp/load après validation initData."""
    skeleton = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no"/>
<title>Family Bot</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;background:#0f0f1a;color:#f0f0f0;font-family:sans-serif;display:flex;align-items:center;justify-content:center}
.loader{text-align:center}
.spinner{width:48px;height:48px;border:4px solid #2a2a4a;border-top-color:#f7c948;border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 20px}
@keyframes spin{to{transform:rotate(360deg)}}
.lbl{color:#a0a0b0;font-size:13px}
</style>
</head>
<body>
<div class="loader" id="loader">
  <div class="spinner"></div>
  <div class="lbl">Chargement...</div>
</div>
<script>
(async function() {
  const tg = window.Telegram?.WebApp;
  if (tg) { tg.ready(); tg.expand(); }

  const initData = tg?.initData || '';
  const userId   = tg?.initDataUnsafe?.user?.id || null;

  // Pas dans Telegram du tout → page bientôt
  if (!initData && !userId) {
    document.getElementById('loader').innerHTML = '<div style="font-size:48px">🚧</div><div style="font-family:sans-serif;font-size:22px;font-weight:900;color:#f7c948;margin:16px 0 8px;letter-spacing:2px">BIENTÔT</div><div style="color:#a0a0b0;font-size:13px">La Mini App arrive très bientôt !</div>';
    return;
  }

  try {
    const res = await fetch('/api/webapp/load', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ init_data: initData, user_id: userId })
    });

    if (!res.ok) {
      document.getElementById('loader').innerHTML = '<div style="font-size:48px">🚧</div><div style="font-family:sans-serif;font-size:22px;font-weight:900;color:#f7c948;margin:16px 0 8px;letter-spacing:2px">BIENTÔT</div><div style="color:#a0a0b0;font-size:13px">La Mini App arrive très bientôt !</div>';
      return;
    }

    const html = await res.text();
    document.open();
    document.write(html);
    document.close();
  } catch(e) {
    document.getElementById('loader').innerHTML = '<div style="color:#ff6b6b;font-size:13px">Erreur de connexion</div>';
  }
})();
</script>
</body>
</html>"""
    return web.Response(text=skeleton, content_type='text/html')


async def webapp_load_app(request: web.Request) -> web.Response:
    """POST /api/webapp/load — valide initData Telegram et sert le vrai HTML si autorisé."""
    try:
        body = await request.json()
    except Exception:
        return web.Response(text="Bad request", status=400)

    init_data = body.get('init_data', '')
    valid, uid = _verify_init_data(init_data)

    if not valid or not uid:
        return web.json_response({'error': 'access denied'}, status=403)

    import os
    path = os.path.join(os.path.dirname(__file__), '..', 'webapp', 'index.html')
    with open(path, 'r', encoding='utf-8') as f:
        real_html = f.read()
    return web.Response(text=real_html, content_type='text/html')


def _build_locked_page() -> str:
    return """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>*{margin:0;padding:0;box-sizing:border-box}body{min-height:100vh;background:#0f0f1a;display:flex;align-items:center;justify-content:center;font-family:sans-serif}</style>
</head><body>
<div style="text-align:center;padding:32px;max-width:320px">
  <div style="font-size:72px">🚧</div>
  <div style="font-size:26px;font-weight:900;color:#f7c948;margin:16px 0 8px;letter-spacing:2px">BIENTÔT</div>
  <div style="color:#a0a0b0;font-size:14px;line-height:1.8">
    La Mini App <b style="color:#f0f0f0">Family Bot</b><br>arrive très bientôt !
  </div>
</div>
</body></html>"""



# ── Route : Catalogue marché ─────────────────────────────────────────────────
async def webapp_market_catalog(request: web.Request) -> web.Response:
    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

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
    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

    try:
        return await _webapp_market_portfolio_inner(uid)
    except Exception as e:
        import traceback
        return web.json_response({'error': str(e), 'trace': traceback.format_exc()}, status=500)


async def _webapp_market_portfolio_inner(uid: int) -> web.Response:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Investment).where(Investment.user_id == uid, Investment.status == 'active')
        )
        investments = result.scalars().all()

        positions = []
        total_invested = 0
        total_current = 0
        for inv in investments:
            try:
                a = ASSETS.get(inv.asset_id, {})
                if not a:
                    continue
                buy_price = inv.buy_price or 0
                quantity  = inv.quantity or 0
                buy_total = buy_price * quantity
                cur_price = _current_price(inv.asset_id)
                cur_total = cur_price * quantity
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
                    'quantity': quantity,
                    'buy_price': buy_price,
                    'buy_price_fmt': _fmt(buy_price),
                    'cur_price': cur_price,
                    'cur_price_fmt': _fmt(cur_price),
                    'buy_total_fmt': _fmt(buy_total),
                    'cur_total_fmt': _fmt(cur_total),
                    'pnl': pnl,
                    'pnl_fmt': ('+' if pnl >= 0 else '') + str(_fmt(abs(pnl))),
                    'pnl_pct': pnl_pct,
                    'pnl_positive': pnl >= 0,
                    'bought_at': inv.bought_at.strftime('%d/%m/%Y') if inv.bought_at else '—',
                })
            except Exception:
                continue

        total_pnl = total_current - total_invested
        return web.json_response({
            'positions': positions,
            'summary': {
                'invested': _fmt(total_invested),
                'current': _fmt(total_current),
                'pnl': ('+' if total_pnl >= 0 else '') + str(_fmt(abs(total_pnl))),
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

    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

    action = body.get('action')  # 'buy' ou 'sell'
    asset_id = body.get('asset_id', '')
    qty = int(body.get('quantity', 1))

    # Pour la vente par inv_id, pas besoin d'asset_id
    if action != 'sell' or not body.get('inv_id'):
        if asset_id not in ASSETS:
            return web.json_response({'error': 'Asset inconnu'}, status=400)
        if qty < 1:
            return web.json_response({'error': 'Quantité invalide'}, status=400)

    a = ASSETS.get(asset_id, {})
    price = _current_price(asset_id) if asset_id else 0

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

    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

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
        if game == 'crash_state':
            # Polling public — pas besoin d'auth mais on l'a déjà
            _crash_tick_lobby()
            return web.json_response(_crash_lobby_public())

        if game == 'crash_start':
            _crash_tick_lobby()
            if _CRASH_LOBBY['phase'] != 'waiting':
                return web.json_response({'error': 'Mises fermées — round en cours'})
            if uid in _CRASH_LOBBY['players']:
                return web.json_response({'error': 'Tu as déjà misé ce round'})
            if mise < 1000:
                return web.json_response({'error': 'Mise minimum 1 000 $'})
            if user.coins < mise:
                return web.json_response({'error': 'Fonds insuffisants'})

            user.coins -= mise
            await session.commit()

            display_name = (user.first_name or user.username or f'User{uid}')[:20]
            avatar = getattr(user, 'avatar_emoji', '👤') or '👤'
            _CRASH_LOBBY['players'][uid] = {
                'name': display_name,
                'avatar': avatar,
                'mise': mise,
                'cashed_out': False,
                'cashout_mult': None,
            }
            return web.json_response({'ok': True, **_crash_lobby_public()})

        if game == 'crash_cashout':
            _crash_tick_lobby()
            lobby = _CRASH_LOBBY
            p = lobby['players'].get(uid)
            if not p:
                return web.json_response({'error': 'Tu n\'as pas misé ce round'})
            if p['cashed_out']:
                return web.json_response({'error': 'Déjà cash out'})
            if lobby['phase'] != 'flying':
                return web.json_response({'error': 'Pas en vol actuellement', 'crashed': lobby['phase'] == 'crashed'})

            now = _time_mod.time()
            elapsed = now - lobby['phase_start']
            mult = _crash_compute_mult(elapsed)
            if mult >= lobby['crash_point']:
                return web.json_response({'error': 'Trop tard — crash !', 'crashed': True})

            p['cashed_out'] = True
            p['cashout_mult'] = round(mult, 2)
            gain = int(p['mise'] * mult)
            user.coins += gain
            await session.commit()
            return web.json_response({'ok': True, 'gain': gain, 'profit': gain - p['mise'], 'mult': round(mult, 2)})

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

        # ── COCKFIGHT ─────────────────────────────────────────────────────────
        if game == 'cockfight':
            mise = int(body.get('mise', 0))
            if mise < 1000:
                return web.json_response({'error': 'Mise minimum 1 000 $'})
            if user.coins < mise:
                return web.json_response({'error': 'Fonds insuffisants'})

            import random as _cr
            def _gen_coq():
                return {'hp': 100, 'atk': _cr.randint(15, 35), 'def': _cr.randint(5, 20)}

            coq1 = _gen_coq()
            coq2 = _gen_coq()
            log = []
            round_n = 1
            while coq1['hp'] > 0 and coq2['hp'] > 0 and round_n <= 10:
                # Player attacks bot
                dmg1 = max(1, coq1['atk'] - _cr.randint(0, coq2['def']))
                coq2['hp'] = max(0, coq2['hp'] - dmg1)
                log.append(f"🐔 Tour {round_n} : Ton coq inflige {dmg1} dégâts (bot : {coq2['hp']} ❤️)")
                if coq2['hp'] <= 0:
                    break
                # Bot attacks player
                dmg2 = max(1, coq2['atk'] - _cr.randint(0, coq1['def']))
                coq1['hp'] = max(0, coq1['hp'] - dmg2)
                log.append(f"🐓 Bot riposte : {dmg2} dégâts (toi : {coq1['hp']} ❤️)")
                round_n += 1

            if coq1['hp'] > coq2['hp']:
                winner = 'player'
                gain = int(mise * 1.8)
                user.coins += gain - mise
            elif coq2['hp'] > coq1['hp']:
                winner = 'bot'
                gain = 0
                user.coins -= mise
            else:
                winner = 'tie'
                gain = mise
            await session.commit()
            return web.json_response({'ok': True, 'winner': winner, 'gain': gain if winner == 'player' else 0, 'log': log})

        # ── PPC ───────────────────────────────────────────────────────────────
        if game == 'ppc':
            mise = int(body.get('mise', 0))
            choice = body.get('choice', '')
            choices = ['pierre', 'papier', 'ciseaux']
            beats = {'pierre': 'ciseaux', 'papier': 'pierre', 'ciseaux': 'papier'}
            if mise < 1000:
                return web.json_response({'error': 'Mise minimum 1 000 $'})
            if user.coins < mise:
                return web.json_response({'error': 'Fonds insuffisants'})
            if choice not in choices:
                return web.json_response({'error': 'Choix invalide'})

            import random as _pr
            bot_choice = _pr.choice(choices)
            if beats[choice] == bot_choice:
                result = 'win'
                gain = int(mise * 1.9)
                user.coins += gain - mise
            elif beats[bot_choice] == choice:
                result = 'lose'
                gain = 0
                user.coins -= mise
            else:
                result = 'tie'
                gain = mise
            await session.commit()
            return web.json_response({'ok': True, 'result': result, 'bot_choice': bot_choice, 'gain': gain if result == 'win' else 0})

        # ── LANCER DE DÉS ────────────────────────────────────────────────────
        if game == 'lancer':
            mise = int(body.get('mise', 0))
            if mise < 1000:
                return web.json_response({'error': 'Mise minimum 1 000 $'})
            if user.coins < mise:
                return web.json_response({'error': 'Fonds insuffisants'})

            import random as _lr
            p1 = [_lr.randint(1, 6), _lr.randint(1, 6)]
            p2 = [_lr.randint(1, 6), _lr.randint(1, 6)]
            t1, t2 = sum(p1), sum(p2)
            if t1 > t2:
                winner = 'player'
                gain = int(mise * 1.9)
                user.coins += gain - mise
            elif t2 > t1:
                winner = 'bot'
                gain = 0
                user.coins -= mise
            else:
                winner = 'tie'
                gain = mise
            await session.commit()
            return web.json_response({'ok': True, 'winner': winner, 'p1_dice': p1, 'p2_dice': p2,
                                      'p1_total': t1, 'p2_total': t2, 'gain': gain if winner == 'player' else 0})

        if game == 'roulette':
            import random as _rr
            mise = int(body.get('mise', 0))
            choix = str(body.get('choix', '')).lower().strip()
            if mise < 1000:
                return web.json_response({'error': 'Mise minimum 1 000 $'})
            if not choix:
                return web.json_response({'error': 'Choix manquant'})
            if user.coins < mise:
                return web.json_response({'error': 'Fonds insuffisants'})

            REDS   = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
            BLACKS = {2,4,6,8,10,11,13,15,17,20,22,24,26,28,29,31,33,35}

            numero  = _rr.randint(0, 36)
            couleur = 'rouge' if numero in REDS else ('noir' if numero in BLACKS else 'vert')

            if choix in ('rouge', 'noir'):
                win  = (choix == couleur)
                mult = 2
            elif choix in ('pair', 'impair'):
                win  = numero != 0 and ((numero % 2 == 0) == (choix == 'pair'))
                mult = 2
            elif choix.isdigit() and 0 <= int(choix) <= 36:
                win  = (numero == int(choix))
                mult = 36
            else:
                return web.json_response({'error': 'Choix invalide (rouge/noir/pair/impair/0-36)'})

            if win:
                gain = mise * (mult - 1)
                user.coins += gain
            else:
                gain = 0
                user.coins -= mise

            await session.commit()
            return web.json_response({
                'ok': True, 'numero': numero, 'couleur': couleur,
                'win': win, 'gain': gain, 'new_balance': int(user.coins)
            })

    return web.json_response({'error': 'Jeu inconnu'}, status=400)


_CRASH_SESSIONS: dict = {}   # legacy, kept for compat
_MINES_SESSIONS: dict = {}
_APPLE_SESSIONS: dict = {}
_REBET_SESSIONS: dict = {}

# ── Crash Multijoueur ─────────────────────────────────────────────────────────
import time as _time_mod

_CRASH_LOBBY: dict = {
    # phase: 'waiting' | 'flying' | 'crashed'
    'phase': 'waiting',
    'round_id': 0,
    'phase_start': 0.0,      # timestamp début de la phase courante
    'betting_duration': 10.0, # secondes d'ouverture des mises
    'crash_point': 1.0,
    'players': {},  # uid -> {name, avatar, mise, cashed_out, cashout_mult}
}
_CRASH_LOBBY_HISTORY: list = []  # derniers rounds [{round_id, crash_point, ts}]

def _crash_compute_mult(elapsed: float) -> float:
    """Même formule que le front : 1 + t*0.12 + t^1.5*0.04"""
    return round(1.0 + elapsed * 0.12 + (elapsed ** 1.5) * 0.04, 2)

def _crash_tick_lobby() -> None:
    """Avancer le lobby selon l'heure. Appelé avant chaque lecture/écriture."""
    lobby = _CRASH_LOBBY
    now = _time_mod.time()
    elapsed = now - lobby['phase_start']

    if lobby['phase'] == 'waiting':
        if elapsed >= lobby['betting_duration']:
            # Lancer le vol
            r = _random_game.random()
            if r < 0.05:
                cp = 1.0
            else:
                cp = round(min(0.99 / (1 - r * 0.95), 100.0), 2)
            lobby['crash_point'] = cp
            lobby['phase'] = 'flying'
            lobby['phase_start'] = now

    elif lobby['phase'] == 'flying':
        mult = _crash_compute_mult(elapsed)
        if mult >= lobby['crash_point']:
            # Crash !
            lobby['phase'] = 'crashed'
            lobby['phase_start'] = now
            # Ajouter à l'historique
            _CRASH_LOBBY_HISTORY.append({
                'round_id': lobby['round_id'],
                'crash_point': lobby['crash_point'],
                'ts': now,
            })
            if len(_CRASH_LOBBY_HISTORY) > 20:
                _CRASH_LOBBY_HISTORY.pop(0)

    elif lobby['phase'] == 'crashed':
        if elapsed >= 5.0:
            # Nouveau round
            lobby['round_id'] += 1
            lobby['phase'] = 'waiting'
            lobby['phase_start'] = now
            lobby['crash_point'] = 1.0
            lobby['players'] = {}

def _crash_lobby_public() -> dict:
    """Renvoie l'état public du lobby (sans crash_point si flying)."""
    lobby = _CRASH_LOBBY
    now = _time_mod.time()
    elapsed = now - lobby['phase_start']
    mult = _crash_compute_mult(elapsed) if lobby['phase'] == 'flying' else 1.0

    players_list = []
    for uid, p in lobby['players'].items():
        players_list.append({
            'name': p['name'],
            'avatar': p['avatar'],
            'mise': p['mise'],
            'cashed_out': p['cashed_out'],
            'cashout_mult': p.get('cashout_mult'),
        })

    result = {
        'phase': lobby['phase'],
        'round_id': lobby['round_id'],
        'elapsed': round(elapsed, 2),
        'mult': round(mult, 2),
        'betting_remaining': max(0.0, round(lobby['betting_duration'] - elapsed, 1)) if lobby['phase'] == 'waiting' else 0.0,
        'players': players_list,
        'history': _CRASH_LOBBY_HISTORY[-10:],
    }
    if lobby['phase'] == 'crashed':
        result['crash_point'] = lobby['crash_point']
    return result

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


async def webapp_profile_customize(request: web.Request) -> web.Response:
    """POST /api/webapp/profile/customize — Sauvegarder thème cover + badges équipés."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid json'}, status=400)

    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

    cover_theme     = body.get('cover_theme', 'purple')
    cover_fx        = body.get('cover_fx', '')
    badges_equipped = body.get('badges_equipped', [])

    async with AsyncSessionLocal() as session:
        user = (await session.execute(
            select(User).where(User.user_id == uid)
        )).scalar_one_or_none()
        if not user:
            return web.json_response({'error': 'user not found'}, status=404)

        # Lire la customisation actuelle — PRÉSERVER badges_owned
        try:
            current = json.loads(user.profile_color) if user.profile_color and user.profile_color.startswith('{') else {}
        except Exception:
            current = {}

        current['cover_theme']     = cover_theme
        current['cover_fx']        = cover_fx
        current['badges_equipped'] = badges_equipped
        # badges_owned est préservé tel quel — on ne l'écrase jamais ici

        user.profile_color = json.dumps(current)
        session.add(user)
        await session.commit()

    return web.json_response({'ok': True})


async def webapp_profile_badge_buy(request: web.Request) -> web.Response:
    """POST /api/webapp/profile/badge/buy — Acheter un badge avec des coins."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid json'}, status=400)

    uid = _get_verified_uid(request)
    badge_id = body.get('badge_id', '')
    price    = int(body.get('price', 0))

    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
    if not badge_id or price < 0:
        return web.json_response({'error': 'Paramètres invalides'}, status=400)

    VALID_BADGES = {'gold_star','diamond','fire','crown','rocket','skull','trophy','unicorn'}
    if badge_id not in VALID_BADGES:
        return web.json_response({'error': 'Badge inconnu'}, status=400)

    async with AsyncSessionLocal() as session:
        user = (await session.execute(
            select(User).where(User.user_id == uid)
        )).scalar_one_or_none()
        if not user:
            return web.json_response({'error': 'user not found'}, status=404)

        try:
            current = json.loads(user.profile_color or '{}') if user.profile_color and user.profile_color.startswith('{') else {}
        except Exception:
            current = {}

        owned = current.get('badges_owned', [])
        if badge_id in owned:
            return web.json_response({'error': 'Badge déjà possédé'})

        if (user.coins or 0) < price:
            return web.json_response({'error': f'Fonds insuffisants ({_fmt(user.coins)} $ disponible)'})

        user.coins -= price
        owned.append(badge_id)
        current['badges_owned'] = owned
        user.profile_color = json.dumps(current)
        session.add(user)
        await session.commit()

    return web.json_response({'ok': True, 'badges_owned': owned})


async def webapp_profile_photo(request: web.Request) -> web.Response:
    """POST /api/webapp/profile/photo — Sauvegarder une photo de profil personnalisée (base64).
    Stocke le contenu en tant que données brutes dans un champ dédié, servi ensuite
    par /api/webapp/photo comme fallback si photo_file_id Telegram est absent.
    """
    import base64 as _b64
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid json'}, status=400)

    uid = _get_verified_uid(request)
    photo_b64    = body.get('photo_b64', '') or body.get('photo_base64', '')  # les deux clés acceptées

    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
    if not photo_b64:
        return web.json_response({'error': 'Photo manquante'})

    # Extraire le contenu brut
    try:
        if ',' in photo_b64:
            header, data = photo_b64.split(',', 1)
        else:
            data = photo_b64
        img_bytes = _b64.b64decode(data)
    except Exception:
        return web.json_response({'error': 'Image invalide'})

    # Limite 5 Mo
    if len(img_bytes) > 5 * 1024 * 1024:
        return web.json_response({'error': 'Image trop lourde (max 5 Mo)'})

    # Stocker en base : on réutilise photo_file_id avec un préfixe spécial
    # "custom:<base64>" — le proxy /photo détecte ce préfixe et renvoie les bytes directement
    async with AsyncSessionLocal() as session:
        user = (await session.execute(
            select(User).where(User.user_id == uid)
        )).scalar_one_or_none()
        if not user:
            return web.json_response({'error': 'Utilisateur introuvable'}, status=404)

        # Stocker le base64 encodé dans photo_file_id avec préfixe custom:
        user.photo_file_id   = 'custom:' + data[:2000]  # tronqué pour la clé
        user.photo_file_type = 'custom_b64'
        # Stocker le base64 complet dans avatar_data.photo si possible (champ Text)
        try:
            existing = json.loads(user.avatar_data or '{}') if user.avatar_data else {}
        except Exception:
            existing = {}
        existing['_custom_photo'] = data  # base64 complet
        user.avatar_data = json.dumps(existing)
        session.add(user)
        await session.commit()

    return web.json_response({'ok': True})


def setup_webapp_routes(app: web.Application):
    """Enregistre les routes de la Mini App."""
    app.router.add_get('/',                       webapp_index)
    app.router.add_get('/webapp',                 webapp_index)
    app.router.add_post('/api/webapp/load',       webapp_load_app)
    app.router.add_post('/api/webapp/profile/customize',   webapp_profile_customize)
    app.router.add_post('/api/webapp/profile/badge/buy',   webapp_profile_badge_buy)
    app.router.add_post('/api/webapp/profile/photo',       webapp_profile_photo)
    app.router.add_get('/api/webapp/user',        webapp_user)
    app.router.add_get('/api/webapp/photo',       webapp_photo_proxy)
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
    # ── Journal / Événements / Annonces ─────────────────────────────────────
    app.router.add_get('/api/webapp/journal',   webapp_journal)
    app.router.add_get('/api/webapp/events',    webapp_events)
    app.router.add_get('/api/webapp/annonces',  webapp_annonces)
    app.router.add_get('/api/webapp/auctions/live',      webapp_auctions_live)
    app.router.add_get('/api/webapp/auctions/inventory', webapp_auctions_inventory)
    app.router.add_post('/api/webapp/auctions/bid',      webapp_auctions_bid)
    # ── La Salle VIP ────────────────────────────────────────────────────────
    app.router.add_get('/api/webapp/salle/status',  webapp_salle_status)
    app.router.add_post('/api/webapp/salle/pay',    webapp_salle_pay)
    app.router.add_post('/api/webapp/salle/bid',    webapp_salle_bid)
    app.router.add_get('/api/webapp/salle/history', webapp_salle_history)
    app.router.add_get('/api/webapp/company',              webapp_company_data)
    app.router.add_post('/api/webapp/company/depot',       webapp_company_depot)
    app.router.add_post('/api/webapp/company/retrait',     webapp_company_retrait)
    app.router.add_post('/api/webapp/company/payerimpots', webapp_company_payerimpots)
    app.router.add_post('/api/webapp/company/accepter',    webapp_company_accepter)
    app.router.add_post('/api/webapp/company/refuser',     webapp_company_refuser)
    app.router.add_post('/api/webapp/company/licencier',   webapp_company_licencier)
    app.router.add_post('/api/webapp/company/nommer',      webapp_company_nommer)
    app.router.add_post('/api/webapp/company/demissionner',webapp_company_demissionner)
    app.router.add_post('/api/webapp/company/acheterparts',webapp_company_acheter_parts)
    app.router.add_post('/api/webapp/company/gereroffre',  webapp_company_gerer_offre)

    app.router.add_get('/api/webapp/gains',        webapp_gains)
    app.router.add_post('/api/webapp/gains/daily', webapp_gains_daily)
    app.router.add_post('/api/webapp/gains/work',  webapp_gains_work)
    app.router.add_get('/api/webapp/diplomes',     webapp_diplomes)
    app.router.add_get('/api/webapp/online',       webapp_online_users)

    # ── Notifications persistantes ───────────────────────────────────────────
    app.router.add_post('/api/webapp/notifications/read',     webapp_notifications_read)
    app.router.add_post('/api/webapp/notifications/delete',   webapp_notifications_delete)
    app.router.add_post('/api/webapp/notifications/announce', webapp_notifications_announce)
    app.router.add_get('/api/webapp/notifications/debug',     webapp_notifications_debug)

    # ── Nouvelles routes webapp (isolées du bot) ──────────────────────────────
    from api.webapp_actions import setup_actions_routes
    setup_actions_routes(app)

    # ── Family Tickets ────────────────────────────────────────────────────────
    app.router.add_get('/api/webapp/tickets/owned',   webapp_tickets_owned)
    app.router.add_post('/api/webapp/tickets/invoice', webapp_tickets_invoice)
    app.router.add_post('/api/webapp/tickets/send',    webapp_tickets_send)



# ══════════════════════════════════════════════════════════════════════════════
#  GAINS — Daily & Work
# ══════════════════════════════════════════════════════════════════════════════

async def webapp_gains(request: web.Request) -> web.Response:
    """GET /api/webapp/gains?user_id=xxx"""
    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
    from datetime import datetime
    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.user_id == uid))).scalar_one_or_none()
        if not user:
            return web.json_response({'error': 'user not found'}, status=404)
        now = datetime.utcnow()
        now_key = now.strftime('%Y-%m-%d')
        daily_available = (user.last_daily != now_key)
        work_available = True
        work_wait_min = 0
        if user.last_work:
            from database.db import get_karma_level
            level = get_karma_level(user.karma or 0)
            base_cd = 8 * 3600
            reduction = level.get('work_red', 0) / 100
            cooldown = int(base_cd * (1 - reduction))
            elapsed = (now - user.last_work).total_seconds()
            if elapsed < cooldown:
                work_available = False
                work_wait_min = int((cooldown - elapsed) / 60)
    return web.json_response({
        'daily_available': daily_available,
        'work_available':  work_available,
        'work_wait_min':   work_wait_min,
    })


async def webapp_gains_daily(request: web.Request) -> web.Response:
    """POST /api/webapp/gains/daily"""
    data = await request.json()
    uid = int(data.get('user_id', 0))
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
    from database.db import claim_daily
    async with AsyncSessionLocal() as session:
        result = await claim_daily(session, uid)
    return web.json_response(result)


async def webapp_gains_work(request: web.Request) -> web.Response:
    """POST /api/webapp/gains/work"""
    data = await request.json()
    uid = int(data.get('user_id', 0))
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
    from database.db import claim_work
    async with AsyncSessionLocal() as session:
        result = await claim_work(session, uid)
    return web.json_response(result)


# ══════════════════════════════════════════════════════════════════════════════
#  JOURNAL — dernières actions de l'utilisateur
# ══════════════════════════════════════════════════════════════════════════════

async def webapp_journal(request: web.Request) -> web.Response:
    """GET /api/webapp/journal?user_id=xxx"""
    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

    from datetime import datetime as _ddt
    entries = []

    async with AsyncSessionLocal() as session:
        # Dernières transactions banque
        try:
            r = await session.execute(
                text("""
                    SELECT 'Dépôt banque' as title, '🏦' as emoji, amount, created_at
                    FROM bank_transactions
                    WHERE user_id = :uid
                    ORDER BY created_at DESC LIMIT 5
                """), {'uid': uid}
            )
            for row in r.fetchall():
                entries.append({
                    'emoji': '🏦',
                    'title': 'Transaction bancaire',
                    'body': f'{_fmt(row[2])} $',
                    'date': str(row[3])[:10] if row[3] else '',
                })
        except Exception:
            pass

        # Derniers investissements
        try:
            invs = (await session.execute(
                select(Investment).where(Investment.user_id == uid).order_by(Investment.bought_at.desc()).limit(5)
            )).scalars().all()
            for inv in invs:
                a = ASSETS.get(inv.asset_id, {})
                entries.append({
                    'emoji': a.get('emoji', '📈'),
                    'title': f"Achat {a.get('name', inv.asset_id)}",
                    'body': f"{inv.quantity}x à {_fmt(inv.buy_price)} $ l'unité",
                    'date': inv.bought_at.strftime('%d/%m/%Y') if inv.bought_at else '',
                })
        except Exception:
            pass

    # Trier par date décroissante
    entries.sort(key=lambda e: e.get('date', ''), reverse=True)
    return web.json_response({'entries': entries[:15]})


# ══════════════════════════════════════════════════════════════════════════════
#  ÉVÉNEMENTS — événements globaux du bot
# ══════════════════════════════════════════════════════════════════════════════

async def webapp_events(request: web.Request) -> web.Response:
    """GET /api/webapp/events?user_id=xxx"""
    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

    from datetime import datetime as _ddt
    events = []

    async with AsyncSessionLocal() as session:
        try:
            r = await session.execute(
                text("SELECT * FROM bot_events ORDER BY created_at DESC LIMIT 20")
            )
            for row in r.fetchall():
                events.append({
                    'emoji': getattr(row, 'emoji', '📅'),
                    'title': getattr(row, 'title', '—'),
                    'desc':  getattr(row, 'description', ''),
                    'date':  str(getattr(row, 'created_at', ''))[:10],
                    'color': getattr(row, 'color', 'var(--accent)'),
                })
        except Exception:
            pass

    if not events:
        # Fallback : événements génériques basés sur les données dispo
        from datetime import date
        today = date.today().strftime('%d/%m/%Y')
        events = [
            {'emoji':'🌅','title':'Bienvenue sur Family Bot','desc':'La Mini App est maintenant disponible !','date':today,'color':'#a29bfe'},
            {'emoji':'💰','title':'Marché actif','desc':'Des dizaines d\'assets disponibles à l\'achat.','date':today,'color':'#f7c948'},
        ]

    return web.json_response({'events': events})


# ══════════════════════════════════════════════════════════════════════════════
#  ANNONCES
# ══════════════════════════════════════════════════════════════════════════════

async def webapp_annonces(request: web.Request) -> web.Response:
    """GET /api/webapp/annonces?user_id=xxx"""
    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

    from datetime import date
    annonces = []

    async with AsyncSessionLocal() as session:
        try:
            r = await session.execute(
                text("SELECT * FROM announcements ORDER BY created_at DESC LIMIT 20")
            )
            for row in r.fetchall():
                annonces.append({
                    'title':     getattr(row, 'title', '—'),
                    'body':      getattr(row, 'body', ''),
                    'date':      str(getattr(row, 'created_at', ''))[:10],
                    'important': getattr(row, 'important', False),
                })
        except Exception:
            pass

    if not annonces:
        today = date.today().strftime('%d/%m/%Y')
        annonces = [
            {'title':'Mini App lancée 🎉','body':'La webapp Family Bot est désormais en ligne. Profites-en pour consulter tes stats, gérer ta banque et jouer au casino !','date':today,'important':True},
        ]

    return web.json_response({'annonces': annonces})


# ══════════════════════════════════════════════════════════════════════════════
#  ÉTAPE 1 — BANQUE
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
#  ENCHÈRES — Live + Inventaire + Mise
# ══════════════════════════════════════════════════════════════════════════════

async def webapp_auctions_live(request: web.Request) -> web.Response:
    """GET /api/webapp/auctions/live?user_id=xxx"""
    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

    auctions = []
    async with AsyncSessionLocal() as session:
        try:
            from datetime import datetime as _dt_now
            _now = _dt_now.utcnow()

            # Auto-clôturer les enchères expirées (bot restart peut rater les run_once jobs)
            await session.execute(text("""
                UPDATE auction_sessions
                SET status = 'closed'
                WHERE status = 'active' AND ends_at <= NOW()
            """))
            await session.commit()

            r = await session.execute(text("""
                SELECT id, item_id, item_name, item_emoji, rarity, true_value,
                       start_price, current_bid, leader_id, leader_name, ends_at
                FROM auction_sessions
                WHERE status = 'active' AND ends_at > NOW()
                ORDER BY ends_at ASC
                LIMIT 20
            """))
            for row in r.fetchall():
                ends_at_val = row[10]
                # Toujours sérialiser proprement sans timezone
                if ends_at_val is not None:
                    if hasattr(ends_at_val, 'strftime'):
                        ends_at_str = ends_at_val.strftime('%Y-%m-%dT%H:%M:%S')
                    else:
                        # string de Neon — normaliser
                        s = str(ends_at_val).replace(' ', 'T')
                        # Retirer offset +00:00 ou -HH:MM
                        import re as _re
                        s = _re.sub(r'[+-]\d{2}:\d{2}$', '', s)
                        s = s.rstrip('Z')
                        ends_at_str = s[:19]
                else:
                    ends_at_str = None

                auctions.append({
                    'id':          row[0],
                    'item_id':     row[1],
                    'item_name':   row[2],
                    'item_emoji':  row[3],
                    'rarity':      row[4],
                    'start_price': row[6],
                    'current_bid': row[7],
                    'leader_id':   row[8],
                    'leader_name': row[9],
                    'ends_at':     ends_at_str,
                })
        except Exception as e:
            return web.json_response({'auctions': [], 'error': str(e)})

    return web.json_response({'auctions': auctions})


async def webapp_auctions_inventory(request: web.Request) -> web.Response:
    """GET /api/webapp/auctions/inventory?user_id=xxx"""
    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

    items = []
    async with AsyncSessionLocal() as session:
        try:
            r = await session.execute(text("""
                SELECT id, item_id, item_name, item_emoji, rarity, true_value, acquired_at, for_sale, sale_price
                FROM auction_inventory
                WHERE user_id = :uid
                ORDER BY acquired_at DESC
                LIMIT 50
            """), {'uid': uid})
            for row in r.fetchall():
                items.append({
                    'id':          row[0],
                    'item_id':     row[1],
                    'item_name':   row[2],
                    'item_emoji':  row[3],
                    'rarity':      row[4],
                    'true_value':  row[5],
                    'acquired_at': str(row[6])[:10] if row[6] else '',
                    'for_sale':    bool(row[7]),
                    'sale_price':  row[8],
                })
        except Exception as e:
            return web.json_response({'items': [], 'error': str(e)})

    return web.json_response({'items': items})


async def webapp_auctions_bid(request: web.Request) -> web.Response:
    """POST /api/webapp/auctions/bid  body: {user_id, auction_id, amount}"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'ok': False, 'error': 'JSON invalide'}, status=400)

    uid = _get_verified_uid(request)
    auction_id = int(body.get('auction_id', 0))
    amount     = int(body.get('amount', 0))

    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
    if amount <= 0:
        return web.json_response({'ok': False, 'error': 'Montant invalide'})

    async with AsyncSessionLocal() as session:
        try:
            r = await session.execute(text("""
                SELECT id, current_bid, leader_id, status
                FROM auction_sessions WHERE id = :aid
            """), {'aid': auction_id})
            row = r.fetchone()
            if not row:
                return web.json_response({'ok': False, 'error': 'Enchère introuvable'})
            _, current_bid, leader_id, status = row
            if status != 'active':
                return web.json_response({'ok': False, 'error': 'Enchère terminée'})
            if leader_id == uid:
                return web.json_response({'ok': False, 'error': 'Tu mènes déjà !'})
            if amount <= current_bid:
                return web.json_response({'ok': False, 'error': f'Mise trop basse (min {current_bid + 1} $)'})

            # Récupérer le nom du leader
            ru = await session.execute(text("SELECT username FROM users WHERE user_id = :uid"), {'uid': uid})
            row_u = ru.fetchone()
            leader_name = row_u[0] if row_u else str(uid)

            # Vérifier les fonds
            rc = await session.execute(text("SELECT coins FROM users WHERE user_id = :uid"), {'uid': uid})
            row_c = rc.fetchone()
            if not row_c or row_c[0] < amount:
                return web.json_response({'ok': False, 'error': 'Fonds insuffisants'})

            # Débiter + update enchère
            await session.execute(text("UPDATE users SET coins = coins - :amt WHERE user_id = :uid"), {'amt': amount, 'uid': uid})
            # Rembourser l'ancien leader
            if leader_id:
                await session.execute(text("UPDATE users SET coins = coins + :amt WHERE user_id = :lid"), {'amt': current_bid, 'lid': leader_id})
            await session.execute(text("""
                UPDATE auction_sessions
                SET current_bid = :amt, leader_id = :uid, leader_name = :name
                WHERE id = :aid
            """), {'amt': amount, 'uid': uid, 'name': leader_name, 'aid': auction_id})
            await session.commit()
            return web.json_response({'ok': True})
        except Exception as e:
            await session.rollback()
            return web.json_response({'ok': False, 'error': str(e)})


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
    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

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

    uid = _get_verified_uid(request)
    bank_id = body.get('bank_id', '')
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
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

    uid = _get_verified_uid(request)
    bank_id = body.get('bank_id', '')
    amount  = int(body.get('amount', 0))
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
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

    uid = _get_verified_uid(request)
    bank_id = body.get('bank_id', '')
    amount  = int(body.get('amount', 0))
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
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

    uid = _get_verified_uid(request)
    bank_id = body.get('bank_id', '')
    amount  = int(body.get('amount', 0))
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
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

    uid = _get_verified_uid(request)
    bank_id = body.get('bank_id', '')
    amount  = int(body.get('amount', 0))
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
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
    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

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
    uid = _get_verified_uid(request)
    group_id = int(request.rel_url.query.get('group_id', 0))
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

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
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

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
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

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
    """GET /api/webapp/ranking?user_id=xxx&cat=coins|family|company"""
    uid = _get_verified_uid(request)
    cat = request.rel_url.query.get('cat', 'coins')
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

    async with AsyncSessionLocal() as session:
        ranking = []
        my_row = None

        if cat == 'coins':
            result = await session.execute(
                select(User).where(User.is_banned == False).order_by(User.coins.desc()).limit(20)
            )
            users = list(result.scalars().all())
            for i, u in enumerate(users):
                ranking.append({
                    'rank': i + 1, 'user_id': u.user_id,
                    'name': u.first_name, 'username': u.username or '',
                    'value': u.coins or 0, 'is_me': u.user_id == uid,
                })
            if not any(r['is_me'] for r in ranking):
                me = (await session.execute(select(User).where(User.user_id == uid))).scalar_one_or_none()
                if me:
                    cnt = (await session.execute(
                        select(func.count()).where(User.is_banned == False, User.coins > (me.coins or 0))
                    )).scalar() or 0
                    my_row = {'rank': cnt + 1, 'user_id': me.user_id, 'name': me.first_name,
                              'username': me.username or '', 'value': me.coins or 0, 'is_me': True}

        elif cat == 'family':
            # Classement par taille de famille
            rows = (await session.execute(
                text("""
                    SELECT u.user_id, u.first_name, u.username,
                           COUNT(r.id) AS fam_size
                    FROM users u
                    LEFT JOIN relationships r ON r.user_id = u.user_id
                    WHERE u.is_banned = false
                    GROUP BY u.user_id, u.first_name, u.username
                    ORDER BY fam_size DESC
                    LIMIT 20
                """)
            )).fetchall()
            for i, row in enumerate(rows):
                ranking.append({
                    'rank': i + 1, 'user_id': row[0],
                    'name': row[1] or '—', 'username': row[2] or '',
                    'value': row[3] or 0, 'is_me': row[0] == uid,
                })

        elif cat == 'company':
            # Classement entreprises par valeur
            result = await session.execute(
                select(Company).where(Company.is_active == True).order_by(Company.value.desc()).limit(20)
            )
            companies = list(result.scalars().all())
            for i, co in enumerate(companies):
                # Trouver le PDG
                pdg_emp = (await session.execute(
                    select(CompanyEmployee).where(
                        CompanyEmployee.company_id == co.id,
                        CompanyEmployee.role == 'pdg',
                        CompanyEmployee.left_at == None
                    )
                )).scalar_one_or_none()
                pdg_name = '—'
                is_me = False
                if pdg_emp:
                    pdg_user = await session.get(User, pdg_emp.user_id)
                    if pdg_user:
                        pdg_name = pdg_user.first_name
                        is_me = pdg_emp.user_id == uid
                ranking.append({
                    'rank': i + 1, 'user_id': co.id,
                    'name': co.name, 'username': f'PDG: {pdg_name}',
                    'value': co.value or 0, 'is_me': is_me,
                })

    return web.json_response({'ranking': ranking, 'my_row': my_row})


# ══════════════════════════════════════════════════════════════════════════════
#  CRIME
# ══════════════════════════════════════════════════════════════════════════════

async def webapp_crime(request: web.Request) -> web.Response:
    """GET /api/webapp/crime?user_id=xxx"""
    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

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
    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

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
    import traceback as _tb
    try:
        uid = _get_verified_uid(request)
        if not uid:
            return web.json_response({'error': 'unauthorized'}, status=401)

        DIPLOMES_INFO = [
            {'key': 'bac',     'label': 'Baccalaureat', 'emoji': '📜', 'prerequis': None},
            {'key': 'licence', 'label': 'Licence',       'emoji': '🎓', 'prerequis': 'bac'},
            {'key': 'master',  'label': 'Master',        'emoji': '🏛', 'prerequis': 'licence'},
            {'key': 'mba',     'label': 'MBA',           'emoji': '💼', 'prerequis': 'master'},
        ]

        async with AsyncSessionLocal() as session:
            user = (await session.execute(select(User).where(User.user_id == uid))).scalar_one_or_none()
            if not user:
                return web.json_response({'error': 'user not found'}, status=404)

            cooldown_left = 0
            if user.exam_cooldown:
                from datetime import datetime as _dt_now
                cooldown_left = max(0, int((user.exam_cooldown - _dt_now.utcnow()).total_seconds() / 60))

            diplomes = []
            for d in DIPLOMES_INFO:
                key      = d['key']
                field    = f'diplome_{key}'
                obtained = bool(getattr(user, field, False))
                diplomes.append({
                    'key':       key,
                    'label':     d['label'],
                    'emoji':     d['emoji'],
                    'obtained':  obtained,
                    'prerequis': d['prerequis'],
                })

            payload = {
                'diplomes':      diplomes,
                'domain':        user.diplome_domain or '',
                'cooldown_left': cooldown_left,
                'coins':         int(user.coins or 0),
            }

        return web.json_response(payload)

    except Exception as _e:
        return web.json_response({'error': 'DIPLOMES_ERR: ' + str(_e), 'trace': _tb.format_exc()[-600:]})



# ══════════════════════════════════════════════════════════════════════════════
#  ENTREPRISE — système complet
# ══════════════════════════════════════════════════════════════════════════════
from database.models import TaxRecord, BureauContrat, CompanyApplication, CompanyAutoContract, CompanyShareOffer


async def _get_user_company(session, uid: int):
    """Retourne (Company, CompanyEmployee) pour l utilisateur, ou (None, None)."""
    emp = (await session.execute(
        select(CompanyEmployee).where(
            CompanyEmployee.user_id == uid,
            CompanyEmployee.left_at == None,
        )
    )).scalar_one_or_none()
    if not emp:
        return None, None
    company = await session.get(Company, emp.company_id)
    if not company or not company.is_active:
        return None, None
    return company, emp


async def webapp_company_data(request: web.Request) -> web.Response:
    """GET /api/webapp/company — données complètes entreprise"""
    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

    async with AsyncSessionLocal() as session:
        # Trouver l'entreprise du user (PDG ou employé)
        emp = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.user_id == uid,
                CompanyEmployee.left_at == None,
            ).order_by(
                CompanyEmployee.role == 'pdg',
            ).limit(1)
        )).scalar_one_or_none()

        if not emp:
            return web.json_response({'company': None})

        company = await session.get(Company, emp.company_id)
        if not company or not company.is_active:
            return web.json_response({'company': None})

        # Employés
        all_emps = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.left_at == None,
            )
        )).scalars().all()

        employees = []
        for e in all_emps:
            eu = await session.get(__import__('database.models', fromlist=['User']).User, e.user_id)
            employees.append({
                'user_id':     e.user_id,
                'name':        eu.first_name if eu else '—',
                'username':    eu.username if eu else '',
                'role':        e.role,
                'cmd_count':   e.command_count or 0,
                'joined_at':   e.joined_at.strftime('%d/%m/%Y') if e.joined_at else '—',
                'is_me':       e.user_id == uid,
            })

        # Parts
        parts_data = []
        try:
            from database.models import CompanyShare
            shares = (await session.execute(
                select(CompanyShare).where(CompanyShare.company_id == company.id)
            )).scalars().all()
            UserModel2 = __import__('database.models', fromlist=['User']).User
            for s in shares:
                su = await session.get(UserModel2, s.owner_id)
                parts_data.append({
                    'user_id': s.owner_id,
                    'name':    (su.first_name or su.username or '—') if su else '—',
                    'username': su.username if su else '',
                    'parts':   s.quantity,
                    'is_me':   s.owner_id == uid,
                })
        except Exception:
            pass

        # Contrats Bureau
        contrats = (await session.execute(
            select(BureauContrat).where(
                BureauContrat.company_id == company.id,
                BureauContrat.status.in_(['active', 'pending']),
            )
        )).scalars().all()

        from sqlalchemy import func as _func
        total_cmds = (await session.execute(
            select(_func.sum(CompanyEmployee.command_count)).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.left_at == None,
            )
        )).scalar() or 0

        contrats_data = []
        from datetime import datetime as _dtnow
        now = _dtnow.utcnow()
        for c in contrats:
            cmds_done = max(0, total_cmds - (c.cmds_at_start or 0))
            obj = c.objective_cmds or 1
            pct = min(100, int(cmds_done / obj * 100))
            remaining_days = (c.ends_at - now).days if c.ends_at else 0
            contrats_data.append({
                'id':            c.id,
                'title':         c.title,
                'reward':        _fmt(c.reward),
                'reward_raw':    c.reward,
                'objective':     c.objective_cmds,
                'cmds_done':     cmds_done,
                'pct':           pct,
                'status':        c.status,
                'days_left':     max(0, remaining_days),
                'ends_at':       c.ends_at.strftime('%d/%m à %H:%M') if c.ends_at else '—',
            })

        # Impôts
        tax_records = (await session.execute(
            select(TaxRecord).where(
                TaxRecord.company_id == company.id,
                TaxRecord.status == 'pending',
            ).order_by(TaxRecord.created_at.desc())
        )).scalars().all()

        tax_data = []
        for t in tax_records:
            tax_data.append({
                'id':          t.id,
                'amount_due':  _fmt(t.amount_due),
                'amount_due_raw': t.amount_due,
                'amount_paid': _fmt(t.amount_paid or 0),
                'remaining':   _fmt((t.amount_due or 0) - (t.amount_paid or 0)),
                'remaining_raw': (t.amount_due or 0) - (t.amount_paid or 0),
                'due_at':      t.due_at.strftime('%d/%m à %H:%M') if t.due_at else '—',
                'overdue':     now > t.due_at if t.due_at else False,
            })

        # Logs
        from database.models import CompanyLog
        logs = []
        try:
            log_rows = (await session.execute(
                select(CompanyLog).where(
                    CompanyLog.company_id == company.id,
                ).order_by(CompanyLog.created_at.desc()).limit(20)
            )).scalars().all()
            for l in log_rows:
                logs.append({
                    'event': l.event_type,
                    'desc':  l.description or '',
                    'amount': _fmt(l.amount) if l.amount else '',
                    'date':  l.created_at.strftime('%d/%m %H:%M') if l.created_at else '',
                })
        except Exception:
            pass

        # Candidatures (PDG only)
        candidatures = []
        if emp.role == 'pdg':
            try:
                apps = (await session.execute(
                    select(CompanyApplication).where(
                        CompanyApplication.company_id == company.id,
                        CompanyApplication.status == 'pending',
                    ).order_by(CompanyApplication.created_at.desc())
                )).scalars().all()
                UserModel3 = __import__('database.models', fromlist=['User']).User
                for a in apps:
                    au = await session.get(UserModel3, a.user_id)
                    candidatures.append({
                        'id':       a.id,
                        'user_id':  a.user_id,
                        'name':     (au.first_name or au.username or '—') if au else '—',
                        'username': au.username if au else '',
                        'date':     a.created_at.strftime('%d/%m à %H:%M') if a.created_at else '—',
                    })
            except Exception:
                pass

        # Contrats automatiques (IA)
        auto_contrats = []
        try:
            ac_rows = (await session.execute(
                select(CompanyAutoContract).where(
                    CompanyAutoContract.company_id == company.id,
                    CompanyAutoContract.status.in_(['active', 'pending', 'negotiating']),
                ).order_by(CompanyAutoContract.created_at.desc())
            )).scalars().all()
            for ac in ac_rows:
                cmds_done_ac = max(0, int(total_cmds) - (ac.cmds_at_start or 0))
                obj_ac = ac.objective_cmds or 1
                pct_ac = min(100, int(cmds_done_ac / obj_ac * 100))
                deadline_left = (ac.deadline_at - now).days if ac.deadline_at else 0
                auto_contrats.append({
                    'id':          ac.id,
                    'client':      ac.client_name,
                    'desc':        ac.description,
                    'objective':   ac.objective_cmds,
                    'cmds_done':   cmds_done_ac,
                    'pct':         pct_ac,
                    'reward':      _fmt(ac.negotiated_reward or ac.reward),
                    'reward_raw':  ac.negotiated_reward or ac.reward,
                    'status':      ac.status,
                    'deadline_at': ac.deadline_at.strftime('%d/%m à %H:%M') if ac.deadline_at else '—',
                    'days_left':   max(0, deadline_left),
                })
        except Exception:
            pass

        # Offres de parts en attente
        share_offers = []
        try:
            so_rows = (await session.execute(
                select(CompanyShareOffer).where(
                    CompanyShareOffer.company_id == company.id,
                    CompanyShareOffer.status == 'pending',
                ).order_by(CompanyShareOffer.created_at.desc())
            )).scalars().all()
            UserModel4 = __import__('database.models', fromlist=['User']).User
            for so in so_rows:
                bu = await session.get(UserModel4, so.buyer_id)
                share_offers.append({
                    'id':          so.id,
                    'buyer_name':  (bu.first_name or bu.username or '—') if bu else '—',
                    'buyer_username': bu.username if bu else '',
                    'quantity':    so.quantity,
                    'price_each':  _fmt(so.price_each),
                    'total_price': _fmt(so.total_price),
                    'expires_at':  so.expires_at.strftime('%d/%m à %H:%M') if so.expires_at else '—',
                })
        except Exception:
            pass

        # Niveau
        LEVELS = {1:('⭐','Startup'),2:('⭐⭐','PME'),3:('⭐⭐⭐','ETI'),4:('⭐⭐⭐⭐','Grande Entreprise'),5:('⭐⭐⭐⭐⭐','Multinationale')}
        lvl_emoji, lvl_name = LEVELS.get(company.level or 1, ('⭐','Startup'))

        return web.json_response({
            'company': {
                'id':            company.id,
                'name':          company.name,
                'sector':        company.sector or '—',
                'level':         company.level or 1,
                'level_label':   f"{lvl_emoji} {lvl_name}",
                'reputation':    company.reputation or 0,
                'value':         _fmt(company.value),
                'value_raw':     company.value or 0,
                'treasury':      _fmt(company.treasury),
                'treasury_raw':  company.treasury or 0,
                'frozen':        company.treasury_frozen or False,
                'tax_debt':      _fmt(company.tax_debt or 0),
                'tax_debt_raw':  company.tax_debt or 0,
                'is_pdg':        emp.role == 'pdg',
                'my_role':       emp.role,
                'my_cmds':       emp.command_count or 0,
                'my_contract_status': emp.contract_status or 'none',
                'my_pending_salary': emp.pending_salary or 0,
                'my_pending_bonus':  emp.pending_bonus or 0,
                'employees':     employees,
                'nb_employees':  len(employees),
                'max_employees': {1:5,2:10,3:50,4:100,5:200}.get(company.level or 1, 50) + (company.extra_slots or 0),
                'parts':         parts_data,
                'total_shares':  company.total_shares or 100,
                'owner_shares':  company.owner_shares or 100,
                'contrats':      contrats_data,
                'nb_contrats':   len(contrats_data),
                'taxes':         tax_data,
                'logs':          logs,
                'candidatures':  candidatures,
                'nb_candidatures': len(candidatures),
                'auto_contrats': auto_contrats,
                'share_offers':  share_offers,
                'total_cmds':    int(total_cmds),
                'owner_id':      company.owner_id,
                'weekly_revenue': _fmt(company.weekly_revenue or 0),
                'weekly_revenue_raw': company.weekly_revenue or 0,
                'legal_reserve': _fmt(company.legal_reserve or 0),
                'legal_reserve_raw': company.legal_reserve or 0,
            }
        })


async def webapp_company_depot(request: web.Request) -> web.Response:
    """POST /api/webapp/company/depot"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid json'}, status=400)

    uid = _get_verified_uid(request)
    amount = int(body.get('amount', 0))
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
    if amount <= 0:
        return web.json_response({'error': 'Montant invalide'})

    async with AsyncSessionLocal() as session:
        company = (await session.execute(
            select(Company).where(Company.owner_id == uid, Company.is_active == True)
        )).scalar_one_or_none()
        if not company:
            return web.json_response({'error': 'Tu n\'es PDG d\'aucune entreprise'})

        from database.models import User as UserModel
        user = await session.get(UserModel, uid)
        if not user or (user.coins or 0) < amount:
            return web.json_response({'error': 'Solde insuffisant'})

        user.coins -= amount
        company.treasury = (company.treasury or 0) + amount
        company.value = company.treasury
        await session.commit()

    return web.json_response({'ok': True, 'msg': f'✅ {_fmt(amount)} $ déposés en trésorerie !'})


async def webapp_company_retrait(request: web.Request) -> web.Response:
    """POST /api/webapp/company/retrait"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid json'}, status=400)

    uid = _get_verified_uid(request)
    amount = int(body.get('amount', 0))
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
    if amount <= 0:
        return web.json_response({'error': 'Montant invalide'})

    async with AsyncSessionLocal() as session:
        company = (await session.execute(
            select(Company).where(Company.owner_id == uid, Company.is_active == True)
        )).scalar_one_or_none()
        if not company:
            return web.json_response({'error': 'Tu n\'es PDG d\'aucune entreprise'})
        if company.treasury_frozen:
            return web.json_response({'error': '🔒 Trésorerie gelée — paie tes impôts d\'abord'})
        if (company.treasury or 0) < amount:
            return web.json_response({'error': f'Trésorerie insuffisante ({_fmt(company.treasury)} $ disponible)'})

        from database.models import User as UserModel
        user = await session.get(UserModel, uid)
        if not user:
            return web.json_response({'error': 'Utilisateur introuvable'})

        # Cooldown 24h
        from datetime import timedelta
        from database.models import CompanyLog
        last_retrait = (await session.execute(
            select(CompanyLog).where(
                CompanyLog.company_id == company.id,
                CompanyLog.event_type == 'retrait',
            ).order_by(CompanyLog.created_at.desc()).limit(1)
        )).scalar_one_or_none()

        if last_retrait and last_retrait.created_at:
            from datetime import datetime as _dtnow2
            since = (_dtnow2.utcnow() - last_retrait.created_at).total_seconds()
            if since < 86400:
                h = int((86400 - since) // 3600)
                m = int(((86400 - since) % 3600) // 60)
                return web.json_response({'error': f'⏳ Cooldown retrait : encore {h}h{m:02d}m à attendre'})

        company.treasury -= amount
        company.value = company.treasury
        user.coins = (user.coins or 0) + amount

        log = CompanyLog(
            company_id=company.id,
            event_type='retrait',
            description=f'Retrait PDG via webapp',
            amount=amount,
        )
        session.add(log)
        await session.commit()

    return web.json_response({'ok': True, 'msg': f'✅ {_fmt(amount)} $ retirés de la trésorerie !'})


async def webapp_company_payerimpots(request: web.Request) -> web.Response:
    """POST /api/webapp/company/payerimpots"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid json'}, status=400)

    uid = _get_verified_uid(request)
    amount = int(body.get('amount', 0))
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
    if amount <= 0:
        return web.json_response({'error': 'Montant invalide'})

    async with AsyncSessionLocal() as session:
        company = (await session.execute(
            select(Company).where(Company.owner_id == uid, Company.is_active == True)
        )).scalar_one_or_none()
        if not company:
            return web.json_response({'error': 'Tu n\'es PDG d\'aucune entreprise'})
        if (company.treasury or 0) < amount:
            return web.json_response({'error': 'Trésorerie insuffisante'})

        # Payer sur les factures pendantes
        pending = (await session.execute(
            select(TaxRecord).where(
                TaxRecord.company_id == company.id,
                TaxRecord.status == 'pending',
            ).order_by(TaxRecord.created_at.asc())
        )).scalars().all()

        remaining_payment = amount
        for record in pending:
            if remaining_payment <= 0:
                break
            owed = (record.amount_due or 0) - (record.amount_paid or 0)
            pay = min(owed, remaining_payment)
            record.amount_paid = (record.amount_paid or 0) + pay
            remaining_payment -= pay
            if record.amount_paid >= record.amount_due:
                record.status = 'paid'

        company.treasury -= amount
        company.value = company.treasury
        company.tax_debt = max(0, (company.tax_debt or 0) - amount)

        # Vérifier si dette soldée → dégeler
        total_remaining = sum(
            (r.amount_due or 0) - (r.amount_paid or 0)
            for r in pending if r.status == 'pending'
        )
        if total_remaining <= 0 and company.treasury_frozen:
            company.treasury_frozen = False

        # Ajouter à la caisse d'État
        from database.models import StateCaisse
        caisse = (await session.execute(select(StateCaisse))).scalar_one_or_none()
        if caisse:
            caisse.total = (caisse.total or 0) + amount

        await session.commit()

    msg = f'✅ {_fmt(amount)} $ d\'impôts payés !'
    if not company.treasury_frozen:
        msg += ' Trésorerie dégelée !'
    return web.json_response({'ok': True, 'msg': msg})


async def webapp_company_accepter(request: web.Request) -> web.Response:
    """POST /api/webapp/company/accepter"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid json'}, status=400)

    uid = _get_verified_uid(request)
    target_id = int(body.get('target_id', 0))
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
    if not target_id:
        return web.json_response({'error': 'target_id manquant'})

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company or emp.role not in ('pdg', 'directeur'):
            return web.json_response({'error': 'Réservé au PDG et Directeur'})

        app = (await session.execute(
            select(CompanyApplication).where(
                CompanyApplication.company_id == company.id,
                CompanyApplication.user_id == target_id,
                CompanyApplication.status == 'pending',
            )
        )).scalar_one_or_none()
        if not app:
            return web.json_response({'error': 'Candidature introuvable ou déjà traitée'})

        # Vérifier capacité
        from sqlalchemy import func as _func2
        nb_emp = (await session.execute(
            select(_func2.count()).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.left_at == None,
            )
        )).scalar()
        max_emp = {1:5,2:10,3:50,4:100,5:200}.get(company.level or 1, 50) + (company.extra_slots or 0)
        if nb_emp >= max_emp:
            return web.json_response({'error': f'Entreprise au complet ({nb_emp}/{max_emp})'})

        candidate = await session.get(User, target_id)

        # Rôle selon diplôme
        role = 'stagiaire'
        if candidate:
            if candidate.diplome_mba:       role = 'directeur'
            elif candidate.diplome_master:  role = 'manager'
            elif candidate.diplome_licence: role = 'employe'
            elif candidate.diplome_bac:     role = 'employe'
            # PDG ailleurs → forcer employe
            own_co = (await session.execute(
                select(Company).where(
                    Company.owner_id == candidate.user_id,
                    Company.is_active == True,
                    Company.is_bot_company == False,
                )
            )).scalar_one_or_none()
            if own_co:
                role = 'employe'

        app.status = 'accepted'
        new_emp = CompanyEmployee(company_id=company.id, user_id=target_id, role=role)
        session.add(new_emp)

        from database.models import CompanyLog as _CLog
        session.add(_CLog(
            company_id=company.id,
            event_type='recrutement',
            description=f"{candidate.first_name if candidate else target_id} recruté comme {role}",
        ))
        await session.commit()

        # Notifier le candidat via Telegram
        if candidate:
            try:
                import aiohttp as _aiohttp
                from config import BOT_TOKEN as _BT
                async with _aiohttp.ClientSession() as _s:
                    await _s.post(
                        f'https://api.telegram.org/bot{_BT}/sendMessage',
                        json={
                            'chat_id': candidate.user_id,
                            'text': f"🎉 Ta candidature chez <b>{company.name}</b> a été <b>acceptée</b> ! Tu es désormais <b>{role.capitalize()}</b>.",
                            'parse_mode': 'HTML',
                        }, timeout=_aiohttp.ClientTimeout(total=5)
                    )
            except Exception:
                pass

        name = candidate.first_name if candidate else str(target_id)
        return web.json_response({'ok': True, 'msg': f'✅ {name} recruté(e) comme {role.capitalize()} !'})


async def webapp_company_refuser(request: web.Request) -> web.Response:
    """POST /api/webapp/company/refuser"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid json'}, status=400)

    uid = _get_verified_uid(request)
    target_id = int(body.get('target_id', 0))
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
    if not target_id:
        return web.json_response({'error': 'target_id manquant'})

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company or emp.role not in ('pdg', 'directeur'):
            return web.json_response({'error': 'Réservé au PDG et Directeur'})

        app = (await session.execute(
            select(CompanyApplication).where(
                CompanyApplication.company_id == company.id,
                CompanyApplication.user_id == target_id,
                CompanyApplication.status == 'pending',
            )
        )).scalar_one_or_none()
        if not app:
            return web.json_response({'error': 'Candidature introuvable ou déjà traitée'})

        app.status = 'rejected'
        candidate = await session.get(User, target_id)
        await session.commit()

        if candidate:
            try:
                import aiohttp as _aiohttp
                from config import BOT_TOKEN as _BT
                async with _aiohttp.ClientSession() as _s:
                    await _s.post(
                        f'https://api.telegram.org/bot{_BT}/sendMessage',
                        json={
                            'chat_id': candidate.user_id,
                            'text': f"😔 Ta candidature chez <b>{company.name}</b> a été <b>refusée</b>.\n💡 Tu peux postuler ailleurs avec /listeboites.",
                            'parse_mode': 'HTML',
                        }, timeout=_aiohttp.ClientTimeout(total=5)
                    )
            except Exception:
                pass

        name = candidate.first_name if candidate else str(target_id)
        return web.json_response({'ok': True, 'msg': f'❌ Candidature de {name} refusée.'})


async def webapp_company_licencier(request: web.Request) -> web.Response:
    """POST /api/webapp/company/licencier"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid json'}, status=400)

    uid = _get_verified_uid(request)
    target_id = int(body.get('target_id', 0))
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
    if not target_id:
        return web.json_response({'error': 'target_id manquant'})

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company or emp.role not in ('pdg', 'directeur'):
            return web.json_response({'error': 'Réservé au PDG et Directeur'})

        if company.is_bot_company:
            return web.json_response({'error': 'Impossible dans une entreprise officielle'})

        if target_id == uid:
            return web.json_response({'error': 'Utilise Démissionner pour quitter toi-même'})

        target = await session.get(User, target_id)
        if not target:
            return web.json_response({'error': 'Utilisateur introuvable'})

        target_emp = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.user_id == target_id,
                CompanyEmployee.left_at == None,
            )
        )).scalar_one_or_none()
        if not target_emp:
            return web.json_response({'error': f'{target.first_name} ne fait pas partie de cette entreprise'})

        ROLES_ORDER = ["stagiaire", "secretaire", "employe", "manager", "directeur", "pdg"]
        if emp.role == 'directeur' and target_emp.role in ('pdg', 'directeur'):
            return web.json_response({'error': 'Tu ne peux pas licencier quelqu\'un de rang égal ou supérieur'})

        target_emp.left_at = datetime.utcnow()
        from handlers.company import _add_log as _co_log
        await _co_log(session, company.id, 'licenciement',
                      f'{target.first_name} a été licencié')
        await session.commit()

        try:
            import aiohttp as _aiohttp
            from config import BOT_TOKEN as _BT
            async with _aiohttp.ClientSession() as _s:
                await _s.post(
                    f'https://api.telegram.org/bot{_BT}/sendMessage',
                    json={'chat_id': target.user_id,
                          'text': f'🚨 Tu as été licencié de <b>{company.name}</b> par la direction.',
                          'parse_mode': 'HTML'},
                    timeout=_aiohttp.ClientTimeout(total=5)
                )
        except Exception:
            pass

        return web.json_response({'ok': True, 'msg': f'✅ {target.first_name} a été licencié.'})


async def webapp_company_nommer(request: web.Request) -> web.Response:
    """POST /api/webapp/company/nommer"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid json'}, status=400)

    uid = _get_verified_uid(request)
    target_id = int(body.get('target_id', 0))
    new_role  = str(body.get('role', '')).lower()
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

    ROLES_ORDER = ["stagiaire", "secretaire", "employe", "manager", "directeur", "pdg"]
    MANAGEMENT_ROLES = ("pdg", "directeur", "manager")
    ROLE_DIPLOMA = {"secretaire": "brevet", "employe": "brevet",
                    "manager": "licence", "directeur": "master"}

    if new_role not in ROLES_ORDER or new_role in ('pdg', 'stagiaire'):
        return web.json_response({'error': 'Poste invalide. Choix : secretaire | employe | manager | directeur'})

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company or emp.role not in MANAGEMENT_ROLES:
            return web.json_response({'error': 'Tu dois être au moins Manager'})

        if ROLES_ORDER.index(new_role) >= ROLES_ORDER.index(emp.role):
            return web.json_response({'error': 'Tu ne peux pas nommer quelqu\'un à un rang égal ou supérieur au tien'})

        target = await session.get(User, target_id)
        if not target:
            return web.json_response({'error': 'Utilisateur introuvable'})

        target_emp = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.user_id == target_id,
                CompanyEmployee.left_at == None,
            )
        )).scalar_one_or_none()
        if not target_emp:
            return web.json_response({'error': f'{target.first_name} ne fait pas partie de cette entreprise'})

        from handlers.company import _has_diploma as _hd, _add_log as _co_log
        required = ROLE_DIPLOMA.get(new_role)
        if required and not _hd(target, required):
            return web.json_response({'error': f'{target.first_name} n\'a pas le diplôme requis ({required}) pour ce poste'})

        old_role = target_emp.role
        target_emp.role = new_role
        await _co_log(session, company.id, 'promotion',
                      f'{target.first_name} : {old_role} → {new_role}')
        await session.commit()

        ROLE_EMOJI = {"pdg":"👑","directeur":"🎯","manager":"📊","employe":"💼",
                      "secretaire":"📋","stagiaire":"🌱"}
        emoji = ROLE_EMOJI.get(new_role, '👤')
        return web.json_response({'ok': True, 'msg': f'✅ {target.first_name} est désormais {emoji} {new_role.capitalize()} !'})


async def webapp_company_demissionner(request: web.Request) -> web.Response:
    """POST /api/webapp/company/demissionner"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid json'}, status=400)

    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, uid)
        if not company or not emp:
            return web.json_response({'error': 'Tu ne fais partie d\'aucune entreprise'})

        if emp.role == 'pdg':
            director = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == company.id,
                    CompanyEmployee.role == 'directeur',
                    CompanyEmployee.left_at == None,
                ).limit(1)
            )).scalar_one_or_none()
            if not director:
                return web.json_response({'error': 'En tant que PDG, nomme un Directeur avant de partir'})

        emp.left_at = datetime.utcnow()
        from handlers.company import _add_log as _co_log
        await _co_log(session, company.id, 'demission',
                      f'Un employé a démissionné')
        await session.commit()

        return web.json_response({'ok': True, 'msg': f'✅ Tu as quitté {company.name}.'})


async def webapp_company_acheter_parts(request: web.Request) -> web.Response:
    """POST /api/webapp/company/acheterparts — soumet une offre d'achat de parts"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid json'}, status=400)

    uid = _get_verified_uid(request)
    company_id = int(body.get('company_id', 0))
    qty        = int(body.get('quantity', 0))
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
    if qty <= 0:
        return web.json_response({'error': 'Quantité invalide'})

    async with AsyncSessionLocal() as session:
        company = await session.get(Company, company_id)
        if not company or not company.is_active:
            return web.json_response({'error': 'Entreprise introuvable'})
        if company.is_bot_company:
            return web.json_response({'error': 'Les parts de cette entreprise ne sont pas cessibles'})
        available = company.owner_shares or 0
        if available <= 0:
            return web.json_response({'error': 'Aucune part disponible à l\'achat'})
        if qty > available:
            return web.json_response({'error': f'Seulement {available} parts disponibles'})

        price_per = company.treasury // company.total_shares if (company.total_shares or 0) > 0 else 1
        total = qty * price_per

        buyer = await session.get(User, uid)
        if not buyer:
            return web.json_response({'error': 'Utilisateur introuvable'})
        if buyer.coins < total:
            return web.json_response({'error': f'Solde insuffisant. Coût total : {total:,} $'})

        # Vérifier offre en attente existante
        existing = (await session.execute(
            select(CompanyShareOffer).where(
                CompanyShareOffer.company_id == company_id,
                CompanyShareOffer.buyer_id == uid,
                CompanyShareOffer.status == 'pending',
            )
        )).scalar_one_or_none()
        if existing:
            return web.json_response({'error': 'Tu as déjà une offre en attente sur cette entreprise (48h)'})

        buyer.coins -= total
        offer = CompanyShareOffer(
            company_id=company_id, buyer_id=uid,
            quantity=qty, price_each=price_per, total_price=total,
            status='pending',
            expires_at=datetime.utcnow() + timedelta(hours=48),
        )
        session.add(offer)
        await session.flush()

        from handlers.company import _add_log as _co_log
        await _co_log(session, company_id, 'offre_parts',
                      f'{buyer.first_name} soumet une offre pour {qty} parts', amount=total)
        await session.commit()

        try:
            import aiohttp as _aiohttp
            from config import BOT_TOKEN as _BT
            async with _aiohttp.ClientSession() as _s:
                await _s.post(
                    f'https://api.telegram.org/bot{_BT}/sendMessage',
                    json={'chat_id': company.owner_id,
                          'text': f'💼 <b>Offre d\'achat de parts !</b>\n👤 {buyer.first_name} veut acheter <b>{qty} parts</b> de <b>{company.name}</b>\n💰 Offre : <b>{total:,} $</b>\n\nAccepter : <code>/accepteroffre {offer.id}</code>\nRefuser : <code>/refuseroffre {offer.id}</code>',
                          'parse_mode': 'HTML'},
                    timeout=_aiohttp.ClientTimeout(total=5)
                )
        except Exception:
            pass

        return web.json_response({'ok': True, 'msg': f'📩 Offre envoyée ! {total:,} $ bloqués (remboursés si refus).'})


async def webapp_company_gerer_offre(request: web.Request) -> web.Response:
    """POST /api/webapp/company/gereroffre — PDG accepte ou refuse une offre"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid json'}, status=400)

    uid = _get_verified_uid(request)
    offer_id = int(body.get('offer_id', 0))
    action   = str(body.get('action', ''))  # 'accept' ou 'refuse'
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

    async with AsyncSessionLocal() as session:
        offer = await session.get(CompanyShareOffer, offer_id)
        if not offer or offer.status != 'pending':
            return web.json_response({'error': 'Offre introuvable ou déjà traitée'})

        company = await session.get(Company, offer.company_id)
        if not company or company.owner_id != uid:
            return web.json_response({'error': 'Seul le PDG peut gérer cette offre'})

        buyer = await session.get(User, offer.buyer_id)

        if action == 'accept':
            if datetime.utcnow() > offer.expires_at:
                offer.status = 'expired'
                if buyer: buyer.coins += offer.total_price
                await session.commit()
                return web.json_response({'error': 'Offre expirée — acheteur remboursé'})
            available = company.owner_shares or 0
            if offer.quantity > available:
                offer.status = 'rejected'
                if buyer: buyer.coins += offer.total_price
                await session.commit()
                return web.json_response({'error': 'Plus assez de parts disponibles — acheteur remboursé'})
            company.treasury += offer.total_price
            company.owner_shares -= offer.quantity
            # Mettre à jour ou créer la ligne actionnaire
            existing_shares = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == company.id,
                    CompanyEmployee.user_id == offer.buyer_id,
                    CompanyEmployee.left_at == None,
                )
            )).scalar_one_or_none()
            if existing_shares:
                existing_shares.shares = (existing_shares.shares or 0) + offer.quantity
            offer.status = 'accepted'
            from handlers.company import _add_log as _co_log
            await _co_log(session, company.id, 'vente_parts',
                          f'Offre #{offer_id} acceptée — {offer.quantity} parts', amount=offer.total_price)
            await session.commit()
            return web.json_response({'ok': True, 'msg': f'✅ Offre acceptée ! {offer.total_price:,} $ reçus en trésorerie.'})
        else:
            offer.status = 'rejected'
            if buyer: buyer.coins += offer.total_price
            from handlers.company import _add_log as _co_log
            await _co_log(session, company.id, 'refus_offre',
                          f'Offre #{offer_id} refusée')
            await session.commit()
            return web.json_response({'ok': True, 'msg': f'❌ Offre refusée. Acheteur remboursé.'})


# ══════════════════════════════════════════════════════════════════════════════
#  🏛️ LA SALLE — Routes API
# ══════════════════════════════════════════════════════════════════════════════

SALLE_ACCESS_PRICE = 1_000_000

async def webapp_salle_status(request: web.Request) -> web.Response:
    """GET /api/webapp/salle/status?user_id=xxx — accès + enchère active"""
    import traceback as _tb2
    try:
        return await _webapp_salle_status_inner(request)
    except Exception as _e2:
        return web.json_response({'error': str(_e2), 'trace': _tb2.format_exc()[-1000:]}, status=500)

# ══════════════════════════════════════════════════════════════════════════════
#  SYSTÈME DE NOTIFICATIONS PERSISTANTES
# ══════════════════════════════════════════════════════════════════════════════

_notif_table_ready = False

async def _ensure_notif_table():
    """Crée la table user_notifications si elle n'existe pas (idempotent)."""
    global _notif_table_ready
    if _notif_table_ready:
        return
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("""
                CREATE TABLE IF NOT EXISTS user_notifications (
                    id          SERIAL PRIMARY KEY,
                    user_id     BIGINT NOT NULL,
                    icon        VARCHAR(10) DEFAULT '🔔',
                    title       VARCHAR(200) NOT NULL,
                    body        TEXT NOT NULL,
                    is_read     BOOLEAN DEFAULT FALSE,
                    created_at  TIMESTAMP DEFAULT NOW()
                )
            """))
            await session.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_notif_user ON user_notifications(user_id, is_read)"
            ))
            await session.commit()
        _notif_table_ready = True
    except Exception as e:
        import logging as _nlog
        _nlog.getLogger(__name__).error(f"_ensure_notif_table error: {e}", exc_info=True)


async def push_db_notif(user_id: int, icon: str, title: str, body: str):
    """Insère une notification persistante pour un joueur."""
    try:
        await _ensure_notif_table()
        async with AsyncSessionLocal() as session:
            await session.execute(text("""
                INSERT INTO user_notifications (user_id, icon, title, body)
                VALUES (:uid, :icon, :title, :body)
            """), {"uid": user_id, "icon": icon, "title": title, "body": body})
            await session.commit()
    except Exception as e:
        import logging as _nlog
        _nlog.getLogger(__name__).error(f"push_db_notif error: {e}", exc_info=True)


async def webapp_notifications_debug(request: web.Request) -> web.Response:
    """GET /api/webapp/notifications/debug?user_id=xxx — diagnostic complet"""
    try:
        uid = int(request.rel_url.query.get("user_id", 0))
    except Exception:
        uid = 0
    result = {"uid": uid, "table_ready": _notif_table_ready}
    try:
        async with AsyncSessionLocal() as session:
            # Table existe ?
            tbl = await session.execute(text(
                "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='user_notifications')"
            ))
            result["table_exists"] = tbl.scalar()
            if result["table_exists"]:
                # Compter les notifs de cet user
                cnt = await session.execute(text(
                    "SELECT COUNT(*) FROM user_notifications WHERE user_id = :uid"
                ), {"uid": uid})
                result["notif_count"] = cnt.scalar()
                # Les 5 dernières
                rows = (await session.execute(text(
                    "SELECT id, icon, title, body, is_read, created_at FROM user_notifications WHERE user_id = :uid ORDER BY created_at DESC LIMIT 5"
                ), {"uid": uid})).fetchall()
                result["last_5"] = [
                    {"id": r[0], "icon": r[1], "title": r[2], "body": r[3], "is_read": r[4], "created_at": str(r[5])}
                    for r in rows
                ]
                # Total toutes notifs
                total = await session.execute(text("SELECT COUNT(*) FROM user_notifications"))
                result["total_all_users"] = total.scalar()
    except Exception as e:
        result["error"] = str(e)
    return web.json_response(result)


async def webapp_notifications_read(request: web.Request) -> web.Response:
    """POST /api/webapp/notifications/read — body: {user_id, notif_id?}
    Si notif_id absent : marque toutes comme lues."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    uid = int(body.get("user_id", 0))
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
    notif_id = body.get("notif_id")
    try:
        await _ensure_notif_table()
        async with AsyncSessionLocal() as session:
            if notif_id:
                await session.execute(text(
                    "UPDATE user_notifications SET is_read = TRUE WHERE id = :nid AND user_id = :uid"
                ), {"nid": int(notif_id), "uid": uid})
            else:
                await session.execute(text(
                    "UPDATE user_notifications SET is_read = TRUE WHERE user_id = :uid"
                ), {"uid": uid})
            await session.commit()
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def webapp_notifications_delete(request: web.Request) -> web.Response:
    """POST /api/webapp/notifications/delete — body: {user_id, notif_id}
    Supprime une notification. Si notif_id absent : supprime toutes."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    uid = int(body.get("user_id", 0))
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
    notif_id = body.get("notif_id")
    try:
        await _ensure_notif_table()
        async with AsyncSessionLocal() as session:
            if notif_id:
                await session.execute(text(
                    "DELETE FROM user_notifications WHERE id = :nid AND user_id = :uid"
                ), {"nid": int(notif_id), "uid": uid})
            else:
                await session.execute(text(
                    "DELETE FROM user_notifications WHERE user_id = :uid"
                ), {"uid": uid})
            await session.commit()
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def webapp_notifications_announce(request: web.Request) -> web.Response:
    """POST /api/webapp/notifications/announce — body: {admin_id, title, body, icon?}
    Admin uniquement : envoie une notif à tous les joueurs."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    admin_id = int(body.get("admin_id", 0))
    # Vérifier que c'est un admin
    try:
        from config import ADMIN_IDS
        if admin_id not in ADMIN_IDS:
            return web.json_response({"error": "unauthorized"}, status=403)
    except Exception:
        return web.json_response({"error": "unauthorized"}, status=403)

    title = str(body.get("title", "")).strip()
    msg   = str(body.get("body", "")).strip()
    icon  = str(body.get("icon", "📢")).strip() or "📢"
    if not title or not msg:
        return web.json_response({"error": "title et body requis"}, status=400)

    try:
        await _ensure_notif_table()
        async with AsyncSessionLocal() as session:
            # Récupérer tous les user_id actifs
            rows = (await session.execute(
                text("SELECT user_id FROM users WHERE is_banned = FALSE")
            )).fetchall()
            count = 0
            for row in rows:
                await session.execute(text("""
                    INSERT INTO user_notifications (user_id, icon, title, body)
                    VALUES (:uid, :icon, :title, :body)
                """), {"uid": row[0], "icon": icon, "title": title, "body": msg})
                count += 1
            await session.commit()
        return web.json_response({"ok": True, "sent": count})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def _ensure_salle_tables():
    """Crée les tables Salle si elles n'existent pas (idempotent)."""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("""
                CREATE TABLE IF NOT EXISTS salle_auctions (
                    id           SERIAL PRIMARY KEY,
                    item_id      VARCHAR(50)  NOT NULL,
                    item_name    VARCHAR(100) NOT NULL,
                    item_emoji   VARCHAR(10)  NOT NULL,
                    rarity       VARCHAR(20)  NOT NULL,
                    true_value   BIGINT       NOT NULL,
                    start_price  BIGINT       NOT NULL,
                    current_bid  BIGINT       NOT NULL,
                    leader_id    BIGINT,
                    leader_name  VARCHAR(255),
                    custom_desc  TEXT,
                    status       VARCHAR(20)  DEFAULT 'active',
                    started_at   TIMESTAMP    DEFAULT NOW(),
                    ends_at      TIMESTAMP    NOT NULL
                )
            """))
            await session.execute(text("""
                CREATE TABLE IF NOT EXISTS salle_vip_access (
                    user_id    BIGINT,
                    expires_at TIMESTAMP NOT NULL,
                    paid_at    TIMESTAMP DEFAULT NOW()
                )
            """))
            # Ajouter la PK si elle manque (migration safe)
            await session.execute(text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conrelid = 'salle_vip_access'::regclass
                        AND contype = 'p'
                    ) THEN
                        ALTER TABLE salle_vip_access ADD PRIMARY KEY (user_id);
                    END IF;
                END$$
            """))
            await session.commit()
    except Exception as _ste:
        import logging as _stlog
        _stlog.getLogger(__name__).error(f"_ensure_salle_tables error: {_ste}", exc_info=True)

async def _webapp_salle_status_inner(request: web.Request) -> web.Response:
    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

    await _ensure_salle_tables()

    from datetime import datetime as _dt2
    now = _dt2.utcnow()

    async with AsyncSessionLocal() as session:
        # Accès VIP
        acc = await session.execute(text(
            "SELECT expires_at FROM salle_vip_access WHERE user_id = :uid"
        ), {"uid": uid})
        acc_row = acc.fetchone()
        has_access = bool(acc_row and acc_row[0] > now)
        expires_at = str(acc_row[0])[:16] if has_access else None
        seconds_left = int((acc_row[0] - now).total_seconds()) if has_access else 0

        # Enchère active
        auc = await session.execute(text("""
            SELECT id, item_id, item_name, item_emoji, rarity,
                   start_price, current_bid, leader_id, leader_name,
                   custom_desc, ends_at
            FROM salle_auctions WHERE status = 'active' ORDER BY id DESC LIMIT 1
        """))
        auction = auc.fetchone()

        auction_data = None
        if auction:
            from datetime import datetime as _dtparse
            ends_at_val = auction[10]
            if isinstance(ends_at_val, str):
                ends_at_val = _dtparse.fromisoformat(ends_at_val)
            time_left_s = max(0, int((ends_at_val - now).total_seconds()))
            auction_data = {
                'id':          auction[0],
                'item_id':     auction[1],
                'item_name':   auction[2],
                'item_emoji':  auction[3],
                'rarity':      auction[4],
                'start_price': auction[5],
                'current_bid': auction[6],
                'leader_id':   auction[7],
                'leader_name': auction[8],
                'custom_desc': auction[9],
                'ends_at':     str(ends_at_val),
                'time_left_s': time_left_s,
                'is_leading':  auction[7] == uid,
            }

        # Wallet
        wallet_r = await session.execute(text(
            "SELECT coins FROM users WHERE user_id = :uid"
        ), {"uid": uid})
        wallet_row = wallet_r.fetchone()
        wallet = int(wallet_row[0]) if wallet_row else 0

    return web.json_response({
        'has_access':    has_access,
        'expires_at':    expires_at,
        'seconds_left':  seconds_left,
        'access_price':  SALLE_ACCESS_PRICE,
        'auction':       auction_data,
        'wallet':        wallet,
    })


async def webapp_salle_pay(request: web.Request) -> web.Response:
    """POST /api/webapp/salle/pay — acheter l'accès 24h"""
    import logging as _paylog
    _logger = _paylog.getLogger(__name__)
    try:
        body = await request.json()
    except Exception as e:
        _logger.error(f"webapp_salle_pay json parse error: {e}")
        return web.json_response({'error': 'invalid json'}, status=400)

    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

    try:
        await _ensure_salle_tables()
    except Exception as e:
        _logger.error(f"webapp_salle_pay ensure_tables error: {e}", exc_info=True)

    from datetime import datetime as _dt3, timedelta as _td
    now = _dt3.utcnow()

    try:
        async with AsyncSessionLocal() as session:
            wallet_r = await session.execute(text(
                "SELECT coins FROM users WHERE user_id = :uid"
            ), {"uid": uid})
            wallet_row = wallet_r.fetchone()
            if not wallet_row or int(wallet_row[0]) < SALLE_ACCESS_PRICE:
                return web.json_response({'ok': False, 'error': f'Fonds insuffisants (besoin de {_fmt(SALLE_ACCESS_PRICE)} $)'})

            await session.execute(text(
                "UPDATE users SET coins = CAST(coins AS BIGINT) - :amt WHERE user_id = :uid"
            ), {"amt": SALLE_ACCESS_PRICE, "uid": uid})

            # Renouveler ou créer
            existing = await session.execute(text(
                "SELECT expires_at FROM salle_vip_access WHERE user_id = :uid"
            ), {"uid": uid})
            ex = existing.fetchone()
            ex0 = ex[0] if ex else None
            if isinstance(ex0, str):
                from datetime import datetime as _dtp
                ex0 = _dtp.fromisoformat(ex0.replace('+00:00',''))
            base = ex0 if (ex0 and ex0 > now) else now
            new_exp = base + _td(hours=24)

            # ON CONFLICT fiable même si la contrainte PK n'existe pas encore en DB
            upd = await session.execute(text(
                "UPDATE salle_vip_access SET expires_at = :exp, paid_at = NOW() WHERE user_id = :uid"
            ), {"uid": uid, "exp": new_exp})
            if upd.rowcount == 0:
                await session.execute(text(
                    "INSERT INTO salle_vip_access (user_id, expires_at) VALUES (:uid, :exp)"
                ), {"uid": uid, "exp": new_exp})
            await session.commit()

        return web.json_response({
            'ok': True,
            'expires_at': str(new_exp)[:16],
            'msg': f"🏛️ Accès à La Salle accordé jusqu'au {new_exp.strftime('%d/%m à %H:%M')} !"
        })
    except Exception as e:
        _logger.error(f"webapp_salle_pay DB error: {e}", exc_info=True)
        return web.json_response({'ok': False, 'error': f'Erreur serveur: {str(e)}'}, status=500)


async def webapp_salle_bid(request: web.Request) -> web.Response:
    """POST /api/webapp/salle/bid — enchérir dans La Salle"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid json'}, status=400)

    uid = _get_verified_uid(request)
    auction_id = int(body.get('auction_id', 0))
    amount     = int(body.get('amount', 0))

    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

    await _ensure_salle_tables()

    from datetime import datetime as _dt4
    now = _dt4.utcnow()

    async with AsyncSessionLocal() as session:
        # Vérifier accès VIP
        acc = await session.execute(text(
            "SELECT expires_at FROM salle_vip_access WHERE user_id = :uid"
        ), {"uid": uid})
        acc_row = acc.fetchone()
        if not acc_row or acc_row[0] <= now:
            return web.json_response({'ok': False, 'error': '🔒 Accès à La Salle expiré'})

        # Enchère
        auc_r = await session.execute(text("""
            SELECT id, current_bid, leader_id, ends_at, status
            FROM salle_auctions WHERE id = :aid AND status = 'active'
        """), {"aid": auction_id})
        auction = auc_r.fetchone()
        if not auction:
            return web.json_response({'ok': False, 'error': 'Enchère introuvable ou terminée'})
        auc_id, auc_current_bid, auc_leader_id, auc_ends_at, auc_status = auction
        from datetime import datetime as _dtparse2
        if isinstance(auc_ends_at, str):
            auc_ends_at = _dtparse2.fromisoformat(auc_ends_at)
        if auc_ends_at <= now:
            return web.json_response({'ok': False, 'error': '⏰ Enchère terminée !'})
        if auc_leader_id == uid:
            return web.json_response({'ok': False, 'error': '👑 Tu mènes déjà cette enchère !'})

        min_bid = max(auc_current_bid + 1, int(auc_current_bid * 1.05))
        if amount < min_bid:
            return web.json_response({'ok': False, 'error': f'Minimum : {_fmt(min_bid)} $ (+5%)'})

        # Fonds
        wallet_r = await session.execute(text(
            "SELECT coins, username FROM users WHERE user_id = :uid"
        ), {"uid": uid})
        user_row = wallet_r.fetchone()
        if not user_row or user_row[0] < amount:
            return web.json_response({'ok': False, 'error': 'Fonds insuffisants'})
        leader_name = user_row[1] or str(uid)

        # Rembourser ancien leader
        if auc_leader_id:
            await session.execute(text(
                "UPDATE users SET coins = coins + :amt WHERE user_id = :lid"
            ), {"amt": auc_current_bid, "lid": auc_leader_id})

        await session.execute(text(
            "UPDATE users SET coins = coins - :amt WHERE user_id = :uid"
        ), {"amt": amount, "uid": uid})
        await session.execute(text("""
            UPDATE salle_auctions
            SET current_bid = :bid, leader_id = :uid, leader_name = :name
            WHERE id = :aid
        """), {"bid": amount, "uid": uid, "name": leader_name, "aid": auction_id})
        await session.commit()

    return web.json_response({'ok': True, 'new_bid': amount})


async def webapp_salle_history(request: web.Request) -> web.Response:
    """GET /api/webapp/salle/history?user_id=xxx — dernières enchères Salle fermées"""
    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

    async with AsyncSessionLocal() as session:
        rows = await session.execute(text("""
            SELECT item_emoji, item_name, rarity, current_bid, leader_name, ends_at
            FROM salle_auctions
            WHERE status = 'closed'
            ORDER BY ends_at DESC
            LIMIT 10
        """))
        history = []
        for r in rows.fetchall():
            history.append({
                'emoji':       r[0],
                'name':        r[1],
                'rarity':      r[2],
                'final_bid':   r[3],
                'winner':      r[4] or 'Personne',
                'closed_at':   str(r[5])[:16],
            })

    return web.json_response({'history': history})


async def webapp_online_users(request: web.Request) -> web.Response:
    """GET /api/webapp/online?user_id=xxx — Utilisateurs actifs les 30 dernières minutes"""
    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)

    from datetime import timedelta
    from database.models import Relationship, RelationType

    cutoff = datetime.utcnow() - timedelta(minutes=30)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User)
            .where(User.is_banned == False, User.last_seen >= cutoff)
            .order_by(User.last_seen.desc())
            .limit(50)
        )
        users = result.scalars().all()

        # Récupérer les relations existantes avec moi
        my_rels = (await session.execute(
            select(Relationship).where(Relationship.user_id == uid)
        )).scalars().all()
        rel_map = {r.related_user_id: r.relation_type.value for r in my_rels}

        # Aussi ceux qui ont une relation vers moi
        their_rels = (await session.execute(
            select(Relationship).where(Relationship.related_user_id == uid)
        )).scalars().all()
        for r in their_rels:
            if r.user_id not in rel_map:
                rel_map[r.user_id] = r.relation_type.value

        online = []
        for u in users:
            if u.user_id == uid:
                continue
            mins_ago = int((datetime.utcnow() - u.last_seen).total_seconds() / 60) if u.last_seen else None
            online.append({
                'user_id':    u.user_id,
                'name':       u.first_name or '—',
                'username':   u.username or '',
                'coins':      int(u.coins or 0),
                'gender':     u.gender or '',
                'relation':   rel_map.get(u.user_id, ''),
                'last_seen':  u.last_seen.isoformat() if u.last_seen else '',
                'minutes_ago': mins_ago,
            })

    return web.json_response({'online': online, 'count': len(online)})


# ══════════════════════════════════════════════════════════════════════════════
#  FAMILY TICKETS
# ══════════════════════════════════════════════════════════════════════════════

import random as _ft_random

_FT_PRICES = { 'basique': 3, 'premium': 5, 'vip': 10 }
_FT_NAMES  = { 'basique': '🎫 Ticket Basique', 'premium': '🎟️ Ticket Premium', 'vip': '👑 Ticket VIP' }

async def _ft_ensure_table(session):
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS user_tickets (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            ticket_type VARCHAR(20) NOT NULL,
            qty INTEGER NOT NULL DEFAULT 0,
            UNIQUE(user_id, ticket_type)
        )
    """))
    await session.commit()

async def _ft_get_owned(session, uid: int) -> dict:
    rows = await session.execute(
        text("SELECT ticket_type, qty FROM user_tickets WHERE user_id=:uid AND qty>0"),
        {'uid': uid}
    )
    return {r[0]: r[1] for r in rows.fetchall()}

async def _ft_add(session, uid: int, ticket_type: str, qty: int):
    await session.execute(text("""
        INSERT INTO user_tickets (user_id, ticket_type, qty) VALUES (:uid, :t, :q)
        ON CONFLICT (user_id, ticket_type) DO UPDATE SET qty = user_tickets.qty + :q
    """), {'uid': uid, 't': ticket_type, 'q': qty})

async def _ft_remove(session, uid: int, ticket_type: str, qty: int):
    await session.execute(text("""
        UPDATE user_tickets SET qty = GREATEST(0, qty - :q)
        WHERE user_id=:uid AND ticket_type=:t
    """), {'uid': uid, 't': ticket_type, 'q': qty})


async def webapp_tickets_owned(request: web.Request) -> web.Response:
    """GET /api/webapp/tickets/owned"""
    uid = _get_verified_uid(request)
    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
    async with AsyncSessionLocal() as session:
        await _ft_ensure_table(session)
        owned = await _ft_get_owned(session, uid)
    tickets = [{'type': k, 'qty': v} for k, v in owned.items()]
    return web.json_response({'tickets': tickets})


async def webapp_tickets_invoice(request: web.Request) -> web.Response:
    """POST /api/webapp/tickets/invoice — crée une facture Telegram Stars."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'error': 'bad json'}, status=400)

    uid         = int(data.get('user_id', 0))
    ticket_type = data.get('ticket_type', '')
    qty         = max(1, min(99, int(data.get('qty', 1))))

    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
    if ticket_type not in _FT_PRICES:
        return web.json_response({'error': 'ticket_type invalide'}, status=400)

    unit_price  = _FT_PRICES[ticket_type]
    total_price = unit_price * qty
    name        = _FT_NAMES[ticket_type]

    try:
        import aiohttp as _aiohttp
        invoice_data = {
            'title': f'{name} ×{qty}',
            'description': f'Achat de {qty} {name} pour FarmBot',
            'payload': f'ticket_{ticket_type}_{qty}_{uid}',
            'currency': 'XTR',
            'prices': [{'label': f'{name} ×{qty}', 'amount': total_price}],
        }
        async with _aiohttp.ClientSession() as sess:
            async with sess.post(
                f'https://api.telegram.org/bot{BOT_TOKEN}/createInvoiceLink',
                json=invoice_data
            ) as resp:
                result = await resp.json()
        if result.get('ok'):
            print(f"[INVOICE] OK → {result['result']}", flush=True)
            return web.json_response({'invoice_url': result['result']})
        else:
            print(f"[INVOICE] Telegram error: {result}", flush=True)
            return web.json_response({'error': result.get('description', 'Erreur Telegram')}, status=500)
    except Exception as e:
        print(f"[INVOICE] Exception: {e}", flush=True)
        return web.json_response({'error': str(e)}, status=500)


async def webapp_tickets_send(request: web.Request) -> web.Response:
    """POST /api/webapp/tickets/send — envoyer des tickets à un autre joueur."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'error': 'bad json'}, status=400)

    uid         = int(data.get('user_id', 0))
    to_uid      = int(data.get('to_user_id', 0))
    ticket_type = data.get('ticket_type', '')
    qty         = max(1, int(data.get('qty', 1)))

    if not uid:
        return web.json_response({'error': 'unauthorized'}, status=401)
    if ticket_type not in _FT_PRICES:
        return web.json_response({'error': 'ticket_type invalide'}, status=400)
    if uid == to_uid:
        return web.json_response({'error': 'Tu ne peux pas t\'envoyer des tickets à toi-même'}, status=400)

    async with AsyncSessionLocal() as session:
        await _ft_ensure_table(session)

        # Vérifier que le destinataire existe
        target = await session.get(User, to_uid)
        if not target:
            return web.json_response({'error': 'Joueur introuvable'}, status=404)

        # Vérifier solde expéditeur
        owned = await _ft_get_owned(session, uid)
        if owned.get(ticket_type, 0) < qty:
            return web.json_response({'error': 'Pas assez de tickets'}, status=400)

        # Transfert
        await _ft_remove(session, uid, ticket_type, qty)
        await _ft_add(session, to_uid, ticket_type, qty)
        await session.commit()

    name = _FT_NAMES[ticket_type]
    return web.json_response({
        'status': 'ok',
        'message': f'{qty}× {name} envoyé(s) à {target.first_name or to_uid}'
    })
