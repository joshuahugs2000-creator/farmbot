"""
Système de Loterie 🎰
─────────────────────────────────────────────────────────────
LOTERIE PRIVÉE  (créée par un user)
  /createloto <prix>  — lancer une loterie (prix min 1 000 $)
  /ticket [nb]        — acheter des tickets (défaut : 1)
  /loto               — voir la loterie active du groupe
  /tirage             — forcer le tirage (créateur OU admin)
  /cancelloto         — annuler la loterie (créateur OU admin)

LOTERIE BOT  (automatique)
  Le bot crée une loterie chaque jour avec un prix aléatoire.
  Tirage automatique à 20h00 GMT.
  /tirageforcé        — forcer le tirage bot (admin uniquement)
─────────────────────────────────────────────────────────────
"""

import random
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes, Application
from telegram.constants import ParseMode
from sqlalchemy import select

from database.db import AsyncSessionLocal, get_user
from database.models import LotterySession, LotteryTicket
from utils.helpers import ensure_user, is_group, mention
from handlers.admin import is_admin

logger = logging.getLogger(__name__)

MIN_TICKET_PRICE = 1_000
MAX_TICKET_PRICE = 5_000
MAX_TICKETS_PER_USER = 20
BOT_WIN_SHARE = 0.80   # 80 % de la cagnotte au gagnant


# ─── helpers ──────────────────────────────────────────────────────────────────

async def _active_session(session, group_id: int) -> LotterySession | None:
    r = await session.execute(
        select(LotterySession).where(
            LotterySession.group_id == group_id,
            LotterySession.status == "active",
        )
    )
    return r.scalar_one_or_none()


async def _ticket_count(session, session_id: int, user_id: int | None = None) -> int:
    from sqlalchemy import func
    conds = [LotteryTicket.session_id == session_id]
    if user_id is not None:
        conds.append(LotteryTicket.user_id == user_id)
    r = await session.execute(select(func.count()).where(*conds))
    return r.scalar() or 0


async def _do_draw(bot, loto: LotterySession, session) -> dict | None:
    """Effectue le tirage. Retourne un dict avec les infos ou None si pas de tickets."""
    r = await session.execute(
        select(LotteryTicket).where(LotteryTicket.session_id == loto.id)
    )
    tickets = r.scalars().all()
    if not tickets:
        loto.status = "closed"
        loto.drawn_at = datetime.utcnow()
        await session.commit()
        return None

    winner_ticket = random.choice(tickets)
    winner = await get_user(session, winner_ticket.user_id)
    prize = int(loto.pot * BOT_WIN_SHARE)

    if winner:
        winner.coins += prize

    loto.status    = "closed"
    loto.winner_id = winner_ticket.user_id
    loto.drawn_at  = datetime.utcnow()
    await session.commit()

    total_tickets = len(tickets)
    participants  = len(set(t.user_id for t in tickets))

    return {
        "winner":        winner,
        "prize":         prize,
        "pot":           loto.pot,
        "total_tickets": total_tickets,
        "participants":  participants,
        "ticket_price":  loto.ticket_price,
    }


def _loto_summary(loto: LotterySession, total: int, participants: int, my_tickets: int = 0) -> str:
    loto_type = "🤖 Bot" if loto.loto_type == "bot" else "👤 Privée"
    jackpot   = int(loto.pot * BOT_WIN_SHARE)
    lines = [
        f"🎰 <b>Loterie active</b> — {loto_type}",
        f"🏷️ Prix du ticket  : <b>{loto.ticket_price:,} $</b>",
        f"🎟️ Tickets vendus  : <b>{total}</b>",
        f"👥 Participants    : <b>{participants}</b>",
        f"💰 Jackpot (80 %) : <b>{jackpot:,} $</b>",
    ]
    if my_tickets:
        lines.append(f"🃏 Tes tickets     : <b>{my_tickets}</b>")
    if loto.loto_type == "bot":
        lines.append("⏰ Tirage à <b>20h00 GMT</b>")
    else:
        lines.append("⚡ Le créateur peut lancer le tirage à tout moment.")
    lines.append("➡️ Acheter des tickets : /ticket [nb]")
    return "\n".join(lines)


# ─── /createloto ──────────────────────────────────────────────────────────────

async def createloto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return await update.message.reply_text("Commande de groupe uniquement.")

    if not context.args:
        return await update.message.reply_text(
            f"Usage : /createloto <prix>\nPrix minimum : {MIN_TICKET_PRICE:,} $"
        )

    try:
        price = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("Le prix doit être un nombre entier.")

    if price < MIN_TICKET_PRICE:
        return await update.message.reply_text(
            f"❌ Prix minimum : {MIN_TICKET_PRICE:,} $"
        )

    user     = await ensure_user(update.effective_user)
    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        existing = await _active_session(session, group_id)
        if existing:
            return await update.message.reply_text(
                "⚠️ Il y a déjà une loterie active dans ce groupe. "
                "Fais /loto pour voir les infos ou /cancelloto pour l'annuler."
            )

        loto = LotterySession(
            group_id=group_id,
            creator_id=user.user_id,
            ticket_price=price,
            loto_type="private",
            pot=0,
        )
        session.add(loto)
        await session.commit()

    await update.message.reply_text(
        f"🎰 {mention(user)} a lancé une <b>Loterie Privée</b> !\n"
        f"🏷️ Prix d'un ticket : <b>{price:,} $</b>\n"
        f"🎟️ Achetez vos tickets : /ticket [nb]\n"
        f"⚡ Le créateur peut forcer le tirage avec /tirage.",
        parse_mode=ParseMode.HTML,
    )


# ─── /loto ────────────────────────────────────────────────────────────────────

async def loto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return await update.message.reply_text("Commande de groupe uniquement.")

    group_id = update.effective_chat.id
    user     = await ensure_user(update.effective_user)

    async with AsyncSessionLocal() as session:
        loto_session = await _active_session(session, group_id)
        if not loto_session:
            return await update.message.reply_text(
                "Aucune loterie active.\n"
                "Lance une loterie avec /createloto <prix>"
            )
        total      = await _ticket_count(session, loto_session.id)
        parts      = len(set(
            t.user_id for t in (
                await session.execute(select(LotteryTicket).where(LotteryTicket.session_id == loto_session.id))
            ).scalars().all()
        ))
        my_tickets = await _ticket_count(session, loto_session.id, user.user_id)

    await update.message.reply_text(
        _loto_summary(loto_session, total, parts, my_tickets),
        parse_mode=ParseMode.HTML,
    )


# ─── /ticket ──────────────────────────────────────────────────────────────────

async def ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return await update.message.reply_text("Commande de groupe uniquement.")

    nb = 1
    if context.args:
        try:
            nb = int(context.args[0])
            if nb < 1:
                raise ValueError
        except ValueError:
            return await update.message.reply_text("Nombre de tickets invalide.")

    user     = await ensure_user(update.effective_user)
    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        loto_session = await _active_session(session, group_id)
        if not loto_session:
            return await update.message.reply_text(
                "Aucune loterie active dans ce groupe."
            )

        already = await _ticket_count(session, loto_session.id, user.user_id)
        if already + nb > MAX_TICKETS_PER_USER:
            return await update.message.reply_text(
                f"⚠️ Tu ne peux pas dépasser {MAX_TICKETS_PER_USER} tickets par loterie.\n"
                f"Tu en as déjà {already}."
            )

        total_cost = loto_session.ticket_price * nb
        u = await get_user(session, user.user_id)
        if not u or u.coins < total_cost:
            have = u.coins if u else 0
            return await update.message.reply_text(
                f"❌ Pas assez de coins.\n"
                f"Coût : {total_cost:,} $ | Solde : {have:,} $"
            )

        u.coins          -= total_cost
        loto_session.pot += total_cost

        for _ in range(nb):
            session.add(LotteryTicket(session_id=loto_session.id, user_id=user.user_id))
        await session.commit()

        total      = await _ticket_count(session, loto_session.id)
        jackpot    = int(loto_session.pot * BOT_WIN_SHARE)

    await update.message.reply_text(
        f"🎟️ {mention(user)} a acheté <b>{nb}</b> ticket(s) !\n"
        f"Tu en as maintenant <b>{already + nb}</b>.\n"
        f"💰 Jackpot actuel : <b>{jackpot:,} $</b>\n"
        f"🎰 Bonne chance !",
        parse_mode=ParseMode.HTML,
    )


# ─── /tirage ──────────────────────────────────────────────────────────────────

async def tirage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forcer le tirage d'une loterie PRIVÉE (créateur ou admin)."""
    if not is_group(update):
        return await update.message.reply_text("Commande de groupe uniquement.")

    user     = await ensure_user(update.effective_user)
    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        loto_session = await _active_session(session, group_id)
        if not loto_session:
            return await update.message.reply_text("Aucune loterie active.")

        if loto_session.loto_type == "bot":
            return await update.message.reply_text(
                "Cette loterie est gérée par le bot.\n"
                "Seul un admin peut forcer le tirage avec /tirageforcé."
            )

        is_creator = loto_session.creator_id == user.user_id
        if not is_creator and not await is_admin(user.user_id):
            return await update.message.reply_text(
                "⛔ Seul le créateur de la loterie ou un admin peut forcer le tirage."
            )

        result = await _do_draw(context.bot, loto_session, session)

    await _send_result(context.bot, group_id, result)


# ─── /tirageforcé ─────────────────────────────────────────────────────────────

async def tirage_force(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forcer le tirage de la loterie BOT (admin uniquement)."""
    if not await is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ Réservé aux admins.")

    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        loto_session = await _active_session(session, group_id)
        if not loto_session:
            return await update.message.reply_text("Aucune loterie bot active.")
        result = await _do_draw(context.bot, loto_session, session)

    await _send_result(context.bot, group_id, result)


# ─── /cancelloto ──────────────────────────────────────────────────────────────

async def cancelloto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user     = await ensure_user(update.effective_user)
    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        loto_session = await _active_session(session, group_id)
        if not loto_session:
            return await update.message.reply_text("Aucune loterie active.")

        is_creator = loto_session.creator_id == user.user_id
        if not is_creator and not await is_admin(user.user_id):
            return await update.message.reply_text("⛔ Seul le créateur ou un admin peut annuler.")

        # Rembourser tous les participants
        tickets_r = await session.execute(
            select(LotteryTicket).where(LotteryTicket.session_id == loto_session.id)
        )
        tickets = tickets_r.scalars().all()
        for t in tickets:
            u = await get_user(session, t.user_id)
            if u:
                u.coins += loto_session.ticket_price

        loto_session.status = "closed"
        await session.commit()

    await update.message.reply_text(
        "❌ Loterie annulée. Tous les participants ont été remboursés.",
        parse_mode=ParseMode.HTML,
    )


# ─── Envoi du résultat ────────────────────────────────────────────────────────

async def _send_result(bot, group_id: int, result: dict | None):
    if result is None:
        await bot.send_message(
            chat_id=group_id,
            text="🎰 Tirage effectué — Aucun participant. La loterie est terminée.",
        )
        return

    winner = result["winner"]
    name   = winner.first_name if winner else "Inconnu"
    uid    = winner.user_id if winner else 0

    await bot.send_message(
        chat_id=group_id,
        text=(
            f"🎉 <b>TIRAGE DE LA LOTERIE !</b>\n\n"
            f"🎟️ Tickets vendus   : <b>{result['total_tickets']}</b>\n"
            f"👥 Participants     : <b>{result['participants']}</b>\n"
            f"💸 Cagnotte totale  : <b>{result['pot']:,} $</b>\n\n"
            f"🏆 GAGNANT : <a href='tg://user?id={uid}'>{name}</a>\n"
            f"💰 Gain             : <b>{result['prize']:,} $</b>"
        ),
        parse_mode=ParseMode.HTML,
    )


# ─── Job automatique — Loterie Bot ────────────────────────────────────────────

async def daily_bot_lottery_create(context: ContextTypes.DEFAULT_TYPE):
    """Crée une nouvelle loterie bot dans tous les groupes actifs (à lancer à minuit)."""
    from sqlalchemy import select as _sel
    from database.models import GroupSettings

    async with AsyncSessionLocal() as session:
        groups_r = await session.execute(_sel(GroupSettings))
        groups   = groups_r.scalars().all()

        for g in groups:
            existing = await _active_session(session, g.group_id)
            if existing:
                continue   # déjà une loterie active

            price = random.randint(MIN_TICKET_PRICE, MAX_TICKET_PRICE)
            loto  = LotterySession(
                group_id=g.group_id,
                creator_id=None,
                ticket_price=price,
                loto_type="bot",
                pot=0,
            )
            session.add(loto)

        await session.commit()


async def daily_bot_lottery_draw(context: ContextTypes.DEFAULT_TYPE):
    """Effectue le tirage de la loterie bot dans tous les groupes (à lancer à 20h GMT)."""
    from sqlalchemy import select as _sel
    from database.models import GroupSettings

    async with AsyncSessionLocal() as session:
        groups_r = await session.execute(_sel(GroupSettings))
        groups   = groups_r.scalars().all()

        for g in groups:
            loto_session = await _active_session(session, g.group_id)
            if not loto_session or loto_session.loto_type != "bot":
                continue
            result = await _do_draw(context.bot, loto_session, session)
            try:
                await _send_result(context.bot, g.group_id, result)
            except Exception as e:
                logger.warning(f"Impossible d'envoyer résultat loterie groupe {g.group_id}: {e}")


def setup_lottery_jobs(app: Application):
    from datetime import time as dtime
    # Création de la loterie bot chaque jour à 00h01 GMT
    app.job_queue.run_daily(
        daily_bot_lottery_create,
        time=dtime(hour=0, minute=1, tzinfo=timezone.utc),
        name="loto_bot_create",
    )
    # Tirage chaque jour à 20h00 GMT
    app.job_queue.run_daily(
        daily_bot_lottery_draw,
        time=dtime(hour=20, minute=0, tzinfo=timezone.utc),
        name="loto_bot_draw",
    )
