from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, and_, or_, delete, func, text
from .models import (
    Base, User, GroupSettings, Relationship, PendingRequest,
    Garden, DailyWaifu, KarmaVote, UserBet, RelationType, RequestType,
    CoupleAccount, ActivityLog, BotGroup,
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
    import logging as _logging
    _log = _logging.getLogger(__name__)

    # ── ÉTAPE 1 : Colonnes critiques en SQL pur AVANT tout accès ORM ─────────
    # On passe par une connexion raw pour ne pas dépendre du modèle SQLAlchemy.
    critical_cols = [
        ("karma",         "INTEGER DEFAULT 0"),
        ("harvest_count", "INTEGER DEFAULT 0"),
        ("photo_file_id", "VARCHAR(512)"),
        ("profile_color", "VARCHAR(20) DEFAULT 'blue'"),
        ("family_name",   "VARCHAR(100)"),
    ]
    try:
        async with engine.begin() as conn:
            for col, coltype in critical_cols:
                sql = f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {coltype}"
                try:
                    await conn.execute(text(sql))
                    _log.info(f"Migration OK : {sql}")
                except Exception as e:
                    _log.debug(f"Migration skipped ({col}): {e}")
    except Exception as e:
        _log.warning(f"pre_migrations: table users n'existe pas encore ({e})")

    # ── ÉTAPE 2 : Créer toutes les tables manquantes ──────────────────────────
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    migrations = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_daily  VARCHAR(20)  DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_work   TIMESTAMP    DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS coins       BIGINT       DEFAULT 10000",
        "ALTER TABLE users ALTER COLUMN coins TYPE BIGINT USING coins::BIGINT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned   BOOLEAN      NOT NULL DEFAULT FALSE",
        "UPDATE users SET coins = 10000 WHERE coins IS NULL OR coins < 10000",
        # karma & harvest
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS karma         INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS harvest_count INTEGER DEFAULT 0",
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
            user_id    BIGINT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        # ─── TABLE DES LOGS D'ACTIVITÉ ────────────────────────────────────────
        """CREATE TABLE IF NOT EXISTS activity_logs (
            id         SERIAL PRIMARY KEY,
            user_id    BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            username   VARCHAR(255),
            command    VARCHAR(100) NOT NULL,
            args       VARCHAR(500),
            amount     BIGINT,
            result     VARCHAR(50),
            group_id   BIGINT,
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_activity_logs_user_date ON activity_logs (user_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_activity_logs_date ON activity_logs (created_at)",
        """CREATE TABLE IF NOT EXISTS bot_groups (
            group_id     BIGINT PRIMARY KEY,
            title        VARCHAR(255),
            username     VARCHAR(255),
            chat_type    VARCHAR(20),
            member_count INTEGER,
            invite_link  VARCHAR(512),
            is_active    BOOLEAN DEFAULT TRUE,
            first_seen   TIMESTAMP DEFAULT NOW(),
            last_seen    TIMESTAMP DEFAULT NOW()
        )""",
    ]
    # Chaque migration dans sa propre transaction pour éviter les rollbacks en cascade
    for sql in migrations:
        try:
            async with engine.begin() as conn:
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

# Niveaux de karma : (karma_min, emoji, label, daily_bonus_pct, work_cooldown_reduction_pct)
KARMA_LEVELS = [
    (100,  "👑", "Légende",        50,  30),
    (50,   "🌟", "Vénéré",         35,  20),
    (25,   "😊", "Populaire",      20,  10),
    (10,   "🙂", "Apprécié",       10,   5),
    (0,    "🧍", "Citoyen lambda",  0,   0),
    (-19,  "😕", "En disgrâce",    -5,   0),
    (-49,  "😤", "Mal aimé",      -10,   0),
    (-999, "😈", "Paria",         -25,   0),
]


def get_karma_level(karma: int) -> dict:
    """Retourne le niveau de karma correspondant."""
    for threshold, emoji, label, daily_pct, work_red in KARMA_LEVELS:
        if karma >= threshold:
            return {
                "emoji":    emoji,
                "label":    label,
                "daily_pct": daily_pct,
                "work_red":  work_red,
                "threshold": threshold,
            }
    return {"emoji": "😈", "label": "Paria", "daily_pct": -25, "work_red": 0, "threshold": -999}


def karma_bar(karma: int, width: int = 10) -> str:
    """Génère une barre de progression visuelle pour le karma."""
    # On affiche le karma dans une plage [-50, 100]
    low, high = -50, 100
    clamped = max(low, min(high, karma))
    ratio = (clamped - low) / (high - low)
    filled = round(ratio * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}]"


async def adjust_karma(session: AsyncSession, user_id: int, delta: int) -> Optional[int]:
    """Modifie le karma d'un utilisateur et retourne le nouveau karma."""
    r = await session.execute(select(User).where(User.user_id == user_id))
    u = r.scalar_one_or_none()
    if not u:
        return None
    u.karma = (u.karma or 0) + delta
    await session.commit()
    return u.karma


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
    """Ajoute (ou retire si négatif) des $. F-strings pour éviter l'inférence de type asyncpg."""
    import asyncpg
    uid = int(user_id)
    amt = int(amount)
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await conn.execute(
            f"UPDATE users SET coins = GREATEST(0, coins::bigint + {amt}::bigint) WHERE user_id = {uid}::bigint"
        )
        row = await conn.fetchrow(
            f"SELECT coins FROM users WHERE user_id = {uid}::bigint"
        )
    finally:
        await conn.close()
    return int(row["coins"]) if row else 0


async def set_coins(user_id: int, amount: int) -> int:
    """Définit le solde exact. F-strings pour éviter l'inférence de type asyncpg."""
    import asyncpg
    uid = int(user_id)
    amt = int(amount)
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await conn.execute(
            f"UPDATE users SET coins = {amt}::bigint WHERE user_id = {uid}::bigint"
        )
        row = await conn.fetchrow(
            f"SELECT coins FROM users WHERE user_id = {uid}::bigint"
        )
    finally:
        await conn.close()
    return int(row["coins"]) if row else 0


async def transfer_coins(session: AsyncSession, from_id: int, to_id: int, amount: int) -> str:
    """Transfère des $. F-strings pour éviter l'inférence de type asyncpg."""
    import asyncpg
    uid_from = int(from_id)
    uid_to   = int(to_id)
    amt      = int(amount)
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        row = await conn.fetchrow(
            f"SELECT coins FROM users WHERE user_id = {uid_from}::bigint"
        )
        if not row:
            return "not_found"
        row2 = await conn.fetchrow(
            f"SELECT coins FROM users WHERE user_id = {uid_to}::bigint"
        )
        if not row2:
            return "not_found"
        if int(row["coins"]) < amt:
            return "insufficient"
        await conn.execute(
            f"UPDATE users SET coins = coins::bigint - {amt}::bigint WHERE user_id = {uid_from}::bigint"
        )
        await conn.execute(
            f"UPDATE users SET coins = coins::bigint + {amt}::bigint WHERE user_id = {uid_to}::bigint"
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
    base_amount = random.randint(5_000, 20_000)
    # Bonus/malus karma
    level = get_karma_level(user.karma or 0)
    pct   = level["daily_pct"]
    amount = max(100, int(base_amount * (1 + pct / 100)))
    await session.execute(
        text("UPDATE users SET coins = CAST(coins AS BIGINT) + CAST(:amt AS BIGINT), last_daily = :ld WHERE user_id = :uid"),
        {"amt": amount, "ld": now_key, "uid": user_id}
    )
    await session.commit()
    return {"status": "ok", "amount": amount, "balance": user.coins + amount,
            "karma_bonus_pct": pct, "karma_label": level["label"]}


async def claim_work(session: AsyncSession, user_id: int) -> dict:
    import random
    r    = await session.execute(select(User).where(User.user_id == user_id))
    user = r.scalar_one_or_none()
    if not user:
        return {"status": "not_found"}
    now = datetime.utcnow()
    # Cooldown réduit selon karma
    level       = get_karma_level(user.karma or 0)
    base_cd     = 8 * 3600   # 8h de base
    reduction   = level["work_red"] / 100
    cooldown    = int(base_cd * (1 - reduction))
    if user.last_work and (now - user.last_work).total_seconds() < cooldown:
        wait = int((cooldown - (now - user.last_work).total_seconds()) / 60)
        return {"status": "cooldown", "wait_min": wait}
    amount = random.randint(3_000, 30_000)
    await session.execute(
        text("UPDATE users SET coins = CAST(coins AS BIGINT) + CAST(:amt AS BIGINT), last_work = :lw WHERE user_id = :uid"),
        {"amt": amount, "lw": now, "uid": user_id}
    )
    await session.commit()
    return {"status": "ok", "amount": amount, "balance": user.coins + amount,
            "cooldown_h": round(cooldown / 3600, 1), "karma_label": level["label"]}


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
    """Dépose vers compte commun. F-strings pour éviter inférence de type asyncpg."""
    import asyncpg
    uid = int(user_id)
    amt = int(amount)
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        row = await conn.fetchrow(
            f"SELECT coins FROM users WHERE user_id = {uid}::bigint"
        )
        if not row:
            return "not_found"
        if int(row["coins"]) < amt:
            return "insufficient"
        # Trouver le compte commun via la table couple_accounts
        acc_row = await conn.fetchrow(
            f"SELECT id, balance FROM couple_accounts WHERE user1_id = {uid}::bigint OR user2_id = {uid}::bigint LIMIT 1"
        )
        if not acc_row:
            return "no_account"
        await conn.execute(
            f"UPDATE users SET coins = coins::bigint - {amt}::bigint WHERE user_id = {uid}::bigint"
        )
        await conn.execute(
            f"UPDATE couple_accounts SET balance = balance::bigint + {amt}::bigint WHERE id = {acc_row['id']}"
        )
    finally:
        await conn.close()
    return "ok"


async def couple_withdraw(session: AsyncSession, user_id: int, amount: int) -> str:
    """Retire du compte commun. F-strings pour éviter inférence de type asyncpg."""
    import asyncpg
    uid = int(user_id)
    amt = int(amount)
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        acc_row = await conn.fetchrow(
            f"SELECT id, balance FROM couple_accounts WHERE user1_id = {uid}::bigint OR user2_id = {uid}::bigint LIMIT 1"
        )
        if not acc_row:
            return "no_account"
        if int(acc_row["balance"]) < amt:
            return "insufficient"
        user_row = await conn.fetchrow(
            f"SELECT user_id FROM users WHERE user_id = {uid}::bigint"
        )
        if not user_row:
            return "not_found"
        await conn.execute(
            f"UPDATE couple_accounts SET balance = balance::bigint - {amt}::bigint WHERE id = {acc_row['id']}"
        )
        await conn.execute(
            f"UPDATE users SET coins = coins::bigint + {amt}::bigint WHERE user_id = {uid}::bigint"
        )
    finally:
        await conn.close()
    return "ok"


async def dissolve_couple_account(session: AsyncSession, user1_id: int, user2_id: int):
    """Dissout le compte commun. F-strings pour éviter inférence de type asyncpg."""
    import asyncpg
    uid1 = int(user1_id)
    uid2 = int(user2_id)
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        acc_row = await conn.fetchrow(
            f"SELECT id, balance FROM couple_accounts WHERE user1_id = {uid1}::bigint OR user2_id = {uid1}::bigint LIMIT 1"
        )
        if not acc_row:
            return
        share = int(acc_row["balance"]) // 2
        for uid in (uid1, uid2):
            await conn.execute(
                f"UPDATE users SET coins = coins::bigint + {share}::bigint WHERE user_id = {uid}::bigint"
            )
        await conn.execute(
            f"DELETE FROM couple_accounts WHERE id = {acc_row['id']}"
        )
    finally:
        await conn.close()


async def deduct_for_game(session: AsyncSession, user_id: int, amount: int) -> str:
    """Déduit pour un jeu. F-strings pour éviter inférence de type asyncpg."""
    import asyncpg
    uid = int(user_id)
    amt = int(amount)
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        row = await conn.fetchrow(
            f"SELECT coins FROM users WHERE user_id = {uid}::bigint"
        )
        if not row:
            return "insufficient"
        if int(row["coins"]) >= amt:
            await conn.execute(
                f"UPDATE users SET coins = coins::bigint - {amt}::bigint WHERE user_id = {uid}::bigint"
            )
            return "perso"
        # Essayer le compte commun
        acc_row = await conn.fetchrow(
            f"SELECT id, balance FROM couple_accounts WHERE user1_id = {uid}::bigint OR user2_id = {uid}::bigint LIMIT 1"
        )
        if acc_row and int(acc_row["balance"]) >= amt:
            await conn.execute(
                f"UPDATE couple_accounts SET balance = balance::bigint - {amt}::bigint WHERE id = {acc_row['id']}"
            )
            return "couple"
        return "insufficient"
    finally:
        await conn.close()


async def add_coins_smart(session: AsyncSession, user_id: int, amount: int):
    """Ajoute gains perso. F-strings pour éviter inférence de type asyncpg."""
    import asyncpg
    uid = int(user_id)
    amt = int(amount)
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await conn.execute(
            f"UPDATE users SET coins = coins::bigint + {amt}::bigint WHERE user_id = {uid}::bigint"
        )
    finally:
        await conn.close()


# ─── LISTE DE TOUS LES UTILISATEURS ──────────────────────────────────────────

async def get_all_users(session: AsyncSession) -> List[User]:
    """Retourne tous les utilisateurs enregistrés (ayant utilisé au moins une commande)."""
    r = await session.execute(select(User).order_by(User.created_at.asc()))
    return list(r.scalars().all())


# ─── ACTIVITY LOGS ────────────────────────────────────────────────────────────

async def init_logs_table() -> None:
    """Crée la table activity_logs si elle n'existe pas (appelé au démarrage)."""
    sql = """
        CREATE TABLE IF NOT EXISTS activity_logs (
            id         SERIAL PRIMARY KEY,
            user_id    BIGINT NOT NULL,
            username   VARCHAR(255),
            command    VARCHAR(100) NOT NULL,
            args       VARCHAR(500),
            amount     BIGINT,
            result     VARCHAR(50),
            group_id   BIGINT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """
    idx1 = "CREATE INDEX IF NOT EXISTS idx_alog_user_date ON activity_logs (user_id, created_at)"
    idx2 = "CREATE INDEX IF NOT EXISTS idx_alog_date ON activity_logs (created_at)"
    async with engine.begin() as conn:
        await conn.execute(text(sql))
        await conn.execute(text(idx1))
        await conn.execute(text(idx2))


async def log_action(
    session: AsyncSession,
    user_id: int,
    username: Optional[str],
    command: str,
    *,
    args: Optional[str] = None,
    amount: Optional[int] = None,
    result: Optional[str] = None,
    group_id: Optional[int] = None,
) -> None:
    """Enregistre une action utilisateur — SQL pur, aucune dépendance ORM."""
    try:
        await session.execute(
            text("""
                INSERT INTO activity_logs (user_id, username, command, args, amount, result, group_id, created_at)
                VALUES (:uid, :uname, :cmd, :args, :amount, :result, :gid, :now)
            """),
            {
                "uid":    user_id,
                "uname":  username,
                "cmd":    command,
                "args":   args,
                "amount": amount,
                "result": result,
                "gid":    group_id,
                "now":    datetime.utcnow(),
            }
        )
        await session.commit()
    except Exception:
        pass  # Ne jamais bloquer une commande à cause des logs


async def get_logs_for_user(
    session: AsyncSession,
    user_id: int,
    date_str: Optional[str] = None,
    limit: int = 50,
) -> list:
    """Retourne les logs d'un utilisateur pour une date donnée (YYYY-MM-DD) ou aujourd'hui."""
    if not date_str:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
    start = datetime.strptime(date_str, "%Y-%m-%d")
    end   = start + timedelta(days=1)
    try:
        r = await session.execute(
            text("""
                SELECT id, user_id, username, command, args, amount, result, group_id, created_at
                FROM activity_logs
                WHERE user_id = :uid
                  AND created_at >= :start
                  AND created_at <  :end
                ORDER BY created_at ASC
                LIMIT :lim
            """),
            {"uid": user_id, "start": start, "end": end, "lim": limit}
        )
        return r.fetchall()
    except Exception:
        # Table manquante → la créer et retourner vide
        await init_logs_table()
        return []


async def get_suspicious_users(session: AsyncSession) -> List[dict]:
    """Retourne la liste des comportements suspects du jour — SQL pur."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    start = datetime.strptime(today, "%Y-%m-%d")
    end   = start + timedelta(days=1)
    now   = datetime.utcnow()

    # Résumé par utilisateur pour aujourd'hui
    r = await session.execute(
        text("""
            SELECT user_id, username,
                   COUNT(*)        AS cmd_count,
                   COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 0) AS total_gain
            FROM activity_logs
            WHERE created_at >= :start AND created_at < :end
            GROUP BY user_id, username
            ORDER BY cmd_count DESC
        """),
        {"start": start, "end": end}
    )
    rows = r.fetchall()

    suspicious = []
    for row in rows:
        uid, uname, cmd_count, total_gain = row.user_id, row.username, row.cmd_count, row.total_gain
        flags = []

        if cmd_count >= 60:
            flags.append(f"🚨 {cmd_count} commandes aujourd'hui")
        if total_gain >= 500_000:
            flags.append(f"💰 +{int(total_gain):,} $ gagnés")

        # Rafale dans la dernière heure
        one_hour_ago = now - timedelta(hours=1)
        r2 = await session.execute(
            text("SELECT COUNT(*) FROM activity_logs WHERE user_id=:uid AND created_at>=:t"),
            {"uid": uid, "t": one_hour_ago}
        )
        hourly = r2.scalar() or 0
        if hourly >= 30:
            flags.append(f"⚡ {hourly} cmd/heure")

        # Jeux en rafale (30 min)
        thirty_ago = now - timedelta(minutes=30)
        r3 = await session.execute(
            text("""
                SELECT COUNT(*) FROM activity_logs
                WHERE user_id=:uid AND created_at>=:t
                  AND command IN ('rebet','casino','cockfight','ppc')
            """),
            {"uid": uid, "t": thirty_ago}
        )
        game_count = r3.scalar() or 0
        if game_count >= 10:
            flags.append(f"🎰 {game_count} jeux en 30 min")

        if flags:
            suspicious.append({
                "user_id":   uid,
                "username":  uname or str(uid),
                "flags":     flags,
                "cmd_count": cmd_count,
            })

    return suspicious


# ─── BOT GROUPS ───────────────────────────────────────────────────────────────

async def init_groups_table() -> None:
    sql = """
        CREATE TABLE IF NOT EXISTS bot_groups (
            group_id     BIGINT PRIMARY KEY,
            title        VARCHAR(255),
            username     VARCHAR(255),
            chat_type    VARCHAR(20),
            member_count INTEGER,
            invite_link  VARCHAR(512),
            is_active    BOOLEAN DEFAULT TRUE,
            first_seen   TIMESTAMP DEFAULT NOW(),
            last_seen    TIMESTAMP DEFAULT NOW()
        )
    """
    async with engine.begin() as conn:
        await conn.execute(text(sql))


async def upsert_group(
    group_id: int,
    title: str,
    username: Optional[str],
    chat_type: str,
    member_count: Optional[int] = None,
    invite_link: Optional[str] = None,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO bot_groups (group_id, title, username, chat_type, member_count, invite_link, is_active, first_seen, last_seen)
            VALUES (:gid, :title, :username, :chat_type, :member_count, :invite_link, TRUE, NOW(), NOW())
            ON CONFLICT (group_id) DO UPDATE SET
                title        = EXCLUDED.title,
                username     = EXCLUDED.username,
                chat_type    = EXCLUDED.chat_type,
                member_count = COALESCE(EXCLUDED.member_count, bot_groups.member_count),
                invite_link  = COALESCE(EXCLUDED.invite_link, bot_groups.invite_link),
                is_active    = TRUE,
                last_seen    = NOW()
        """), {"gid": group_id, "title": title, "username": username,
               "chat_type": chat_type, "member_count": member_count, "invite_link": invite_link})


async def mark_group_inactive(group_id: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE bot_groups SET is_active = FALSE WHERE group_id = :gid"),
            {"gid": group_id}
        )


async def get_all_groups(active_only: bool = True) -> list:
    try:
        async with engine.begin() as conn:
            if active_only:
                r = await conn.execute(text(
                    "SELECT * FROM bot_groups WHERE is_active = TRUE ORDER BY last_seen DESC"
                ))
            else:
                r = await conn.execute(text(
                    "SELECT * FROM bot_groups ORDER BY is_active DESC, last_seen DESC"
                ))
            return r.fetchall()
    except Exception:
        await init_groups_table()
        return []
