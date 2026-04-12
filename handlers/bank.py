"""
Système bancaire complet.

5 banques de rang différent — plus la banque est prestigieuse :
  • Dépôt minimum plus élevé
  • Taux d'intérêt plus avantageux (versés toutes les 6h via job)
  • Prêts plus importants disponibles

Commandes :
  /banks         — liste des banques
  /bankopen      — ouvrir un compte
  /bankdeposit   — déposer
  /bankwithdraw  — retirer
  /bankbalance   — voir ses comptes
  /bankloan      — emprunter
  /bankrepay     — rembourser un prêt
  /bankloans     — voir ses prêts
"""

import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from sqlalchemy import select

from database.db import AsyncSessionLocal, get_user, add_coins
from database.models import BankAccount, Loan
from utils.helpers import ensure_user

logger = logging.getLogger(__name__)

# ─── Définition des banques ───────────────────────────────────────────────────

BANKS = {
    "bronze": {
        "name":          "🥉 Banque Bronze",
        "rank":          1,
        "emoji":         "🥉",
        "desc":          "Banque populaire, accessible à tous",
        "min_deposit":   1_000,
        "max_deposit":   500_000,
        "interest_rate": 0.005,    # 0.5% toutes les 6h → ~2%/jour
        "max_loan":      100_000,
        "loan_rate":     0.08,     # 8% d'intérêt sur le prêt
        "loan_days":     7,
    },
    "silver": {
        "name":          "🥈 Banque Silver",
        "rank":          2,
        "emoji":         "🥈",
        "desc":          "Pour les épargnants sérieux",
        "min_deposit":   10_000,
        "max_deposit":   2_000_000,
        "interest_rate": 0.008,    # 0.8% → ~3.2%/jour
        "max_loan":      500_000,
        "loan_rate":     0.06,
        "loan_days":     14,
    },
    "gold": {
        "name":          "🥇 Banque Gold",
        "rank":          3,
        "emoji":         "🥇",
        "desc":          "Banque des investisseurs fortunés",
        "min_deposit":   100_000,
        "max_deposit":   10_000_000,
        "interest_rate": 0.012,    # 1.2% → ~4.8%/jour
        "max_loan":      2_000_000,
        "loan_rate":     0.05,
        "loan_days":     21,
    },
    "platinum": {
        "name":          "💠 Banque Platinum",
        "rank":          4,
        "emoji":         "💠",
        "desc":          "Réservée aux élites financières",
        "min_deposit":   500_000,
        "max_deposit":   50_000_000,
        "interest_rate": 0.018,    # 1.8% → ~7.2%/jour
        "max_loan":      10_000_000,
        "loan_rate":     0.04,
        "loan_days":     30,
    },
    "diamond": {
        "name":          "💎 Banque Diamond",
        "rank":          5,
        "emoji":         "💎",
        "desc":          "La banque des milliardaires",
        "min_deposit":   2_000_000,
        "max_deposit":   999_999_999_999,
        "interest_rate": 0.025,    # 2.5% → ~10%/jour
        "max_loan":      50_000_000,
        "loan_rate":     0.03,
        "loan_days":     60,
    },
}

BANK_KEYS = ["bronze", "silver", "gold", "platinum", "diamond"]

INTEREST_INTERVAL_HOURS = 6  # toutes les 6h


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


# ─── /banks ───────────────────────────────────────────────────────────────────

async def banks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["<b>🏦 Banques disponibles</b>\n"]
    for key in BANK_KEYS:
        b = BANKS[key]
        lines.append(
            f"{b['emoji']} <b>{b['name']}</b>  (Rang {b['rank']}/5)\n"
            f"  └ {b['desc']}\n"
            f"  └ Dépôt min : {_fmt(b['min_deposit'])} $ · Max : {_fmt(b['max_deposit'])} $\n"
            f"  └ Intérêts  : +{b['interest_rate']*100:.1f}% / {INTEREST_INTERVAL_HOURS}h\n"
            f"  └ Prêt max  : {_fmt(b['max_loan'])} $  (taux {b['loan_rate']*100:.0f}%)\n"
        )
    lines.append("Utilisez /bankopen [banque] pour ouvrir un compte.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ─── /bankopen ────────────────────────────────────────────────────────────────

async def bankopen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        keys = " | ".join(BANK_KEYS)
        return await update.message.reply_text(
            f"Usage : /bankopen [banque]\nBanques : {keys}"
        )

    bank_id = context.args[0].lower()
    if bank_id not in BANKS:
        return await update.message.reply_text(f"Banque inconnue. Choix : {' | '.join(BANK_KEYS)}")

    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        existing = await session.execute(
            select(BankAccount).where(
                BankAccount.user_id == user.user_id,
                BankAccount.bank_id == bank_id,
            )
        )
        if existing.scalar_one_or_none():
            return await update.message.reply_text(
                f"Tu as déjà un compte à la {BANKS[bank_id]['name']} !"
            )

        acc = BankAccount(
            user_id       = user.user_id,
            bank_id       = bank_id,
            balance       = 0,
            last_interest = datetime.utcnow(),
        )
        session.add(acc)
        await session.commit()

    b = BANKS[bank_id]
    await update.message.reply_text(
        f"✅ Compte ouvert à la <b>{b['name']}</b> !\n"
        f"Dépôt minimum : {_fmt(b['min_deposit'])} $\n"
        f"Intérêts : +{b['interest_rate']*100:.1f}% toutes les {INTEREST_INTERVAL_HOURS}h\n\n"
        f"Utilisez /bankdeposit {bank_id} [montant] pour alimenter votre compte.",
        parse_mode=ParseMode.HTML,
    )


# ─── /bankdeposit ─────────────────────────────────────────────────────────────

async def bankdeposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        return await update.message.reply_text("Usage : /bankdeposit [banque] [montant]")

    bank_id = context.args[0].lower()
    if bank_id not in BANKS:
        return await update.message.reply_text("Banque inconnue.")

    try:
        amount = int(context.args[1].replace(",", "").replace(" ", ""))
        assert amount > 0
    except (ValueError, AssertionError):
        return await update.message.reply_text("Montant invalide.")

    b    = BANKS[bank_id]
    user = await ensure_user(update.effective_user)

    if amount < b["min_deposit"]:
        return await update.message.reply_text(
            f"Dépôt minimum pour la {b['name']} : {_fmt(b['min_deposit'])} $"
        )

    async with AsyncSessionLocal() as session:
        acc = (await session.execute(
            select(BankAccount).where(
                BankAccount.user_id == user.user_id,
                BankAccount.bank_id == bank_id,
            )
        )).scalar_one_or_none()

        if not acc:
            return await update.message.reply_text(
                f"Tu n'as pas de compte à la {b['name']}. Utilise /bankopen {bank_id}"
            )

        if acc.balance + amount > b["max_deposit"]:
            return await update.message.reply_text(
                f"Dépôt maximum atteint ({_fmt(b['max_deposit'])} $)."
            )

        u = await get_user(session, user.user_id)
        if not u or u.coins < amount:
            return await update.message.reply_text("Solde insuffisant !")

        u.coins     -= amount
        acc.balance += amount
        await session.commit()
        new_wallet  = u.coins
        new_balance = acc.balance

    await update.message.reply_text(
        f"🏦 Dépôt effectué à la <b>{b['name']}</b>\n"
        f"💰 Déposé   : +{_fmt(amount)} $\n"
        f"📊 En banque : {_fmt(new_balance)} $\n"
        f"👛 Portefeuille : {_fmt(new_wallet)} $",
        parse_mode=ParseMode.HTML,
    )


# ─── /bankwithdraw ────────────────────────────────────────────────────────────

async def bankwithdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        return await update.message.reply_text("Usage : /bankwithdraw [banque] [montant]")

    bank_id = context.args[0].lower()
    if bank_id not in BANKS:
        return await update.message.reply_text("Banque inconnue.")

    try:
        amount = int(context.args[1].replace(",", "").replace(" ", ""))
        assert amount > 0
    except (ValueError, AssertionError):
        return await update.message.reply_text("Montant invalide.")

    user = await ensure_user(update.effective_user)
    b    = BANKS[bank_id]

    async with AsyncSessionLocal() as session:
        acc = (await session.execute(
            select(BankAccount).where(
                BankAccount.user_id == user.user_id,
                BankAccount.bank_id == bank_id,
            )
        )).scalar_one_or_none()

        if not acc:
            return await update.message.reply_text(f"Pas de compte à la {b['name']}.")

        if acc.balance < amount:
            return await update.message.reply_text(
                f"Solde bancaire insuffisant ! Tu as {_fmt(acc.balance)} $ dans cette banque."
            )

        u = await get_user(session, user.user_id)
        acc.balance -= amount
        u.coins     += amount
        await session.commit()
        new_wallet  = u.coins
        new_balance = acc.balance

    await update.message.reply_text(
        f"🏦 Retrait effectué de la <b>{b['name']}</b>\n"
        f"💸 Retiré    : {_fmt(amount)} $\n"
        f"📊 En banque : {_fmt(new_balance)} $\n"
        f"👛 Portefeuille : {_fmt(new_wallet)} $",
        parse_mode=ParseMode.HTML,
    )


# ─── /bankbalance ─────────────────────────────────────────────────────────────

async def bankbalance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(BankAccount).where(BankAccount.user_id == user.user_id)
        )
        accounts = result.scalars().all()
        u = await get_user(session, user.user_id)

    if not accounts:
        return await update.message.reply_text(
            "Tu n'as aucun compte bancaire.\nUtilise /banks pour voir les banques disponibles."
        )

    lines = [f"<b>🏦 Comptes bancaires de {update.effective_user.first_name}</b>\n"]
    total = 0
    for acc in accounts:
        b   = BANKS.get(acc.bank_id, {})
        rate = b.get("interest_rate", 0) * 100
        lines.append(
            f"{b.get('emoji','🏦')} <b>{b.get('name', acc.bank_id)}</b>\n"
            f"  └ Solde : {_fmt(acc.balance)} $\n"
            f"  └ Taux  : +{rate:.1f}% / {INTEREST_INTERVAL_HOURS}h\n"
        )
        total += acc.balance

    lines.append(f"\n💼 Total en banque : <b>{_fmt(total)} $</b>")
    lines.append(f"👛 Portefeuille     : {_fmt(u.coins if u else 0)} $")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ─── /bankloan ────────────────────────────────────────────────────────────────

async def bankloan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        return await update.message.reply_text("Usage : /bankloan [banque] [montant]")

    bank_id = context.args[0].lower()
    if bank_id not in BANKS:
        return await update.message.reply_text("Banque inconnue.")

    try:
        amount = int(context.args[1].replace(",", "").replace(" ", ""))
        assert amount > 0
    except (ValueError, AssertionError):
        return await update.message.reply_text("Montant invalide.")

    b    = BANKS[bank_id]
    user = await ensure_user(update.effective_user)

    if amount > b["max_loan"]:
        return await update.message.reply_text(
            f"Prêt maximum pour la {b['name']} : {_fmt(b['max_loan'])} $"
        )

    async with AsyncSessionLocal() as session:
        # Vérifier qu'il a un compte dans cette banque
        acc = (await session.execute(
            select(BankAccount).where(
                BankAccount.user_id == user.user_id,
                BankAccount.bank_id == bank_id,
            )
        )).scalar_one_or_none()

        if not acc:
            return await update.message.reply_text(
                f"Tu dois d'abord ouvrir un compte à la {b['name']} (/bankopen {bank_id})."
            )

        # Vérifier prêt actif existant dans cette banque
        existing_loan = (await session.execute(
            select(Loan).where(
                Loan.user_id == user.user_id,
                Loan.bank_id == bank_id,
                Loan.status  == "active",
            )
        )).scalar_one_or_none()

        if existing_loan:
            return await update.message.reply_text(
                f"Tu as déjà un prêt actif à la {b['name']} !\n"
                f"Remboursement restant : {_fmt(existing_loan.remaining)} $\n"
                f"Utilise /bankrepay {bank_id} [montant] pour rembourser."
            )

        interest    = int(amount * b["loan_rate"])
        total_due   = amount + interest
        due_at      = datetime.utcnow() + timedelta(days=b["loan_days"])

        loan = Loan(
            user_id       = user.user_id,
            bank_id       = bank_id,
            amount        = amount,
            remaining     = total_due,
            interest_rate = b["loan_rate"],
            due_at        = due_at,
        )
        session.add(loan)

        u = await get_user(session, user.user_id)
        u.coins += amount
        await session.commit()
        new_balance = u.coins

    await update.message.reply_text(
        f"💳 <b>Prêt accordé par la {b['name']}</b>\n\n"
        f"💰 Montant emprunté : {_fmt(amount)} $\n"
        f"📈 Intérêts ({b['loan_rate']*100:.0f}%) : {_fmt(interest)} $\n"
        f"💸 Total à rembourser : <b>{_fmt(total_due)} $</b>\n"
        f"📅 Date limite : {due_at.strftime('%d/%m/%Y')}\n\n"
        f"👛 Nouveau solde : {_fmt(new_balance)} $\n"
        f"Utilisez /bankrepay {bank_id} [montant] pour rembourser.",
        parse_mode=ParseMode.HTML,
    )


# ─── /bankrepay ───────────────────────────────────────────────────────────────

async def bankrepay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        return await update.message.reply_text("Usage : /bankrepay [banque] [montant]")

    bank_id = context.args[0].lower()
    if bank_id not in BANKS:
        return await update.message.reply_text("Banque inconnue.")

    try:
        amount = int(context.args[1].replace(",", "").replace(" ", ""))
        assert amount > 0
    except (ValueError, AssertionError):
        return await update.message.reply_text("Montant invalide.")

    user = await ensure_user(update.effective_user)
    b    = BANKS[bank_id]

    async with AsyncSessionLocal() as session:
        loan = (await session.execute(
            select(Loan).where(
                Loan.user_id == user.user_id,
                Loan.bank_id == bank_id,
                Loan.status  == "active",
            )
        )).scalar_one_or_none()

        if not loan:
            return await update.message.reply_text(f"Aucun prêt actif à la {b['name']}.")

        u = await get_user(session, user.user_id)
        if not u or u.coins < amount:
            return await update.message.reply_text("Solde insuffisant !")

        pay_amount = min(amount, loan.remaining)
        u.coins        -= pay_amount
        loan.remaining -= pay_amount

        if loan.remaining <= 0:
            loan.status = "paid"
            msg_extra   = "\n✅ <b>Prêt entièrement remboursé !</b>"
        else:
            msg_extra   = f"\n💳 Reste à rembourser : {_fmt(loan.remaining)} $"

        await session.commit()
        new_wallet = u.coins

    await update.message.reply_text(
        f"🏦 Remboursement à la <b>{b['name']}</b>\n"
        f"💸 Payé : {_fmt(pay_amount)} $\n"
        f"👛 Portefeuille : {_fmt(new_wallet)} $"
        + msg_extra,
        parse_mode=ParseMode.HTML,
    )


# ─── /bankloans ───────────────────────────────────────────────────────────────

async def bankloans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Loan).where(Loan.user_id == user.user_id, Loan.status == "active")
        )
        loans = result.scalars().all()

    if not loans:
        return await update.message.reply_text("Aucun prêt actif en cours.")

    lines = [f"<b>💳 Prêts actifs de {update.effective_user.first_name}</b>\n"]
    total_debt = 0
    for loan in loans:
        b       = BANKS.get(loan.bank_id, {})
        overdue = " ⚠️ EN RETARD" if datetime.utcnow() > loan.due_at else ""
        lines.append(
            f"{b.get('emoji','🏦')} <b>{b.get('name', loan.bank_id)}</b>{overdue}\n"
            f"  └ Reste à payer : {_fmt(loan.remaining)} $\n"
            f"  └ Date limite   : {loan.due_at.strftime('%d/%m/%Y')}\n"
        )
        total_debt += loan.remaining

    lines.append(f"\n💸 Dette totale : <b>{_fmt(total_debt)} $</b>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ─── Job : versement des intérêts toutes les 6h ───────────────────────────────

async def pay_interests(context):
    """Appelé par le job_queue toutes les 6h. Verse les intérêts sur tous les comptes."""
    now = datetime.utcnow()
    paid_count = 0
    total_paid = 0

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(BankAccount))
        accounts = result.scalars().all()

        for acc in accounts:
            b = BANKS.get(acc.bank_id)
            if not b or acc.balance <= 0:
                continue

            last = acc.last_interest or acc.opened_at
            hours_elapsed = (now - last).total_seconds() / 3600

            if hours_elapsed >= INTEREST_INTERVAL_HOURS:
                interest = int(acc.balance * b["interest_rate"])
                acc.balance      += interest
                acc.last_interest = now
                total_paid += interest
                paid_count += 1

        # Vérifier les prêts en retard → pénalité
        loan_result = await session.execute(
            select(Loan).where(Loan.status == "active")
        )
        loans = loan_result.scalars().all()
        for loan in loans:
            if now > loan.due_at:
                # Pénalité de 5% du restant dû
                penalty = int(loan.remaining * 0.05)
                loan.remaining += penalty

        await session.commit()

    logger.info(f"[BANK] Intérêts versés : {paid_count} comptes, {total_paid:,} $ au total.")
