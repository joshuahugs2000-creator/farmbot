from sqlalchemy import (
    Column, BigInteger, String, DateTime, Boolean,
    Integer, ForeignKey, Enum as SAEnum, Text, Float,
)
from sqlalchemy.orm import DeclarativeBase
from datetime import datetime
import enum


class Base(DeclarativeBase):
    pass


class RelationType(enum.Enum):
    SPOUSE = "spouse"
    PARENT = "parent"
    FRIEND = "friend"


class RequestType(enum.Enum):
    MARRY  = "marry"
    ADOPT  = "adopt"
    FRIEND = "friend"


class User(Base):
    __tablename__ = "users"
    user_id       = Column(BigInteger, primary_key=True)
    username      = Column(String(255), nullable=True)
    first_name    = Column(String(255), nullable=False)
    photo_file_id = Column(String(512), nullable=True)
    profile_color = Column(String(20), default="blue")
    coins         = Column(BigInteger, default=10_000)
    karma         = Column(Integer, default=0)
    family_name   = Column(String(100), nullable=True)
    last_daily    = Column(String(20), nullable=True)
    last_work     = Column(DateTime, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    is_banned     = Column(Boolean, default=False)

    # ─── DIPLÔMES ─────────────────────────────────────────────────────────────
    diplome_bac     = Column(Boolean, default=False)
    diplome_licence = Column(Boolean, default=False)
    diplome_master  = Column(Boolean, default=False)
    diplome_mba     = Column(Boolean, default=False)
    diplome_domain  = Column(String(50), nullable=True)   # finance, informatique, ...
    exam_cooldown   = Column(DateTime, nullable=True)      # bloqué jusqu'à cette date


class GroupSettings(Base):
    __tablename__ = "group_settings"
    group_id       = Column(BigInteger, primary_key=True)
    mode           = Column(String(20), default="global")
    garden_enabled = Column(Boolean, default=True)
    waifu_enabled  = Column(Boolean, default=True)


class Relationship(Base):
    __tablename__ = "relationships"
    id              = Column(Integer, primary_key=True, autoincrement=True)
    user_id         = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    related_user_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    relation_type   = Column(SAEnum(RelationType), nullable=False)
    group_id        = Column(BigInteger, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)


class PendingRequest(Base):
    __tablename__ = "pending_requests"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    from_user_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    to_user_id   = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    request_type = Column(SAEnum(RequestType), nullable=False)
    group_id     = Column(BigInteger, nullable=False)
    message_id   = Column(BigInteger, nullable=True)
    expires_at   = Column(DateTime, nullable=False)
    created_at   = Column(DateTime, default=datetime.utcnow)


class Garden(Base):
    __tablename__ = "gardens"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    user_id    = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    group_id   = Column(BigInteger, nullable=False)
    slot       = Column(Integer, nullable=False)
    plant_type = Column(String(50), nullable=False)
    planted_at = Column(DateTime, default=datetime.utcnow)
    harvested  = Column(Boolean, default=False)


class DailyWaifu(Base):
    __tablename__ = "daily_waifu"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    group_id      = Column(BigInteger, nullable=False)
    date          = Column(String(10), nullable=False)
    waifu_user_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)


class KarmaVote(Base):
    __tablename__ = "karma_votes"
    id        = Column(Integer, primary_key=True, autoincrement=True)
    voter_id  = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    target_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    group_id  = Column(BigInteger, nullable=False)
    date      = Column(String(10), nullable=False)


class UserBet(Base):
    __tablename__ = "user_bets"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    proposer_id   = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    target_id     = Column(BigInteger, ForeignKey("users.user_id"), nullable=True)
    group_id      = Column(BigInteger, nullable=False)
    amount        = Column(BigInteger, nullable=False)
    description   = Column(String(500), nullable=False)
    status        = Column(String(20), default="pending")
    winner_id     = Column(BigInteger, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    expires_at    = Column(DateTime, nullable=False)


# ─── BANQUE ───────────────────────────────────────────────────────────────────

class BankAccount(Base):
    __tablename__ = "bank_accounts"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    user_id       = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    bank_id       = Column(String(30), nullable=False)
    balance       = Column(BigInteger, default=0)
    last_interest = Column(DateTime, nullable=True)
    opened_at     = Column(DateTime, default=datetime.utcnow)


class Loan(Base):
    __tablename__ = "loans"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    user_id       = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    bank_id       = Column(String(30), nullable=False)
    amount        = Column(BigInteger, nullable=False)
    remaining     = Column(BigInteger, nullable=False)
    interest_rate = Column(Float, nullable=False)
    due_at        = Column(DateTime, nullable=False)
    created_at    = Column(DateTime, default=datetime.utcnow)
    status        = Column(String(20), default="active")


# ─── COMPTE COMMUN ────────────────────────────────────────────────────────────

class CoupleAccount(Base):
    __tablename__ = "couple_accounts"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    user1_id   = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    user2_id   = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    balance    = Column(BigInteger, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


# ─── INVESTISSEMENTS ──────────────────────────────────────────────────────────

class Investment(Base):
    __tablename__ = "investments"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    user_id    = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    asset_id   = Column(String(50), nullable=False)
    quantity   = Column(Integer, default=1)
    buy_price  = Column(BigInteger, nullable=False)
    bought_at  = Column(DateTime, default=datetime.utcnow)
    sold_at    = Column(DateTime, nullable=True)
    sell_price = Column(BigInteger, nullable=True)
    status     = Column(String(20), default="active")


# ─── LOTERIE ──────────────────────────────────────────────────────────────────

class LotterySession(Base):
    __tablename__ = "lottery_sessions"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    group_id     = Column(BigInteger, nullable=False)
    creator_id   = Column(BigInteger, nullable=True)   # NULL = lancée par le bot
    ticket_price = Column(BigInteger, nullable=False)
    loto_type    = Column(String(10), nullable=False, default="private")  # private | bot
    status       = Column(String(10), default="active")   # active | closed
    winner_id    = Column(BigInteger, nullable=True)
    pot          = Column(BigInteger, default=0)
    created_at   = Column(DateTime, default=datetime.utcnow)
    drawn_at     = Column(DateTime, nullable=True)


class LotteryTicket(Base):
    __tablename__ = "lottery_tickets"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("lottery_sessions.id"), nullable=False)
    user_id    = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)




# ─── GROUPES DU BOT ──────────────────────────────────────────────────────────

class BotGroup(Base):
    __tablename__ = "bot_groups"
    group_id     = Column(BigInteger, primary_key=True)
    title        = Column(String(255), nullable=True)
    username     = Column(String(255), nullable=True)
    chat_type    = Column(String(20), nullable=True)
    member_count = Column(Integer, nullable=True)
    invite_link  = Column(String(512), nullable=True)
    is_active    = Column(Boolean, default=True)
    first_seen   = Column(DateTime, default=datetime.utcnow)
    last_seen    = Column(DateTime, default=datetime.utcnow)

# ─── LOGS D'ACTIVITÉ ─────────────────────────────────────────────────────────

class ActivityLog(Base):
    __tablename__ = "activity_logs"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    user_id    = Column(BigInteger, nullable=False)   # pas de FK pour éviter violations
    username   = Column(String(255), nullable=True)
    command    = Column(String(100), nullable=False)
    args       = Column(String(500), nullable=True)
    amount     = Column(BigInteger, nullable=True)
    result     = Column(String(50), nullable=True)
    group_id   = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
