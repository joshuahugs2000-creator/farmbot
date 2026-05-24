"""
📋 Bureau des Contrats — contrats B2B générés selon le profil de l'entreprise
"""
import random
from datetime import datetime, timedelta

from sqlalchemy import select, func
from telegram import Update
from telegram.ext import ContextTypes

from database import AsyncSessionLocal
from database.models import Company, BureauContrat, CompanyEmployee
from handlers.company import _fmt, _add_log

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

# Secteur par défaut si non reconnu
DEFAULT_SECTOR = "commerce"

# Clients fictifs
CLIENTS = [
    "Groupe Meridian", "Holdings Apex", "TerraVast Corp", "Nexus Partners",
    "Alliance Stratégique", "Consortium Delta", "Fonds Olympia", "Société Générale du Sud",
    "Groupe Lumière", "Cabinet Aurum", "TechnoSphere Ltd", "Réseau Étoile",
    "Consortium Atlantis", "Groupe Phoenix", "FutureBuild SA",
]


def _generate_contracts(company: Company, employees: int) -> list[dict]:
    """Génère 3 propositions de contrats adaptées au profil de l'entreprise."""
    sector = company.sector if company.sector in CONTRATS_TEMPLATES else DEFAULT_SECTOR
    templates = CONTRATS_TEMPLATES[sector]
    selected = random.sample(templates, min(3, len(templates)))

    treasury = company.treasury
    contracts = []

    # Durées et taux de rendement selon la taille
    if treasury < 500_000_000:          # < 500M
        durations = [7, 14, 21]
        rates = [0.08, 0.12, 0.18]     # 8%, 12%, 18% de la tréso
    elif treasury < 5_000_000_000:      # < 5B
        durations = [10, 20, 30]
        rates = [0.05, 0.09, 0.14]
    else:                               # 5B+
        durations = [15, 21, 30]
        rates = [0.03, 0.06, 0.10]

    for i, (title, desc) in enumerate(selected):
        client = random.choice(CLIENTS)
        duration = durations[i]
        reward = int(treasury * rates[i])
        reward = max(reward, 1_000_000)  # minimum 1M

        # Bonus employés
        if employees >= 5:
            reward = int(reward * 1.1)
        if employees >= 10:
            reward = int(reward * 1.2)

        contracts.append({
            "title": title,
            "description": f"{desc} Client : <b>{client}</b>.",
            "reward": reward,
            "duration_days": duration,
        })

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
        proposals = _generate_contracts(company, employees)

        # Sauvegarder les propositions temporairement
        # On supprime d'abord les anciennes propositions en attente
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
                status="pending",
            )
            session.add(record)
            await session.flush()
            saved_ids.append(record.id)

        await session.commit()

        # Afficher les 3 propositions
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
                f"💵 Récompense : <b>{_fmt(p['reward'])} $</b>\n"
                f"⏳ Durée : <b>{p['duration_days']} jours</b>\n"
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

        # Vérifier limite 2 contrats actifs
        active_count = (await session.execute(
            select(func.count()).where(
                BureauContrat.company_id == company.id,
                BureauContrat.status == "active",
            )
        )).scalar() or 0

        if active_count >= 2:
            await update.message.reply_text("❌ Tu as déjà 2 contrats actifs.")
            return

        # Activer le contrat
        now = datetime.utcnow()
        contract.status = "active"
        contract.starts_at = now
        contract.ends_at = now + timedelta(days=contract.duration_days)

        # Supprimer les autres propositions en attente
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
                       f"Contrat '{contract.title}' accepté — {_fmt(contract.reward)} $ à l'échéance")
        await session.commit()

        await update.message.reply_text(
            f"✅ <b>Contrat accepté !</b>\n\n"
            f"📋 <b>{contract.title}</b>\n"
            f"💵 Récompense : <b>{_fmt(contract.reward)} $</b>\n"
            f"📅 Fin le : <b>{contract.ends_at.strftime('%d/%m/%Y à %Hh%M')}</b>\n\n"
            f"La récompense sera versée automatiquement dans la trésorerie à l'échéance.\n"
            f"📊 Suivi : <code>/mescontratsbc</code>",
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
        msg = f"📋 <b>CONTRATS — {company.name}</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"

        for c in contracts:
            if c.status == "active":
                remaining = c.ends_at - now if c.ends_at else timedelta(0)
                days = remaining.days
                hours = remaining.seconds // 3600
                status_str = f"🟢 En cours — {days}j {hours}h restants"
            elif c.status == "completed":
                status_str = "✅ Terminé"
            else:
                status_str = "❌ Échoué"

            msg += (
                f"<b>{c.title}</b>\n"
                f"💵 {_fmt(c.reward)} $ · ⏳ {c.duration_days}j\n"
                f"{status_str}\n\n"
            )

        await update.message.reply_text(msg, parse_mode="HTML")


# ─── JOB : vérifier les contrats terminés ────────────────────────────────────
async def bureau_check_job(context: ContextTypes.DEFAULT_TYPE):
    """Verse les récompenses des contrats terminés dans la trésorerie."""
    async with AsyncSessionLocal() as session:
        now = datetime.utcnow()

        completed = (await session.execute(
            select(BureauContrat).where(
                BureauContrat.status == "active",
                BureauContrat.ends_at <= now,
            )
        )).scalars().all()

        for contract in completed:
            company = await session.get(Company, contract.company_id)
            if not company or not company.is_active:
                contract.status = "failed"
                continue

            # Verser la récompense dans la trésorerie
            company.treasury += contract.reward
            company.value = company.treasury
            contract.status = "completed"

            await _add_log(session, company.id, "contrat_bureau",
                           f"Contrat '{contract.title}' terminé — +{_fmt(contract.reward)} $",
                           amount=contract.reward)

            # Notifier le PDG
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
                            f"📋 <b>Contrat terminé !</b>\n\n"
                            f"🏢 <b>{company.name}</b>\n"
                            f"📄 <b>{contract.title}</b>\n"
                            f"💰 <b>+{_fmt(contract.reward)} $</b> versés en trésorerie !\n\n"
                            f"🏦 Trésorerie : <b>{_fmt(company.treasury)} $</b>\n\n"
                            f"Tu peux soumettre un nouveau dossier : <code>/soumettredossier</code>"
                        ),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

        await session.commit()
