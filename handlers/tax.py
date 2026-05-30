"""
🏛️ Agence Fiscale — impôts tous les 2 jours sur les entreprises
"""
from datetime import datetime, timedelta

from sqlalchemy import select, func
from telegram import Update
from telegram.ext import ContextTypes

from database import AsyncSessionLocal
from database.models import Company, TaxRecord, StateCaisse, CompanyEmployee
from handlers.company import _fmt, _add_log

# ─── ADMIN IDS ────────────────────────────────────────────────────────────────
ADMIN_IDS = {5338202791}   # à compléter selon tes admins

# ─── TAUX D'IMPOSITION ────────────────────────────────────────────────────────
def _compute_tax(treasury: int) -> int:
    """Calcule l'impôt tous les 2 jours selon la trésorerie."""
    if treasury <= 0:
        return 0
    if treasury < 1_000_000_000:          # < 1B  → 0.05%
        return int(treasury * 0.0005)
    elif treasury < 10_000_000_000:       # < 10B → 0.1%
        return int(treasury * 0.001)
    else:                                  # 10B+  → 0.15%
        return int(treasury * 0.0015)


# ─── JOB TOUS LES 2 JOURS ─────────────────────────────────────────────────────
async def tax_daily_job(context: ContextTypes.DEFAULT_TYPE):
    """Génère les factures fiscales tous les 2 jours — garde en DB pour éviter les doublons au redémarrage."""
    async with AsyncSessionLocal() as session:
        # ── Vérification garde globale en DB ────────────────────────────────
        caisse = (await session.execute(select(StateCaisse))).scalar_one_or_none()
        if not caisse:
            caisse = StateCaisse(total=0, last_tax_at=None)
            session.add(caisse)
            await session.flush()

        now = datetime.utcnow()
        if caisse.last_tax_at and (now - caisse.last_tax_at) < timedelta(hours=47):
            return  # Moins de 47h depuis la dernière émission → on skip

        # Marquer l'émission AVANT de créer les factures
        caisse.last_tax_at = now
        await session.flush()

        # Toutes les entreprises actives avec tréso > 50M
        companies = (await session.execute(
            select(Company).where(
                Company.is_active == True,
                Company.is_bot_company == False,
                Company.treasury > 50_000_000,
            )
        )).scalars().all()

        for company in companies:
            tax = _compute_tax(company.treasury)
            if tax <= 0:
                continue

            # Créer la facture avec délai de 48h
            record = TaxRecord(
                company_id=company.id,
                amount_due=tax,
                amount_paid=0,
                due_at=datetime.utcnow() + timedelta(hours=48),
                status="pending",
            )
            session.add(record)
            await session.flush()

            # Notifier le PDG en DM
            pdg_emp = (await session.execute(
                select(CompanyEmployee).where(
                    CompanyEmployee.company_id == company.id,
                    CompanyEmployee.role == "pdg",
                    CompanyEmployee.left_at == None,
                )
            )).scalar_one_or_none()

            if pdg_emp:
                taux = "0.05%" if company.treasury < 1_000_000_000 else ("0.1%" if company.treasury < 10_000_000_000 else "0.15%")
                gel_note = "\n⚠️ <b>Ta trésorerie est actuellement gelée.</b> Paie tes impôts pour la débloquer." if company.treasury_frozen else ""
                try:
                    await context.bot.send_message(
                        chat_id=pdg_emp.user_id,
                        text=(
                            f"🏛️ <b>AGENCE FISCALE — Nouvelle facture</b>\n\n"
                            f"🏢 Entreprise : <b>{company.name}</b>\n"
                            f"💰 Trésorerie : <b>{_fmt(company.treasury)} $</b>\n"
                            f"📊 Taux appliqué : <b>{taux}</b>\n"
                            f"🧾 Montant dû : <b>{_fmt(tax)} $</b>\n\n"
                            f"⏳ Tu as <b>48h</b> pour payer.\n"
                            f"💳 Commande : <code>/payerimpots {tax}</code>\n"
                            f"📋 Facture n°{record.id}{gel_note}"
                        ),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

        await session.commit()


# ─── COMMANDE : /payerimpots [montant] ────────────────────────────────────────
async def payerimpots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text(
            "❌ Usage : <code>/payerimpots [montant]</code>",
            parse_mode="HTML"
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
        # Trouver l'entreprise dont l'user est PDG
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

        # Factures à payer : pending, partial ET overdue (pour solder les anciennes)
        pending = (await session.execute(
            select(TaxRecord).where(
                TaxRecord.company_id == company.id,
                TaxRecord.status.in_(["pending", "partial", "overdue"]),
            ).order_by(TaxRecord.created_at.asc())
        )).scalars().all()

        if not pending:
            await update.message.reply_text(
                f"✅ <b>{company.name}</b> n'a aucune facture fiscale en attente.",
                parse_mode="HTML"
            )
            return

        # Vérifier solde PDG
        from database.models import User
        db_user = (await session.execute(
            select(User).where(User.user_id == user.id)
        )).scalar_one_or_none()

        if not db_user or db_user.coins < amount:
            await update.message.reply_text(
                f"❌ Solde insuffisant. Tu as <b>{_fmt(db_user.coins if db_user else 0)} $</b>.",
                parse_mode="HTML"
            )
            return

        # Distribuer le paiement sur les factures (du plus ancien au plus récent)
        reste = amount
        db_user.coins -= amount

        for record in pending:
            if reste <= 0:
                break
            du = record.amount_due - record.amount_paid
            if du <= 0:
                record.status = "paid"
                continue
            paye = min(reste, du)
            record.amount_paid += paye
            reste -= paye
            if record.amount_paid >= record.amount_due:
                record.status = "paid"
            else:
                record.status = "partial"

        # Rembourser le surplus éventuel
        if reste > 0:
            db_user.coins += reste
            amount -= reste

        # Mettre à jour la caisse d'État
        caisse = (await session.execute(select(StateCaisse))).scalar_one_or_none()
        if not caisse:
            caisse = StateCaisse(total=0)
            session.add(caisse)
        caisse.total += amount

        # Recalculer la dette fiscale réelle depuis les factures overdue restantes
        remaining_overdue = (await session.execute(
            select(func.sum(TaxRecord.amount_due - TaxRecord.amount_paid)).where(
                TaxRecord.company_id == company.id,
                TaxRecord.status == "overdue",
            )
        )).scalar() or 0
        company.tax_debt = max(0, remaining_overdue)

        # Compter les overdue non entièrement payées
        overdue_unpaid_count = (await session.execute(
            select(func.count()).where(
                TaxRecord.company_id == company.id,
                TaxRecord.status == "overdue",
                TaxRecord.amount_paid < TaxRecord.amount_due,
            )
        )).scalar() or 0

        # Vérifier si on peut dégeler la trésorerie
        if company.treasury_frozen:
            if overdue_unpaid_count == 0 or company.tax_debt <= 0:
                # Plus aucun overdue impayé → dégel total
                company.treasury_frozen = False
                company.tax_debt = 0
                gel_msg = "\n🔓 <b>Ta trésorerie a été dégelée !</b>"
            else:
                # Dégel si on a payé au moins 50% de la dette
                total_overdue_original = (await session.execute(
                    select(func.sum(TaxRecord.amount_due)).where(
                        TaxRecord.company_id == company.id,
                        TaxRecord.status == "overdue",
                    )
                )).scalar() or 0
                if total_overdue_original > 0 and remaining_overdue <= total_overdue_original * 0.5:
                    company.treasury_frozen = False
                    gel_msg = "\n🔓 <b>Ta trésorerie a été dégelée !</b>"
                else:
                    gel_msg = f"\n⚠️ Trésorerie encore gelée. Il reste <b>{_fmt(company.tax_debt)} $</b> à payer. Paie 50% pour dégeler."
        else:
            gel_msg = ""

        # Compter les vrais impayés restants (overdue avec du restant)
        overdue_count = (await session.execute(
            select(func.count()).where(
                TaxRecord.company_id == company.id,
                TaxRecord.status == "overdue",
                TaxRecord.amount_paid < TaxRecord.amount_due,
            )
        )).scalar() or 0

        await _add_log(session, company.id, "impots", f"Paiement fiscal de {_fmt(amount)} $", amount=amount)
        await session.commit()

        retard_line = f"\n🧾 Factures en retard : <b>{overdue_count}/3</b>" if overdue_count > 0 else "\n✅ Aucune facture en retard"

        await update.message.reply_text(
            f"🏛️ <b>Agence Fiscale — Paiement enregistré</b>\n\n"
            f"✅ <b>{_fmt(amount)} $</b> payés pour <b>{company.name}</b>"
            f"{retard_line}{gel_msg}",
            parse_mode="HTML"
        )


# ─── COMMANDE : /caisse (admin) ───────────────────────────────────────────────
async def caisse_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Accès réservé à l'administration.")
        return

    async with AsyncSessionLocal() as session:
        caisse = (await session.execute(select(StateCaisse))).scalar_one_or_none()
        total = caisse.total if caisse else 0

        # Stats impôts
        pending_total = (await session.execute(
            select(func.sum(TaxRecord.amount_due - TaxRecord.amount_paid)).where(
                TaxRecord.status.in_(["pending", "partial"])
            )
        )).scalar() or 0

        overdue_count = (await session.execute(
            select(func.count()).where(
                TaxRecord.status == "overdue",
                TaxRecord.amount_paid < TaxRecord.amount_due,
            )
        )).scalar() or 0

        frozen_count = (await session.execute(
            select(func.count()).where(
                Company.treasury_frozen == True,
                Company.is_active == True,
            )
        )).scalar() or 0

        await update.message.reply_text(
            f"🏛️ <b>CAISSE D'ÉTAT</b>\n\n"
            f"💰 Total collecté : <b>{_fmt(total)} $</b>\n\n"
            f"📋 En attente de paiement : <b>{_fmt(pending_total)} $</b>\n"
            f"⏰ Factures en retard : <b>{overdue_count}</b>\n"
            f"🔒 Entreprises gelées : <b>{frozen_count}</b>",
            parse_mode="HTML"
        )


# ─── JOB : marquer les impayés et geler si 3 impayés ─────────────────────────
async def tax_overdue_job(context: ContextTypes.DEFAULT_TYPE):
    """Marque les factures expirées et gèle les trésoreries après 3 impayés."""
    async with AsyncSessionLocal() as session:
        now = datetime.utcnow()

        # Récupérer les factures expirées non payées
        expired = (await session.execute(
            select(TaxRecord).where(
                TaxRecord.due_at <= now,
                TaxRecord.status.in_(["pending", "partial"]),
            )
        )).scalars().all()

        for record in expired:
            record.status = "overdue"
            company = await session.get(Company, record.company_id)
            if not company:
                continue

            # Recalculer la dette depuis les overdue réels
            remaining_overdue = (await session.execute(
                select(func.sum(TaxRecord.amount_due - TaxRecord.amount_paid)).where(
                    TaxRecord.company_id == company.id,
                    TaxRecord.status == "overdue",
                )
            )).scalar() or 0
            company.tax_debt = max(0, remaining_overdue)

            # Compter les vrais impayés (avec du restant)
            overdue_count = (await session.execute(
                select(func.count()).where(
                    TaxRecord.company_id == company.id,
                    TaxRecord.status == "overdue",
                    TaxRecord.amount_paid < TaxRecord.amount_due,
                )
            )).scalar()

            if overdue_count >= 3 and not company.treasury_frozen:
                company.treasury_frozen = True
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
                                f"🔒 <b>AGENCE FISCALE — Gel de trésorerie</b>\n\n"
                                f"🏢 <b>{company.name}</b> a accumulé <b>3 factures impayées</b>.\n"
                                f"Ta trésorerie est désormais <b>gelée</b>.\n\n"
                                f"💳 Pour dégeler : paye au moins <b>50%</b> du total dû "
                                f"(<b>{_fmt(company.tax_debt // 2)} $</b>)\n"
                                f"avec <code>/payerimpots [montant]</code>"
                            ),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

        await session.commit()


# ─── COMMANDE : /mesimpots ────────────────────────────────────────────────────
async def mesimpots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le statut fiscal de l'entreprise du PDG."""
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

        # Factures en attente
        pending = (await session.execute(
            select(TaxRecord).where(
                TaxRecord.company_id == company.id,
                TaxRecord.status.in_(["pending", "partial", "overdue"]),
            ).order_by(TaxRecord.created_at.asc())
        )).scalars().all()

        # Total déjà payé (historique)
        total_paid = (await session.execute(
            select(func.sum(TaxRecord.amount_paid)).where(
                TaxRecord.company_id == company.id,
                TaxRecord.status == "paid",
            )
        )).scalar() or 0

        gel = "🔒 <b>GELÉE</b>" if company.treasury_frozen else "✅ Libre"
        dette = company.tax_debt or 0

        lines = [
            f"🏛️ <b>AGENCE FISCALE — {company.name}</b>",
            f"━━━━━━━━━━━━━━━━━━━━━━",
            f"💰 Trésorerie : <b>{_fmt(company.treasury)} $</b>",
            f"🔐 État : {gel}",
            f"📊 Dette fiscale : <b>{_fmt(dette)} $</b>",
            f"✅ Total payé (historique) : <b>{_fmt(total_paid)} $</b>",
            "",
        ]

        if not pending:
            lines.append("✅ <b>Aucune facture en attente.</b> Tu es à jour !")
        else:
            lines.append(f"📋 <b>{len(pending)} facture(s) en attente :</b>")
            for r in pending:
                reste = r.amount_due - r.amount_paid
                status_label = {
                    "pending": "⏳ En attente",
                    "partial": "🔸 Partiellement payée",
                    "overdue": "🔴 En retard",
                }.get(r.status, r.status)
                due_str = ""
                if r.due_at:
                    now = datetime.utcnow()
                    if r.due_at > now:
                        diff = r.due_at - now
                        h = int(diff.total_seconds() // 3600)
                        m = int((diff.total_seconds() % 3600) // 60)
                        due_str = f" · ⏰ Expire dans {h}h{m:02d}"
                    else:
                        due_str = " · ⏰ <b>Expirée</b>"
                lines.append(
                    f"  • Facture #{r.id} — {status_label}{due_str}\n"
                    f"    Dû : <b>{_fmt(reste)} $</b>  "
                    f"(<code>/payerimpots {reste}</code>)"
                )

        if pending:
            total_du = sum(r.amount_due - r.amount_paid for r in pending)
            lines.append(f"\n💸 <b>Total à payer : {_fmt(total_du)} $</b>")
            if company.treasury_frozen:
                lines.append(f"💡 Paie <b>50%</b> minimum ({_fmt(total_du // 2)} $) pour dégeler.")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
