import logging
from datetime import time, timedelta

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
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
from handlers.waifu    import waifu, upvote, downvote
from handlers.profile  import me, setpic, customize, color_callback, titles
from handlers.events   import check_anniversaries
from handlers.economy  import (
    acc, daily, work, pay, richlist,
    blackjack, roulette, slots,
)
from handlers.race_bet import bet, race_bet_callback
from handlers.admin    import (
    adminhelp, give, take, setcoins, userinfo,
    ban, unban, resetuser,
    adminadd, adminremove, adminlist, broadcast,
)
from handlers.bank     import (
    banks, bankopen, bankdeposit, bankwithdraw,
    bankbalance, bankloan, bankrepay, bankloans,
    pay_interests,
)
from handlers.invest   import market, buy, sell, portfolio
from handlers.couple   import couple

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def on_startup(app: Application):
    await init_db()
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
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("mode",        mode))
    app.add_handler(CommandHandler("toggle",      toggle))

    # ── Famille ──────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("marry",         marry))
    app.add_handler(CommandHandler("adopt",         adopt))
    app.add_handler(CommandHandler("friend",        friend))
    app.add_handler(CommandHandler("divorce",       divorce))
    app.add_handler(CommandHandler("disown",        disown))
    app.add_handler(CommandHandler("unfriend",      unfriend))
    app.add_handler(CommandHandler("setfamilyname", setfamilyname))
    app.add_handler(CommandHandler("leave",         leave))
    app.add_handler(CommandHandler("familyphoto",   familyphoto))

    # ── Arbre ────────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("tree",    tree))
    app.add_handler(CommandHandler("bigtree", bigtree))

    # ── Jardin ───────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("garden",  garden))
    app.add_handler(CommandHandler("plant",   plant_cmd))
    app.add_handler(CommandHandler("harvest", harvest))

    # ── Waifu & Karma ─────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("waifu",    waifu))
    app.add_handler(CommandHandler("upvote",   upvote))
    app.add_handler(CommandHandler("downvote", downvote))

    # ── Profil ───────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("me",        me))
    app.add_handler(CommandHandler("setpic",    setpic))
    app.add_handler(CommandHandler("customize", customize))
    app.add_handler(CommandHandler("titles",    titles))

    # ── Économie ─────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("acc",        acc))
    app.add_handler(CommandHandler("daily",      daily))
    app.add_handler(CommandHandler("work",       work))
    app.add_handler(CommandHandler("pay",        pay))
    app.add_handler(CommandHandler("richlist",   richlist))
    app.add_handler(CommandHandler("blackjack",  blackjack))
    app.add_handler(CommandHandler("roulette",   roulette))
    app.add_handler(CommandHandler("slots",      slots))
    app.add_handler(CommandHandler("bet",        bet))

    # ── Admin ─────────────────────────────────────────────────────────────────
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

    # ── Banque ────────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("banks",        banks))
    app.add_handler(CommandHandler("bankopen",     bankopen))
    app.add_handler(CommandHandler("bankdeposit",  bankdeposit))
    app.add_handler(CommandHandler("bankwithdraw", bankwithdraw))
    app.add_handler(CommandHandler("bankbalance",  bankbalance))
    app.add_handler(CommandHandler("bankloan",     bankloan))
    app.add_handler(CommandHandler("bankrepay",    bankrepay))
    app.add_handler(CommandHandler("bankloans",    bankloans))

    # ── Investissements ───────────────────────────────────────────────────────
    app.add_handler(CommandHandler("market",    market))
    app.add_handler(CommandHandler("buy",       buy))
    app.add_handler(CommandHandler("sell",      sell))
    app.add_handler(CommandHandler("portfolio", portfolio))

    # ── Couple ────────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("couple",    couple))

    # ── Callbacks ─────────────────────────────────────────────────────────────
    app.add_handler(CallbackQueryHandler(request_callback,  pattern=r"^req:"))
    app.add_handler(CallbackQueryHandler(leave_callback,    pattern=r"^leave:"))
    app.add_handler(CallbackQueryHandler(color_callback,    pattern=r"^color:"))
    app.add_handler(CallbackQueryHandler(race_bet_callback, pattern=r"^rb:"))

    # ── Jobs périodiques ──────────────────────────────────────────────────────
    # Anniversaires : 1x/jour à 8h
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

    logger.info("Bot demarre.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
