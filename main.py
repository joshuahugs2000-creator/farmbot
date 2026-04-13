import logging
from datetime import time, timedelta

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from config import BOT_TOKEN
from database import init_db

from handlers.misc     import start, help_cmd, leaderboard, mode, toggle
from handlers.family   import (
    marry, adopt, friend, divorce, disown, unfriend,
    setfamilyname, leave, familyphoto,
    request_callback, leave_callback,
)
from handlers.tree     import tree, bigtree
from handlers.garden   import garden, plant_cmd, harvest
from handlers.profile  import me, setpic, customize, color_callback, titles
from handlers.events   import check_anniversaries
from handlers.events_random import setup_random_events, open_chest_cmd
from handlers.economy  import (
    acc, daily, work, pay, richlist,
    blackjack, roulette, slots, des,
)
from handlers.race_bet import bet, race_bet_callback
from handlers.admin    import (
    adminhelp, give, take, setcoins, userinfo,
    ban, unban, resetuser,
    adminadd, adminremove, adminlist, broadcast,
    liberer, prisonlist, emprisonner,
    is_admin,
)
from handlers.bank     import (
    banks, bankopen, bankdeposit, bankwithdraw,
    bankbalance, bankloan, bankrepay, bankloans,
    pay_interests, remind_loans,
)
from handlers.invest   import market, buy, sell, portfolio
from handlers.couple   import couple
from handlers.lottery  import (
    createloto, loto, ticket, tirage, tirage_force, cancelloto,
    setup_lottery_jobs,
)
from handlers.crime    import (
    rob, police, bail, bail_judgment, juge, juge_callback,
    rebet, rebet_callback,
    security, security_callback,
    init_crime_tables,
    _is_in_prison, _get_prison, _fmt,
)
from database.db import AsyncSessionLocal

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Commandes exemptées du blocage prison ────────────────────────────────────
# Ces commandes restent accessibles même en prison
PRISON_EXEMPT_COMMANDS = {
    "start",
    "help",
    # /bail et /bail_judgment sont accessibles pour PAYER la caution de quelqu'un d'autre
    # mais pas pour se libérer soi-même (géré dans la fonction)
    "bail",
    "bail_judgment",
    # Les commandes admin restent accessibles pour les admins
    "adminhelp", "give", "take", "setcoins", "userinfo",
    "ban", "unban", "resetuser", "adminadd", "adminremove",
    "adminlist", "broadcast", "liberer", "prisonlist", "emprisonner",
}


async def prison_middleware(update: Update, context) -> bool:
    """
    Middleware global : bloque toutes les commandes si l'utilisateur est en prison.
    Retourne True si bloqué, False sinon.
    Appelé AVANT chaque handler de commande.
    """
    if not update.message or not update.message.text:
        return False

    user = update.effective_user
    if not user:
        return False

    # Extraire la commande (ex: "/daily" → "daily")
    text = update.message.text
    if not text.startswith("/"):
        return False

    command = text.split()[0].lstrip("/").split("@")[0].lower()

    # Les commandes exemptées passent toujours
    if command in PRISON_EXEMPT_COMMANDS:
        return False

    # Les admins ne sont jamais bloqués par la prison
    if await is_admin(user.id):
        return False

    # Vérifier si en prison
    try:
        async with AsyncSessionLocal() as session:
            from datetime import datetime
            prison_row_res = await session.execute(
                __import__("sqlalchemy").text("SELECT * FROM crime_prison WHERE user_id = :uid"),
                {"uid": user.id}
            )
            prison_row = prison_row_res.fetchone()

            if not prison_row:
                return False

            from datetime import datetime
            now = datetime.utcnow()
            if now >= prison_row.released_at:
                # Peine expirée → libérer automatiquement
                await session.execute(
                    __import__("sqlalchemy").text("DELETE FROM crime_prison WHERE user_id = :uid"),
                    {"uid": user.id}
                )
                await session.commit()
                return False

            # Bloqué !
            minutes_left = max(0, int((prison_row.released_at - now).total_seconds() / 60))
            h = minutes_left // 60
            m = minutes_left % 60
            duration_str = f"{h}h{m:02d}m" if h > 0 else f"{m} minute(s)"

            await update.message.reply_text(
                f"🔒 <b>Tu es en prison !</b>\n\n"
                f"La commande <code>/{command}</code> n'est pas disponible.\n"
                f"⏳ Libération dans : <b>{duration_str}</b>\n"
                f"💸 Caution : <b>{_fmt(prison_row.bail_amount)} 💰</b>\n\n"
                f"Demande à quelqu'un de payer ta caution avec <code>/bail @toi</code>",
                parse_mode="HTML"
            )
            return True

    except Exception as e:
        logger.error(f"Erreur prison_middleware: {e}")
        return False


async def on_startup(app: Application):
    await init_db()
    await init_crime_tables()
    logger.info("Base de données initialisée.")


async def error_handler(update: object, context):
    logger.error("Exception dans un handler :", exc_info=context.error)
    if isinstance(update, Update) and update.message:
        try:
            await update.message.reply_text(
                f"Erreur interne : {type(context.error).__name__}: {context.error}"
            )
        except Exception:
            pass


def _prison_checked(handler_func):
    """
    Décorateur qui vérifie la prison avant d'exécuter un handler de commande.
    """
    async def wrapper(update: Update, context):
        if await prison_middleware(update, context):
            return  # Bloqué par la prison
        return await handler_func(update, context)
    wrapper.__name__ = handler_func.__name__
    return wrapper


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )

    app.add_error_handler(error_handler)

    # ── Général ──────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("help",        help_cmd))
    app.add_handler(CommandHandler("leaderboard", _prison_checked(leaderboard)))
    app.add_handler(CommandHandler("mode",        _prison_checked(mode)))
    app.add_handler(CommandHandler("toggle",      _prison_checked(toggle)))

    # ── Famille ──────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("marry",         _prison_checked(marry)))
    app.add_handler(CommandHandler("adopt",         _prison_checked(adopt)))
    app.add_handler(CommandHandler("friend",        _prison_checked(friend)))
    app.add_handler(CommandHandler("divorce",       _prison_checked(divorce)))
    app.add_handler(CommandHandler("disown",        _prison_checked(disown)))
    app.add_handler(CommandHandler("unfriend",      _prison_checked(unfriend)))
    app.add_handler(CommandHandler("setfamilyname", _prison_checked(setfamilyname)))
    app.add_handler(CommandHandler("leave",         _prison_checked(leave)))
    app.add_handler(CommandHandler("familyphoto",   _prison_checked(familyphoto)))

    # ── Arbre ────────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("tree",    _prison_checked(tree)))
    app.add_handler(CommandHandler("bigtree", _prison_checked(bigtree)))

    # ── Jardin ───────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("garden",  _prison_checked(garden)))
    app.add_handler(CommandHandler("plant",   _prison_checked(plant_cmd)))
    app.add_handler(CommandHandler("harvest", _prison_checked(harvest)))

    # ── Profil ───────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("me",        _prison_checked(me)))
    app.add_handler(CommandHandler("setpic",    _prison_checked(setpic)))
    app.add_handler(CommandHandler("customize", _prison_checked(customize)))
    app.add_handler(CommandHandler("titles",    _prison_checked(titles)))

    # ── Économie ─────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("acc",        _prison_checked(acc)))
    app.add_handler(CommandHandler("daily",      _prison_checked(daily)))
    app.add_handler(CommandHandler("work",       _prison_checked(work)))
    app.add_handler(CommandHandler("pay",        _prison_checked(pay)))
    app.add_handler(CommandHandler("richlist",   _prison_checked(richlist)))
    app.add_handler(CommandHandler("blackjack",  _prison_checked(blackjack)))
    app.add_handler(CommandHandler("roulette",   _prison_checked(roulette)))
    app.add_handler(CommandHandler("slots",      _prison_checked(slots)))
    app.add_handler(CommandHandler("des",        _prison_checked(des)))
    app.add_handler(CommandHandler("bet",        _prison_checked(bet)))

    # ── Admin (jamais bloqués) ────────────────────────────────────────────────
    app.add_handler(CommandHandler("adminhelp",    adminhelp))
    app.add_handler(CommandHandler("give",         give))
    app.add_handler(CommandHandler("take",         take))
    app.add_handler(CommandHandler("setcoins",     setcoins))
    app.add_handler(CommandHandler("userinfo",     userinfo))
    app.add_handler(CommandHandler("ban",          ban))
    app.add_handler(CommandHandler("unban",        unban))
    app.add_handler(CommandHandler("resetuser",    resetuser))
    app.add_handler(CommandHandler("adminadd",     adminadd))
    app.add_handler(CommandHandler("adminremove",  adminremove))
    app.add_handler(CommandHandler("adminlist",    adminlist))
    app.add_handler(CommandHandler("broadcast",    broadcast))
    app.add_handler(CommandHandler("liberer",        liberer))
    app.add_handler(CommandHandler("prisonlist",     prisonlist))
    app.add_handler(CommandHandler("emprisonner",    emprisonner))

    # ── Banque ────────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("banks",        _prison_checked(banks)))
    app.add_handler(CommandHandler("bankopen",     _prison_checked(bankopen)))
    app.add_handler(CommandHandler("bankdeposit",  _prison_checked(bankdeposit)))
    app.add_handler(CommandHandler("bankwithdraw", _prison_checked(bankwithdraw)))
    app.add_handler(CommandHandler("bankbalance",  _prison_checked(bankbalance)))
    app.add_handler(CommandHandler("bankloan",     _prison_checked(bankloan)))
    app.add_handler(CommandHandler("bankrepay",    _prison_checked(bankrepay)))
    app.add_handler(CommandHandler("bankloans",    _prison_checked(bankloans)))

    # ── Investissements ───────────────────────────────────────────────────────
    app.add_handler(CommandHandler("market",    _prison_checked(market)))
    app.add_handler(CommandHandler("buy",       _prison_checked(buy)))
    app.add_handler(CommandHandler("sell",      _prison_checked(sell)))
    app.add_handler(CommandHandler("portfolio", _prison_checked(portfolio)))

    # ── Couple ────────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("couple",    _prison_checked(couple)))

    # ── Loterie ───────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("createloto",   _prison_checked(createloto)))
    app.add_handler(CommandHandler("loto",         _prison_checked(loto)))
    app.add_handler(CommandHandler("ticket",       _prison_checked(ticket)))
    app.add_handler(CommandHandler("tirage",       _prison_checked(tirage)))
    app.add_handler(CommandHandler("tirageforce",  _prison_checked(tirage_force)))
    app.add_handler(CommandHandler("cancelloto",   _prison_checked(cancelloto)))

    # ── Criminalité ───────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("rob",           _prison_checked(rob)))
    app.add_handler(CommandHandler("police",        _prison_checked(police)))
    app.add_handler(CommandHandler("bail",          bail))           # exempt : payer pour autrui
    app.add_handler(CommandHandler("bail_judgment", bail_judgment))  # exempt : payer pour autrui
    app.add_handler(CommandHandler("juge",          _prison_checked(juge)))
    app.add_handler(CommandHandler("rebet",         _prison_checked(rebet)))
    app.add_handler(CommandHandler("security",      _prison_checked(security)))

    # ── Événements aléatoires ─────────────────────────────────────────────────
    app.add_handler(CommandHandler("open", _prison_checked(open_chest_cmd)))
    setup_random_events(app)

    # ── Callbacks ─────────────────────────────────────────────────────────────
    app.add_handler(CallbackQueryHandler(request_callback,  pattern=r"^req:"))
    app.add_handler(CallbackQueryHandler(leave_callback,    pattern=r"^leave:"))
    app.add_handler(CallbackQueryHandler(color_callback,    pattern=r"^color:"))
    app.add_handler(CallbackQueryHandler(race_bet_callback, pattern=r"^rb:"))
    app.add_handler(CallbackQueryHandler(juge_callback,     pattern=r"^juge:"))
    app.add_handler(CallbackQueryHandler(rebet_callback,    pattern=r"^rebet:"))
    app.add_handler(CallbackQueryHandler(security_callback, pattern=r"^sec:"))

    # ── Jobs périodiques ──────────────────────────────────────────────────────
    app.job_queue.run_daily(
        check_anniversaries,
        time=time(hour=8, minute=0),
        name="anniversary_check",
    )
    # Intérêts bancaires : toutes les 6h
    app.job_queue.run_repeating(
        pay_interests,
        interval=timedelta(hours=6),
        first=timedelta(minutes=5),
        name="bank_interests",
    )
    # Rappels remboursement : 2x par semaine (toutes les 84h)
    app.job_queue.run_repeating(
        remind_loans,
        interval=timedelta(hours=84),
        first=timedelta(minutes=10),
        name="loan_reminders",
    )

    # ── Loterie Bot ───────────────────────────────────────────────────────────
    setup_lottery_jobs(app)

    logger.info("Bot demarre.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
