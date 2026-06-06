"""
handlers/buildings.py — Système de bâtiments et filiales d'entreprise

Commandes :
  /batiments              → liste les bâtiments disponibles + prix pour ton niveau
  /acheterbatiment [type] → achète un bâtiment (payé depuis trésorerie)
  /mesbatiments           → liste tes bâtiments actifs
  /creerfiliale [nom] [%] → crée une filiale de ton entreprise
  /mesfiliates            → liste tes filiales
  /nommerdir @pseudo [nom_filiale] → nomme un directeur dans une filiale
  /infofiliale [nom]      → infos sur une filiale
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, func
from telegram import Update
from telegram.ext import ContextTypes

from database.db import AsyncSessionLocal, get_user
from database.models import (
    User, Company, CompanyEmployee, CompanyBuilding, CompanyAnnex,
)

logger = logging.getLogger(__name__)

# ─── CATALOGUE DES BÂTIMENTS ─────────────────────────────────────────────────
# base_cost × multiplicateur_niveau (×5 par niveau)

LEVEL_MULTIPLIER = {1: 1, 2: 5, 3: 25, 4: 125, 5: 625}

BUILDINGS = {
    "salle_reunion": {
        "name":      "🪑 Salle de Réunion",
        "base_cost": 500_000,
        "effect":    "-10% délai négociation de contrats",
        "unlock_lvl": 1,
        "maintenance_pct": 0.005,  # 0.5% du coût d'achat / jour
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

# Filiales max par niveau d'entreprise
ANNEX_MAX = {1: 0, 2: 1, 3: 2, 4: 3, 5: 5}

LEVEL_NAMES = {1: "Startup", 2: "PME", 3: "Société", 4: "Corporation", 5: "Holding"}

# ─── UTILITAIRES ─────────────────────────────────────────────────────────────

def _fmt(n: int) -> str:
    if n >= 1_000_000_000: return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:     return f"{n/1_000_000:.1f}M"
    if n >= 1_000:         return f"{n/1_000:.0f}K"
    return str(n)


def _building_cost(btype: str, company_level: int) -> int:
    b = BUILDINGS.get(btype)
    if not b:
        return 0
    mult = LEVEL_MULTIPLIER.get(company_level, 1)
    return b["base_cost"] * mult


def _building_maintenance(btype: str, company_level: int) -> int:
    b = BUILDINGS.get(btype)
    if not b:
        return 0
    cost = _building_cost(btype, company_level)
    return int(cost * b["maintenance_pct"])


async def _get_user_company(session, user_id: int):
    from database.models import Company
    r = await session.execute(
        select(CompanyEmployee, Company).join(
            Company, Company.id == CompanyEmployee.company_id
        ).where(
            CompanyEmployee.user_id == user_id,
            CompanyEmployee.left_at == None,
            Company.is_active == True,
        ).order_by(
            CompanyEmployee.role.in_(["pdg"]).desc(),
        )
    )
    row = r.first()
    if row:
        return row[1], row[0]
    return None, None


# ─── COMMANDE : /batiments ────────────────────────────────────────────────────

async def batiments_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le catalogue des bâtiments avec les prix adaptés au niveau de l'entreprise."""
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)

        if not company:
            await update.message.reply_text(
                "❌ Tu ne fais partie d'aucune entreprise.",
                parse_mode="HTML"
            )
            return

        # Récupérer les bâtiments déjà achetés
        owned = (await session.execute(
            select(CompanyBuilding).where(
                CompanyBuilding.company_id == company.id
            )
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
                status = owned_types[btype]
                status_icon = "✅" if status == "active" else "⚠️"
                lines.append(
                    f"{status_icon} <b>{info['name']}</b> — <i>Déjà acheté</i>\n"
                    f"   {info['effect']}"
                )
            elif locked:
                lines.append(
                    f"🔒 <b>{info['name']}</b> — Débloqué niveau {unlock_lvl}\n"
                    f"   {info['effect']}"
                )
            else:
                can_afford = "✅" if company.treasury >= cost else "❌"
                lines.append(
                    f"{can_afford} <b>{info['name']}</b>\n"
                    f"   💰 Achat : <b>{_fmt(cost)} $</b> · Maintenance : <b>{_fmt(maint)} $/j</b>\n"
                    f"   {info['effect']}\n"
                    f"   👉 <code>/acheterbatiment {btype}</code>"
                )
            lines.append("")

        lines.append("─────────────────────────────")
        lines.append("💡 La maintenance est prélevée quotidiennement sur la trésorerie.")
        lines.append("⚠️ Si trésorerie insuffisante, le bâtiment est suspendu.")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── COMMANDE : /acheterbatiment [type] ──────────────────────────────────────

async def acheterbatiment_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Achète un bâtiment pour l'entreprise depuis la trésorerie."""
    user = update.effective_user

    if not context.args:
        types_list = " | ".join(BUILDINGS.keys())
        await update.message.reply_text(
            f"❌ Usage : <code>/acheterbatiment [type]</code>\n"
            f"Types : {types_list}\n\n"
            f"💡 Voir les prix avec <code>/batiments</code>",
            parse_mode="HTML"
        )
        return

    btype = context.args[0].lower()
    if btype not in BUILDINGS:
        await update.message.reply_text(
            f"❌ Type de bâtiment invalide.\n"
            f"💡 Liste : <code>/batiments</code>",
            parse_mode="HTML"
        )
        return

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)

        if not company:
            await update.message.reply_text("❌ Tu ne fais partie d'aucune entreprise.")
            return

        if emp.role != "pdg":
            await update.message.reply_text("❌ Seul le PDG peut acheter des bâtiments.")
            return

        if company.is_bot_company:
            await update.message.reply_text("❌ Impossible sur une entreprise officielle.")
            return

        info = BUILDINGS[btype]
        unlock_lvl = info["unlock_lvl"]

        if company.level < unlock_lvl:
            await update.message.reply_text(
                f"🔒 <b>{info['name']}</b> est débloqué au niveau <b>{unlock_lvl}</b>.\n"
                f"Ton entreprise est niveau <b>{company.level}</b>.",
                parse_mode="HTML"
            )
            return

        # Vérifier doublon
        already = (await session.execute(
            select(CompanyBuilding).where(
                CompanyBuilding.company_id == company.id,
                CompanyBuilding.building_type == btype,
            )
        )).scalar_one_or_none()
        if already:
            await update.message.reply_text(
                f"❌ Tu as déjà <b>{info['name']}</b> dans ton entreprise.\n"
                f"Maximum 1 bâtiment de chaque type.",
                parse_mode="HTML"
            )
            return

        cost = _building_cost(btype, company.level)
        maint = _building_maintenance(btype, company.level)

        if company.treasury < cost:
            await update.message.reply_text(
                f"❌ Trésorerie insuffisante.\n\n"
                f"💸 Coût : <b>{_fmt(cost)} $</b>\n"
                f"🏦 Trésorerie : <b>{_fmt(company.treasury)} $</b>",
                parse_mode="HTML"
            )
            return

        # Achat
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
            f"💸 Coût débité : <b>{_fmt(cost)} $</b>\n"
            f"🔧 Maintenance quotidienne : <b>{_fmt(maint)} $/j</b>\n"
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
            select(CompanyBuilding).where(
                CompanyBuilding.company_id == company.id
            )
        )).scalars().all()

        if not buildings:
            await update.message.reply_text(
                f"🏗️ <b>{company.name}</b> n'a aucun bâtiment.\n\n"
                f"💡 Achète un bâtiment avec <code>/batiments</code>",
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
            name = info.get("name", b.building_type)
            effect = info.get("effect", "—")
            maint = _building_maintenance(b.building_type, company.level)
            status_icon = "✅" if b.status == "active" else "⚠️ SUSPENDU"
            bought = b.purchased_at.strftime("%d/%m/%Y") if b.purchased_at else "?"
            lines.append(
                f"{status_icon} <b>{name}</b>\n"
                f"   {effect}\n"
                f"   🔧 Maintenance : {_fmt(maint)} $/j · Acheté le {bought}"
            )

        lines.append("─────────────────────────────")
        lines.append(f"💰 Total maintenance/j : <b>{_fmt(total_maint)} $</b>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── COMMANDE : /creerfiliale [nom_entreprise] [pourcentage] ─────────────────

async def creerfiliale_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Crée une filiale d'une entreprise existante.
    Usage : /creerfiliale [nom_filiale_existante] [pourcentage 5-40]
    La filiale doit déjà exister (être une entreprise créée par un joueur).
    Le PDG de la maison mère propose la relation de filiale au PDG de l'autre boite.
    """
    user = update.effective_user

    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Usage : <code>/creerfiliale [nom entreprise] [% reversement 5-40]</code>\n\n"
            "📋 La filiale doit être une entreprise existante dont le PDG accepte la relation.\n"
            "Le pourcentage est la part de revenus que la filiale reverse chaque jour à la maison mère.\n\n"
            "Exemple : <code>/creerfiliale NexaFils 20</code>",
            parse_mode="HTML"
        )
        return

    try:
        pct = float(context.args[-1])
    except ValueError:
        await update.message.reply_text("❌ Pourcentage invalide. Exemple : <code>/creerfiliale NexaFils 20</code>", parse_mode="HTML")
        return

    if not (5 <= pct <= 40):
        await update.message.reply_text("❌ Le pourcentage doit être entre <b>5%</b> et <b>40%</b>.", parse_mode="HTML")
        return

    annex_name = " ".join(context.args[:-1])

    async with AsyncSessionLocal() as session:
        parent_company, emp = await _get_user_company(session, user.id)

        if not parent_company:
            await update.message.reply_text("❌ Tu ne fais partie d'aucune entreprise.")
            return

        if emp.role != "pdg":
            await update.message.reply_text("❌ Seul le PDG peut créer des filiales.")
            return

        if parent_company.is_bot_company:
            await update.message.reply_text("❌ Les entreprises officielles ne peuvent pas avoir de filiales.")
            return

        # Vérifier le nombre de filiales autorisées
        max_annexes = ANNEX_MAX.get(parent_company.level, 0)
        if max_annexes == 0:
            await update.message.reply_text(
                f"❌ Les <b>{LEVEL_NAMES.get(parent_company.level, 'Startup')}</b> ne peuvent pas créer de filiale.\n"
                f"Monte au niveau <b>PME</b> (niveau 2) pour débloquer cette fonctionnalité.",
                parse_mode="HTML"
            )
            return

        current_annexes = (await session.execute(
            select(func.count()).where(
                CompanyAnnex.parent_id == parent_company.id,
                CompanyAnnex.is_active == True,
            )
        )).scalar()

        if current_annexes >= max_annexes:
            await update.message.reply_text(
                f"❌ Tu as atteint le maximum de <b>{max_annexes} filiale(s)</b> "
                f"pour une entreprise de niveau <b>{LEVEL_NAMES.get(parent_company.level, '?')}</b>.",
                parse_mode="HTML"
            )
            return

        # Chercher l'entreprise cible
        from database.models import Company
        child = (await session.execute(
            select(Company).where(
                Company.name.ilike(annex_name),
                Company.is_active == True,
                Company.is_bot_company == False,
            )
        )).scalar_one_or_none()

        if not child:
            await update.message.reply_text(
                f"❌ Entreprise <b>{annex_name}</b> introuvable.\n"
                f"La filiale doit être une entreprise joueur existante.",
                parse_mode="HTML"
            )
            return

        if child.id == parent_company.id:
            await update.message.reply_text("❌ Une entreprise ne peut pas être sa propre filiale.")
            return

        # Vérifier que ce n'est pas déjà une filiale
        existing = (await session.execute(
            select(CompanyAnnex).where(
                CompanyAnnex.parent_id == parent_company.id,
                CompanyAnnex.child_id == child.id,
                CompanyAnnex.is_active == True,
            )
        )).scalar_one_or_none()
        if existing:
            await update.message.reply_text(
                f"❌ <b>{child.name}</b> est déjà une filiale de <b>{parent_company.name}</b>.",
                parse_mode="HTML"
            )
            return

        # Vérifier que la filiale n'est pas déjà rattachée à une autre maison mère
        already_child = (await session.execute(
            select(CompanyAnnex).where(
                CompanyAnnex.child_id == child.id,
                CompanyAnnex.is_active == True,
            )
        )).scalar_one_or_none()
        if already_child:
            await update.message.reply_text(
                f"❌ <b>{child.name}</b> est déjà la filiale d'une autre entreprise.",
                parse_mode="HTML"
            )
            return

        # Créer la relation de filiale
        annex = CompanyAnnex(
            parent_id=parent_company.id,
            child_id=child.id,
            director_id=None,
            revenue_pct=pct,
            is_active=True,
        )
        session.add(annex)
        await session.commit()

        # Notifier le PDG de la filiale
        try:
            await context.bot.send_message(
                chat_id=child.owner_id,
                text=(
                    f"🏢 <b>Proposition de filialisation !</b>\n\n"
                    f"<b>{parent_company.name}</b> (niveau {LEVEL_NAMES.get(parent_company.level, '?')}) "
                    f"souhaite faire de <b>{child.name}</b> sa filiale.\n\n"
                    f"📊 Reversement proposé : <b>{pct}% de tes revenus journaliers</b>\n\n"
                    f"La relation est active. Si tu n'es pas d'accord, contacte le PDG."
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass

        await update.message.reply_text(
            f"✅ <b>{child.name}</b> est maintenant une filiale de <b>{parent_company.name}</b> !\n\n"
            f"💸 Reversement quotidien : <b>{pct}%</b> de ses revenus → trésorerie maison mère\n"
            f"👤 Directeur : <i>Non nommé</i> — <code>/nommerdir @pseudo {child.name}</code>",
            parse_mode="HTML"
        )


# ─── COMMANDE : /mesfiliates ─────────────────────────────────────────────────

async def mesfiliates_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Liste les filiales de l'entreprise du PDG."""
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)

        if not company or emp.role != "pdg":
            await update.message.reply_text("❌ Réservé au PDG.", parse_mode="HTML")
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
            f"📊 {len(annexes)}/{max_annexes} filiale(s)",
            "─────────────────────────────",
        ]

        if not annexes:
            lines.append("📭 Aucune filiale.")
            lines.append("\n💡 Crée une filiale avec <code>/creerfiliale [nom] [%]</code>")
        else:
            from database.models import Company as CompanyModel
            for a in annexes:
                child = await session.get(CompanyModel, a.child_id)
                if not child:
                    continue
                director = await session.get(User, a.director_id) if a.director_id else None
                dir_name = f"@{director.username or director.first_name}" if director else "Non nommé"
                lines.append(
                    f"🏪 <b>{child.name}</b>\n"
                    f"   💸 Reversement : <b>{a.revenue_pct}%</b>/jour\n"
                    f"   👤 Directeur : {dir_name}\n"
                    f"   📅 Créé : {a.created_at.strftime('%d/%m/%Y')}"
                )

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── COMMANDE : /nommerdir @pseudo [nom_filiale] ─────────────────────────────

async def nommerdir_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Nomme un employé comme directeur d'une filiale.
    Un employé ne peut diriger qu'une seule filiale.
    Usage : /nommerdir @pseudo [nom_filiale]
    """
    user = update.effective_user

    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Usage : <code>/nommerdir @pseudo [nom filiale]</code>",
            parse_mode="HTML"
        )
        return

    mention = context.args[0].lstrip("@")
    annex_name = " ".join(context.args[1:])

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)

        if not company or emp.role != "pdg":
            await update.message.reply_text("❌ Réservé au PDG.")
            return

        # Chercher l'employé cible
        target = (await session.execute(
            select(User).where(User.username == mention)
        )).scalar_one_or_none()
        if not target:
            await update.message.reply_text(f"❌ @{mention} introuvable.")
            return

        # Vérifier que la cible est employée de l'entreprise mère
        target_emp = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.user_id == target.user_id,
                CompanyEmployee.left_at == None,
            )
        )).scalar_one_or_none()
        if not target_emp:
            await update.message.reply_text(
                f"❌ <b>{target.first_name}</b> n'est pas employé(e) dans <b>{company.name}</b>.",
                parse_mode="HTML"
            )
            return

        # Vérifier que cet employé ne dirige pas déjà une filiale
        already_dir = (await session.execute(
            select(CompanyAnnex).where(
                CompanyAnnex.parent_id == company.id,
                CompanyAnnex.director_id == target.user_id,
                CompanyAnnex.is_active == True,
            )
        )).scalar_one_or_none()
        if already_dir:
            from database.models import Company as CompanyModel
            child_co = await session.get(CompanyModel, already_dir.child_id)
            await update.message.reply_text(
                f"❌ <b>{target.first_name}</b> dirige déjà la filiale <b>{child_co.name if child_co else '?'}</b>.\n"
                f"Un employé ne peut diriger qu'une seule filiale.",
                parse_mode="HTML"
            )
            return

        # Trouver la filiale par nom
        from database.models import Company as CompanyModel
        child = (await session.execute(
            select(CompanyModel).where(
                CompanyModel.name.ilike(annex_name),
                CompanyModel.is_active == True,
            )
        )).scalar_one_or_none()
        if not child:
            await update.message.reply_text(f"❌ Filiale <b>{annex_name}</b> introuvable.", parse_mode="HTML")
            return

        annex = (await session.execute(
            select(CompanyAnnex).where(
                CompanyAnnex.parent_id == company.id,
                CompanyAnnex.child_id == child.id,
                CompanyAnnex.is_active == True,
            )
        )).scalar_one_or_none()
        if not annex:
            await update.message.reply_text(
                f"❌ <b>{child.name}</b> n'est pas une filiale de <b>{company.name}</b>.",
                parse_mode="HTML"
            )
            return

        annex.director_id = target.user_id
        await session.commit()

        await update.message.reply_text(
            f"✅ <b>{target.first_name}</b> est nommé(e) Directeur(ice) de la filiale <b>{child.name}</b> !",
            parse_mode="HTML"
        )
        try:
            await context.bot.send_message(
                chat_id=target.user_id,
                text=(
                    f"🎖️ <b>Tu es nommé(e) Directeur(ice) de filiale !</b>\n\n"
                    f"🏢 Maison mère : <b>{company.name}</b>\n"
                    f"🏪 Filiale : <b>{child.name}</b>\n"
                    f"💸 Reversement vers la maison mère : <b>{annex.revenue_pct}%</b>/jour"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass


# ─── JOB : MAINTENANCE DES BÂTIMENTS (quotidien) ────────────────────────────

async def job_building_maintenance(context: ContextTypes.DEFAULT_TYPE):
    """Prélève la maintenance des bâtiments sur la trésorerie. Suspend si insuffisant."""
    async with AsyncSessionLocal() as session:
        from database.models import Company
        buildings = (await session.execute(
            select(CompanyBuilding)
        )).scalars().all()

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
    """Prélève le % de revenus des filiales et le verse à la maison mère."""
    async with AsyncSessionLocal() as session:
        from database.models import Company
        annexes = (await session.execute(
            select(CompanyAnnex).where(CompanyAnnex.is_active == True)
        )).scalars().all()

        for annex in annexes:
            child = await session.get(Company, annex.child_id)
            parent = await session.get(Company, annex.parent_id)
            if not child or not parent or not child.is_active or not parent.is_active:
                continue

            # Calcul du reversement sur les revenus journaliers de la filiale
            from handlers.company import LEVELS as COMP_LEVELS
            _, _, _, monthly_rate, _ = COMP_LEVELS.get(child.level, COMP_LEVELS[1])
            daily_revenue = int(child.value * monthly_rate) // 30
            transfer = int(daily_revenue * annex.revenue_pct / 100)

            if transfer <= 0:
                continue

            if child.treasury >= transfer:
                child.treasury -= transfer
                parent.treasury += transfer
            # Si trésorerie filiale insuffisante, on skip silencieusement

        await session.commit()
