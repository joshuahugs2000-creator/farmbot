"""
handlers/buildings.py — Système de bâtiments et filiales d'entreprise

Bâtiments :
  /batiments              → catalogue des bâtiments + prix par niveau
  /acheterbatiment [type] → achète depuis la trésorerie
  /mesbatiments           → liste tes bâtiments actifs

Filiales :
  /creerfiliale [NomFiliale] [capital] [% reversement]
      → Le PDG crée une nouvelle entreprise filiale (même secteur, capital injecté
        depuis la trésorerie mère). La filiale est une vraie entreprise avec ses
        propres employés, contrats, etc. Elle reverse chaque jour X% de ses revenus
        à la maison mère. Une filiale ne peut PAS avoir ses propres filiales.

  /nommerdir @pseudo
      → Le PDG nomme un de ses employés (non-directeur) comme Directeur de filiale.
        Il obtient le rôle "directeur" dans la filiale et peut tout gérer sauf :
        dissolution, cession, création de sous-filiale, retrait > 20% tréso/semaine.

  /mesfiliates            → liste les filiales de l'entreprise mère
  /infofiliale [nom]      → détails d'une filiale
  /retirerfiliale [nom]   → PDG détache une filiale (trésorerie reste à la filiale)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, func, text
from telegram import Update
from telegram.ext import ContextTypes

from database.db import AsyncSessionLocal, get_user
from database.models import (
    User, Company, CompanyEmployee, CompanyBuilding, CompanyAnnex, CompanyShare,
)

logger = logging.getLogger(__name__)

# ─── CATALOGUE DES BÂTIMENTS ─────────────────────────────────────────────────

LEVEL_MULTIPLIER = {1: 1, 2: 5, 3: 25, 4: 125, 5: 625}

BUILDINGS = {
    "salle_reunion": {
        "name":      "🪑 Salle de Réunion",
        "base_cost": 500_000,
        "effect":    "-10% délai négociation de contrats",
        "unlock_lvl": 1,
        "maintenance_pct": 0.005,
        "boost": {"nego_speed": 0.10},
    },
    "entrepot": {
        "name":      "📦 Entrepôt",
        "base_cost": 1_000_000,
        "effect":    "+15% trésorerie max autorisée",
        "unlock_lvl": 1,
        "maintenance_pct": 0.005,
        "boost": {"treasury_cap": 0.15},
    },
    "siege_social": {
        "name":      "🏛️ Siège Social",
        "base_cost": 2_000_000,
        "effect":    "+10% réputation (boost passif)",
        "unlock_lvl": 2,
        "maintenance_pct": 0.005,
        "boost": {"reputation": 0.10},
    },
    "datacenter": {
        "name":      "🖥️ Datacenter",
        "base_cost": 5_000_000,
        "effect":    "+10% revenus des contrats",
        "unlock_lvl": 3,
        "maintenance_pct": 0.005,
        "boost": {"contract_revenue": 0.10},
    },
    "usine": {
        "name":      "🏭 Usine",
        "base_cost": 8_000_000,
        "effect":    "+10% revenus journaliers",
        "unlock_lvl": 3,
        "maintenance_pct": 0.005,
        "boost": {"daily_revenue": 0.10},
    },
    "agence_bancaire": {
        "name":      "🏦 Agence Bancaire",
        "base_cost": 15_000_000,
        "effect":    "Débloque les prêts inter-entreprises",
        "unlock_lvl": 4,
        "maintenance_pct": 0.005,
        "boost": {"interbank_loans": True},
    },
    "campus_rd": {
        "name":      "🔬 Campus R&D",
        "base_cost": 30_000_000,
        "effect":    "Débloque les contrats exclusifs",
        "unlock_lvl": 5,
        "maintenance_pct": 0.005,
        "boost": {"exclusive_contracts": True},
    },
    "tour_controle": {
        "name":      "🗼 Tour de Contrôle",
        "base_cost": 50_000_000,
        "effect":    "Visibilité dans le classement mondial",
        "unlock_lvl": 5,
        "maintenance_pct": 0.005,
        "boost": {"global_visibility": True},
    },
}

# Filiales max par niveau d'entreprise mère
ANNEX_MAX = {1: 0, 2: 1, 3: 2, 4: 3, 5: 5}
LEVEL_NAMES = {1: "Startup", 2: "PME", 3: "Société", 4: "Corporation", 5: "Holding"}

# Capital minimum à injecter dans une filiale
FILIALE_CAPITAL_MIN = 50_000_000  # 50M

# Limite de retrait hebdomadaire pour un directeur de filiale (% de la trésorerie)
DIRECTOR_WITHDRAW_WEEKLY_PCT = 0.20  # 20% max par semaine

# ─── UTILITAIRES ─────────────────────────────────────────────────────────────

def _fmt(n: int) -> str:
    if n >= 1_000_000_000: return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:     return f"{n/1_000_000:.1f}M"
    if n >= 1_000:         return f"{n/1_000:.0f}K"
    return str(n)


def _building_cost(btype: str, company_level: int) -> int:
    b = BUILDINGS.get(btype)
    if not b: return 0
    return b["base_cost"] * LEVEL_MULTIPLIER.get(company_level, 1)


def _building_maintenance(btype: str, company_level: int) -> int:
    b = BUILDINGS.get(btype)
    if not b: return 0
    return int(_building_cost(btype, company_level) * b["maintenance_pct"])


async def _get_pdg_company(session, user_id: int):
    """Retourne (Company, CompanyEmployee) si l'user est PDG d'une entreprise active.
    Exclut les filiales pour le PDG mère — retourne toujours l'entreprise principale."""
    from database.models import CompanyAnnex
    from sqlalchemy import select as _select
    filiale_ids = _select(CompanyAnnex.child_id)

    r = await session.execute(
        select(CompanyEmployee, Company).join(
            Company, Company.id == CompanyEmployee.company_id
        ).where(
            CompanyEmployee.user_id == user_id,
            CompanyEmployee.left_at == None,
            Company.is_active == True,
            CompanyEmployee.role == "pdg",
            Company.id.not_in(filiale_ids),
        )
    )
    row = r.first()
    if row:
        return row[1], row[0]
    # Fallback : si pas trouvé hors filiales (il EST pdg d'une filiale), retourner quand même
    r2 = await session.execute(
        select(CompanyEmployee, Company).join(
            Company, Company.id == CompanyEmployee.company_id
        ).where(
            CompanyEmployee.user_id == user_id,
            CompanyEmployee.left_at == None,
            Company.is_active == True,
            CompanyEmployee.role == "pdg",
        )
    )
    row2 = r2.first()
    if row2:
        return row2[1], row2[0]
    return None, None


async def _get_user_company(session, user_id: int):
    """Retourne (Company, CompanyEmployee) pour n'importe quel rôle actif."""
    r = await session.execute(
        select(CompanyEmployee, Company).join(
            Company, Company.id == CompanyEmployee.company_id
        ).where(
            CompanyEmployee.user_id == user_id,
            CompanyEmployee.left_at == None,
            Company.is_active == True,
        )
    )
    row = r.first()
    if row:
        return row[1], row[0]
    return None, None


async def _is_filiale(session, company_id: int) -> bool:
    """Vérifie si une entreprise est déjà une filiale d'une autre."""
    annex = (await session.execute(
        select(CompanyAnnex).where(
            CompanyAnnex.child_id == company_id,
            CompanyAnnex.is_active == True,
        )
    )).scalar_one_or_none()
    return annex is not None


# ─── COMMANDE : /batiments ────────────────────────────────────────────────────

async def batiments_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le catalogue des bâtiments avec les prix adaptés au niveau."""
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company:
            await update.message.reply_text("❌ Tu ne fais partie d'aucune entreprise.", parse_mode="HTML")
            return

        owned = (await session.execute(
            select(CompanyBuilding).where(CompanyBuilding.company_id == company.id)
        )).scalars().all()
        owned_types = {b.building_type: b.status for b in owned}

        lines = [
            f"🏗️ <b>BÂTIMENTS — {company.name}</b>",
            f"📊 Niveau : <b>{LEVEL_NAMES.get(company.level, '?')}</b> · 🏦 Trésorerie : <b>{_fmt(company.treasury)} $</b>",
            "─────────────────────────────",
        ]

        for btype, info in BUILDINGS.items():
            unlock_lvl = info["unlock_lvl"]
            cost = _building_cost(btype, company.level)
            maint = _building_maintenance(btype, company.level)
            locked = company.level < unlock_lvl

            if btype in owned_types:
                status_icon = "✅" if owned_types[btype] == "active" else "⚠️"
                lines.append(f"{status_icon} <b>{info['name']}</b> — <i>Déjà acheté</i>\n   {info['effect']}")
            elif locked:
                lines.append(f"🔒 <b>{info['name']}</b> — Débloqué niveau {unlock_lvl}\n   {info['effect']}")
            else:
                can = "✅" if company.treasury >= cost else "❌"
                lines.append(
                    f"{can} <b>{info['name']}</b>\n"
                    f"   💰 <b>{_fmt(cost)} $</b> · Maintenance : <b>{_fmt(maint)} $/j</b>\n"
                    f"   {info['effect']}\n"
                    f"   👉 <code>/acheterbatiment {btype}</code>"
                )
            lines.append("")

        lines.append("─────────────────────────────")
        lines.append("💡 Maintenance prélevée quotidiennement sur la trésorerie.")
        lines.append("⚠️ Trésorerie insuffisante → bâtiment suspendu.")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── COMMANDE : /acheterbatiment [type] ──────────────────────────────────────

async def acheterbatiment_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Achète un bâtiment pour l'entreprise depuis la trésorerie."""
    user = update.effective_user
    if not context.args:
        await update.message.reply_text(
            f"❌ Usage : <code>/acheterbatiment [type]</code>\n"
            f"Types : {' | '.join(BUILDINGS.keys())}\n\n"
            f"💡 Voir les prix : <code>/batiments</code>",
            parse_mode="HTML"
        )
        return

    btype = context.args[0].lower()
    if btype not in BUILDINGS:
        await update.message.reply_text("❌ Type invalide. Voir <code>/batiments</code>", parse_mode="HTML")
        return

    async with AsyncSessionLocal() as session:
        company, emp = await _get_pdg_company(session, user.id)
        if not company:
            await update.message.reply_text("❌ Seul le PDG peut acheter des bâtiments.")
            return
        if company.is_bot_company:
            await update.message.reply_text("❌ Impossible sur une entreprise officielle.")
            return

        info = BUILDINGS[btype]
        if company.level < info["unlock_lvl"]:
            await update.message.reply_text(
                f"🔒 <b>{info['name']}</b> se débloque au niveau <b>{info['unlock_lvl']}</b>.\n"
                f"Ton entreprise est niveau <b>{company.level}</b>.",
                parse_mode="HTML"
            )
            return

        already = (await session.execute(
            select(CompanyBuilding).where(
                CompanyBuilding.company_id == company.id,
                CompanyBuilding.building_type == btype,
            )
        )).scalar_one_or_none()
        if already:
            await update.message.reply_text(f"❌ Tu as déjà <b>{info['name']}</b>. Maximum 1 par type.", parse_mode="HTML")
            return

        cost = _building_cost(btype, company.level)
        maint = _building_maintenance(btype, company.level)
        if company.treasury < cost:
            await update.message.reply_text(
                f"❌ Trésorerie insuffisante.\n💸 Coût : <b>{_fmt(cost)} $</b>\n🏦 Trésorerie : <b>{_fmt(company.treasury)} $</b>",
                parse_mode="HTML"
            )
            return

        company.treasury -= cost
        building = CompanyBuilding(
            company_id=company.id,
            building_type=btype,
            status="active",
            purchased_at=datetime.utcnow(),
            last_maintenance=datetime.utcnow(),
        )
        session.add(building)
        await session.commit()

        await update.message.reply_text(
            f"✅ <b>{info['name']}</b> construit !\n\n"
            f"💸 Coût : <b>{_fmt(cost)} $</b>\n"
            f"🔧 Maintenance : <b>{_fmt(maint)} $/j</b>\n"
            f"⚡ Effet : {info['effect']}\n\n"
            f"🏦 Trésorerie restante : <b>{_fmt(company.treasury)} $</b>",
            parse_mode="HTML"
        )


# ─── COMMANDE : /mesbatiments ─────────────────────────────────────────────────

async def mesbatiments_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Liste les bâtiments actifs de l'entreprise."""
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company:
            await update.message.reply_text("❌ Tu ne fais partie d'aucune entreprise.")
            return

        buildings = (await session.execute(
            select(CompanyBuilding).where(CompanyBuilding.company_id == company.id)
        )).scalars().all()

        if not buildings:
            await update.message.reply_text(
                f"🏗️ <b>{company.name}</b> n'a aucun bâtiment.\n💡 <code>/batiments</code> pour voir le catalogue.",
                parse_mode="HTML"
            )
            return

        total_maint = sum(_building_maintenance(b.building_type, company.level) for b in buildings)
        lines = [
            f"🏗️ <b>BÂTIMENTS — {company.name}</b>",
            f"🏦 Trésorerie : <b>{_fmt(company.treasury)} $</b>",
            "─────────────────────────────",
        ]
        for b in buildings:
            info = BUILDINGS.get(b.building_type, {})
            icon = "✅" if b.status == "active" else "⚠️ SUSPENDU"
            bought = b.purchased_at.strftime("%d/%m/%Y") if b.purchased_at else "?"
            lines.append(
                f"{icon} <b>{info.get('name', b.building_type)}</b>\n"
                f"   {info.get('effect', '—')}\n"
                f"   🔧 {_fmt(_building_maintenance(b.building_type, company.level))} $/j · {bought}"
            )
        lines.append("─────────────────────────────")
        lines.append(f"💰 Total maintenance/j : <b>{_fmt(total_maint)} $</b>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════════════════
# SYSTÈME FILIALES
# ═══════════════════════════════════════════════════════════════════════════════

# ─── COMMANDE : /creerfiliale [NomFiliale] [capital] [%reversement] ───────────

async def creerfiliale_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Crée une nouvelle entreprise filiale à partir de zéro.
    Usage : /creerfiliale [Nom Filiale] [capital] [% reversement 5-50]

    - La filiale hérite du même secteur que la maison mère.
    - Le capital est prélevé sur la trésorerie de la maison mère (min 50M).
    - Le % est reversé chaque jour à la maison mère depuis les revenus de la filiale.
    - La filiale est une vraie entreprise (contrats, employés, bâtiments...).
    - Une filiale ne peut PAS créer ses propres filiales.
    """
    user = update.effective_user

    if len(context.args) < 3:
        await update.message.reply_text(
            "❌ Usage : <code>/creerfiliale [Nom Filiale] [capital] [% reversement]</code>\n\n"
            "📋 Exemple : <code>/creerfiliale NexaFils 100000000 20</code>\n"
            f"💰 Capital minimum : <b>{_fmt(FILIALE_CAPITAL_MIN)} $</b>\n"
            "📊 Reversement : entre <b>5%</b> et <b>50%</b> des revenus quotidiens",
            parse_mode="HTML"
        )
        return

    # Parser les arguments : tout sauf les 2 derniers = nom
    try:
        pct = float(context.args[-1])
        capital = int(context.args[-2].replace("_", "").replace(" ", ""))
        filiale_name = " ".join(context.args[:-2]).strip()
    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Format invalide.\nExemple : <code>/creerfiliale NexaFils 100000000 20</code>",
            parse_mode="HTML"
        )
        return

    if not filiale_name:
        await update.message.reply_text("❌ Tu dois donner un nom à la filiale.", parse_mode="HTML")
        return

    if not (5 <= pct <= 50):
        await update.message.reply_text("❌ Le reversement doit être entre <b>5%</b> et <b>50%</b>.", parse_mode="HTML")
        return

    if capital < FILIALE_CAPITAL_MIN:
        await update.message.reply_text(
            f"❌ Capital minimum : <b>{_fmt(FILIALE_CAPITAL_MIN)} $</b>.\n"
            f"Tu as essayé d'injecter : <b>{_fmt(capital)} $</b>",
            parse_mode="HTML"
        )
        return

    async with AsyncSessionLocal() as session:
        # Vérifier que l'utilisateur est bien PDG
        parent_company, emp = await _get_pdg_company(session, user.id)
        if not parent_company:
            await update.message.reply_text("❌ Tu dois être PDG d'une entreprise pour créer une filiale.")
            return

        if parent_company.is_bot_company:
            await update.message.reply_text("❌ Les entreprises officielles ne peuvent pas créer de filiales.")
            return

        # Vérifier que la maison mère n'est pas elle-même une filiale
        if await _is_filiale(session, parent_company.id):
            await update.message.reply_text(
                "❌ <b>Une filiale ne peut pas créer ses propres filiales.</b>\n"
                "Seule une entreprise mère indépendante peut en créer.",
                parse_mode="HTML"
            )
            return

        # Vérifier le niveau (min niveau 2)
        max_annexes = ANNEX_MAX.get(parent_company.level, 0)
        if max_annexes == 0:
            await update.message.reply_text(
                f"❌ Les <b>{LEVEL_NAMES.get(parent_company.level, 'Startup')}</b> ne peuvent pas créer de filiale.\n"
                f"Monte au niveau <b>PME</b> (niveau 2) pour débloquer.",
                parse_mode="HTML"
            )
            return

        # Vérifier le quota de filiales
        current_count = (await session.execute(
            select(func.count()).where(
                CompanyAnnex.parent_id == parent_company.id,
                CompanyAnnex.is_active == True,
            )
        )).scalar() or 0

        if current_count >= max_annexes:
            await update.message.reply_text(
                f"❌ Tu as atteint le maximum de <b>{max_annexes} filiale(s)</b> "
                f"pour une <b>{LEVEL_NAMES.get(parent_company.level, '?')}</b>.\n"
                f"Monte de niveau pour en créer davantage.",
                parse_mode="HTML"
            )
            return

        # Vérifier la trésorerie mère
        if parent_company.treasury < capital:
            await update.message.reply_text(
                f"❌ Trésorerie insuffisante.\n\n"
                f"💰 Capital à injecter : <b>{_fmt(capital)} $</b>\n"
                f"🏦 Trésorerie disponible : <b>{_fmt(parent_company.treasury)} $</b>",
                parse_mode="HTML"
            )
            return

        # Vérifier que le nom n'existe pas déjà
        name_taken = (await session.execute(
            select(Company).where(Company.name.ilike(filiale_name))
        )).scalar_one_or_none()
        if name_taken:
            await update.message.reply_text(
                f"❌ Une entreprise nommée <b>{filiale_name}</b> existe déjà.\nChoisis un autre nom.",
                parse_mode="HTML"
            )
            return

        from handlers.company import SECTORS
        sec_emoji, sec_name = SECTORS.get(parent_company.sector, ("🏢", parent_company.sector))

        # Créer la filiale comme vraie entreprise
        filiale = Company(
            name=filiale_name,
            sector=parent_company.sector,   # même secteur que la mère
            owner_id=user.id,               # le PDG mère est propriétaire légal
            group_id=parent_company.group_id,
            description=f"Filiale de {parent_company.name}",
            value=capital,
            treasury=capital,               # capital injecté = trésorerie de départ
            total_shares=100,
            owner_shares=100,
            level=1,
            reputation=3.0,
            is_bot_company=False,
            is_active=True,
        )
        session.add(filiale)

        # Prélever le capital sur la trésorerie mère
        parent_company.treasury -= capital

        try:
            await session.flush()  # récupérer filiale.id
        except Exception as e:
            await session.rollback()
            await update.message.reply_text(f"❌ Erreur lors de la création : {e}", parse_mode="HTML")
            return

        # Le PDG mère n'est PAS employé de la filiale par défaut (il a un directeur)
        # On crée quand même ses parts (propriétaire légal)
        share = CompanyShare(
            company_id=filiale.id,
            owner_id=user.id,
            quantity=100,
        )
        session.add(share)

        # Créer le lien filiale ↔ maison mère
        annex = CompanyAnnex(
            parent_id=parent_company.id,
            child_id=filiale.id,
            director_id=None,
            revenue_pct=pct,
            is_active=True,
        )
        session.add(annex)

        await session.commit()

        await update.message.reply_text(
            f"🏢 <b>Filiale créée : {filiale_name}</b>\n\n"
            f"{sec_emoji} Secteur : <b>{sec_name}</b> (hérité de {parent_company.name})\n"
            f"💰 Capital injecté : <b>{_fmt(capital)} $</b>\n"
            f"📊 Reversement quotidien : <b>{pct}%</b> des revenus → {parent_company.name}\n"
            f"📦 Parts : <b>100/100</b> (détenues par toi)\n\n"
            f"👤 <b>Nomme un directeur :</b>\n"
            f"<code>/nommerdir @pseudo</code>\n\n"
            f"💡 La filiale est une vraie entreprise — elle peut recruter, passer des contrats, acheter des bâtiments...\n"
            f"⚠️ Elle ne peut pas créer ses propres filiales.",
            parse_mode="HTML"
        )


# ─── COMMANDE : /nommerdir @pseudo ───────────────────────────────────────────

async def nommerdir_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Nomme un employé de la maison mère comme Directeur de la filiale.
    Usage : /nommerdir @pseudo

    Le directeur obtient le rôle "directeur" dans la filiale.
    Il peut tout gérer SAUF : dissoudre, céder, créer une sous-filiale,
    retirer plus de 20% de la trésorerie par semaine.
    """
    user = update.effective_user

    if not context.args:
        await update.message.reply_text(
            "❌ Usage : <code>/nommerdir @pseudo</code>\n"
            "💡 L'employé doit être dans ton entreprise (non-directeur).",
            parse_mode="HTML"
        )
        return

    mention = context.args[0].lstrip("@")

    async with AsyncSessionLocal() as session:
        parent_company, emp = await _get_pdg_company(session, user.id)
        if not parent_company:
            await update.message.reply_text("❌ Réservé au PDG.")
            return

        # Chercher la filiale de cette entreprise (qui n'a pas encore de directeur, ou en changer)
        annexes = (await session.execute(
            select(CompanyAnnex).where(
                CompanyAnnex.parent_id == parent_company.id,
                CompanyAnnex.is_active == True,
            )
        )).scalars().all()

        if not annexes:
            await update.message.reply_text(
                f"❌ <b>{parent_company.name}</b> n'a aucune filiale.\n"
                f"Crée d'abord une filiale avec <code>/creerfiliale</code>.",
                parse_mode="HTML"
            )
            return

        # Si plusieurs filiales, demander laquelle (on gère le cas simple : 1 filiale)
        # Pour plusieurs filiales : /nommerdir @pseudo NomFiliale
        target_annex = None
        filiale_obj = None

        if len(annexes) == 1:
            target_annex = annexes[0]
            filiale_obj = await session.get(Company, target_annex.child_id)
        else:
            # Chercher par nom si fourni comme 2e arg
            if len(context.args) < 2:
                filiale_list = ""
                for a in annexes:
                    f = await session.get(Company, a.child_id)
                    if f:
                        filiale_list += f"\n  • <b>{f.name}</b>"
                await update.message.reply_text(
                    f"❌ Tu as plusieurs filiales. Précise laquelle :\n"
                    f"<code>/nommerdir @pseudo [NomFiliale]</code>\n\n"
                    f"Tes filiales :{filiale_list}",
                    parse_mode="HTML"
                )
                return
            filiale_name_arg = " ".join(context.args[1:])
            for a in annexes:
                f = await session.get(Company, a.child_id)
                if f and f.name.lower() == filiale_name_arg.lower():
                    target_annex = a
                    filiale_obj = f
                    break
            if not target_annex:
                await update.message.reply_text(f"❌ Filiale <b>{filiale_name_arg}</b> introuvable.", parse_mode="HTML")
                return

        # Chercher la cible par username
        target = (await session.execute(
            select(User).where(User.username == mention)
        )).scalar_one_or_none()
        if not target:
            await update.message.reply_text(f"❌ @{mention} introuvable en base.")
            return

        # Vérifier que la cible est employée dans la maison mère
        target_emp_mere = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == parent_company.id,
                CompanyEmployee.user_id == target.user_id,
                CompanyEmployee.left_at == None,
            )
        )).scalar_one_or_none()

        if not target_emp_mere:
            await update.message.reply_text(
                f"❌ <b>{target.first_name}</b> n'est pas employé(e) dans <b>{parent_company.name}</b>.\n"
                f"Le directeur de filiale doit être un employé de la maison mère.",
                parse_mode="HTML"
            )
            return

        # Un directeur ne peut pas en être un autre en même temps
        if target_emp_mere.role == "directeur":
            await update.message.reply_text(
                f"❌ <b>{target.first_name}</b> est déjà Directeur(ice) dans <b>{parent_company.name}</b>.\n"
                f"Un directeur ne peut pas cumuler deux postes de direction.",
                parse_mode="HTML"
            )
            return

        # Vérifier qu'il ne dirige pas déjà une autre filiale
        already_dir = (await session.execute(
            select(CompanyAnnex).where(
                CompanyAnnex.parent_id == parent_company.id,
                CompanyAnnex.director_id == target.user_id,
                CompanyAnnex.is_active == True,
            )
        )).scalar_one_or_none()
        if already_dir:
            other_filiale = await session.get(Company, already_dir.child_id)
            await update.message.reply_text(
                f"❌ <b>{target.first_name}</b> dirige déjà la filiale <b>{other_filiale.name if other_filiale else '?'}</b>.",
                parse_mode="HTML"
            )
            return

        # Remplacer l'ancien directeur si besoin
        if target_annex.director_id:
            old_dir_emp = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == filiale_obj.id,
                    CompanyEmployee.user_id == target_annex.director_id,
                    CompanyEmployee.left_at == None,
                )
            )).scalar_one_or_none()
            if old_dir_emp:
                old_dir_emp.role = "employe"  # rétrograder l'ancien directeur

        # Nommer le nouveau directeur dans la filiale
        # Vérifier s'il est déjà dans la filiale
        existing_in_filiale = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == filiale_obj.id,
                CompanyEmployee.user_id == target.user_id,
                CompanyEmployee.left_at == None,
            )
        )).scalar_one_or_none()

        if existing_in_filiale:
            existing_in_filiale.role = "pdg"
        else:
            new_dir_emp = CompanyEmployee(
                company_id=filiale_obj.id,
                user_id=target.user_id,
                role="pdg",
            )
            session.add(new_dir_emp)

        # ── CLEF DU FIX : le directeur devient owner_id de la filiale ──
        # Il est ainsi reconnu comme PDG à part entière par toutes les commandes
        # La maison mère reste propriétaire légal via company_annexes
        filiale_obj.owner_id = target.user_id

        # Mettre à jour l'annex
        target_annex.director_id = target.user_id
        await session.commit()

        # Notifier le nouveau directeur
        try:
            await context.bot.send_message(
                chat_id=target.user_id,
                text=(
                    f"🎖️ <b>Tu es nommé(e) PDG de filiale !</b>\n\n"
                    f"🏢 Maison mère : <b>{parent_company.name}</b>\n"
                    f"🏪 Filiale dont tu prends la direction : <b>{filiale_obj.name}</b>\n"
                    f"📊 Reversement vers la mère : <b>{target_annex.revenue_pct}%</b>/jour\n\n"
                    f"✅ Tu es PDG à part entière de ta filiale :\n"
                    f"   recruter, licencier, verser les salaires, passer des contrats,\n"
                    f"   déposer/retirer en trésorerie, acheter des bâtiments...\n"
                    f"❌ Tu ne peux PAS : dissoudre la filiale, la céder à quelqu'un d'autre,\n"
                    f"   créer des sous-filiales, ni retirer plus de 20% de la trésorerie par semaine."
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass

        await update.message.reply_text(
            f"✅ <b>{target.first_name}</b> est nommé(e) Directeur(ice) de <b>{filiale_obj.name}</b> !\n\n"
            f"🏢 Maison mère : <b>{parent_company.name}</b>\n"
            f"🏪 Filiale : <b>{filiale_obj.name}</b>\n"
            f"📊 Reversement : <b>{target_annex.revenue_pct}%</b>/jour → {parent_company.name}",
            parse_mode="HTML"
        )


# ─── COMMANDE : /mesfiliates ─────────────────────────────────────────────────

async def mesfiliates_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Liste les filiales de l'entreprise du PDG."""
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        company, emp = await _get_pdg_company(session, user.id)
        if not company:
            await update.message.reply_text("❌ Réservé au PDG.")
            return

        annexes = (await session.execute(
            select(CompanyAnnex).where(
                CompanyAnnex.parent_id == company.id,
                CompanyAnnex.is_active == True,
            )
        )).scalars().all()

        max_annexes = ANNEX_MAX.get(company.level, 0)
        lines = [
            f"🏢 <b>FILIALES — {company.name}</b>",
            f"📊 {len(annexes)}/{max_annexes} filiale(s) · Niveau {LEVEL_NAMES.get(company.level, '?')}",
            "─────────────────────────────",
        ]

        if not annexes:
            lines.append("📭 Aucune filiale.")
            lines.append(f"\n💡 <code>/creerfiliale [NomFiliale] [capital] [%]</code>")
        else:
            from handlers.company import LEVELS as COMP_LEVELS
            for a in annexes:
                filiale = await session.get(Company, a.child_id)
                if not filiale:
                    continue
                director = await session.get(User, a.director_id) if a.director_id else None
                dir_str = f"@{director.username or director.first_name}" if director else "⚠️ Non nommé"

                _, _, _, monthly_rate, _ = COMP_LEVELS.get(filiale.level, COMP_LEVELS[1])
                daily_rev = int(filiale.value * monthly_rate) // 30
                daily_transfer = int(daily_rev * a.revenue_pct / 100)

                # Compter les employés de la filiale
                emp_count = (await session.execute(
                    select(func.count()).where(
                        CompanyEmployee.company_id == filiale.id,
                        CompanyEmployee.left_at == None,
                    )
                )).scalar() or 0

                lines.append(
                    f"🏪 <b>{filiale.name}</b>\n"
                    f"   📊 Niveau {filiale.level} · {LEVEL_NAMES.get(filiale.level, '?')}\n"
                    f"   🏦 Trésorerie : <b>{_fmt(filiale.treasury)} $</b>\n"
                    f"   💸 Reversement : <b>{a.revenue_pct}%</b>/j (~{_fmt(daily_transfer)} $/j)\n"
                    f"   👤 Directeur : {dir_str}\n"
                    f"   👷 Employés : {emp_count}\n"
                    f"   📅 Créée le {a.created_at.strftime('%d/%m/%Y')}"
                )
                lines.append("")

        lines.append("─────────────────────────────")
        lines.append(f"💡 Nommer un directeur : <code>/nommerdir @pseudo</code>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── COMMANDE : /retirerfiliale [NomFiliale] ─────────────────────────────────

async def retirerfiliale_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Détache une filiale de la maison mère.
    La filiale devient une entreprise indépendante (trésorerie conservée).
    Seul le PDG mère peut faire ça.
    """
    user = update.effective_user

    if not context.args:
        await update.message.reply_text(
            "❌ Usage : <code>/retirerfiliale [NomFiliale]</code>\n"
            "⚠️ La filiale devient indépendante, sa trésorerie est conservée.",
            parse_mode="HTML"
        )
        return

    filiale_name_arg = " ".join(context.args)

    async with AsyncSessionLocal() as session:
        company, emp = await _get_pdg_company(session, user.id)
        if not company:
            await update.message.reply_text("❌ Réservé au PDG.")
            return

        # Trouver l'annex correspondante
        annexes = (await session.execute(
            select(CompanyAnnex).where(
                CompanyAnnex.parent_id == company.id,
                CompanyAnnex.is_active == True,
            )
        )).scalars().all()

        target_annex = None
        filiale_obj = None
        for a in annexes:
            f = await session.get(Company, a.child_id)
            if f and f.name.lower() == filiale_name_arg.lower():
                target_annex = a
                filiale_obj = f
                break

        if not target_annex:
            await update.message.reply_text(
                f"❌ Filiale <b>{filiale_name_arg}</b> introuvable parmi tes filiales.\n"
                f"💡 <code>/mesfiliates</code> pour voir la liste.",
                parse_mode="HTML"
            )
            return

        # Rétrograder le directeur (ex-PDG de filiale) → employé simple
        if target_annex.director_id:
            dir_emp = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == filiale_obj.id,
                    CompanyEmployee.user_id == target_annex.director_id,
                    CompanyEmployee.left_at == None,
                )
            )).scalar_one_or_none()
            if dir_emp:
                dir_emp.role = "employe"

        # Remettre owner_id au PDG mère (la filiale redevient sa propriété légale)
        filiale_obj.owner_id = user.id

        # Désactiver le lien filiale
        target_annex.is_active = False
        await session.commit()

        # Notifier si possible
        try:
            if target_annex.director_id:
                await context.bot.send_message(
                    chat_id=target_annex.director_id,
                    text=(
                        f"⚠️ <b>{filiale_obj.name}</b> a été détachée de <b>{company.name}</b>.\n"
                        f"L'entreprise est désormais indépendante. Tu restes employé(e)."
                    ),
                    parse_mode="HTML"
                )
        except Exception:
            pass

        await update.message.reply_text(
            f"✅ <b>{filiale_obj.name}</b> a été détachée.\n\n"
            f"🏢 Elle est désormais une entreprise indépendante.\n"
            f"💰 Sa trésorerie (<b>{_fmt(filiale_obj.treasury)} $</b>) est conservée.",
            parse_mode="HTML"
        )


# ─── JOB : MAINTENANCE DES BÂTIMENTS (quotidien) ────────────────────────────

async def job_building_maintenance(context: ContextTypes.DEFAULT_TYPE):
    """Prélève la maintenance des bâtiments sur la trésorerie. Suspend si insuffisant."""
    async with AsyncSessionLocal() as session:
        buildings = (await session.execute(select(CompanyBuilding))).scalars().all()
        company_cache: dict[int, Company] = {}

        for b in buildings:
            if b.company_id not in company_cache:
                company_cache[b.company_id] = await session.get(Company, b.company_id)
            company = company_cache[b.company_id]
            if not company or not company.is_active:
                continue

            maint = _building_maintenance(b.building_type, company.level)
            if maint <= 0:
                b.status = "active"
                continue

            if company.treasury >= maint:
                company.treasury -= maint
                b.status = "active"
                b.last_maintenance = datetime.utcnow()
            else:
                b.status = "suspended"

        await session.commit()


# ─── JOB : REVERSEMENT FILIALES (quotidien) ─────────────────────────────────

async def job_annex_revenue(context: ContextTypes.DEFAULT_TYPE):
    """
    Prélève le % de revenus des filiales et le verse à la maison mère.
    Si la trésorerie de la filiale est insuffisante, le reversement est partiel.
    """
    async with AsyncSessionLocal() as session:
        from handlers.company import LEVELS as COMP_LEVELS

        annexes = (await session.execute(
            select(CompanyAnnex).where(CompanyAnnex.is_active == True)
        )).scalars().all()

        for annex in annexes:
            child = await session.get(Company, annex.child_id)
            parent = await session.get(Company, annex.parent_id)
            if not child or not parent or not child.is_active or not parent.is_active:
                continue

            _, _, _, monthly_rate, _ = COMP_LEVELS.get(child.level, COMP_LEVELS[1])
            daily_revenue = int(child.value * monthly_rate) // 30
            transfer = int(daily_revenue * annex.revenue_pct / 100)

            if transfer <= 0:
                continue

            # Reversement partiel si trésorerie insuffisante
            actual_transfer = min(transfer, child.treasury)
            if actual_transfer <= 0:
                continue

            child.treasury -= actual_transfer
            parent.treasury += actual_transfer
            logger.info(
                f"[FILIALE] {child.name} → {parent.name} : {_fmt(actual_transfer)} $ "
                f"({annex.revenue_pct}% de {_fmt(daily_revenue)} $/j)"
            )

        await session.commit()
