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
    SIBLING = "sibling"


class RequestType(enum.Enum):
    MARRY  = "marry"
    ADOPT  = "adopt"
    FRIEND = "friend"


class User(Base):
    __tablename__ = "users"
    user_id       = Column(BigInteger, primary_key=True)
    username      = Column(String(255), nullable=True)
    first_name    = Column(String(255), nullable=False)
    photo_file_id   = Column(String(512), nullable=True)
    photo_file_type = Column(String(10), nullable=True, default='photo')  # 'photo' ou 'sticker'
    profile_color = Column(Text, default="blue")
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

    # ─── IDENTITÉ ─────────────────────────────────────────────────────────────
    gender          = Column(String(10), nullable=True)    # homme / femme / None (non défini)
    marriage_type   = Column(String(10), default="monogame")  # monogame / polygame

    # ─── AVATAR ───────────────────────────────────────────────────────────────
    avatar_data     = Column(Text, nullable=True)          # JSON stocké côté serveur
    nationality     = Column(String(50), nullable=True)        # nationalite choisie par le joueur

    # ─── ACTIVITÉ GLOBALE ─────────────────────────────────────────────────────
    total_commands  = Column(BigInteger, default=0)        # compteur cumulatif, jamais réinitialisé


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
    relation_type   = Column(SAEnum(RelationType, values_callable=lambda x: [e.value for e in x]), nullable=False)
    group_id        = Column(BigInteger, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)


class PendingRequest(Base):
    __tablename__ = "pending_requests"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    from_user_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    to_user_id   = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    request_type = Column(SAEnum(RequestType, values_callable=lambda x: [e.name for e in x]), nullable=False)
    group_id     = Column(BigInteger, nullable=False)
    message_id   = Column(BigInteger, nullable=True)
    expires_at   = Column(DateTime, nullable=False)
    created_at   = Column(DateTime, default=datetime.utcnow)
    extra        = Column(String(50), nullable=True)   # données supplémentaires (ex: type de mariage)


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


# ─── ENTREPRISES ──────────────────────────────────────────────────────────────

class Company(Base):
    __tablename__ = "companies"
    id              = Column(Integer, primary_key=True, autoincrement=True)
    name            = Column(String(100), nullable=False, unique=True)
    sector          = Column(String(50), nullable=False)   # tech, finance, commerce, ...
    owner_id        = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    group_id        = Column(BigInteger, nullable=False)
    description     = Column(String(300), nullable=True)
    value           = Column(BigInteger, default=50_000_000)
    treasury        = Column(BigInteger, default=0)        # caisse de l'entreprise
    total_shares    = Column(Integer, default=100)         # nombre total de parts
    owner_shares    = Column(Integer, default=100)         # parts détenues par le fondateur
    level           = Column(Integer, default=1)           # 1=Startup … 5=Holding
    reputation      = Column(Float, default=3.0)           # /5
    is_bot_company  = Column(Boolean, default=False)       # entreprise créée par le bot
    is_active       = Column(Boolean, default=True)
    created_at      = Column(DateTime, default=datetime.utcnow)
    last_revenue    = Column(DateTime, nullable=True)      # dernier versement revenus
    last_payroll    = Column(DateTime, nullable=True)      # dernière paie manuelle par le PDG
    last_active     = Column(DateTime, default=datetime.utcnow)  # pour détecter inactivité PDG
    last_annonce    = Column(DateTime, nullable=True)            # dernière annonce de recrutement
    last_rename     = Column(DateTime, nullable=True)            # dernier renommage de l'entreprise
    last_retrait_pdg = Column(DateTime, nullable=True)           # dernier retrait PDG (cooldown 24h anti-exploit)
    extra_slots     = Column(Integer, default=0)                 # places supplémentaires achetées
    legal_reserve   = Column(BigInteger, default=0)              # réserve légale intouchable (10% des bénéfices)
    weekly_revenue  = Column(BigInteger, default=0)              # revenus nets de la semaine (reset après dividendes lundi)
    treasury_frozen = Column(Boolean, default=False)             # trésorerie gelée par l'agence fiscale
    tax_debt        = Column(BigInteger, default=0)              # total impôts impayés cumulés
    city            = Column(String(50), nullable=True)              # localisation géographique


class CompanyEmployee(Base):
    __tablename__ = "company_employees"
    id          = Column(Integer, primary_key=True, autoincrement=True)
    company_id  = Column(Integer, ForeignKey("companies.id"), nullable=False)
    user_id     = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    role        = Column(String(30), default="stagiaire")  # stagiaire/employe/manager/directeur/pdg
    joined_at   = Column(DateTime, default=datetime.utcnow)
    left_at     = Column(DateTime, nullable=True)          # date de démission (cooldown)
    command_count = Column(Integer, default=0)             # commandes utilisées depuis l'entrée
    activity_since_payroll = Column(Integer, default=0)    # commandes depuis la dernière paie (reset après /versersalaires)
    # ── Système de contrat ────────────────────────────────────────────────────
    daily_salary   = Column(BigInteger, default=0)         # salaire journalier fixé par le PDG (signé)
    pending_salary = Column(BigInteger, default=0)         # salaire proposé en négociation
    pending_bonus  = Column(BigInteger, default=0)         # prime proposée (optionnelle)
    contract_status = Column(String(30), default="none")   # none / pending_employee / pending_pdg / signed


class CompanyShare(Base):
    __tablename__ = "company_shares"
    id          = Column(Integer, primary_key=True, autoincrement=True)
    company_id  = Column(Integer, ForeignKey("companies.id"), nullable=False)
    owner_id    = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    quantity    = Column(Integer, default=0)
    acquired_at = Column(DateTime, default=datetime.utcnow)


class CompanyApplication(Base):
    __tablename__ = "company_applications"
    id          = Column(Integer, primary_key=True, autoincrement=True)
    company_id  = Column(Integer, ForeignKey("companies.id"), nullable=False)
    user_id     = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    status      = Column(String(20), default="pending")    # pending/accepted/rejected
    created_at  = Column(DateTime, default=datetime.utcnow)


class CompanyInvite(Base):
    __tablename__ = "company_invites"
    id          = Column(Integer, primary_key=True, autoincrement=True)
    company_id  = Column(Integer, ForeignKey("companies.id"), nullable=False)
    target_id   = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    role        = Column(String(30), default="employe")
    invited_by  = Column(BigInteger, nullable=False)
    status      = Column(String(20), default="pending")
    created_at  = Column(DateTime, default=datetime.utcnow)
    expires_at  = Column(DateTime, nullable=False)


class CompanyLog(Base):
    __tablename__ = "company_logs"
    id          = Column(Integer, primary_key=True, autoincrement=True)
    company_id  = Column(Integer, ForeignKey("companies.id"), nullable=False)
    event_type  = Column(String(50), nullable=False)       # recrutement, promotion, depot, ...
    description = Column(String(500), nullable=False)
    amount      = Column(BigInteger, nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)


class CompanyWorkShift(Base):
    """Pointage de présence d'un employé (nécessaire pour recevoir son salaire)."""
    __tablename__ = "company_work_shifts"
    id          = Column(Integer, primary_key=True, autoincrement=True)
    company_id  = Column(Integer, ForeignKey("companies.id"), nullable=False)
    user_id     = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    worked_at   = Column(DateTime, default=datetime.utcnow)
    paid        = Column(Boolean, default=False)       # True une fois le salaire versé
    paid_at     = Column(DateTime, nullable=True)



class CompanyLoan(Base):
    """Prêt bancaire contracté par une entreprise."""
    __tablename__ = "company_loans"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    company_id    = Column(Integer, ForeignKey("companies.id"), nullable=False)
    amount        = Column(BigInteger, nullable=False)       # montant initial emprunté
    remaining     = Column(BigInteger, nullable=False)       # reste à rembourser (principal + intérêts)
    interest_rate = Column(Float, nullable=False)            # taux annuel (ex: 0.10 = 10%)
    daily_payment = Column(BigInteger, nullable=False)       # montant dû chaque jour (auto-prélevé)
    taken_at      = Column(DateTime, default=datetime.utcnow)
    due_at        = Column(DateTime, nullable=False)         # échéance finale (30 jours)
    status        = Column(String(20), default="active")     # active / repaid / defaulted
    missed_days   = Column(Integer, default=0)               # jours de retard consécutifs


class CompanyShareOffer(Base):
    """Offre d'achat de parts soumise à l'accord du PDG."""
    __tablename__ = "company_share_offers"
    id          = Column(Integer, primary_key=True, autoincrement=True)
    company_id  = Column(Integer, ForeignKey("companies.id"), nullable=False)
    buyer_id    = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    quantity    = Column(Integer, nullable=False)
    price_each  = Column(BigInteger, nullable=False)   # prix par part au moment de l'offre
    total_price = Column(BigInteger, nullable=False)   # montant bloqué (escrow)
    status      = Column(String(20), default="pending")  # pending / accepted / rejected / expired
    created_at  = Column(DateTime, default=datetime.utcnow)
    expires_at  = Column(DateTime, nullable=False)


class CompanyAutoContract(Base):
    """Contrat automatique généré par l'IA et proposé à une entreprise."""
    __tablename__ = "company_auto_contracts"
    id               = Column(Integer, primary_key=True, autoincrement=True)
    company_id       = Column(Integer, ForeignKey("companies.id"), nullable=False)
    sector           = Column(String(50), nullable=False)
    client_name      = Column(String(150), nullable=False)   # nom client fictif généré
    description      = Column(String(600), nullable=False)   # description de la mission
    objective_cmds   = Column(Integer, nullable=False)       # commandes à effectuer
    reward           = Column(BigInteger, nullable=False)    # récompense initiale proposée
    deadline_hours   = Column(Integer, nullable=False)       # délai en heures
    status           = Column(String(20), default="pending") # pending/active/completed/failed/rejected/negotiating
    created_at       = Column(DateTime, default=datetime.utcnow)
    accepted_at      = Column(DateTime, nullable=True)
    deadline_at      = Column(DateTime, nullable=True)
    cmds_at_start    = Column(BigInteger, default=0)         # total commandes équipe au moment de l'acceptation
    cmds_done        = Column(BigInteger, default=0)         # progression cumulée (incrémentée en temps réel)
    negotiated_reward= Column(BigInteger, nullable=True)     # montant après négociation (si accepté)
    negotiation_round= Column(Integer, default=0)            # nb de tours de négociation
    notif_message_id = Column(BigInteger, nullable=True)     # message_id de la notif PDG (pour éditer)


class CompanySettings(Base):
    """Paramètres par entreprise (autopay, etc.)."""
    __tablename__ = "company_settings"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    company_id    = Column(Integer, ForeignKey("companies.id"), nullable=False, unique=True)
    auto_payroll  = Column(Boolean, default=False)   # True = paie auto selon suggestions
    next_contract_at = Column(DateTime, nullable=True)  # prochain contrat IA planifié


class TaxRecord(Base):
    """Facture d'impôt journalière par entreprise."""
    __tablename__ = "tax_records"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    company_id   = Column(Integer, ForeignKey("companies.id"), nullable=False)
    amount_due   = Column(BigInteger, nullable=False)
    amount_paid  = Column(BigInteger, default=0)
    created_at   = Column(DateTime, default=datetime.utcnow)
    due_at       = Column(DateTime, nullable=False)             # created_at + 24h
    status       = Column(String(20), default="pending")        # pending / paid / partial / overdue


class StateCaisse(Base):
    """Caisse d'État — cumul des impôts collectés."""
    __tablename__ = "state_caisse"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    total        = Column(BigInteger, default=0)
    last_tax_at  = Column(DateTime, nullable=True, default=None)  # dernière émission fiscale


class BureauContrat(Base):
    """Contrat proposé par le Bureau des Contrats."""
    __tablename__ = "bureau_contrats"
    id             = Column(Integer, primary_key=True, autoincrement=True)
    company_id     = Column(Integer, ForeignKey("companies.id"), nullable=False)
    title          = Column(String(200), nullable=False)
    description    = Column(String(600), nullable=False)
    reward         = Column(BigInteger, nullable=False)
    duration_days  = Column(Integer, nullable=False)
    objective_cmds = Column(Integer, default=0)                 # nb de commandes d'équipe à atteindre
    cmds_at_start  = Column(BigInteger, default=0)              # snapshot commandes au moment de l'acceptation
    cmds_done      = Column(BigInteger, default=0)              # progression cumulée (fiable, jamais réinitialisée)
    starts_at      = Column(DateTime, nullable=True)
    ends_at        = Column(DateTime, nullable=True)
    status         = Column(String(20), default="pending")      # pending / active / completed / failed
    created_at     = Column(DateTime, default=datetime.utcnow)


# ─── BÂTIMENTS D'ENTREPRISE ───────────────────────────────────────────────────

class CompanyBuilding(Base):
    __tablename__ = "company_buildings"
    id          = Column(Integer, primary_key=True, autoincrement=True)
    company_id  = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    building_type = Column(String(50), nullable=False)   # siege, entrepot, salle_reunion, ...
    status      = Column(String(20), default="active")   # active | suspended
    purchased_at = Column(DateTime, default=datetime.utcnow)
    last_maintenance = Column(DateTime, nullable=True)


# ─── MESSAGERIE PRIVÉE (chat entre liens) ──────────────────────────────────────

class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    from_user_id  = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    to_user_id    = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    content       = Column(Text, nullable=False)
    sent_at       = Column(DateTime, default=datetime.utcnow)
    read_at       = Column(DateTime, nullable=True)


# ─── FILIALES ─────────────────────────────────────────────────────────────────
