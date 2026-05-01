import logging
import os
import asyncio
from datetime import time, timedelta

from aiohttp import web
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    CallbackQueryHandler,
    TypeHandler,
)

from config import BOT_TOKEN
from database import init_db

from handlers.misc     import start, help_cmd, leaderboard, mode, toggle
from handlers.family   import (
    marry, adopt, friend, divorce, disown, unfriend,
    setfamilyname, leave, familyphoto,
    request_callback, leave_callback,
)
from handlers.tree     import tree
from handlers.garden   import garden, plant_cmd, harvest
from handlers.profile  import me, setpic, customize, color_callback, titles, karmainfo
from handlers.events   import check_anniversaries
from handlers.events_random import setup_random_events, open_chest_cmd
from handlers.economy  import (
    acc, daily, work, pay, richlist,
    blackjack, roulette, slots,
)
from handlers.games import (
    crash_cmd, crash_callback,
    apple_cmd, apple_callback,
    roue_cmd,
    rebet_cmd, rebet_callback,
    setmood_cmd,
    mood_facile_cmd, mood_normal_cmd, mood_difficile_cmd, mood_impitoyable_cmd, mood_auto_cmd,
    mines_cmd, mines_callback,
)
from handlers.arena import (
    cockfight_cmd, cockfight_callback,
    ppc_cmd, ppc_callback,
    lancer_cmd, lancer_callback,
)
from handlers.admin    import (
    adminhelp, give, take, setcoins, userinfo,
    ban, unban, resetuser,
    adminadd, adminremove, adminlist, userlist, broadcast,
    liberer, prisonlist, emprisonner,
    is_admin, pause, resume,
    giveportfolio, takeportfolio, marketlist,
    useractivity,
    enquete,
    richlista,
    logs_cmd, suspicious_cmd, grouplist_cmd, groupscan_cmd,
    fin, donate,
)
from handlers.bank     import (
    banks, bankopen, bankdeposit, bankwithdraw,
    bankbalance, bankloan, bankrepay, bankloans,
    pay_interests, remind_loans,
)
from handlers.invest   import market, market_callback, buy, sell, portfolio
from handlers.crime    import (
    police, bail, bail_judgment, juge, juge_callback,
    security, security_callback,
    init_crime_tables,
    _is_in_prison, _get_prison, _fmt,
)
from handlers.auction import (
    bid, expertise, expertise_callback, auction_callback,
    myitems, sellitem, shopitems, buyitem,
    init_auction_tables, setup_auction_jobs,
)
from handlers.wealth_drain import (
    impots, cambrioler,
    init_drain_tables, setup_drain_jobs, _ensure_cambriolage_cd_table,
    job_tax_top10, job_tax_top30,
)
from handlers.drames import drame, setdramesesuil
from handlers.article import article_cmd
from handlers.diplome import diplome_cmd, diplome_callback
from handlers.journal import init_journal_table, setup_journal_jobs, testjournal_cmd
from database.db import AsyncSessionLocal, log_action, init_logs_table, upsert_group, mark_group_inactive, init_groups_table

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

WEBHOOK_URL = "https://farmbot-77xl.onrender.com"
PORT = int(os.environ.get("PORT", 8080))

PRISON_EXEMPT_COMMANDS = {
    "start", "help", "bail", "bail_judgment",
    "adminhelp", "give", "take", "setcoins", "userinfo",
    "ban", "unban", "resetuser", "adminadd", "adminremove",
    "adminlist", "userlist", "broadcast", "liberer", "prisonlist", "emprisonner",
    "pause", "resume", "enquete", "richlista",
    "fin", "donate",
}

# Commandes accessibles même en étant banni (commandes admin uniquement)
BAN_EXEMPT_COMMANDS = {
    "ban", "unban", "adminhelp", "give", "take", "setcoins", "userinfo",
    "adminadd", "adminremove", "adminlist", "userlist", "broadcast",
    "liberer", "prisonlist", "emprisonner", "pause", "resume",
    "enquete", "richlista", "logs", "suspicious", "grouplist", "groupscan",
    "fin", "donate", "resetuser", "useractivity",
}


async def ban_middleware(update: Update, context) -> None:
    """Bloque toutes les interactions des utilisateurs bannis."""
    user = update.effective_user
    if not user:
        return
    try:
        async with AsyncSessionLocal() as _s:
            from database.db import get_user as _gu
            _u = await _gu(_s, user.id)
            if not _u or not _u.is_banned:
                return
        # L'utilisateur est banni — vérifier s'il est admin
        if await is_admin(user.id):
            return
        # Laisser passer les commandes admin
        if update.message and update.message.text:
            cmd = update.message.text.split()[0].lstrip("/").split("@")[0].lower()
            if cmd in BAN_EXEMPT_COMMANDS:
                return
        # Ignorer silencieusement — aucune réponse, aucun tag
        if update.callback_query:
            await update.callback_query.answer()  # acquitter sans message visible
        raise ApplicationHandlerStop()
    except ApplicationHandlerStop:
        raise
    except Exception:
        pass


async def prison_middleware(update: Update, context) -> bool:
    if not update.message or not update.message.text:
        return False
    user = update.effective_user
    if not user:
        return False
    text = update.message.text
    if not text.startswith("/"):
        return False
    command = text.split()[0].lstrip("/").split("@")[0].lower()
    if command in PRISON_EXEMPT_COMMANDS:
        return False
    if await is_admin(user.id):
        return False
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
            now = datetime.utcnow()
            if now >= prison_row.released_at:
                await session.execute(
                    __import__("sqlalchemy").text("DELETE FROM crime_prison WHERE user_id = :uid"),
                    {"uid": user.id}
                )
                await session.commit()
                return False
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





async def group_tracking_middleware(update: Update, context) -> None:
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup", "channel"):
        return
    try:
        invite_link  = None
        username     = chat.username
        if not username:
            try:
                invite_link = await context.bot.export_chat_invite_link(chat.id)
            except Exception:
                pass
        try:
            member_count = await context.bot.get_chat_member_count(chat.id)
        except Exception:
            member_count = None
        await upsert_group(group_id=chat.id, title=chat.title or "Sans nom",
                           username=username, chat_type=chat.type,
                           member_count=member_count, invite_link=invite_link)
    except Exception as e:
        logger.debug(f"group_tracking_middleware error: {e}")


async def my_chat_member_handler(update: Update, context) -> None:
    from telegram import ChatMemberLeft, ChatMemberBanned
    result = update.my_chat_member
    if not result:
        return
    chat = result.chat
    new  = result.new_chat_member
    if chat.type not in ("group", "supergroup", "channel"):
        return
    try:
        if isinstance(new, (ChatMemberLeft, ChatMemberBanned)):
            await mark_group_inactive(chat.id)
        else:
            invite_link  = None
            username     = chat.username
            if not username:
                try:
                    invite_link = await context.bot.export_chat_invite_link(chat.id)
                except Exception:
                    pass
            try:
                member_count = await context.bot.get_chat_member_count(chat.id)
            except Exception:
                member_count = None
            await upsert_group(group_id=chat.id, title=chat.title or "Sans nom",
                               username=username, chat_type=chat.type,
                               member_count=member_count, invite_link=invite_link)
    except Exception as e:
        logger.error(f"my_chat_member_handler error: {e}")


async def activity_logging_middleware(update: Update, context) -> None:
    """Logue automatiquement chaque commande utilisée."""
    if not update.message or not update.message.text:
        return
    user = update.effective_user
    if not user:
        return
    txt = update.message.text
    if not txt.startswith("/"):
        return
    parts   = txt.split()
    command = parts[0].lstrip("/").split("@")[0].lower()
    args    = " ".join(parts[1:]) if len(parts) > 1 else None
    group_id = update.effective_chat.id if update.effective_chat else None
    # Extraire un montant si possible (premier arg numérique)
    amount = None
    for p in parts[1:]:
        try:
            amount = int(p.replace("_", "").replace(" ", ""))
            break
        except ValueError:
            pass
    try:
        async with AsyncSessionLocal() as session:
            await log_action(
                session,
                user_id  = user.id,
                username = user.username or user.first_name,
                command  = command,
                args     = args,
                amount   = amount,
                group_id = group_id,
            )
    except Exception as e:
        logger.debug(f"Erreur log_action: {e}")

async def on_startup(application: Application):
    await init_db()
    await init_logs_table()
    await init_groups_table()   # ← crée activity_logs si elle n'existe pas
    await init_journal_table()
    await init_crime_tables()
    await init_drain_tables()
    await _ensure_cambriolage_cd_table()
    await init_auction_tables()
    logger.info("Base de données initialisée.")


async def error_handler(update: object, context):
    logger.error("Exception dans un handler :", exc_info=context.error)
    if isinstance(update, Update) and update.message:
        try:
            await update.message.reply_text(
                "⚠️ Une erreur s'est produite. Réessaie dans quelques instants."
            )
        except Exception:
            pass


def _prison_checked(handler_func):
    async def wrapper(update: Update, context):
        import handlers.admin as _admin_mod
        if _admin_mod.BOT_PAUSED:
            if not await is_admin(update.effective_user.id):
                await update.message.reply_text(
                    "⏸️ <b>Le bot est actuellement en pause.</b>\nRevenez plus tard !",
                    parse_mode="HTML",
                )
                return
        if await prison_middleware(update, context):
            return
        return await handler_func(update, context)
    wrapper.__name__ = handler_func.__name__
    return wrapper


async def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .updater(None)
        .post_init(on_startup)
        .build()
    )

    app.add_error_handler(error_handler)

    # ── Middleware de logging automatique ─────────────────────────────────────
    app.add_handler(TypeHandler(Update, activity_logging_middleware), group=-1)
    app.add_handler(TypeHandler(Update, group_tracking_middleware),   group=-2)
    app.add_handler(TypeHandler(Update, ban_middleware),              group=-3)
    from telegram.ext import ChatMemberHandler
    app.add_handler(ChatMemberHandler(my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER))

    # ── Général ───────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("help",        help_cmd))
    app.add_handler(CommandHandler("leaderboard", _prison_checked(leaderboard)))
    app.add_handler(CommandHandler("mode",        _prison_checked(mode)))
    app.add_handler(CommandHandler("toggle",      _prison_checked(toggle)))

    # ── Famille ───────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("marry",         _prison_checked(marry)))
    app.add_handler(CommandHandler("adopt",         _prison_checked(adopt)))
    app.add_handler(CommandHandler("friend",        _prison_checked(friend)))
    app.add_handler(CommandHandler("divorce",       _prison_checked(divorce)))
    app.add_handler(CommandHandler("disown",        _prison_checked(disown)))
    app.add_handler(CommandHandler("unfriend",      _prison_checked(unfriend)))
    app.add_handler(CommandHandler("setfamilyname", _prison_checked(setfamilyname)))
    app.add_handler(CommandHandler("leave",         _prison_checked(leave)))
    app.add_handler(CommandHandler("familyphoto",   _prison_checked(familyphoto)))

    # ── Arbre ─────────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("tree",    _prison_checked(tree)))

    # ── Jardin ────────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("garden",  _prison_checked(garden)))
    app.add_handler(CommandHandler("plant",   _prison_checked(plant_cmd)))
    app.add_handler(CommandHandler("harvest", _prison_checked(harvest)))

    # ── Profil ────────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("me",        _prison_checked(me)))
    app.add_handler(CommandHandler("setpic",    _prison_checked(setpic)))
    app.add_handler(CommandHandler("customize", _prison_checked(customize)))
    app.add_handler(CommandHandler("titles",    _prison_checked(titles)))
    app.add_handler(CommandHandler("karmainfo", karmainfo))

    # ── Économie ──────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("acc",        _prison_checked(acc)))
    app.add_handler(CommandHandler("daily",      _prison_checked(daily)))
    app.add_handler(CommandHandler("work",       _prison_checked(work)))
    app.add_handler(CommandHandler("pay",        _prison_checked(pay)))
    app.add_handler(CommandHandler("richlist",   _prison_checked(richlist)))
    app.add_handler(CommandHandler("blackjack",  _prison_checked(blackjack)))
    app.add_handler(CommandHandler("roulette",   _prison_checked(roulette)))
    app.add_handler(CommandHandler("slots",      _prison_checked(slots)))

    # ── Jeux ──────────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("crash",  _prison_checked(crash_cmd)))
    app.add_handler(CommandHandler("apple",  _prison_checked(apple_cmd)))
    app.add_handler(CommandHandler("roue",     _prison_checked(roue_cmd)))
    app.add_handler(CommandHandler("setmood",      setmood_cmd))
    app.add_handler(CommandHandler("facile",       mood_facile_cmd))
    app.add_handler(CommandHandler("normal",       mood_normal_cmd))
    app.add_handler(CommandHandler("difficile",    mood_difficile_cmd))
    app.add_handler(CommandHandler("impitoyable",  mood_impitoyable_cmd))
    app.add_handler(CommandHandler("moodauto",     mood_auto_cmd))
    app.add_handler(CommandHandler("rebet",  _prison_checked(rebet_cmd)))

    # ── Arène PvP ─────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("cockfight", _prison_checked(cockfight_cmd)))
    app.add_handler(CommandHandler("ppc",       _prison_checked(ppc_cmd)))
    app.add_handler(CommandHandler("lancer",    _prison_checked(lancer_cmd)))

    # ── Jeux ──────────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("mines",     _prison_checked(mines_cmd)))

    app.add_handler(CallbackQueryHandler(crash_callback,      pattern=r"^crash:"))
    app.add_handler(CallbackQueryHandler(apple_callback,      pattern=r"^apple:"))
    app.add_handler(CallbackQueryHandler(rebet_callback,      pattern=r"^rebet:"))
    app.add_handler(CallbackQueryHandler(cockfight_callback,  pattern=r"^cf:"))
    app.add_handler(CallbackQueryHandler(ppc_callback,        pattern=r"^ppc:"))
    app.add_handler(CallbackQueryHandler(lancer_callback,     pattern=r"^lancer:"))
    app.add_handler(CallbackQueryHandler(mines_callback,      pattern=r"^mines:"))
    app.add_handler(CallbackQueryHandler(market_callback,     pattern=r"^mkt:"))

    # ── Admin ─────────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("adminhelp",      adminhelp))
    app.add_handler(CommandHandler("give",           give))
    app.add_handler(CommandHandler("take",           take))
    app.add_handler(CommandHandler("setcoins",       setcoins))
    app.add_handler(CommandHandler("userinfo",       userinfo))
    app.add_handler(CommandHandler("ban",            ban))
    app.add_handler(CommandHandler("unban",          unban))
    app.add_handler(CommandHandler("resetuser",      resetuser))
    app.add_handler(CommandHandler("adminadd",       adminadd))
    app.add_handler(CommandHandler("adminremove",    adminremove))
    app.add_handler(CommandHandler("adminlist",      adminlist))
    app.add_handler(CommandHandler("userlist",       userlist))
    app.add_handler(CommandHandler("broadcast",      broadcast))
    app.add_handler(CommandHandler("liberer",        liberer))
    app.add_handler(CommandHandler("prisonlist",     prisonlist))
    app.add_handler(CommandHandler("emprisonner",    emprisonner))
    app.add_handler(CommandHandler("pause",          pause))
    app.add_handler(CommandHandler("resume",         resume))
    app.add_handler(CommandHandler("giveportfolio",  giveportfolio))
    app.add_handler(CommandHandler("takeportfolio",  takeportfolio))
    app.add_handler(CommandHandler("marketlist",     marketlist))
    app.add_handler(CommandHandler("useractivity",   useractivity))
    app.add_handler(CommandHandler("enquete",        enquete))
    app.add_handler(CommandHandler("richlista",      richlista))
    app.add_handler(CommandHandler("drame",          drame))
    app.add_handler(CommandHandler("article",        article_cmd))
    app.add_handler(CommandHandler("setdramesesuil", setdramesesuil))
    app.add_handler(CommandHandler("logs",           logs_cmd))
    app.add_handler(CommandHandler("suspicious",     suspicious_cmd))
    app.add_handler(CommandHandler("grouplist",      grouplist_cmd))
    app.add_handler(CommandHandler("groupscan",       groupscan_cmd))
    app.add_handler(CommandHandler("fin",            fin))
    app.add_handler(CommandHandler("donate",         donate))

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

    # ── Loterie ───────────────────────────────────────────────────────────────

    # ── Criminalité ───────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("police",        _prison_checked(police)))
    app.add_handler(CommandHandler("bail",          bail))
    app.add_handler(CommandHandler("bail_judgment", bail_judgment))
    app.add_handler(CommandHandler("juge",          _prison_checked(juge)))
    app.add_handler(CommandHandler("security",      _prison_checked(security)))

    # ── Événements aléatoires ─────────────────────────────────────────────────
    app.add_handler(CommandHandler("open", _prison_checked(open_chest_cmd)))
    setup_random_events(app)

    # ── Callbacks ─────────────────────────────────────────────────────────────
    app.add_handler(CallbackQueryHandler(request_callback,   pattern=r"^req:"))
    app.add_handler(CallbackQueryHandler(leave_callback,     pattern=r"^leave:"))
    app.add_handler(CallbackQueryHandler(color_callback,     pattern=r"^color:"))
    app.add_handler(CallbackQueryHandler(juge_callback,      pattern=r"^juge:"))
    app.add_handler(CallbackQueryHandler(security_callback,  pattern=r"^sec:"))

    # ── Diplômes ──────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("diplome", _prison_checked(diplome_cmd)))
    app.add_handler(CallbackQueryHandler(diplome_callback, pattern=r"^exam:"))

    # ── Enchères ──────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("bid",       _prison_checked(bid)))
    app.add_handler(CommandHandler("expertise", _prison_checked(expertise)))
    app.add_handler(CommandHandler("myitems",   _prison_checked(myitems)))
    app.add_handler(CommandHandler("sellitem",  _prison_checked(sellitem)))
    app.add_handler(CommandHandler("shopitems", _prison_checked(shopitems)))
    app.add_handler(CommandHandler("buyitem",   _prison_checked(buyitem)))
    app.add_handler(CallbackQueryHandler(auction_callback,   pattern=r"^auction:bid:"))
    app.add_handler(CallbackQueryHandler(auction_callback,   pattern=r"^auction:info:"))
    app.add_handler(CallbackQueryHandler(expertise_callback, pattern=r"^auction:(sellnow|keep):"))

    # ── Drainage ──────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("impots",          _prison_checked(impots)))
    app.add_handler(CommandHandler("cambrioler",      _prison_checked(cambrioler)))
    setup_drain_jobs(app)

    # ── Jobs ──────────────────────────────────────────────────────────────────
    app.job_queue.run_daily(
        check_anniversaries,
        time=time(hour=8, minute=0),
        name="anniversary_check",
    )
    app.job_queue.run_repeating(
        pay_interests,
        interval=timedelta(hours=6),
        first=timedelta(minutes=5),
        name="bank_interests",
    )
    app.job_queue.run_repeating(
        remind_loans,
        interval=timedelta(hours=84),
        first=timedelta(minutes=10),
        name="loan_reminders",
    )
    setup_auction_jobs(app)
    setup_journal_jobs(app)
    app.add_handler(CommandHandler("testjournal", testjournal_cmd))

    # ── Serveur aiohttp : /webhook (Telegram) + / (UptimeRobot) ──────────────
    async def health(request):
        return web.Response(text="OK", status=200)

    async def telegram_webhook(request):
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
        return web.Response(text="OK")

    webserver = web.Application()
    webserver.router.add_get("/", health)
    webserver.router.add_post("/webhook", telegram_webhook)

    await app.initialize()
    await app.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
    await app.start()

    logger.info(f"Bot démarré sur port {PORT} — webhook + health check actifs.")

    runner = web.AppRunner(webserver)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    try:
        await asyncio.Event().wait()
    finally:
        await app.stop()
        await app.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
