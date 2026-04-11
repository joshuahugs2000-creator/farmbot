from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, and_, or_, delete, func
from .models import (
    Base, User, GroupSettings, Relationship, PendingRequest,
    Garden, DailyWaifu, KarmaVote, UserBet, RelationType, RequestType,
)
from config import DATABASE_URL, REQUEST_TIMEOUT, PLANT_TYPES, GARDEN_SLOTS, TITLES
from datetime import datetime, timedelta
from typing import Optional, List

engine            = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


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
    rels = await get_relationships(session, user_id)
    members = set()
    for rel in rels:
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
    r2 = await session.execute(select(User).where(User.user_id == g.user_id))
    user = r2.scalar_one_or_none()
    if user:
        user.coins += value
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
            r = await session.execute(select(User).where(User.user_id == fid))
            m = r.scalar_one_or_none()
            if m:
                m.coins += coins_each
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

async def add_coins(session: AsyncSession, user_id: int, amount: int) -> int:
    """Ajoute (ou retire si négatif) des coins. Retourne le nouveau solde."""
    r    = await session.execute(select(User).where(User.user_id == user_id))
    user = r.scalar_one_or_none()
    if not user:
        return 0
    user.coins = max(0, user.coins + amount)
    await session.commit()
    return user.coins


async def transfer_coins(session: AsyncSession, from_id: int, to_id: int, amount: int) -> str:
    """Transfère des coins entre deux users. Retourne 'ok'|'insufficient'|'not_found'."""
    r1 = await session.execute(select(User).where(User.user_id == from_id))
    r2 = await session.execute(select(User).where(User.user_id == to_id))
    sender = r1.scalar_one_or_none()
    target = r2.scalar_one_or_none()
    if not sender or not target:
        return "not_found"
    if sender.coins < amount:
        return "insufficient"
    sender.coins -= amount
    target.coins += amount
    await session.commit()
    return "ok"


async def claim_daily(session: AsyncSession, user_id: int) -> dict:
    """Bonus quotidien. Retourne {'status': 'ok'|'already', 'amount': int, 'next': str}."""
    import random
    r    = await session.execute(select(User).where(User.user_id == user_id))
    user = r.scalar_one_or_none()
    if not user:
        return {"status": "not_found"}
    now_key = datetime.utcnow().strftime("%Y-%m-%d")
    if user.last_daily == now_key:
        return {"status": "already", "next": "demain"}
    amount       = random.randint(50_000, 300_000)
    user.coins  += amount
    user.last_daily = now_key
    await session.commit()
    return {"status": "ok", "amount": amount, "balance": user.coins}


async def claim_work(session: AsyncSession, user_id: int) -> dict:
    """Travail (cooldown 8h). Retourne {'status': 'ok'|'cooldown', 'amount': int, 'wait_min': int}."""
    import random
    r    = await session.execute(select(User).where(User.user_id == user_id))
    user = r.scalar_one_or_none()
    if not user:
        return {"status": "not_found"}
    now = datetime.utcnow()
    if user.last_work and (now - user.last_work).total_seconds() < 8 * 3600:
        wait = int((8 * 3600 - (now - user.last_work).total_seconds()) / 60)
        return {"status": "cooldown", "wait_min": wait}
    amount       = random.randint(10_000, 150_000)
    user.coins  += amount
    user.last_work = now
    await session.commit()
    return {"status": "ok", "amount": amount, "balance": user.coins}


# ─── BETS ─────────────────────────────────────────────────────────────────────

async def create_bet(session: AsyncSession, proposer_id: int, group_id: int,
                     amount: int, description: str) -> Optional[UserBet]:
    r = await session.execute(select(User).where(User.user_id == proposer_id))
    u = r.scalar_one_or_none()
    if not u or u.coins < amount:
        return None
    u.coins -= amount
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
    u.coins     -= bet.amount
    bet.target_id = acceptor_id
    bet.status    = "active"
    await session.commit()
    return "ok"


async def resolve_bet(session: AsyncSession, bet_id: int, winner_id: int,
                      resolver_id: int) -> str:
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
        winner.coins += bet.amount * 2
    bet.status    = "done"
    bet.winner_id = winner_id
    await session.commit()
    return "ok"
