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

from sqlalchemy import select, func, text
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
    """Somme des command_count de TOUS les employés (actifs + anciens).
    On inclut les anciens pour éviter que les départs ne fassent régresser la barre de progression.
    """
    result = await session.execute(
        select(func.sum(CompanyEmployee.command_count)).where(
            CompanyEmployee.company_id == company_id,
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


async def _generate_one_contract(company: Company, employees: int, level_index: int) -> dict:
    """Génère UN contrat via Gemini (même approche que company_contracts.py).
    level_index : 0=standard, 1=intermédiaire, 2=ambitieux
    """
    params = _get_contract_params(company.treasury)
    sector = company.sector or DEFAULT_SECTOR
    p = params[level_index]

    # Bonus employés
    reward = max(1_000_000, int(company.treasury * p["rate"]))
    if employees >= 10:
        reward = int(reward * 1.2)
    elif employees >= 5:
        reward = int(reward * 1.1)

    level_label = ["standard", "intermédiaire", "ambitieux et prestigieux"][level_index]

    # Templates fallback
    templates_sector = CONTRATS_TEMPLATES.get(sector, CONTRATS_TEMPLATES[DEFAULT_SECTOR])
    fb_title, fb_desc = random.choice(templates_sector)
    fb_client = random.choice(CLIENTS)

    prompt = (
        f"Tu es un générateur de contrats professionnels pour un jeu économique en français.\n"
        f"Secteur de l'entreprise : {sector}\n"
        f"Génère UN contrat fictif et réaliste de niveau {level_label} avec ces contraintes :\n"
        f"- Objectif : {p['cmds']} commandes d'équipe en {p['days']} jours\n"
        f"- Récompense : {_fmt(reward)} $\n"
        + (f"- CONTRAT AMBITIEUX : rends la mission particulièrement prestigieuse et exigeante.\n" if level_index == 2 else "")
        + f"\nRéponds UNIQUEMENT en JSON valide (sans markdown) avec ces champs :\n"
        f"{{\"client\": \"...\", \"mission\": \"...\", \"detail\": \"...\"}}\n"
        f"- client : nom du client fictif (court, réaliste)\n"
        f"- mission : titre court de la mission (max 60 chars)\n"
        f"- detail : description courte et immersive (max 200 chars)"
    )

    raw = await _call_gemini_bureau(prompt)

    # Fallback immédiat si Gemini ne répond pas
    if not raw:
        return {
            "title": fb_title,
            "description": f"{fb_desc} Client : <b>{fb_client}</b>.",
            "reward": reward,
            "duration_days": p["days"],
            "objective_cmds": p["cmds"],
        }

    try:
        raw_clean = raw.strip().strip("```json").strip("```").strip()
        data = json.loads(raw_clean)
        return {
            "title": data.get("mission", fb_title),
            "description": f"{data.get('detail', fb_desc)} Client : <b>{data.get('client', fb_client)}</b>.",
            "reward": reward,
            "duration_days": p["days"],
            "objective_cmds": p["cmds"],
        }
    except Exception:
        return {
            "title": fb_title,
            "description": f"{fb_desc} Client : <b>{fb_client}</b>.",
            "reward": reward,
            "duration_days": p["days"],
            "objective_cmds": p["cmds"],
        }


async def _generate_contracts(company: Company, employees: int) -> list[dict]:
    """Génère 3 propositions de contrats via Gemini (une par une, comme company_contracts.py)."""
    import asyncio
    contracts = await asyncio.gather(
        _generate_one_contract(company, employees, 0),
        _generate_one_contract(company, employees, 1),
        _generate_one_contract(company, employees, 2),
    )
    return list(contracts)


# ─── COMMANDE : /soumettredossier ─────────────────────────────────────────────
async def soumettredossier_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        from database.db import get_main_company
        company = await get_main_company(session, user.id)

        if not company or company.is_bot_company:
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

        # Supprimer anciennes propositions en attente (SQL pur)
        await session.execute(
            text("DELETE FROM bureau_contrats WHERE company_id=:cid AND status='pending'"),
            {"cid": company.id}
        )

        # Insérer les 3 nouveaux contrats en SQL pur (séquence Neon cassée via ORM)
        saved_ids = []
        for p in proposals:
            row = (await session.execute(
                text(
                    "INSERT INTO bureau_contrats "
                    "(company_id, title, description, reward, duration_days, objective_cmds, status, created_at) "
                    "VALUES (:cid, :title, :desc, :reward, :days, :obj, 'pending', NOW()) "
                    "RETURNING id"
                ),
                {
                    "cid": company.id,
                    "title": p["title"],
                    "desc": p["description"],
                    "reward": p["reward"],
                    "days": p["duration_days"],
                    "obj": p["objective_cmds"],
                }
            )).fetchone()
            saved_ids.append(row[0])

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

    # ── Phase 1 : lecture seule ───────────────────────────────────────────────
    async with AsyncSessionLocal() as session:
        from database.db import get_main_company
        comp_row = await get_main_company(session, user.id)
        if not comp_row:
            await update.message.reply_text("❌ Tu n'es PDG d'aucune entreprise.")
            return
        company_id = int(comp_row.id)
        company_name = comp_row.name

        contract_row = (await session.execute(
            text("SELECT id, company_id, status, title, objective_cmds, reward, duration_days "
                 "FROM bureau_contrats WHERE id=:cid LIMIT 1"),
            {"cid": contract_id}
        )).fetchone()
        if not contract_row or int(contract_row[1]) != company_id or contract_row[2] != "pending":
            await update.message.reply_text("❌ Contrat introuvable ou déjà expiré.")
            return

        c_title = contract_row[3]
        c_obj   = int(contract_row[4] or 0)
        c_reward = int(contract_row[5] or 0)
        c_days  = int(contract_row[6] or 7)

        active_count = (await session.execute(
            text("SELECT COUNT(*) FROM bureau_contrats WHERE company_id=:cid AND status='active'"),
            {"cid": company_id}
        )).scalar() or 0
        if active_count >= 2:
            await update.message.reply_text("❌ Tu as déjà 2 contrats actifs.")
            return

        cmds_at_start = (await session.execute(
            select(func.sum(CompanyEmployee.command_count)).where(
                CompanyEmployee.company_id == company_id
            )
        )).scalar() or 0

        emp_rows = (await session.execute(
            text("SELECT user_id FROM company_employees "
                 "WHERE company_id=:cid AND left_at IS NULL AND role!='pdg'"),
            {"cid": company_id}
        )).fetchall()

    # ── Phase 2 : écriture atomique ───────────────────────────────────────────
    now = datetime.utcnow()
    ends_at = now + timedelta(days=c_days)
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("UPDATE bureau_contrats SET status='active', starts_at=:now, ends_at=:end, "
                 "cmds_at_start=:cs WHERE id=:cid"),
            {"now": now, "end": ends_at, "cs": cmds_at_start, "cid": contract_id}
        )
        await session.execute(
            text("DELETE FROM bureau_contrats WHERE company_id=:cid AND status='pending' AND id!=:keep"),
            {"cid": company_id, "keep": contract_id}
        )
        try:
            await session.execute(
                text("INSERT INTO company_logs (company_id, event_type, description, created_at) "
                     "VALUES (:cid, 'contrat_bureau', :desc, NOW())"),
                {"cid": company_id,
                 "desc": f"Contrat '{c_title[:80]}' accepté — objectif {c_obj:,} cmds · {_fmt(c_reward)} $ à l'échéance"}
            )
        except Exception:
            pass
        await session.commit()

    deadline_str = ends_at.strftime("%d/%m à %H:%M UTC")

    # ── Phase 3 : notifications employés ─────────────────────────────────────
    for emp_row in emp_rows:
        try:
            await context.bot.send_message(
                chat_id=emp_row[0],
                text=(
                    f"📋 <b>Nouveau contrat Bureau pour {company_name} !</b>\n\n"
                    f"📌 <b>Mission :</b> {c_title}\n\n"
                    f"🎯 Objectif équipe : <b>{c_obj:,} commandes</b>\n"
                    f"⏰ Deadline : <b>{deadline_str}</b>\n"
                    f"💰 Récompense : <b>{_fmt(c_reward)} $</b>\n\n"
                    f"💪 Soyez actifs sur le bot pour atteindre l'objectif !"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

    # ── Réponse au PDG ────────────────────────────────────────────────────────
    await update.message.reply_text(
        f"✅ <b>Contrat accepté !</b>\n\n"
        f"📋 <b>{c_title}</b>\n"
        f"🎯 Objectif : <b>{c_obj:,} commandes d'équipe</b>\n"
        f"⏰ Deadline : <b>{deadline_str}</b>\n"
        f"💵 Récompense : <b>{_fmt(c_reward)} $</b>\n\n"
        f"Toute l'équipe a été notifiée. Suivez la progression avec <code>/mescontratsbc</code>",
        parse_mode="HTML"
    )
    return


# ─── COMMANDE : /mescontratsbc ────────────────────────────────────────────────
async def mescontratsbc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        from database.db import get_main_company
        company = await get_main_company(session, user.id)

        if not company:
            # Chercher si directeur de filiale
            from database.models import CompanyEmployee
            row = (await session.execute(
                select(CompanyEmployee, Company).join(
                    Company, Company.id == CompanyEmployee.company_id
                ).where(
                    CompanyEmployee.user_id == user.id,
                    CompanyEmployee.role == "directeur",
                    CompanyEmployee.left_at == None,
                    Company.is_active == True,
                )
            )).first()
            if row:
                _, company = row
            else:
                await update.message.reply_text("❌ Tu n'es PDG ni directeur d'aucune entreprise.")
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
                done = int(c.cmds_done or 0)
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
    Tout en SQL pur pour éviter les deadlocks ORM.
    """
    user = update.effective_user
    try:
        now = datetime.utcnow()
        messages = []
        claimed_any = False
        treasury_now = 0

        # ── Phase 1 : lecture seule ───────────────────────────────────────────
        async with AsyncSessionLocal() as s:
            row = (await s.execute(
                text("""
                    SELECT id, treasury FROM companies
                    WHERE (owner_id=:uid OR id IN (
                        SELECT company_id FROM company_employees
                        WHERE user_id=:uid AND role='directeur' AND left_at IS NULL
                    )) AND is_active=TRUE
                    AND id NOT IN (SELECT child_id FROM company_annexes WHERE owner_id=:uid)
                    LIMIT 1
                """),
                {"uid": user.id}
            )).fetchone()
            if not row:
                await update.message.reply_text("❌ Tu n'es PDG ni directeur d'aucune entreprise.")
                return
            company_id = int(row[0])
            treasury_now = int(row[1] or 0)

            contracts_rows = (await s.execute(
                text("SELECT id, title, cmds_done, objective_cmds, reward, ends_at, cmds_at_start "
                     "FROM bureau_contrats WHERE company_id=:cid AND status='active'"),
                {"cid": company_id}
            )).fetchall()

        if not contracts_rows:
            await update.message.reply_text(
                "📭 Aucun contrat Bureau actif en cours.\n"
                "Soumets un dossier avec <code>/soumettredossier</code>",
                parse_mode="HTML"
            )
            return

        # ── Phase 2 : calcul hors DB ──────────────────────────────────────────
        to_claim = []   # (id, title, cmds_done, obj, reward)
        for r in contracts_rows:
            cid, title, cmds_done, obj, reward, ends_at, cmds_at_start = r
            cmds_done     = int(cmds_done or 0)
            cmds_at_start = int(cmds_at_start or 0)
            obj           = int(obj or 1)
            reward        = int(reward or 0)
            progression   = cmds_done - cmds_at_start
            if progression >= obj:
                time_saved = ""
                if ends_at and now < ends_at:
                    diff = ends_at - now
                    h = int(diff.total_seconds() // 3600)
                    m = int((diff.total_seconds() % 3600) // 60)
                    time_saved = f"⚡ Réclamé <b>{h}h{m:02d}</b> avant la deadline !\n\n"
                messages.append(
                    f"🎉 <b>{title}</b>\n"
                    f"✅ {progression:,} / {obj:,} commandes\n"
                    f"{time_saved}"
                    f"💰 <b>+{_fmt(reward)} $</b> crédités en trésorerie"
                )
                to_claim.append((cid, title, cmds_done, obj, reward))
                claimed_any = True
            else:
                pct = min(100, int(progression / obj * 100)) if obj > 0 else 0
                bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                messages.append(
                    f"⏳ <b>{title}</b>\n"
                    f"[{bar}] {progression:,}/{obj:,} cmds ({pct}%)\n"
                    f"Il manque encore <b>{obj - progression:,} commandes</b>"
                )

        # ── Phase 3 : écriture atomique ───────────────────────────────────────
        if to_claim:
            total_reward = sum(r[4] for r in to_claim)
            ids_csv = ",".join(str(r[0]) for r in to_claim)
            async with AsyncSessionLocal() as s:
                await s.execute(
                    text(f"UPDATE bureau_contrats SET status='completed' WHERE id IN ({ids_csv})")
                )
                await s.execute(
                    text("UPDATE companies SET treasury = COALESCE(treasury,0) + :r WHERE id=:cid"),
                    {"r": total_reward, "cid": company_id}
                )
                await s.commit()
                row2 = (await s.execute(
                    text("SELECT treasury FROM companies WHERE id=:cid"),
                    {"cid": company_id}
                )).fetchone()
                treasury_now = int(row2[0] or 0) if row2 else treasury_now + total_reward

            # ── Phase 4 : logs (best-effort) ──────────────────────────────────
            try:
                async with AsyncSessionLocal() as s:
                    for cid, title, _, __, reward in to_claim:
                        await s.execute(
                            text("INSERT INTO company_logs "
                                 "(company_id, event_type, description, amount, created_at) "
                                 "VALUES (:cid, 'contrat_bureau', :desc, :amt, NOW())"),
                            {"cid": company_id,
                             "desc": f"Contrat BC '{title[:80]}' réclamé — +{_fmt(reward)} $",
                             "amt": reward}
                        )
                    await s.commit()
            except Exception:
                pass

        header = "📋 <b>BUREAU DES CONTRATS — Réclamation</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        footer = (f"\n\n🏦 Trésorerie : <b>{_fmt(treasury_now)} $</b>"
                  if claimed_any else "\n\n💡 Aucun contrat n'est encore complété.")
        await update.message.reply_text(
            header + "\n\n".join(messages) + footer,
            parse_mode="HTML"
        )

    except Exception as e:
        import logging as _log
        _log.getLogger(__name__).error(f"claimcontratbc error: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ Erreur lors de la réclamation : {str(e)[:200]}",
            parse_mode=None
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
            cmds_done     = int(contract.cmds_done or 0)
            cmds_at_start = int(contract.cmds_at_start or 0)
            obj           = contract.objective_cmds or 1
            progression   = cmds_done - cmds_at_start

            # Contrat réussi — objectif atteint
            if progression >= obj:
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
                )
                ).limit(1).scalar_one_or_none()

                if pdg_emp:
                    try:
                        await context.bot.send_message(
                            chat_id=pdg_emp.user_id,
                            text=(
                                f"🎉 <b>Contrat Bureau accompli !</b>\n\n"
                                f"🏢 <b>{company.name}</b>\n"
                                f"📄 <b>{contract.title}</b>\n"
                                f"✅ Objectif atteint : <b>{progression:,} / {obj:,} commandes</b>\n"
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
                )
                ).limit(1).scalar_one_or_none()

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
