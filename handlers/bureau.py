"""
📋 Bureau des Contrats — contrats basés sur un objectif de commandes d'équipe
Même mécanique que les contrats automatiques :
  - PDG soumet son dossier → 3 contrats proposés avec objectif en commandes
  - PDG choisit → toute l'équipe doit atteindre l'objectif
  - Job horaire vérifie → verse la récompense si objectif atteint
  - Si délai dépassé sans objectif atteint → contrat échoué
"""
import os
import json
import random
import aiohttp
from datetime import datetime, timedelta

from sqlalchemy import select, func
from telegram import Update
from telegram.ext import ContextTypes

from database import AsyncSessionLocal
from database.models import Company, BureauContrat, CompanyEmployee
from handlers.company import _fmt, _add_log

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"


async def _call_gemini_bureau(prompt: str) -> str | None:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return None
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 600, "temperature": 1.1, "topP": 0.95},
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


# Objectifs de commandes et récompenses selon la trésorerie
def _get_contract_params(treasury: int) -> list[dict]:
    """Génère 3 niveaux de contrats selon la trésorerie."""
    if treasury < 500_000_000:  # < 500M
        return [
            {"cmds": random.randint(300,  500),  "rate": 0.08, "days": 7},
            {"cmds": random.randint(600,  900),  "rate": 0.12, "days": 14},
            {"cmds": random.randint(900, 1400),  "rate": 0.18, "days": 21},
        ]
    elif treasury < 5_000_000_000:  # < 5B
        return [
            {"cmds": random.randint(1000, 1500), "rate": 0.05, "days": 10},
            {"cmds": random.randint(2000, 3000), "rate": 0.09, "days": 20},
            {"cmds": random.randint(3500, 5000), "rate": 0.14, "days": 30},
        ]
    else:  # 5B+
        return [
            {"cmds": random.randint(3000,  5000), "rate": 0.03, "days": 15},
            {"cmds": random.randint(6000,  9000), "rate": 0.06, "days": 21},
            {"cmds": random.randint(10000,15000), "rate": 0.10, "days": 30},
        ]


# ─── TEMPLATES DE CONTRATS PAR SECTEUR ───────────────────────────────────────
CONTRATS_TEMPLATES = {
    "tech": [
        ("Développement d'application mobile", "Créer une app de gestion interne pour un client corporate."),
        ("Audit de sécurité informatique", "Analyser et sécuriser l'infrastructure d'une PME locale."),
        ("Migration cloud", "Migrer les serveurs d'un cabinet comptable vers le cloud."),
        ("Maintenance système", "Assurer la maintenance mensuelle des serveurs d'une banque régionale."),
        ("Intégration ERP", "Intégrer un logiciel ERP dans une entreprise de distribution."),
    ],
    "finance": [
        ("Gestion de portefeuille", "Gérer les investissements d'un fond de pension sur la durée du contrat."),
        ("Audit financier", "Réaliser l'audit annuel des comptes d'une société immobilière."),
        ("Conseil en fusion-acquisition", "Accompagner une PME dans sa stratégie de rachat."),
        ("Optimisation fiscale", "Proposer un plan d'optimisation pour un groupe familial."),
        ("Levée de fonds", "Accompagner une startup dans sa série A."),
    ],
    "commerce": [
        ("Approvisionnement en gros", "Fournir une chaîne de supermarchés en produits locaux."),
        ("Distribution logistique", "Gérer la distribution de marchandises dans la région."),
        ("Partenariat commercial", "Assurer l'exclusivité de vente d'une marque nationale."),
        ("Gestion de stock", "Optimiser la chaîne d'approvisionnement d'un grossiste."),
        ("Export international", "Exporter des produits locaux vers un marché étranger."),
    ],
    "industrie": [
        ("Sous-traitance de production", "Produire des pièces détachées pour un constructeur automobile."),
        ("Maintenance industrielle", "Assurer la maintenance préventive d'une usine agroalimentaire."),
        ("Fourniture de matériaux", "Livrer des matériaux de construction pour un grand chantier."),
        ("Conception mécanique", "Concevoir un prototype pour une entreprise d'ingénierie."),
        ("Contrôle qualité", "Mettre en place un système de contrôle qualité ISO 9001."),
    ],
    "sante": [
        ("Fourniture médicale", "Approvisionner une clinique en matériel médical sur contrat annuel."),
        ("Télémédecine", "Déployer une plateforme de consultation à distance pour un réseau de santé."),
        ("Formation du personnel", "Former les équipes soignantes d'un hôpital régional."),
        ("Gestion de données médicales", "Sécuriser et gérer les dossiers patients d'une clinique."),
        ("Conseil en santé publique", "Accompagner une municipalité dans sa politique de santé."),
    ],
    "immobilier": [
        ("Promotion immobilière", "Développer un projet résidentiel de 20 logements."),
        ("Gestion locative", "Gérer un parc immobilier de bureaux pour un investisseur."),
        ("Rénovation de bâtiment", "Rénover un immeuble commercial en centre-ville."),
        ("Expertise immobilière", "Évaluer un portefeuille immobilier pour une banque."),
        ("Aménagement commercial", "Aménager un espace de coworking pour un opérateur national."),
    ],
}
DEFAULT_SECTOR = "commerce"

CLIENTS = [
    "Groupe Meridian", "Holdings Apex", "TerraVast Corp", "Nexus Partners",
    "Alliance Stratégique", "Consortium Delta", "Fonds Olympia", "Société Générale du Sud",
    "Groupe Lumière", "Cabinet Aurum", "TechnoSphere Ltd", "Réseau Étoile",
    "Consortium Atlantis", "Groupe Phoenix", "FutureBuild SA",
]


async def _get_employee_total_cmds(session, company_id: int) -> int:
    """Somme des command_count de tous les employés actifs."""
    result = await session.execute(
        select(func.sum(CompanyEmployee.command_count)).where(
            CompanyEmployee.company_id == company_id,
            CompanyEmployee.left_at == None,
        )
    )
    return result.scalar() or 0


def _generate_contracts_fallback(company: Company, employees: int) -> list[dict]:
    """Fallback : génère 3 contrats via templates statiques."""
    sector = company.sector if company.sector in CONTRATS_TEMPLATES else DEFAULT_SECTOR
    templates = CONTRATS_TEMPLATES[sector]
    selected = random.sample(templates, min(3, len(templates)))

    params = _get_contract_params(company.treasury)
    contracts = []

    for i, (title, desc) in enumerate(selected):
        client = random.choice(CLIENTS)
        p = params[i]
        reward = int(company.treasury * p["rate"])
        reward = max(reward, 1_000_000)

        if employees >= 5:
            reward = int(reward * 1.1)
        if employees >= 10:
            reward = int(reward * 1.2)

        contracts.append({
            "title": title,
            "description": f"{desc} Client : <b>{client}</b>.",
            "reward": reward,
            "duration_days": p["days"],
            "objective_cmds": p["cmds"],
        })

    return contracts


async def _generate_contracts(company: Company, employees: int) -> list[dict]:
    """Génère 3 propositions de contrats via Gemini, avec fallback sur les templates."""
    params = _get_contract_params(company.treasury)
    sector = company.sector or DEFAULT_SECTOR

    # Bonus employés sur la récompense
    def _apply_bonus(reward: int) -> int:
        if employees >= 10:
            reward = int(reward * 1.2)
        elif employees >= 5:
            reward = int(reward * 1.1)
        return reward

    # Niveaux des 3 contrats : facile / moyen / ambitieux
    LEVEL_LABELS = ["standard", "intermédiaire", "ambitieux et prestigieux"]

    prompt = (
        f"Tu es un générateur de contrats professionnels pour un jeu économique en français.\n"
        f"Secteur de l'entreprise : {sector}\n"
        f"Génère exactement 3 contrats fictifs et réalistes de niveaux croissants "
        f"({', '.join(LEVEL_LABELS)}).\n"
        f"Récompenses approximatives : "
        f"{_fmt(max(1_000_000, int(company.treasury * params[0]['rate'])))} $, "
        f"{_fmt(max(1_000_000, int(company.treasury * params[1]['rate'])))} $, "
        f"{_fmt(max(1_000_000, int(company.treasury * params[2]['rate'])))} $.\n"
        f"Objectifs de commandes d'équipe : "
        f"{params[0]['cmds']}, {params[1]['cmds']}, {params[2]['cmds']}.\n\n"
        f"Réponds UNIQUEMENT en JSON valide (sans markdown) avec un tableau de 3 objets :\n"
        f'[{{"client":"...","mission":"...","detail":"..."}}, ...]\n'
        f"- client : nom d'entreprise fictif et crédible (court)\n"
        f"- mission : titre court de la mission (max 60 chars)\n"
        f"- detail : description immersive de la mission (max 200 chars)"
    )

    raw = await _call_gemini_bureau(prompt)

    contracts = []
    if raw:
        try:
            clean = raw.strip()
            if clean.startswith("```"):
                clean = clean.split("```")[1]
                if clean.startswith("json"):
                    clean = clean[4:]
            data = json.loads(clean.strip())
            if isinstance(data, list) and len(data) == 3:
                for i, item in enumerate(data):
                    p = params[i]
                    reward = _apply_bonus(max(1_000_000, int(company.treasury * p["rate"])))
                    contracts.append({
                        "title": item.get("mission", "Mission sans titre"),
                        "description": f"{item.get('detail', '')} Client : <b>{item.get('client', 'Client inconnu')}</b>.",
                        "reward": reward,
                        "duration_days": p["days"],
                        "objective_cmds": p["cmds"],
                    })
        except Exception:
            pass

    # Fallback si Gemini a échoué ou JSON invalide
    if len(contracts) != 3:
        contracts = _generate_contracts_fallback(company, employees)

    return contracts


# ─── COMMANDE : /soumettredossier ─────────────────────────────────────────────
async def soumettredossier_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        company = (await session.execute(
            select(Company).where(
                Company.owner_id == user.id,
                Company.is_active == True,
                Company.is_bot_company == False,
            )
        )).scalar_one_or_none()

        if not company:
            await update.message.reply_text("❌ Tu n'es PDG d'aucune entreprise.")
            return

        if company.treasury_frozen:
            await update.message.reply_text(
                "🔒 Ta trésorerie est gelée par l'Agence Fiscale.\n"
                "Paie tes impôts avec <code>/payerimpots [montant]</code> pour accéder au Bureau des Contrats.",
                parse_mode="HTML"
            )
            return

        # Vérifier contrats actifs (max 2)
        active_count = (await session.execute(
            select(func.count()).where(
                BureauContrat.company_id == company.id,
                BureauContrat.status == "active",
            )
        )).scalar() or 0

        if active_count >= 2:
            await update.message.reply_text(
                "❌ Tu as déjà <b>2 contrats actifs</b>.\n"
                "Utilise <code>/mescontratsbc</code> pour suivre leur avancement.",
                parse_mode="HTML"
            )
            return

        # Compter les employés
        employees = (await session.execute(
            select(func.count()).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.left_at == None,
                CompanyEmployee.role != "pdg",
            )
        )).scalar() or 0

        # Générer 3 contrats
        proposals = await _generate_contracts(company, employees)

        # Supprimer anciennes propositions en attente
        old_pending = (await session.execute(
            select(BureauContrat).where(
                BureauContrat.company_id == company.id,
                BureauContrat.status == "pending",
            )
        )).scalars().all()
        for old in old_pending:
            await session.delete(old)
        await session.flush()

        saved_ids = []
        for p in proposals:
            record = BureauContrat(
                company_id=company.id,
                title=p["title"],
                description=p["description"],
                reward=p["reward"],
                duration_days=p["duration_days"],
                objective_cmds=p["objective_cmds"],
                status="pending",
            )
            session.add(record)
            await session.flush()
            saved_ids.append(record.id)

        await session.commit()

        sector_label = company.sector.capitalize()
        msg = (
            f"📋 <b>BUREAU DES CONTRATS</b>\n"
            f"🏢 <b>{company.name}</b> · Secteur : {sector_label}\n"
            f"💰 Trésorerie : <b>{_fmt(company.treasury)} $</b> · 👷 Employés : <b>{employees}</b>\n\n"
            f"Voici <b>3 contrats</b> adaptés à votre profil :\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        )

        for i, (p, cid) in enumerate(zip(proposals, saved_ids), 1):
            msg += (
                f"<b>Contrat {i}</b> — {p['title']}\n"
                f"📝 {p['description']}\n"
                f"🎯 Objectif : <b>{p['objective_cmds']:,} commandes d'équipe</b>\n"
                f"⏳ Délai : <b>{p['duration_days']} jours</b>\n"
                f"💵 Récompense : <b>{_fmt(p['reward'])} $</b>\n"
                f"✅ <code>/choisircontrat {cid}</code>\n\n"
            )

        msg += "💡 Tu as <b>24h</b> pour choisir avant expiration des offres."
        await update.message.reply_text(msg, parse_mode="HTML")


# ─── COMMANDE : /choisircontrat [id] ─────────────────────────────────────────
async def choisircontrat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("❌ Usage : <code>/choisircontrat [numéro]</code>", parse_mode="HTML")
        return

    try:
        contract_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Numéro invalide.")
        return

    async with AsyncSessionLocal() as session:
        company = (await session.execute(
            select(Company).where(
                Company.owner_id == user.id,
                Company.is_active == True,
            )
        )).scalar_one_or_none()

        if not company:
            await update.message.reply_text("❌ Tu n'es PDG d'aucune entreprise.")
            return

        contract = await session.get(BureauContrat, contract_id)
        if not contract or contract.company_id != company.id or contract.status != "pending":
            await update.message.reply_text("❌ Contrat introuvable ou déjà expiré.")
            return

        active_count = (await session.execute(
            select(func.count()).where(
                BureauContrat.company_id == company.id,
                BureauContrat.status == "active",
            )
        )).scalar() or 0

        if active_count >= 2:
            await update.message.reply_text("❌ Tu as déjà 2 contrats actifs.")
            return

        # Activer le contrat — snapshot commandes actuelles
        now = datetime.utcnow()
        contract.status = "active"
        contract.starts_at = now
        contract.ends_at = now + timedelta(days=contract.duration_days)
        contract.cmds_at_start = await _get_employee_total_cmds(session, company.id)

        # Supprimer autres propositions en attente
        other_pending = (await session.execute(
            select(BureauContrat).where(
                BureauContrat.company_id == company.id,
                BureauContrat.status == "pending",
                BureauContrat.id != contract_id,
            )
        )).scalars().all()
        for old in other_pending:
            await session.delete(old)

        await _add_log(session, company.id, "contrat_bureau",
                       f"Contrat '{contract.title}' accepté — objectif {contract.objective_cmds:,} cmds · {_fmt(contract.reward)} $ à l'échéance")
        await session.commit()

        # Notifier tous les employés
        emps = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.left_at == None,
                CompanyEmployee.role != "pdg",
            )
        )).scalars().all()

        deadline_str = contract.ends_at.strftime("%d/%m à %H:%M UTC")
        for emp in emps:
            try:
                await context.bot.send_message(
                    chat_id=emp.user_id,
                    text=(
                        f"📋 <b>Nouveau contrat Bureau pour {company.name} !</b>\n\n"
                        f"📌 <b>Mission :</b> {contract.title}\n\n"
                        f"🎯 Objectif équipe : <b>{contract.objective_cmds:,} commandes</b>\n"
                        f"⏰ Deadline : <b>{deadline_str}</b>\n"
                        f"💰 Récompense : <b>{_fmt(contract.reward)} $</b>\n\n"
                        f"💪 Soyez actifs sur le bot pour atteindre l'objectif !"
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass

        await update.message.reply_text(
            f"✅ <b>Contrat accepté !</b>\n\n"
            f"📋 <b>{contract.title}</b>\n"
            f"🎯 Objectif : <b>{contract.objective_cmds:,} commandes d'équipe</b>\n"
            f"⏰ Deadline : <b>{deadline_str}</b>\n"
            f"💵 Récompense : <b>{_fmt(contract.reward)} $</b>\n\n"
            f"Toute l'équipe a été notifiée. Suivez la progression avec <code>/mescontratsbc</code>",
            parse_mode="HTML"
        )


# ─── COMMANDE : /mescontratsbc ────────────────────────────────────────────────
async def mescontratsbc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        company = (await session.execute(
            select(Company).where(
                Company.owner_id == user.id,
                Company.is_active == True,
            )
        )).scalar_one_or_none()

        if not company:
            await update.message.reply_text("❌ Tu n'es PDG d'aucune entreprise.")
            return

        contracts = (await session.execute(
            select(BureauContrat).where(
                BureauContrat.company_id == company.id,
                BureauContrat.status.in_(["active", "completed", "failed"]),
            ).order_by(BureauContrat.created_at.desc()).limit(10)
        )).scalars().all()

        if not contracts:
            await update.message.reply_text(
                f"📋 <b>{company.name}</b> n'a aucun contrat en cours.\n"
                f"Soumets un dossier avec <code>/soumettredossier</code>",
                parse_mode="HTML"
            )
            return

        now = datetime.utcnow()
        msg = f"📋 <b>CONTRATS BUREAU — {company.name}</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"

        for c in contracts:
            if c.status == "active":
                remaining = c.ends_at - now if c.ends_at else timedelta(0)
                days = remaining.days
                hours = remaining.seconds // 3600

                # Progression commandes
                total_now = await _get_employee_total_cmds(session, company.id)
                done = max(0, total_now - (c.cmds_at_start or 0))
                obj = c.objective_cmds or 1
                pct = min(100, int(done / obj * 100))
                bar = "█" * (pct // 10) + "░" * (10 - pct // 10)

                status_str = (
                    f"🟢 En cours — {days}j {hours}h restants\n"
                    f"   [{bar}] {done:,}/{obj:,} cmds ({pct}%)"
                )
            elif c.status == "completed":
                status_str = "✅ Terminé"
            else:
                status_str = "❌ Échoué"

            msg += (
                f"<b>{c.title}</b>\n"
                f"💵 {_fmt(c.reward)} $ · 🎯 {c.objective_cmds:,} cmds · ⏳ {c.duration_days}j\n"
                f"{status_str}\n\n"
            )

        await update.message.reply_text(msg, parse_mode="HTML")


# ─── COMMANDE : /claimcontratbc ──────────────────────────────────────────────
async def claimcontratbc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /claimcontratbc — Réclame immédiatement la récompense d'un contrat Bureau
    si l'objectif est atteint, sans attendre la deadline.
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
            await update.message.reply_text("❌ Tu n'es PDG d'aucune entreprise.")
            return

        active_contracts = (await session.execute(
            select(BureauContrat).where(
                BureauContrat.company_id == company.id,
                BureauContrat.status == "active",
            )
        )).scalars().all()

        if not active_contracts:
            await update.message.reply_text(
                "📭 Aucun contrat Bureau actif en cours.\n"
                "Soumets un dossier avec <code>/soumettredossier</code>",
                parse_mode="HTML"
            )
            return

        total_cmds_now = await _get_employee_total_cmds(session, company.id)
        now = datetime.utcnow()
        claimed_any = False
        messages = []

        for contract in active_contracts:
            cmds_done = max(0, total_cmds_now - (contract.cmds_at_start or 0))
            obj = contract.objective_cmds or 1

            if cmds_done >= obj:
                # Objectif atteint → crédit immédiat
                contract.status = "completed"
                company.treasury += contract.reward
                company.value = company.treasury

                await _add_log(session, company.id, "contrat_bureau",
                               f"Contrat '{contract.title}' réclamé manuellement — +{_fmt(contract.reward)} $",
                               amount=contract.reward)

                time_saved = ""
                if contract.ends_at and now < contract.ends_at:
                    diff = contract.ends_at - now
                    h = int(diff.total_seconds() // 3600)
                    m = int((diff.total_seconds() % 3600) // 60)
                    time_saved = f"⚡ Réclamé <b>{h}h{m:02d}</b> avant la deadline !\n\n"

                messages.append(
                    f"🎉 <b>{contract.title}</b>\n"
                    f"✅ {cmds_done:,} / {obj:,} commandes\n"
                    f"{time_saved}"
                    f"💰 <b>+{_fmt(contract.reward)} $</b> crédités en trésorerie"
                )
                claimed_any = True
            else:
                pct = min(100, int(cmds_done / obj * 100))
                bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                remaining = obj - cmds_done
                messages.append(
                    f"⏳ <b>{contract.title}</b>\n"
                    f"[{bar}] {cmds_done:,}/{obj:,} cmds ({pct}%)\n"
                    f"Il manque encore <b>{remaining:,} commandes</b>"
                )

        await session.commit()

        header = "📋 <b>BUREAU DES CONTRATS — Réclamation</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        if claimed_any:
            footer = f"\n\n🏦 Trésorerie : <b>{_fmt(company.treasury)} $</b>"
        else:
            footer = "\n\n💡 Aucun contrat n'est encore complété."

        await update.message.reply_text(
            header + "\n\n".join(messages) + footer,
            parse_mode="HTML"
        )


# ─── JOB : vérifier les contrats terminés ────────────────────────────────────
async def bureau_check_job(context: ContextTypes.DEFAULT_TYPE):
    """Vérifie chaque heure si les contrats bureau sont complétés ou expirés."""
    async with AsyncSessionLocal() as session:
        now = datetime.utcnow()

        active_contracts = (await session.execute(
            select(BureauContrat).where(BureauContrat.status == "active")
        )).scalars().all()

        for contract in active_contracts:
            company = await session.get(Company, contract.company_id)
            if not company or not company.is_active:
                contract.status = "failed"
                continue

            total_now = await _get_employee_total_cmds(session, contract.company_id)
            cmds_done = max(0, total_now - (contract.cmds_at_start or 0))
            obj = contract.objective_cmds or 1

            # Contrat réussi — objectif atteint
            if cmds_done >= obj:
                contract.status = "completed"
                company.treasury += contract.reward
                company.value = company.treasury

                await _add_log(session, company.id, "contrat_bureau",
                               f"Contrat '{contract.title}' terminé — +{_fmt(contract.reward)} $",
                               amount=contract.reward)

                pdg_emp = (await session.execute(
                    select(CompanyEmployee).where(
                        CompanyEmployee.company_id == company.id,
                        CompanyEmployee.role == "pdg",
                        CompanyEmployee.left_at == None,
                    )
                )).scalar_one_or_none()

                if pdg_emp:
                    try:
                        await context.bot.send_message(
                            chat_id=pdg_emp.user_id,
                            text=(
                                f"🎉 <b>Contrat Bureau accompli !</b>\n\n"
                                f"🏢 <b>{company.name}</b>\n"
                                f"📄 <b>{contract.title}</b>\n"
                                f"✅ Objectif atteint : <b>{cmds_done:,} / {obj:,} commandes</b>\n"
                                f"💰 <b>+{_fmt(contract.reward)} $</b> versés en trésorerie !\n\n"
                                f"🏦 Trésorerie : <b>{_fmt(company.treasury)} $</b>\n\n"
                                f"Tu peux soumettre un nouveau dossier : <code>/soumettredossier</code>"
                            ),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

            # Contrat expiré — délai dépassé sans objectif atteint
            elif contract.ends_at and now > contract.ends_at:
                contract.status = "failed"

                pdg_emp = (await session.execute(
                    select(CompanyEmployee).where(
                        CompanyEmployee.company_id == company.id,
                        CompanyEmployee.role == "pdg",
                        CompanyEmployee.left_at == None,
                    )
                )).scalar_one_or_none()

                if pdg_emp:
                    try:
                        await context.bot.send_message(
                            chat_id=pdg_emp.user_id,
                            text=(
                                f"⚠️ <b>Contrat Bureau échoué !</b>\n\n"
                                f"🏢 <b>{company.name}</b>\n"
                                f"📄 <b>{contract.title}</b>\n"
                                f"❌ Objectif non atteint : <b>{cmds_done:,} / {obj:,} commandes</b>\n"
                                f"Aucune récompense versée.\n\n"
                                f"Soumets un nouveau dossier : <code>/soumettredossier</code>"
                            ),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

        await session.commit()
