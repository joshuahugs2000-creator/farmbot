"""
handlers/company_contracts.py — Contrats automatiques IA pour les entreprises

Fonctionnement :
  - job_dispatch_contracts : tourne toutes les heures, envoie un contrat IA
    à chaque entreprise éligible selon son heure décalée (company.id % 24)
  - Gemini génère : client fictif, mission sectorielle, objectif cmds, récompense, délai
  - PDG reçoit notification avec boutons Accepter / Négocier / Refuser
  - Si accepté → notif à tous les employés + tracking commandes
  - Si négociation → Gemini répond (accepte/refuse/contrepropose)
  - job_check_contracts : tourne toutes les heures, vérifie les contrats actifs
"""

import os
import json
import random
import logging
import aiohttp
from datetime import datetime, timedelta

from sqlalchemy import select, func
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, CallbackQueryHandler

from database.db import AsyncSessionLocal
from database.models import Company, CompanyEmployee, CompanyAutoContract, CompanySettings, User

logger = logging.getLogger(__name__)

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

# Heures entre deux contrats pour une même entreprise (min, max)
CONTRACT_INTERVAL_HOURS = (18, 30)

# Paramètres par secteur : (clients types, missions types, cmds_min, cmds_max, reward_min, reward_max)
SECTOR_CONTRACT_HINTS = {
    "tech": (
        ["DataCorp", "NexaTech", "CloudSys", "ByteWave", "InfoNet"],
        ["développer une application", "auditer un système informatique", "migrer une base de données",
         "sécuriser un réseau", "déployer une infrastructure cloud"],
    ),
    "finance": (
        ["BanqueAlpha", "InvestGroup", "CapitalPro", "TradeStar", "WealthCo"],
        ["réaliser un audit financier", "gérer un portefeuille d'actifs", "analyser les risques d'un marché",
         "préparer un bilan comptable", "conseiller sur une fusion-acquisition"],
    ),
    "commerce": (
        ["MégaStore", "TradeLink", "MarketPlus", "DistribuCo", "RetailMax"],
        ["approvisionner 500 points de vente", "organiser une campagne promotionnelle",
         "gérer la logistique d'un entrepôt", "négocier des contrats fournisseurs",
         "lancer un nouveau produit sur le marché"],
    ),
    "droit": (
        ["CabinetJuris", "LexGroup", "JusticeConseil", "DroctPro", "LegalFirst"],
        ["rédiger un contrat commercial", "représenter un client en arbitrage",
         "auditer la conformité réglementaire", "gérer un litige immobilier",
         "préparer une introduction en bourse"],
    ),
    "agriculture": (
        ["AgroSud", "FermeVerte", "NatureFood", "CulturPro", "BioHarvest"],
        ["livrer 200 tonnes de céréales", "organiser la récolte d'une grande exploitation",
         "mettre en place un système d'irrigation", "certifier des produits biologiques",
         "gérer une coopérative agricole"],
    ),
    "securite": (
        ["SecureZone", "GuardPro", "ShieldCorp", "SafeNet", "VigiForce"],
        ["sécuriser un événement de 5000 personnes", "surveiller un complexe industriel",
         "former du personnel à la cybersécurité", "auditer les accès d'un bâtiment sensible",
         "déployer un système de surveillance"],
    ),
    "immobilier": (
        ["PropriéImmo", "BâtiGroup", "UrbanDev", "ImmoCité", "ConstructPro"],
        ["construire un immeuble résidentiel", "rénover un complexe commercial",
         "gérer un parc immobilier de 50 logements", "expertiser un terrain industriel",
         "coordonner un chantier de grande envergure"],
    ),
    "sante": (
        ["MédiGroup", "SantéPlus", "CliniquePro", "PharmaCo", "BioSanté"],
        ["équiper une clinique en matériel médical", "former du personnel soignant",
         "gérer la logistique d'un hôpital", "lancer une campagne de vaccination",
         "certifier un nouveau médicament"],
    ),
}


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


async def _call_gemini(prompt: str) -> str | None:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return None
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 500, "temperature": 1.0, "topP": 0.95},
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{GEMINI_API_URL}?key={api_key}",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=25),
            ) as resp:
                if resp.status != 200:
                    return None
                result = await resp.json()
                return result["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return None


async def _get_or_create_settings(session, company_id: int) -> CompanySettings:
    settings = (await session.execute(
        select(CompanySettings).where(CompanySettings.company_id == company_id)
    )).scalar_one_or_none()
    if not settings:
        settings = CompanySettings(company_id=company_id)
        session.add(settings)
        await session.flush()
    return settings


async def _get_employee_total_cmds(session, company_id: int) -> int:
    """Somme des command_count de tous les employés actifs."""
    result = await session.execute(
        select(func.sum(CompanyEmployee.command_count)).where(
            CompanyEmployee.company_id == company_id,
            CompanyEmployee.left_at == None,
        )
    )
    return result.scalar() or 0


async def _generate_contract(company: Company) -> dict | None:
    """Génère un contrat via Gemini. Retourne un dict ou None si échec."""
    sector = company.sector
    hints = SECTOR_CONTRACT_HINTS.get(sector, SECTOR_CONTRACT_HINTS["commerce"])
    clients, missions = hints

    # Objectifs et récompenses selon le niveau de l'entreprise
    level = getattr(company, "level", 1) or 1
    CONTRACT_PARAMS = {
        1: {"cmds": (800,    1_200), "reward": (10_000_000,     50_000_000)},
        2: {"cmds": (1_200,  2_000), "reward": (50_000_000,    200_000_000)},
        3: {"cmds": (4_000,  7_000), "reward": (300_000_000,   800_000_000)},
        4: {"cmds": (8_000, 15_000), "reward": (800_000_000, 2_500_000_000)},
        5: {"cmds": (18_000, 30_000),"reward": (2_500_000_000, 8_000_000_000)},
    }

    # Tirage aléatoire de tier — même une startup peut décrocher un gros contrat
    # Probabilités par tier selon le niveau de l'entreprise
    TIER_WEIGHTS = {
        1: [60, 25, 10, 4, 1],   # surtout tier 1, rarement tier 5
        2: [30, 45, 18, 5, 2],
        3: [10, 25, 40, 20, 5],
        4: [5,  10, 25, 45, 15],
        5: [2,   5, 15, 30, 48],
    }
    weights = TIER_WEIGHTS.get(level, TIER_WEIGHTS[1])
    effective_tier = random.choices([1, 2, 3, 4, 5], weights=weights, k=1)[0]
    params = CONTRACT_PARAMS[effective_tier]

    # Label contrat rare
    tier_label = ""
    if effective_tier > level:
        tier_label = {2: "🥈 Contrat Supérieur", 3: "🥇 Contrat Premium",
                      4: "💎 Contrat Prestige", 5: "👑 Contrat Légendaire"}.get(effective_tier, "")

    cmds_obj = random.randint(*params["cmds"])
    reward   = random.randint(*params["reward"])
    # Délai augmente avec le tier
    deadline = {1: 48, 2: 48, 3: 72, 4: 96, 5: 120}.get(effective_tier, 48)

    prompt = (
        f"Tu es un générateur de contrats professionnels pour un jeu économique en français.\n"
        f"Secteur de l'entreprise : {sector}\n"
        f"Génère UN contrat fictif et réaliste avec ces contraintes :\n"
        f"- Objectif : {cmds_obj} commandes d'équipe en {deadline}h\n"
        f"- Récompense proposée : {_fmt(reward)} $\n"
        f"- Client parmi ces types : {', '.join(clients[:3])}\n"
        f"- Mission parmi ces types : {', '.join(missions[:3])}\n"
        + (f"- CONTRAT RARE DE HAUT RANG : rends la mission particulièrement ambitieuse et prestigieuse.\n" if tier_label else "")
        + f"\nRéponds UNIQUEMENT en JSON valide (sans markdown) avec ces champs :\n"
        f"{{\"client\": \"...\", \"mission\": \"...\", \"detail\": \"...\"}}\n"
        f"- client : nom du client fictif (court, réaliste)\n"
        f"- mission : titre court de la mission (max 60 chars)\n"
        f"- detail : description courte et immersive (max 200 chars)"
    )

    raw = await _call_gemini(prompt)

    # Fallback si Gemini échoue
    if not raw:
        return {
            "client": random.choice(clients),
            "mission": random.choice(missions).capitalize(),
            "detail": f"Mission urgente dans le secteur {sector}. Mobilisez votre équipe.",
            "objective_cmds": cmds_obj,
            "reward": reward,
            "deadline_hours": deadline,
            "tier_label": tier_label,
        }

    try:
        raw_clean = raw.strip().strip("```json").strip("```").strip()
        data = json.loads(raw_clean)
        return {
            "client": data.get("client", random.choice(clients)),
            "mission": data.get("mission", random.choice(missions)),
            "detail": data.get("detail", "Mission sectorielle urgente."),
            "objective_cmds": cmds_obj,
            "reward": reward,
            "deadline_hours": deadline,
            "tier_label": tier_label,
        }
    except Exception:
        return {
            "client": random.choice(clients),
            "mission": random.choice(missions).capitalize(),
            "detail": f"Mission urgente dans le secteur {sector}. Mobilisez votre équipe.",
            "objective_cmds": cmds_obj,
            "reward": reward,
            "deadline_hours": deadline,
            "tier_label": tier_label,
        }


def _contract_keyboard(contract_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Accepter", callback_data=f"cnt_accept:{contract_id}"),
            InlineKeyboardButton("🤝 Négocier", callback_data=f"cnt_negoc:{contract_id}"),
            InlineKeyboardButton("❌ Refuser",  callback_data=f"cnt_refuse:{contract_id}"),
        ]
    ])


def _contract_text(contract: CompanyAutoContract, extra: str = "") -> str:
    reward = contract.negotiated_reward or contract.reward
    lines = [
        f"📋 <b>CONTRAT AUTOMATIQUE</b>",
        f"",
        f"🏢 <b>Client :</b> {contract.client_name}",
        f"📌 <b>Mission :</b> {contract.description}",
        f"",
        f"🎯 <b>Objectif :</b> {contract.objective_cmds:,} commandes d'équipe",
        f"⏰ <b>Délai :</b> {contract.deadline_hours}h",
        f"💰 <b>Récompense :</b> {_fmt(reward)} $",
    ]
    if extra:
        lines += ["", extra]
    return "\n".join(lines)


# ─── JOB : DISTRIBUER LES CONTRATS ──────────────────────────────────────────

async def job_dispatch_contracts(context: ContextTypes.DEFAULT_TYPE):
    """
    Tourne toutes les heures.
    Envoie un contrat à chaque entreprise dont c'est l'heure (company.id % 24 == heure UTC courante).
    """
    current_hour = datetime.utcnow().hour
    now = datetime.utcnow()

    async with AsyncSessionLocal() as session:
        companies = (await session.execute(
            select(Company).where(Company.is_active == True, Company.is_bot_company == False)
        )).scalars().all()

        for company in companies:
            # Heure décalée par entreprise
            if company.id % 24 != current_hour:
                continue

            settings = await _get_or_create_settings(session, company.id)

            # Vérifier délai minimum entre contrats
            if settings.next_contract_at and now < settings.next_contract_at:
                continue

            # Vérifier qu'il n'y a pas déjà un contrat pending/active
            existing = (await session.execute(
                select(CompanyAutoContract).where(
                    CompanyAutoContract.company_id == company.id,
                    CompanyAutoContract.status.in_(["pending", "active", "negotiating"]),
                )
            )).scalar_one_or_none()
            if existing:
                continue

            # Générer le contrat via Gemini
            data = await _generate_contract(company)
            if not data:
                continue

            contract = CompanyAutoContract(
                company_id=company.id,
                sector=company.sector,
                client_name=data["client"],
                description=f"{data['mission']} — {data['detail']}",
                objective_cmds=data["objective_cmds"],
                reward=data["reward"],
                deadline_hours=data["deadline_hours"],
                status="pending",
            )
            session.add(contract)
            await session.flush()  # pour avoir contract.id

            # Notifier le PDG
            tier_label = data.get("tier_label", "")
            intro = f"{tier_label}\n👇 Que souhaitez-vous faire ?" if tier_label else "👇 Que souhaitez-vous faire ?"
            try:
                msg = await context.bot.send_message(
                    chat_id=company.owner_id,
                    text=_contract_text(contract, intro),
                    parse_mode="HTML",
                    reply_markup=_contract_keyboard(contract.id),
                )
                contract.notif_message_id = msg.message_id
            except Exception as e:
                logger.warning(f"Impossible de notifier PDG {company.owner_id}: {e}")
                continue

            # Planifier le prochain contrat
            next_h = random.randint(*CONTRACT_INTERVAL_HOURS)
            settings.next_contract_at = now + timedelta(hours=next_h)

        await session.commit()


# ─── JOB : VÉRIFIER LES CONTRATS ACTIFS ─────────────────────────────────────

async def job_check_contracts(context: ContextTypes.DEFAULT_TYPE):
    """Vérifie chaque heure si les contrats actifs sont complétés ou expirés."""
    now = datetime.utcnow()

    async with AsyncSessionLocal() as session:
        active = (await session.execute(
            select(CompanyAutoContract).where(
                CompanyAutoContract.status == "active"
            )
        )).scalars().all()

        for contract in active:
            company = await session.get(Company, contract.company_id)
            if not company:
                continue

            # Commandes effectuées depuis l'acceptation
            total_cmds_now = await _get_employee_total_cmds(session, contract.company_id)
            cmds_done = max(0, total_cmds_now - (contract.cmds_at_start or 0))
            reward = contract.negotiated_reward or contract.reward

            # Contrat réussi
            if cmds_done >= contract.objective_cmds:
                contract.status = "completed"
                company.treasury += reward
                # Bonus réputation variable selon performance
                rep_gain = random.choice([0.05, 0.05, 0.075, 0.075, 0.10])
                company.reputation = min(5.0, (company.reputation or 3.0) + rep_gain)
                try:
                    await context.bot.send_message(
                        chat_id=company.owner_id,
                        text=(
                            f"🎉 <b>Contrat accompli !</b>\n\n"
                            f"🏢 <b>{contract.client_name}</b> — {contract.description[:80]}…\n\n"
                            f"✅ Objectif atteint : <b>{cmds_done:,} / {contract.objective_cmds:,} commandes</b>\n"
                            f"💰 Récompense créditée : <b>+{_fmt(reward)} $</b> en trésorerie\n"
                            f"⭐ Réputation : <b>+{rep_gain}</b> → {company.reputation:.2f}/5"
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

            # Contrat expiré
            elif contract.deadline_at and now > contract.deadline_at:
                contract.status = "failed"
                # Pénalité réputation
                if company.reputation > 1.0:
                    company.reputation = max(1.0, company.reputation - 0.1)
                try:
                    await context.bot.send_message(
                        chat_id=company.owner_id,
                        text=(
                            f"⚠️ <b>Contrat échoué !</b>\n\n"
                            f"🏢 <b>{contract.client_name}</b>\n"
                            f"❌ Objectif non atteint : <b>{cmds_done:,} / {contract.objective_cmds:,} commandes</b>\n"
                            f"📉 Réputation -0.1 · Aucune récompense versée."
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        await session.commit()


# ─── CALLBACKS : ACCEPTER / NÉGOCIER / REFUSER ───────────────────────────────

async def contract_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data  # cnt_accept:ID | cnt_negoc:ID | cnt_refuse:ID

    action, contract_id_str = data.split(":")
    contract_id = int(contract_id_str)
    user = query.from_user

    async with AsyncSessionLocal() as session:
        contract = await session.get(CompanyAutoContract, contract_id)
        if not contract:
            await query.edit_message_text("❌ Contrat introuvable.")
            return

        company = await session.get(Company, contract.company_id)
        if not company or company.owner_id != user.id:
            await query.answer("❌ Seul le PDG peut répondre à ce contrat.", show_alert=True)
            return

        if contract.status not in ("pending", "negotiating"):
            await query.edit_message_text(
                _contract_text(contract, f"ℹ️ Ce contrat est déjà : <b>{contract.status}</b>"),
                parse_mode="HTML",
            )
            return

        # ── ACCEPTER ──────────────────────────────────────────────────────────
        if action == "cnt_accept":
            contract.status = "active"
            contract.accepted_at = datetime.utcnow()
            contract.deadline_at = datetime.utcnow() + timedelta(hours=contract.deadline_hours)
            contract.cmds_at_start = await _get_employee_total_cmds(session, company.id)

            reward = contract.negotiated_reward or contract.reward
            deadline_str = contract.deadline_at.strftime("%d/%m à %H:%M UTC")

            await query.edit_message_text(
                _contract_text(contract, f"✅ <b>Contrat accepté !</b> Deadline : {deadline_str}"),
                parse_mode="HTML",
            )

            # Notifier tous les employés
            emps = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == company.id,
                    CompanyEmployee.left_at == None,
                    CompanyEmployee.role != "pdg",
                )
            )).scalars().all()

            for emp in emps:
                try:
                    await context.bot.send_message(
                        chat_id=emp.user_id,
                        text=(
                            f"📋 <b>Nouveau contrat pour {company.name} !</b>\n\n"
                            f"🏢 <b>Client :</b> {contract.client_name}\n"
                            f"📌 <b>Mission :</b> {contract.description[:100]}\n\n"
                            f"🎯 Objectif équipe : <b>{contract.objective_cmds:,} commandes</b>\n"
                            f"⏰ Deadline : <b>{deadline_str}</b>\n"
                            f"💰 Récompense : <b>{_fmt(reward)} $</b>\n\n"
                            f"💪 Soyez actifs sur le bot pour atteindre l'objectif !"
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        # ── REFUSER ───────────────────────────────────────────────────────────
        elif action == "cnt_refuse":
            contract.status = "rejected"
            await query.edit_message_text(
                _contract_text(contract, "❌ <b>Contrat refusé.</b> Un nouveau contrat sera proposé prochainement."),
                parse_mode="HTML",
            )

        # ── NÉGOCIER ──────────────────────────────────────────────────────────
        elif action == "cnt_negoc":
            if contract.negotiation_round >= 2:
                await query.answer("❌ Nombre maximum de négociations atteint.", show_alert=True)
                return

            contract.status = "negotiating"
            contract.negotiation_round = (contract.negotiation_round or 0) + 1
            current_reward = contract.negotiated_reward or contract.reward

            # Appel Gemini pour répondre à la négociation
            prompt = (
                f"Tu es un client professionnel qui négocie un contrat.\n"
                f"Contrat : {contract.description}\n"
                f"Récompense actuelle proposée : {_fmt(current_reward)} $\n"
                f"L'entreprise veut négocier. Tour {contract.negotiation_round}/2.\n\n"
                f"Réponds en JSON valide sans markdown :\n"
                f"{{\"decision\": \"hausse\" | \"baisse\" | \"maintien\" | \"refus\", "
                f"\"nouveau_montant\": <entier ou null>, \"message\": \"<réponse courte du client, max 120 chars>\"}}\n"
                f"- Si tu accordes une hausse : nouveau_montant > current_reward (max +30%)\n"
                f"- Si tu baisses : nouveau_montant < current_reward (min -10%)\n"
                f"- Si refus : decision=refus, nouveau_montant=null"
            )

            raw = await _call_gemini(prompt)
            decision = "maintien"
            new_reward = current_reward
            client_msg = "Nous maintenons notre offre initiale. C'est à prendre ou à laisser."

            if raw:
                try:
                    raw_c = raw.strip().strip("```json").strip("```").strip()
                    parsed = json.loads(raw_c)
                    decision = parsed.get("decision", "maintien")
                    client_msg = parsed.get("message", client_msg)[:200]
                    if decision in ("hausse", "baisse") and parsed.get("nouveau_montant"):
                        new_reward = int(parsed["nouveau_montant"])
                    elif decision == "refus":
                        contract.status = "rejected"
                except Exception:
                    pass

            if decision == "refus":
                await query.edit_message_text(
                    _contract_text(contract, f"❌ <b>Client :</b> « {client_msg} »\n\nContrat annulé."),
                    parse_mode="HTML",
                )
            else:
                contract.negotiated_reward = new_reward
                contract.status = "pending"
                keyboard = _contract_keyboard(contract.id)
                if contract.negotiation_round >= 2:
                    # Dernière chance : plus de bouton négocier
                    keyboard = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("✅ Accepter", callback_data=f"cnt_accept:{contract.id}"),
                            InlineKeyboardButton("❌ Refuser",  callback_data=f"cnt_refuse:{contract.id}"),
                        ]
                    ])
                await query.edit_message_text(
                    _contract_text(
                        contract,
                        f"💬 <b>Client :</b> « {client_msg} »\n\n"
                        f"💰 Nouvelle offre : <b>{_fmt(new_reward)} $</b>\n"
                        + ("⚠️ Dernière négociation possible." if contract.negotiation_round >= 2 else "")
                    ),
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )

        await session.commit()


# ─── COMMANDE : /mescontrats_auto ─────────────────────────────────────────────

async def mescontratsauto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche les contrats automatiques de l'entreprise du PDG."""
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        company = (await session.execute(
            select(Company).where(
                Company.owner_id == user.id,
                Company.is_active == True,
            )
        )).scalar_one_or_none()

        if not company:
            await update.message.reply_text("❌ Tu n'es pas PDG d'une entreprise active.")
            return

        contracts = (await session.execute(
            select(CompanyAutoContract).where(
                CompanyAutoContract.company_id == company.id,
            ).order_by(CompanyAutoContract.created_at.desc()).limit(10)
        )).scalars().all()

        if not contracts:
            await update.message.reply_text("📭 Aucun contrat automatique reçu pour l'instant.")
            return

        STATUS_LABEL = {
            "pending": "⏳ En attente",
            "active": "🔄 En cours",
            "completed": "✅ Réussi",
            "failed": "❌ Échoué",
            "rejected": "🚫 Refusé",
            "negotiating": "🤝 Négociation",
        }

        lines = [f"📋 <b>Contrats automatiques — {company.name}</b>\n"]
        for c in contracts:
            reward = c.negotiated_reward or c.reward
            label = STATUS_LABEL.get(c.status, c.status)
            progress = ""
            if c.status == "active":
                total_now = await _get_employee_total_cmds(session, company.id)
                done = max(0, total_now - (c.cmds_at_start or 0))
                pct = min(100, int(done / c.objective_cmds * 100))
                bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                progress = f"\n   [{bar}] {done:,}/{c.objective_cmds:,} cmds ({pct}%)"
            lines.append(
                f"• {label} · <b>{c.client_name}</b>\n"
                f"   {c.description[:60]}…\n"
                f"   💰 {_fmt(reward)} $ · ⏰ {c.deadline_hours}h{progress}\n"
            )

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def claimcontrat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /claimcontrat — Permet au PDG de réclamer immédiatement la récompense
    si l'objectif du contrat actif est déjà atteint, sans attendre la deadline.
    """
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        company = (await session.execute(
            select(Company).where(
                Company.owner_id == user.id,
                Company.is_active == True,
            )
        )).scalar_one_or_none()

        if not company:
            await update.message.reply_text("❌ Tu n'es pas PDG d'une entreprise active.")
            return

        contract = (await session.execute(
            select(CompanyAutoContract).where(
                CompanyAutoContract.company_id == company.id,
                CompanyAutoContract.status == "active",
            )
        )).scalar_one_or_none()

        if not contract:
            await update.message.reply_text(
                "📭 Aucun contrat actif en cours.\n"
                "Utilise /mescontratsauto pour voir l'état de tes contrats."
            )
            return

        total_cmds_now = await _get_employee_total_cmds(session, contract.company_id)
        cmds_done = max(0, total_cmds_now - (contract.cmds_at_start or 0))
        reward = contract.negotiated_reward or contract.reward

        if cmds_done < contract.objective_cmds:
            pct = min(100, int(cmds_done / contract.objective_cmds * 100))
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            remaining = contract.objective_cmds - cmds_done
            await update.message.reply_text(
                f"⏳ <b>Objectif pas encore atteint !</b>\n\n"
                f"🏢 <b>{contract.client_name}</b>\n"
                f"📌 {contract.description[:80]}…\n\n"
                f"[{bar}] {cmds_done:,} / {contract.objective_cmds:,} cmds ({pct}%)\n"
                f"Il manque encore <b>{remaining:,} commandes</b> pour réclamer la récompense.",
                parse_mode="HTML",
            )
            return

        # Objectif atteint → on crédite immédiatement
        contract.status = "completed"
        company.treasury += reward
        rep_gain = random.choice([0.05, 0.05, 0.075, 0.075, 0.10])
        company.reputation = min(5.0, (company.reputation or 3.0) + rep_gain)
        await session.commit()

        # Calcul du temps gagné
        now = datetime.utcnow()
        time_saved = ""
        if contract.deadline_at and now < contract.deadline_at:
            diff = contract.deadline_at - now
            hours_saved = int(diff.total_seconds() // 3600)
            mins_saved  = int((diff.total_seconds() % 3600) // 60)
            time_saved = f"⚡ Récompense réclamée <b>{hours_saved}h{mins_saved:02d}</b> avant la deadline !\n\n"

        await update.message.reply_text(
            f"🎉 <b>Contrat accompli — Récompense réclamée !</b>\n\n"
            f"🏢 <b>{contract.client_name}</b> — {contract.description[:80]}…\n\n"
            f"✅ Objectif atteint : <b>{cmds_done:,} / {contract.objective_cmds:,} commandes</b>\n"
            f"{time_saved}"
            f"💰 Récompense créditée : <b>+{_fmt(reward)} $</b> en trésorerie\n"
            f"⭐ Réputation : <b>+{rep_gain}</b> → {company.reputation:.2f}/5",
            parse_mode="HTML",
        )


async def init_contract_tables():
    """Crée les tables si elles n'existent pas."""
    from sqlalchemy import text
    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS company_auto_contracts (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id),
                sector VARCHAR(50) NOT NULL,
                client_name VARCHAR(150) NOT NULL,
                description VARCHAR(600) NOT NULL,
                objective_cmds INTEGER NOT NULL,
                reward BIGINT NOT NULL,
                deadline_hours INTEGER NOT NULL,
                status VARCHAR(20) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW(),
                accepted_at TIMESTAMP,
                deadline_at TIMESTAMP,
                cmds_at_start BIGINT DEFAULT 0,
                negotiated_reward BIGINT,
                negotiation_round INTEGER DEFAULT 0,
                notif_message_id BIGINT
            )
        """))
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS company_settings (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL UNIQUE REFERENCES companies(id),
                auto_payroll BOOLEAN DEFAULT FALSE,
                next_contract_at TIMESTAMP
            )
        """))
        await session.commit()
    logger.info("Tables contrats automatiques initialisées.")
