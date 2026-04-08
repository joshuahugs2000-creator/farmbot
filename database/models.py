from sqlalchemy import (
    Column, BigInteger, String, DateTime, Boolean,
    Integer, ForeignKey, Enum as SAEnum, Text,
)
from sqlalchemy.orm import DeclarativeBase
from datetime import datetime
import enum


class Base(DeclarativeBase):
    pass


class RelationType(enum.Enum):
    SPOUSE = "spouse"
    PARENT = "parent"   # user_id = parent, related_user_id = enfant
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
    coins         = Column(Integer, default=100)
    karma         = Column(Integer, default=0)
    family_name   = Column(String(100), nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)


class GroupSettings(Base):
    __tablename__ = "group_settings"
    group_id       = Column(BigInteger, primary_key=True)
    mode           = Column(String(20), default="global")  # global | group
    garden_enabled = Column(Boolean, default=True)
    waifu_enabled  = Column(Boolean, default=True)


class Relationship(Base):
    __tablename__ = "relationships"
    id              = Column(Integer, primary_key=True, autoincrement=True)
    user_id         = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    related_user_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    relation_type   = Column(SAEnum(RelationType), nullable=False)
    group_id        = Column(BigInteger, nullable=True)   # None = global
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
    slot       = Column(Integer, nullable=False)        # 0 – GARDEN_SLOTS-1
    plant_type = Column(String(50), nullable=False)
    planted_at = Column(DateTime, default=datetime.utcnow)
    harvested  = Column(Boolean, default=False)


class DailyWaifu(Base):
    __tablename__ = "daily_waifu"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    group_id      = Column(BigInteger, nullable=False)
    date          = Column(String(10), nullable=False)   # YYYY-MM-DD
    waifu_user_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)


class KarmaVote(Base):
    __tablename__ = "karma_votes"
    id        = Column(Integer, primary_key=True, autoincrement=True)
    voter_id  = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    target_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    group_id  = Column(BigInteger, nullable=False)
    date      = Column(String(10), nullable=False)   # 1 vote par jour par cible
