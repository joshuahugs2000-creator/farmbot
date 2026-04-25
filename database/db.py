from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, and_, or_, delete, func, text
from .models import (
    Base, User, GroupSettings, Relationship, PendingRequest,
    Garden, DailyWaifu, KarmaVote, UserBet, RelationType, RequestType,
    CoupleAccount,
)
from config import DATABASE_URL, REQUEST_TIMEOUT, PLANT_TYPES, GARDEN_SLOTS, TITLES
from datetime import datetime, timedelta
from typing import Optional, List

engine            = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _asyncpg_dsn() -> str:
    """Convertit DATABASE_URL (format SQLAlchemy) en DSN natif asyncpg."""
    url = DATABASE_URL
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    url = url.replace("postgres+asyncpg://", "postgresql://")
    return url


async def init_db():
    """Crée les tables et ajoute les colonnes manquantes (migration douce)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        migrations = [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_daily  VARCHAR(20)  DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_work   TIMESTAMP    DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS coins       BIGINT       DEFAULT 10000",
            "ALTER TABLE users ALTER COLUMN coins TYPE BIGINT USING coins::BIGINT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned   BOOLEAN      NOT NULL DEFAULT FALSE",
            "UPDATE users SET coins = 10000 WHERE coins < 10000",
            """CREATE TABLE IF NOT EXISTS couple_accounts (
                id         SERIAL PRIMARY KEY,
                user1_id   BIGINT REFERENCES users(user_id),
                user2_id   BIGINT REFERENCES users(user_id),
                balance    BIGINT DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW()
            )""",
            """CREATE TABLE IF NOT EXISTS lottery_sessions (
                id           SERIAL PRIMARY KEY,
                group_id     BIGINT NOT NULL,
                creator_id   BIGINT,
                ticket_price BIGINT NOT NULL,
                loto_type    VARCHAR(10) NOT NULL DEFAULT 'private',
                status       VARCHAR(10) NOT NULL DEFAULT 'active',
                winner_id    BIGINT,
                pot          BIGINT DEFAULT 0,
                created_at   TIMESTAMP DEFAULT NOW(),
                drawn_at     TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS lottery_tickets (
                id         SERIAL PRIMARY KEY,
                session_id INTEGER REFERENCES lottery_sessions(id),
                user_id    BIGINT REFERENCES users(user_id),
                created_at TIMESTAMP DEFAULT NOW()
            )""",
        ]
        for sql in migrations:
            try:
                await conn.execute(text(sql))
            except Exception:
                pass


# ─── USERS ───────────────────────────────────────────────────────────────────

async def upsert_user(session: AsyncSession, user_id: int, username: str, first_name: str) -> User:
    result = await session.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        user = User(user_id=user_id, username=username, first_name=first_name)
        session.add(user)
    else:
        user.username   = username
        user.first_name = first_name
    await session.commit()
    return user


async def get_user(session: AsyncSession, user_id: int) -> Optional[User]:
    r = await session.execute(select(User).where(User.user_id == user_id))
    return r.scalar_one_or_none()


async def get_user_by_username(session: AsyncSession, username: str) -> Optional[User]:
    clean = username.lstrip("@").lower()
    r = await session.execute(select(User).where(func.lower(User.username) == clean))
    return r.scalar_one_or_none()


async def compute_title(session: AsyncSession, user_id: int) -> str:
    user = await get_user(session, user_id)
    if not user:
        return TITLES[0][2]
    family = await get_family_members(session, user_id)
    size   = len(family)
    karma  = user.karma
    title  = TITLES[0][2]
    for min_size, min_karma, t in TITLES:
        if size >= min_size and karma >= min_karma:
            title = t
    return title


# ─── GROUP SETTINGS ──────────────────────────────────────────────────────────

async def get_settings(session: AsyncSession, group_id: int) -> GroupSettings:
    r = await session.execute(select(GroupSettings).where(GroupSettings.group_id == group_id))
    s = r.scalar_one_or_none()
    if not s:
        s = GroupSettings(group_id=group_id)
        session.add(s)
        await session.commit()
    return s


# ─── RELATIONSHIPS ────────────────────────────────────────────────────────────

async def get_relationships(session: AsyncSession, user_id: int, group_id: Optional[int] = None) -> List[Relationship]:
    cond = [or_(Relationship.user_id == user_id, Relationship.related_user_id == user_id)]
    if group_id is not None:
        cond.append(or_(Relationship.group_id == group_id, Relationship.group_id.is_(None)))
    r = await session.execute(select(Relationship).where(and_(*cond)))
    return list(r.scalars().all())


async def get_spouse(session: AsyncSession, user_id: int, group_id: Optional[int] = None) -> Optional[Relationship]:
    cond = [
        or_(Relationship.user_id == user_id, Relationship.related_user_id == user_id),
        Relationship.relation_type == RelationType.SPOUSE,
    ]
    if group_id is not None:
        cond.append(or_(Relationship.group_id == group_id, Relationship.group_id.is_(None)))
    r = await session.execute(select(Relationship).where(and_(*cond)))
    return r.scalar_one_or_none()


async def get_family_members(session: AsyncSession, user_id: int) -> List[int]:
    """Retourne uniquement conjoint + enfants/parents. Les amis sont exclus."""
    rels = await get_relationships(session, user_id)
    members = set()
    for rel in rels:
        if rel.relation_type == RelationType.FRIEND:
            continue
        members.add(rel.user_id)
        members.add(rel.related_user_id)
    members.discard(user_id)
    return list(members)


async def add_relationship(session: AsyncSession, uid: int, rid: int,
                           rel_type: RelationType, group_id: Optional[int]) -> Relationship:
    rel = Relationship(user_id=uid, related_user_id=rid, relation_type=rel_type, group_id=group_id)
    session.add(rel)
    await session.commit()
    return rel


async def remove_relationship(session: AsyncSession, uid: int, rid: int, rel_type: RelationType):
    await session.execute(
        delete(Relationship).where(
            and_(
                or_(
                    and_(Relationship.user_id == uid, Relationship.related_user_id == rid),
                    and_(Relationship.user_id == rid, Relationship.related_user_id == uid),
                ),
                Relationship.relation_type == rel_type,
            )
        )
    )
    await session.commit()


async def relationship_exists(session: AsyncSession, uid: int, rid: int, rel_type: RelationType) -> bool:
    r = await session.execute(
        select(Relationship).where(
            and_(
                or_(
                    and_(Relationship.user_id == uid, Relationship.related_user_id == rid),
                    and_(Relationship.user_id == rid, Relationship.related_user_id == uid),
                ),
                Relationship.relation_type == rel_type,
            )
        )
    )
    return r.scalar_one_or_none() is not None


# ─── PENDING REQUESTS ─────────────────────────────────────────────────────────

async def create_request(session: AsyncSession, from_id: int, to_id: int,
                          req_type: RequestType, group_id: int, msg_id: int) -> PendingRequest:
    await session.execute(
        delete(PendingRequest).where(
            and_(PendingRequest.from_user_id == from_id,
                 PendingRequest.to_user_id   == to_id,
                 PendingRequest.request_type  == req_type)
        )
    )
    req = PendingRequest(
        from_user_id = from_id,
        to_user_id   = to_id,
        request_type  = req_type,
        group_id     = group_id,
        message_id   = msg_id,
        expires_at   = datetime.utcnow() + timedelta(seconds=REQUEST_TIMEOUT),
    )
    session.add(req)
    await session.commit()
    return req


async def get_request(session: AsyncSession, req_id: int) -> Optional[PendingRequest]:
    r = await session.execute(select(PendingRequest).where(PendingRequest.id == req_id))
    return r.scalar_one_or_none()


async def delete_request(session: AsyncSession, req_id: int):
    await session.execute(delete(PendingRequest).where(PendingRequest.id == req_id))
    await session.commit()


# ─── GARDEN ───────────────────────────────────────────────────────────────────

async def get_garden(session: AsyncSession, user_id: int, group_id: int) -> List[Garden]:
    r = await session.execute(
        select(Garden).where(
            and_(Garden.user_id == user_id, Garden.group_id == group_id, Garden.harvested == False)
        )
    )
    return list(r.scalars().all())


async def plant(session: AsyncSession, user_id: int, group_id: int, slot: int, plant_type: str) -> Garden:
    g = Garden(user_id=user_id, group_id=group_id, slot=slot, plant_type=plant_type)
    session.add(g)
    await session.commit()
    return g


async def harvest_plant(session: AsyncSession, garden_id: int) -> int:
    r = await session.execute(select(Garden).where(Garden.id == garden_id))
    g = r.scalar_one_or_none()
    if not g:
        return 0
    g.harvested = True
    value = PLANT_TYPES.get(g.plant_type, {}).get("value", 0)
    if value > 0:
        await session.execute(
            text("UPDATE users SET coins = CAST(coins AS BIGINT) + CAST(:amt AS BIGINT) WHERE user_id = :uid"),
            {"amt": value, "uid": g.user_id}
        )
    await session.commit()
    return value


# ─── DAILY WAIFU ─────────────────────────────────────────────────────────────

async def get_or_set_waifu(session: AsyncSession, group_id: int, family_ids: List[int]) -> int:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    r = await session.execute(
        select(DailyWaifu).where(and_(DailyWaifu.group_id == group_id, DailyWaifu.date == today))
    )
    w = r.scalar_one_or_none()
    if w:
        return w.waifu_user_id
    if not family_ids:
        return 0
    import random
    chosen = random.choice(family_ids)
    dw = DailyWaifu(group_id=group_id, date=today, waifu_user_id=chosen)
    session.add(dw)
    await session.commit()
    return chosen


# ─── KARMA ────────────────────────────────────────────────────────────────────

async def vote_karma(session: AsyncSession, voter_id: int, target_id: int,
                     group_id: int, value: int) -> str:
    if voter_id == target_id:
        return "self"
    today = datetime.utcnow().strftime("%Y-%m-%d")
    r = await session.execute(
        select(KarmaVote).where(
            and_(KarmaVote.voter_id == voter_id, KarmaVote.target_id == target_id,
                 KarmaVote.group_id == group_id, KarmaVote.date == today)
        )
    )
    if r.scalar_one_or_none():
        return "already"
    vote = KarmaVote(voter_id=voter_id, target_id=target_id, group_id=group_id, date=today)
    session.add(vote)
    r2 = await session.execute(select(User).where(User.user_id == target_id))
    target = r2.scalar_one_or_none()
    if target:
        target.karma += value
    await session.commit()
    return "ok"


# ─── INHERITANCE ──────────────────────────────────────────────────────────────

async def process_inheritance(session: AsyncSession, user_id: int) -> dict:
    user = await get_user(session, user_id)
    if not user:
        return {}
    family_ids   = await get_family_members(session, user_id)
    coins_each   = 0
    oldest_child = None
    if family_ids:
        total      = int(user.coins * 0.8)
        coins_each = total // len(family_ids)
        for fid in family_ids:
            await session.execute(
                text("UPDATE users SET coins = CAST(coins AS BIGINT) + CAST(:amt AS BIGINT) WHERE user_id = :uid"),
                {"amt": coins_each, "uid": fid}
            )
        r2 = await session.execute(
            select(Relationship).where(
                and_(Relationship.user_id == user_id,
                     Relationship.relation_type == RelationType.PARENT)
            ).order_by(Relationship.created_at.asc())
        )
        first_child_rel = r2.scalars().first()
        if first_child_rel:
            oldest_child = first_child_rel.related_user_id
    await session.execute(
        delete(Relationship).where(
            or_(Relationship.user_id == user_id, Relationship.related_user_id == user_id)
        )
    )
    user.coins       = 0
    user.family_name = None
    await session.commit()
    return {"coins_each": coins_each, "members": family_ids, "oldest_child": oldest_child}


# ─── LEADERBOARD ─────────────────────────────────────────────────────────────

async def get_leaderboard(session: AsyncSession, limit: int = 10) -> List[dict]:
    r     = await session.execute(select(User))
    users = list(r.scalars().all())
    ranked = []
    for u in users:
        fam = await get_family_members(session, u.user_id)
        ranked.append({"user": u, "size": len(fam)})
    ranked.sort(key=lambda x: x["size"], reverse=True)
    return ranked[:limit]


async def get_richlist(session: AsyncSession, limit: int = 10) -> List[User]:
    r = await session.execute(select(User).order_by(User.coins.desc()).limit(limit))
    return list(r.scalars().all())


# ─── ANNIVERSARIES ────────────────────────────────────────────────────────────

async def get_anniversaries_today(session: AsyncSession) -> List[Relationship]:
    today = datetime.utcnow()
    r     = await session.execute(
        select(Relationship).where(Relationship.relation_type == RelationType.SPOUSE)
    )
    rels    = list(r.scalars().all())
    results = []
    for rel in rels:
        if (rel.created_at.month == today.month and
                rel.created_at.day == today.day and
                rel.created_at.year != today.year):
            results.append(rel)
    return results


# ─── ECONOMY ──────────────────────────────────────────────────────────────────

MAX_COINS = 9_000_000_000_000_000_000  # max BIGINT PostgreSQL


async def add_coins(session: AsyncSession, user_id: int, amount: int) -> int:
    """Ajoute (ou retire si négatif) des coins. Passe str() pour forcer bigint côté asyncpg."""
    import asyncpg
    uid = str(int(user_id))
    amt = str(int(amount))
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await conn.execute(
            "UPDATE users SET coins = GREATEST(0, coins::bigint + $1::bigint) WHERE user_id = $2::bigint",
            amt, uid
        )
        row = await conn.fetchrow(
            "SELECT coins FROM users WHERE user_id = $1::bigint", uid
        )
    finally:
        await conn.close()
    return int(row["coins"]) if row else 0


async def set_coins(user_id: int, amount: int) -> int:
    """Définit le solde exact (pour /setcoins admin). Passe str() pour forcer bigint."""
    import asyncpg
    uid = str(int(user_id))
    amt = str(int(amount))
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await conn.execute(
            "UPDATE users SET coins = $1::bigint WHERE user_id = $2::bigint",
            amt, uid
        )
        row = await conn.fetchrow(
            "SELECT coins FROM users WHERE user_id = $1::bigint", uid
        )
    finally:
        await conn.close()
    return int(row["coins"]) if row else 0


async def transfer_coins(session: AsyncSession, from_id: int, to_id: int, amount: int) -> str:
    """Transfère des coins. Passe str() pour forcer bigint côté asyncpg."""
    import asyncpg
    uid_from = str(int(from_id))
    uid_to   = str(int(to_id))
    amt      = str(int(amount))
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        row = await conn.fetchrow(
            "SELECT coins FROM users WHERE user_id = $1::bigint", uid_from
        )
        if not row:
            return "not_found"
        row2 = await conn.fetchrow(
            "SELECT coins FROM users WHERE user_id = $1::bigint", uid_to
        )
        if not row2:
            return "not_found"
        if int(row["coins"]) < int(amt):
            return "insufficient"
        await conn.execute(
            "UPDATE users SET coins = coins::bigint - $1::bigint WHERE user_id = $2::bigint",
            amt, uid_from
        )
        await conn.execute(
            "UPDATE users SET coins = coins::bigint + $1::bigint WHERE user_id = $2::bigint",
            amt, uid_to
        )
    finally:
        await conn.close()
    return "ok"


async def claim_daily(session: AsyncSession, user_id: int) -> dict:
    import random
    r    = await session.execute(select(User).where(User.user_id == user_id))
    user = r.scalar_one_or_none()
    if not user:
        return {"status": "not_found"}
    now_key = datetime.utcnow().strftime("%Y-%m-%d")
    if user.last_daily == now_key:
        return {"status": "already"}
    amount          = random.randint(500, 3_000)
    await session.execute(
        text("UPDATE users SET coins = CAST(coins AS BIGINT) + CAST(:amt AS BIGINT), last_daily = :ld WHERE user_id = :uid"),
        {"amt": amount, "ld": now_key, "uid": user_id}
    )
    await session.commit()
    return {"status": "ok", "amount": amount, "balance": user.coins + amount}


async def claim_work(session: AsyncSession, user_id: int) -> dict:
    import random
    r    = await session.execute(select(User).where(User.user_id == user_id))
    user = r.scalar_one_or_none()
    if not user:
        return {"status": "not_found"}
    now = datetime.utcnow()
    if user.last_work and (now - user.last_work).total_seconds() < 8 * 3600:
        wait = int((8 * 3600 - (now - user.last_work).total_seconds()) / 60)
        return {"status": "cooldown", "wait_min": wait}
    amount         = random.randint(200, 2_000)
    await session.execute(
        text("UPDATE users SET coins = CAST(coins AS BIGINT) + CAST(:amt AS BIGINT), last_work = :lw WHERE user_id = :uid"),
        {"amt": amount, "lw": now, "uid": user_id}
    )
    await session.commit()
    return {"status": "ok", "amount": amount, "balance": user.coins + amount}


# ─── BETS ─────────────────────────────────────────────────────────────────────

async def create_bet(session: AsyncSession, proposer_id: int, group_id: int,
                     amount: int, description: str) -> Optional[UserBet]:
    r = await session.execute(select(User).where(User.user_id == proposer_id))
    u = r.scalar_one_or_none()
    if not u or u.coins < amount:
        return None
    await session.execute(
        text("UPDATE users SET coins = CAST(coins AS BIGINT) - CAST(:amt AS BIGINT) WHERE user_id = :uid"),
        {"amt": amount, "uid": proposer_id}
    )
    bet = UserBet(
        proposer_id  = proposer_id,
        group_id     = group_id,
        amount       = amount,
        description  = description,
        expires_at   = datetime.utcnow() + timedelta(hours=24),
    )
    session.add(bet)
    await session.commit()
    return bet


async def accept_bet(session: AsyncSession, bet_id: int, acceptor_id: int) -> str:
    r = await session.execute(select(UserBet).where(UserBet.id == bet_id))
    bet = r.scalar_one_or_none()
    if not bet:
        return "not_found"
    if bet.status != "pending":
        return "not_pending"
    if bet.proposer_id == acceptor_id:
        return "self"
    if datetime.utcnow() > bet.expires_at:
        bet.status = "cancelled"
        await session.commit()
        return "expired"
    r2 = await session.execute(select(User).where(User.user_id == acceptor_id))
    u  = r2.scalar_one_or_none()
    if not u or u.coins < bet.amount:
        return "insufficient"
    await session.execute(
        text("UPDATE users SET coins = CAST(coins AS BIGINT) - CAST(:amt AS BIGINT) WHERE user_id = :uid"),
        {"amt": bet.amount, "uid": acceptor_id}
    )
    bet.target_id = acceptor_id
    bet.status    = "active"
    await session.commit()
    return "ok"


async def resolve_bet(session: AsyncSession, bet_id: int, winner_id: int, resolver_id: int) -> str:
    r = await session.execute(select(UserBet).where(UserBet.id == bet_id))
    bet = r.scalar_one_or_none()
    if not bet:
        return "not_found"
    if bet.status != "active":
        return "not_active"
    if resolver_id not in (bet.proposer_id, bet.target_id):
        return "not_participant"
    if winner_id not in (bet.proposer_id, bet.target_id):
        return "invalid_winner"
    r2 = await session.execute(select(User).where(User.user_id == winner_id))
    winner = r2.scalar_one_or_none()
    if winner:
        await session.execute(
            text("UPDATE users SET coins = CAST(coins AS BIGINT) + CAST(:amt AS BIGINT) WHERE user_id = :uid"),
            {"amt": bet.amount * 2, "uid": winner_id}
        )
    bet.status    = "done"
    bet.winner_id = winner_id
    await session.commit()
    return "ok"


# ─── COMPTE COMMUN (COUPLE) ───────────────────────────────────────────────────

async def get_couple_account(session: AsyncSession, user_id: int) -> Optional[CoupleAccount]:
    r = await session.execute(
        select(CoupleAccount).where(
            or_(CoupleAccount.user1_id == user_id, CoupleAccount.user2_id == user_id)
        )
    )
    return r.scalar_one_or_none()


async def create_couple_account(session: AsyncSession, user1_id: int, user2_id: int) -> Optional[CoupleAccount]:
    existing = await get_couple_account(session, user1_id)
    if existing:
        return None
    account = CoupleAccount(user1_id=user1_id, user2_id=user2_id, balance=0)
    session.add(account)
    await session.commit()
    return account


async def couple_deposit(session: AsyncSession, user_id: int, amount: int) -> str:
    """Dépose vers compte commun. Passe str() pour forcer bigint."""
    import asyncpg
    uid = str(int(user_id))
    amt = str(int(amount))
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        row = await conn.fetchrow(
            "SELECT coins FROM users WHERE user_id = $1::bigint", uid
        )
        if not row:
            return "not_found"
        if int(row["coins"]) < int(amt):
            return "insufficient"
        # Trouver le compte commun via la table couple_accounts
        acc_row = await conn.fetchrow(
            """
            SELECT id, balance FROM couple_accounts
            WHERE user1_id = $1::bigint OR user2_id = $1::bigint
            LIMIT 1
            """,
            uid,
        )
        if not acc_row:
            return "no_account"
        await conn.execute(
            "UPDATE users SET coins = coins::bigint - $1::bigint WHERE user_id = $2::bigint",
            amt, uid,
        )
        await conn.execute(
            "UPDATE couple_accounts SET balance = balance::bigint + $1::bigint WHERE id = $2",
            amt, str(acc_row["id"]),
        )
    finally:
        await conn.close()
    return "ok"


async def couple_withdraw(session: AsyncSession, user_id: int, amount: int) -> str:
    """Retire du compte commun. Passe str() pour forcer bigint."""
    import asyncpg
    uid = str(int(user_id))
    amt = str(int(amount))
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        acc_row = await conn.fetchrow(
            """
            SELECT id, balance FROM couple_accounts
            WHERE user1_id = $1::bigint OR user2_id = $1::bigint
            LIMIT 1
            """,
            uid,
        )
        if not acc_row:
            return "no_account"
        if int(acc_row["balance"]) < int(amt):
            return "insufficient"
        user_row = await conn.fetchrow(
            "SELECT user_id FROM users WHERE user_id = $1::bigint", uid
        )
        if not user_row:
            return "not_found"
        await conn.execute(
            "UPDATE couple_accounts SET balance = balance::bigint - $1::bigint WHERE id = $2",
            amt, str(acc_row["id"]),
        )
        await conn.execute(
            "UPDATE users SET coins = coins::bigint + $1::bigint WHERE user_id = $2::bigint",
            amt, uid,
        )
    finally:
        await conn.close()
    return "ok"


async def dissolve_couple_account(session: AsyncSession, user1_id: int, user2_id: int):
    """Dissout le compte commun. Passe str() pour forcer bigint."""
    import asyncpg
    uid1 = str(int(user1_id))
    uid2 = str(int(user2_id))
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        acc_row = await conn.fetchrow(
            """
            SELECT id, balance FROM couple_accounts
            WHERE user1_id = $1::bigint OR user2_id = $1::bigint
            LIMIT 1
            """,
            uid1,
        )
        if not acc_row:
            return
        share = int(acc_row["balance"]) // 2
        for uid in (uid1, uid2):
            await conn.execute(
                "UPDATE users SET coins = coins::bigint + $1::bigint WHERE user_id = $2::bigint",
                str(share), uid,
            )
        await conn.execute(
            "DELETE FROM couple_accounts WHERE id = $1", str(acc_row["id"])
        )
    finally:
        await conn.close()


async def deduct_for_game(session: AsyncSession, user_id: int, amount: int) -> str:
    """Déduit pour un jeu. Passe str() pour forcer bigint."""
    import asyncpg
    uid = str(int(user_id))
    amt = str(int(amount))
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        row = await conn.fetchrow(
            "SELECT coins FROM users WHERE user_id = $1::bigint", str(uid)
        )
        if not row:
            return "insufficient"
        if int(row["coins"]) >= int(amt):
            await conn.execute(
                "UPDATE users SET coins = coins::bigint - $1::bigint WHERE user_id = $2::bigint",
                amt, uid,
            )
            return "perso"
        # Essayer le compte commun
        acc_row = await conn.fetchrow(
            """
            SELECT id, balance FROM couple_accounts
            WHERE user1_id = $1::bigint OR user2_id = $1::bigint
            LIMIT 1
            """,
            uid,
        )
        if acc_row and int(acc_row["balance"]) >= int(amt):
            await conn.execute(
                "UPDATE couple_accounts SET balance = balance::bigint - $1::bigint WHERE id = $2",
                amt, str(acc_row["id"]),
            )
            return "couple"
        return "insufficient"
    finally:
        await conn.close()


async def add_coins_smart(session: AsyncSession, user_id: int, amount: int):
    """Ajoute gains perso. Passe str() pour forcer bigint."""
    import asyncpg
    uid = str(int(user_id))
    amt = str(int(amount))
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await conn.execute(
            "UPDATE users SET coins = coins::bigint + $1::bigint WHERE user_id = $2::bigint",
            amt, uid,
        )
    finally:
        await conn.close()
