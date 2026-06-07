"""
handlers/buildings.py — Système de bâtiments et filiales d'entreprise

Bâtiments :
  /batiments              → catalogue des bâtiments + prix par niveau
  /acheterbatiment [type] → achète depuis la trésorerie
  /mesbatiments           → liste tes bâtiments actifs

Filiales :
      → Le PDG crée une nouvelle entreprise filiale (même secteur, capital injecté
        depuis la trésorerie mère). La filiale est une vraie entreprise avec ses
        propres employés, contrats, etc. Elle reverse chaque jour X% de ses revenus
        à la maison mère. Une filiale ne peut PAS avoir ses propres filiales.

      → Le PDG nomme un de ses employés (non-directeur) comme Directeur de filiale.
        Il obtient le rôle "directeur" dans la filiale et peut tout gérer sauf :
        dissolution, cession, création de sous-filiale, retrait > 20% tréso/semaine.

  /infofiliale [nom]      → détails d'une filiale
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
    User, Company, CompanyEmployee, CompanyBuilding, CompanyShare,
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
