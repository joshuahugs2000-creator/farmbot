# handlers/company_finance.py
# Système financier réaliste pour FarmBot :
#   - /bilan          → bilan financier complet de l'entreprise
#   - /emprunterboite → contracter un prêt bancaire d'entreprise
#   - /pretboite      → voir le prêt actif et les échéances
#   - /rembourserboite→ remboursement anticipé du prêt
#   - /dividendes     → voir ses dividendes reçus cette semaine
#   - job_company_dividends → job hebdo lundi 9h (distribue 30% du revenu hebdo)
#   - job_loan_check  → job quotidien (vérifie les prêts en défaut)

import logging
from datetime import datetime, timedelta

from sqlalchemy import select, func
from telegram import Update
from telegram.ext import ContextTypes

from database.db import AsyncSessionLocal, get_user
from database.models import (
    Company, CompanyEmployee, CompanyShare, CompanyLoan, CompanyLog, User
)
from handlers.company import (
    _fmt, _get_user_company, _add_log, _level_info, DIRECTION_ROLES, LEVELS
)

logger = logging.getLogger(__name__)

# ─── Taux d'intérêt annuel selon le niveau de l'entreprise ───────────────────
LOAN_RATES = {
    1: 0.18,   # Startup       → 18% / an
    2: 0.14,   # PME           → 14% / an
    3: 0.10,   # Société       → 10% / an
    4: 0.07,   # Grande boîte  → 7%  / an
    5: 0.05,   # Holding       → 5%  / an
}

# Capacité d'emprunt max = X% de la valeur de l'entreprise
LOAN_CAPACITY = {
    1: 0.25,
    2: 0.35,
    3: 0.50,
    4: 0.65,
    5: 0.80,
}

LOAN_DURATION_DAYS = 30   # durée standard d'un prêt


# ─── COMMANDE : /bilan ────────────────────────────────────────────────────────

async def bilan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le bilan financier complet de l'entreprise."""
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company:
            await update.message.reply_text("❌ Tu n'appartiens à aucune entreprise.")
            return

        level_name, _, _, monthly_rate, _ = _level_info(company.level)

        # ── Revenus bruts journaliers ─────────────────────────────────────
        gross_daily = int(company.value * monthly_rate) // 30
        legal_daily = int(gross_daily * 0.10)
        net_daily   = gross_daily - legal_daily

        # ── Charges salariales estimées ───────────────────────────────────
        emps = (await session.execute(
            select(CompanyEmployee).where(
                CompanyEmployee.company_id == company.id,
                CompanyEmployee.left_at == None,
            )
        )).scalars().all()

        from handlers.company import ROLE_SHARE
        charges_salaires = sum(
            int(gross_daily * ROLE_SHARE.get(e.role, 0))
            for e in emps
            if e.role not in ("pdg",)
        )
        benefice_net = net_daily - charges_salaires

        # ── Actionnariat ──────────────────────────────────────────────────
        shares = (await session.execute(
            select(CompanyShare).where(CompanyShare.company_id == company.id)
        )).scalars().all()
        nb_actionnaires = len(shares)
        dividend_pool_weekly = int((company.weekly_revenue or 0) * 0.30)

        # ── Prêt actif ────────────────────────────────────────────────────
        loan = (await session.execute(
            select(CompanyLoan).where(
                CompanyLoan.company_id == company.id,
                CompanyLoan.status == "active",
            )
        )).scalar_one_or_none()

        # ── Réserves ──────────────────────────────────────────────────────
        reserve_legale = company.legal_reserve or 0
        emps_count = len([e for e in emps if e.left_at is None])

        lines = [
            f"📊 <b>Bilan financier — {company.name}</b>",
            f"<i>{level_name} · Réputation {company.reputation:.1f}/5 ⭐</i>",
            "",
            "━━━ REVENUS QUOTIDIENS ━━━",
            f"💹 Revenus bruts  : <b>{_fmt(gross_daily)} $</b>",
            f"🔒 Réserve légale : <b>−{_fmt(legal_daily)} $</b> (10%)",
            f"📥 Revenus nets   : <b>{_fmt(net_daily)} $</b>",
            f"👷 Charges sal.   : <b>−{_fmt(charges_salaires)} $</b> ({emps_count} emp.)",
            f"💰 Bénéfice net   : <b>{_fmt(benefice_net)} $</b>/jour",
            "",
            "━━━ TRÉSORERIE ━━━",
            f"🏦 Caisse totale  : <b>{_fmt(company.treasury)} $</b>",
            f"🔐 Réserve légale : <b>{_fmt(reserve_legale)} $</b> (intouchable)",
        ]

        # Disponible pour retrait PDG
        reserve_sal = sum(
            int(gross_daily * ROLE_SHARE.get(e.role, 0))
            for e in emps if e.role not in ("stagiaire", "pdg")
        )
        disponible = max(0, company.treasury - reserve_legale - reserve_sal)
        lines.append(f"💸 Retirable PDG  : <b>{_fmt(disponible)} $</b>")

        if loan:
            jours_restants = max(0, (loan.due_at - datetime.utcnow()).days)
            rate_pct = f"{loan.interest_rate * 100:.0f}%"
            lines += [
                "",
                "━━━ PRÊT BANCAIRE ACTIF ━━━",
                f"🏛️ Restant dû     : <b>{_fmt(loan.remaining)} $</b>",
                f"📅 Échéance       : dans <b>{jours_restants} jours</b>",
                f"💳 Rembt. auto    : <b>{_fmt(loan.daily_payment)} $/jour</b>",
                f"📈 Taux annuel    : <b>{rate_pct}</b>",
            ]
            if loan.missed_days:
                lines.append(f"⚠️ Jours de retard : <b>{loan.missed_days}</b>")

        lines += [
            "",
            "━━━ ACTIONNARIAT ━━━",
            f"📜 Parts totales  : <b>{company.total_shares}</b>",
            f"👤 Actionnaires   : <b>{nb_actionnaires}</b>",
            f"💼 Rev. semaine   : <b>{_fmt(company.weekly_revenue or 0)} $</b>",
            f"🎁 Pool dividendes: <b>{_fmt(dividend_pool_weekly)} $</b> (30%)",
            "",
            "<i>Les dividendes sont distribués chaque lundi à 9h.</i>",
        ]

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── COMMANDE : /emprunterboite [montant] ────────────────────────────────────

async def emprunterboite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """[DÉSACTIVÉ] Prêts entreprise désactivés."""
    await update.message.reply_text(
        "❌ <b>Les prêts entreprise sont temporairement désactivés.</b>",
        parse_mode="HTML"
    )
    return

async def emprunterboite_cmd_DISABLED(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """PDG contracte un prêt bancaire pour son entreprise."""
    user = update.effective_user
    if not context.args:
        await update.message.reply_text(
            "❌ Usage : <code>/emprunterboite [montant]</code>", parse_mode="HTML"
        )
        return
    try:
        amount = int(context.args[0].replace("_", "").replace(" ", ""))
    except ValueError:
        await update.message.reply_text("❌ Montant invalide.")
        return

    if amount <= 0:
        await update.message.reply_text("❌ Montant invalide.")
        return

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company or emp.role != "pdg":
            await update.message.reply_text("❌ Réservé au PDG.")
            return
        if company.is_bot_company:
            await update.message.reply_text("❌ Indisponible pour les entreprises officielles.")
            return

        # Vérifier qu'il n'y a pas déjà un prêt actif
        existing = (await session.execute(
            select(CompanyLoan).where(
                CompanyLoan.company_id == company.id,
                CompanyLoan.status == "active",
            )
        )).scalar_one_or_none()
        if existing:
            await update.message.reply_text(
                f"❌ <b>{company.name}</b> a déjà un prêt actif en cours.\n"
                f"Solde restant : <b>{_fmt(existing.remaining)} $</b>\n"
                f"Utilise <code>/pretboite</code> pour voir les détails.",
                parse_mode="HTML"
            )
            return

        # Capacité d'emprunt
        capacity_rate = LOAN_CAPACITY.get(company.level, 0.25)
        max_loan = int(company.value * capacity_rate)
        if amount > max_loan:
            await update.message.reply_text(
                f"❌ Montant trop élevé pour ton niveau d'entreprise.\n\n"
                f"🏢 Niveau : <b>{_level_info(company.level)[0]}</b>\n"
                f"💰 Valeur : <b>{_fmt(company.value)} $</b>\n"
                f"🏦 Emprunt max : <b>{_fmt(max_loan)} $</b> ({int(capacity_rate*100)}% de la valeur)",
                parse_mode="HTML"
            )
            return

        # Calcul des intérêts
        annual_rate = LOAN_RATES.get(company.level, 0.18)
        monthly_rate = annual_rate / 12
        total_interest = int(amount * monthly_rate)
        total_due = amount + total_interest
        daily_payment = total_due // LOAN_DURATION_DAYS
        due_at = datetime.utcnow() + timedelta(days=LOAN_DURATION_DAYS)

        # Créer le prêt
        loan = CompanyLoan(
            company_id=company.id,
            amount=amount,
            remaining=total_due,
            interest_rate=annual_rate,
            daily_payment=daily_payment,
            due_at=due_at,
            status="active",
            missed_days=0,
        )
        session.add(loan)

        # Virer l'argent dans la trésorerie
        company.treasury += amount

        await _add_log(
            session, company.id, "pret",
            f"Prêt bancaire de {_fmt(amount)} $ contracté (taux {annual_rate*100:.0f}%/an, "
            f"rembt. {_fmt(daily_payment)} $/jour pendant 30 jours)",
            amount=amount
        )
        await session.commit()

        await update.message.reply_text(
            f"🏛️ <b>Prêt bancaire accordé !</b>\n\n"
            f"💰 Montant reçu     : <b>{_fmt(amount)} $</b>\n"
            f"📈 Taux annuel      : <b>{annual_rate*100:.0f}%</b>\n"
            f"💵 Intérêts (1 mois): <b>{_fmt(total_interest)} $</b>\n"
            f"📋 Total à rembourser: <b>{_fmt(total_due)} $</b>\n"
            f"💳 Remboursement auto: <b>{_fmt(daily_payment)} $/jour</b>\n"
            f"📅 Échéance          : <b>dans 30 jours</b>\n\n"
            f"🏦 Trésorerie maintenant : <b>{_fmt(company.treasury)} $</b>\n\n"
            f"<i>Le remboursement est prélevé automatiquement sur la trésorerie chaque jour.</i>",
            parse_mode="HTML"
        )


# ─── COMMANDE : /pretboite ────────────────────────────────────────────────────

async def pretboite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le prêt actif de l'entreprise."""
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company:
            await update.message.reply_text("❌ Tu n'appartiens à aucune entreprise.")
            return

        loan = (await session.execute(
            select(CompanyLoan).where(
                CompanyLoan.company_id == company.id,
                CompanyLoan.status == "active",
            )
        )).scalar_one_or_none()

        if not loan:
            # Vérifier le dernier prêt remboursé
            last_loan = (await session.execute(
                select(CompanyLoan).where(
                    CompanyLoan.company_id == company.id,
                ).order_by(CompanyLoan.taken_at.desc())
            )).scalar_one_or_none()
            if last_loan:
                status_str = "✅ Remboursé" if last_loan.status == "repaid" else "❌ En défaut"
                await update.message.reply_text(
                    f"🏦 <b>{company.name}</b> n'a pas de prêt actif.\n\n"
                    f"<i>Dernier prêt : {_fmt(last_loan.amount)} $ — {status_str}</i>\n\n"
                    f"Utilise <code>/emprunterboite [montant]</code> pour contracter un prêt.",
                    parse_mode="HTML"
                )
            else:
                await update.message.reply_text(
                    f"🏦 <b>{company.name}</b> n'a jamais contracté de prêt.\n\n"
                    f"Utilise <code>/emprunterboite [montant]</code> pour emprunter.",
                    parse_mode="HTML"
                )
            return

        jours_restants = max(0, (loan.due_at - datetime.utcnow()).days)
        paid_so_far = loan.amount + int(loan.amount * loan.interest_rate / 12) - loan.remaining
        progression_pct = int(paid_so_far / (loan.amount + int(loan.amount * loan.interest_rate / 12)) * 100)
        rate_pct = f"{loan.interest_rate * 100:.0f}%"

        bar_filled = progression_pct // 10
        bar = "█" * bar_filled + "░" * (10 - bar_filled)

        warn = ""
        if loan.missed_days:
            warn = f"\n⚠️ <b>{loan.missed_days} jour(s) de retard</b> — pénalités appliquées !"

        await update.message.reply_text(
            f"🏛️ <b>Prêt bancaire — {company.name}</b>\n\n"
            f"💰 Montant initial  : <b>{_fmt(loan.amount)} $</b>\n"
            f"📋 Restant dû       : <b>{_fmt(loan.remaining)} $</b>\n"
            f"💳 Rembt. auto/jour : <b>{_fmt(loan.daily_payment)} $</b>\n"
            f"📈 Taux annuel      : <b>{rate_pct}</b>\n"
            f"📅 Échéance         : dans <b>{jours_restants} jours</b>\n\n"
            f"Progression : [{bar}] {progression_pct}%"
            f"{warn}\n\n"
            f"<i>Utilise <code>/rembourserboite [montant]</code> pour un remboursement anticipé.</i>",
            parse_mode="HTML"
        )


# ─── COMMANDE : /rembourserboite [montant] ────────────────────────────────────

async def rembourserboite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remboursement anticipé (partiel ou total) du prêt d'entreprise."""
    user = update.effective_user
    if not context.args:
        await update.message.reply_text(
            "❌ Usage : <code>/rembourserboite [montant]</code>\n"
            "<i>Utilise 'tout' pour rembourser en une fois.</i>",
            parse_mode="HTML"
        )
        return

    async with AsyncSessionLocal() as session:
        company, emp = await _get_user_company(session, user.id)
        if not company or emp.role != "pdg":
            await update.message.reply_text("❌ Réservé au PDG.")
            return

        loan = (await session.execute(
            select(CompanyLoan).where(
                CompanyLoan.company_id == company.id,
                CompanyLoan.status == "active",
            )
        )).scalar_one_or_none()

        if not loan:
            await update.message.reply_text("❌ Aucun prêt actif.")
            return

        arg = context.args[0].lower().replace("_", "")
        if arg == "tout":
            amount = loan.remaining
        else:
            try:
                amount = int(arg)
            except ValueError:
                await update.message.reply_text("❌ Montant invalide.")
                return

        if amount <= 0:
            await update.message.reply_text("❌ Montant invalide.")
            return
        if amount > loan.remaining:
            amount = loan.remaining

        if company.treasury < amount:
            await update.message.reply_text(
                f"❌ Trésorerie insuffisante.\n"
                f"Disponible : <b>{_fmt(company.treasury)} $</b>\n"
                f"Demandé    : <b>{_fmt(amount)} $</b>",
                parse_mode="HTML"
            )
            return

        company.treasury -= amount
        company.value = max(LEVELS[1][2], company.value - amount)
        loan.remaining -= amount
        loan.missed_days = 0

        msg_extra = ""
        if loan.remaining <= 0:
            loan.status = "repaid"
            msg_extra = "\n\n✅ <b>Prêt entièrement remboursé !</b> Félicitations."
            await _add_log(session, company.id, "pret",
                           f"Remboursement anticipé total du prêt ({_fmt(loan.amount)} $)")
        else:
            await _add_log(session, company.id, "pret",
                           f"Remboursement anticipé partiel de {_fmt(amount)} $", amount=amount)

        await session.commit()

        await update.message.reply_text(
            f"✅ <b>{_fmt(amount)} $</b> remboursés au prêt.\n\n"
            f"📋 Restant dû      : <b>{_fmt(loan.remaining)} $</b>\n"
            f"🏦 Trésorerie      : <b>{_fmt(company.treasury)} $</b>"
            f"{msg_extra}",
            parse_mode="HTML"
        )


# ─── COMMANDE : /dividendes ───────────────────────────────────────────────────

async def dividendes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche les dividendes potentiels de l'utilisateur pour cette semaine."""
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        # Chercher toutes les parts que l'utilisateur détient
        user_shares = (await session.execute(
            select(CompanyShare).where(CompanyShare.owner_id == user.id)
        )).scalars().all()

        if not user_shares:
            await update.message.reply_text(
                "📭 Tu ne détiens aucune part d'entreprise.\n\n"
                "Achète des parts avec <code>/acheterparts</code> pour toucher des dividendes.",
                parse_mode="HTML"
            )
            return

        lines = ["💎 <b>Tes dividendes cette semaine</b>\n"]
        total_estimated = 0

        for share in user_shares:
            company = await session.get(Company, share.company_id)
            if not company or not company.is_active:
                continue

            # Calcul du dividende estimé
            weekly_rev = company.weekly_revenue or 0
            dividend_pool = int(weekly_rev * 0.30)
            my_ratio = share.quantity / company.total_shares if company.total_shares > 0 else 0
            my_dividend = int(dividend_pool * my_ratio)
            total_estimated += my_dividend

            pct = my_ratio * 100
            lines.append(
                f"🏢 <b>{company.name}</b>\n"
                f"   📜 Parts : {share.quantity}/{company.total_shares} ({pct:.1f}%)\n"
                f"   💼 Rev. semaine : {_fmt(weekly_rev)} $\n"
                f"   🎁 Ton estimé   : <b>{_fmt(my_dividend)} $</b>\n"
            )

        lines.append(f"━━━━━━━━━━━━━━━")
        lines.append(f"💰 Total estimé : <b>{_fmt(total_estimated)} $</b>")
        lines.append("\n<i>Distribution automatique chaque lundi à 9h.</i>")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── JOB : DIVIDENDES HEBDOMADAIRES (chaque lundi 9h) ─────────────────────────

async def job_company_dividends(context: ContextTypes.DEFAULT_TYPE):
    """Distribue 30% des revenus hebdomadaires aux actionnaires, puis remet weekly_revenue à 0."""
    logger.info("Dividendes : distribution hebdomadaire...")
    async with AsyncSessionLocal() as session:
        companies = (await session.execute(
            select(Company).where(
                Company.is_active == True,
                Company.is_bot_company == False,
            )
        )).scalars().all()

        for company in companies:
            weekly_rev = company.weekly_revenue or 0
            if weekly_rev <= 0:
                continue

            dividend_pool = int(weekly_rev * 0.30)
            if dividend_pool <= 0 or company.treasury < dividend_pool:
                company.weekly_revenue = 0
                continue

            # Récupérer tous les actionnaires
            shares = (await session.execute(
                select(CompanyShare).where(CompanyShare.company_id == company.id)
            )).scalars().all()

            total_distributed = 0
            notif_lines = [f"💎 <b>Dividendes — {company.name}</b>\n"]

            for sh in shares:
                if sh.quantity <= 0:
                    continue
                ratio = sh.quantity / company.total_shares
                dividend = int(dividend_pool * ratio)
                if dividend <= 0:
                    continue

                sh_user = await session.get(User, sh.owner_id)
                if sh_user:
                    sh_user.coins += dividend
                    total_distributed += dividend
                    notif_lines.append(
                        f"  👤 {sh.quantity} parts ({ratio*100:.1f}%) → <b>+{_fmt(dividend)} $</b>"
                    )
                    # Notifier l'actionnaire en DM
                    try:
                        pct = ratio * 100
                        await context.bot.send_message(
                            chat_id=sh.owner_id,
                            text=(
                                f"💎 <b>Dividendes reçus !</b>\n\n"
                                f"🏢 Entreprise : <b>{company.name}</b>\n"
                                f"📜 Tes parts  : {sh.quantity}/{company.total_shares} ({pct:.1f}%)\n"
                                f"💰 Reçu       : <b>+{_fmt(dividend)} $</b>"
                            ),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

            if total_distributed > 0:
                company.treasury -= total_distributed
                company.value = max(LEVELS[1][2], company.value - total_distributed)
                await _add_log(
                    session, company.id, "dividendes",
                    f"Dividendes hebdo distribués : {_fmt(total_distributed)} $ à {len(shares)} actionnaire(s)",
                    amount=total_distributed
                )

            # Reset weekly revenue
            company.weekly_revenue = 0

        await session.commit()
    logger.info("Dividendes : distribution terminée.")


# ─── INIT : créer les tables manquantes ──────────────────────────────────────

async def init_finance_tables():
    """Crée les nouvelles tables financières si elles n'existent pas."""
    from database.db import engine
    from database.models import Base, CompanyLoan
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Tables finance initialisées (company_loans).")

    # Migration : ajouter les colonnes legal_reserve et weekly_revenue si absentes
    from sqlalchemy import text
    async with engine.begin() as conn:
        for col, default in [("legal_reserve", "0"), ("weekly_revenue", "0"), ("extra_slots", "0"), ("is_muted", "FALSE")]:
            try:
                await conn.execute(
                    text(f"ALTER TABLE companies ADD COLUMN IF NOT EXISTS {col} BIGINT DEFAULT {default}")
                )
                logger.info(f"Colonne companies.{col} vérifiée.")
            except Exception:
                pass
