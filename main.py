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

from handlers.misc     import start, help_cmd, leaderboard, mode, toggle, nouveautes_cmd, nouveautes_callback, helpentreprise_cmd
from handlers.family   import (
    marry, adopt, friend, divorce, disown, unfriend,
    setfamilyname, leave, familyphoto, brother, sister,
    request_callback, leave_callback,
    setsexe, setmariage, marry_type_callback,
)
from handlers.tree     import tree
from handlers.garden   import garden, plant_cmd, harvest
from handlers.profile  import me, setpic, customize, color_callback, titles, karmainfo
from handlers.events   import check_anniversaries
from handlers.events_random import setup_random_events, open_chest_cmd
from handlers.economy  import (
    acc, daily, work, pay, richlist, topactifs,
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
    admindiplome,
    adminexam_reset_cooldown,
    adminexam_set_anciennete,
    adminexam_give_coins_exam,
    adminexam_info,
    adminexam_unlock,
    # ── God Mode ──────────────────────────────────────────────────────────────
    statsbot, resetcooldown, addkarma, setkarma,
    wipeinventory, resetbanque, kickboite, deletecompany,
    forcepdg, purgeprison, freeze, unfreeze,
    inflation, checkuser, setreputation, addvalue,
    wipeloans, broadcastdm, topactifs as topactifs_admin,
    mutecompany, unmutecompany,
    adminboites, adminboite, statsusers,
    adminparts, admintransfert,
    auditboite, setvalue, resetboite,
)
from handlers.bank     import (
    banks, openbank, depositbank, withdrawbank,
    balancebank, loanbank, repaybank, loansbank,
    pay_interests, remind_loans,
)
from handlers.invest   import market, market_callback, buy, sell, portfolio, portfolio_callback
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
    init_salle_tables, setup_salle_jobs,
)
from handlers.wealth_drain import (
    cambrioler,
    init_drain_tables, setup_drain_jobs, _ensure_cambriolage_cd_table,
)
from handlers.drames import drame, setdramesesuil
from handlers.article import article_cmd
from handlers.diplome import diplome_cmd, diplome_callback
from handlers.tax import tax_daily_job, tax_overdue_job, payerimpots_cmd, caisse_cmd, mesimpots_cmd
from handlers.competition import startcompet_cmd, compet_cmd, stopcompet_cmd, compet_autoclose_job
from handlers.bureau import soumettredossier_cmd, choisircontrat_cmd, mescontratsbc_cmd, claimcontratbc_cmd, bureau_check_job
from handlers.company import (
    init_company_tables, update_company_activity, increment_contract_progress,
    listeboites_cmd, listeboites_callback,
    versersalaires_cmd, presences_cmd, offresparts_cmd, infoboite_cmd, creerboite_cmd,
    postuler_cmd, candidatures_cmd, accepter_cmd, refuser_cmd,
    recruter_cmd, rejoindre_cmd, demissionner_cmd,
    nommer_cmd, monentreprise_cmd,
    depotboite_cmd, retraitboite_cmd, logsboite_cmd,
    parts_cmd, vendreparts_cmd, acheterparts_cmd, mesparts_cmd,
    licencier_cmd, dissoudreboite_cmd, job_daily_report, job_company_revenues,
    salaireinfo_cmd, employes_cmd,
    accepteroffre_cmd, refuseroffre_cmd, job_expire_share_offers,
    skipattente_cmd,
    annoncerecrutement_cmd, annoncerecrutement_callback,
    cederentreprise_cmd, payeremploye_cmd,
    negociercontrat_cmd,
    renommerboite_cmd, acheterpla_cmd,
)
from handlers.company_sector import (
    init_sector_tables,
    job_sector_events, evenements_cmd,
    proposercontrat_cmd, acceptercontrat_cmd, refusercontrat_cmd,
    mescontrats_cmd, job_expire_contracts,
    classement_cmd, job_weekly_ranking_reward, job_daily_ranking_broadcast,
)
from handlers.company_finance import (
    init_finance_tables,
    bilan_cmd, emprunterboite_cmd, pretboite_cmd, rembourserboite_cmd,
    dividendes_cmd, job_company_dividends,
)
from handlers.company_contracts import (
    init_contract_tables,
    job_dispatch_contracts, job_check_contracts,
    contract_callback, mescontratsauto_cmd, claimcontrat_cmd,
)
from handlers.journal import init_journal_table, setup_journal_jobs, testjournal_cmd
from database.db import AsyncSessionLocal, log_action, init_logs_table, upsert_group, mark_group_inactive, init_groups_table

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", 8080))

if not WEBHOOK_URL:
    # Aucune URL définie → le bot ne peut pas recevoir les updates Telegram !
    # Sur Railway : Variables → WEBHOOK_URL = https://ton-projet.up.railway.app
    # Sur Render  : Environment → WEBHOOK_URL = https://farmbot-77xl.onrender.com
    raise RuntimeError(
        "WEBHOOK_URL non défini !\n"
        "Railway : ajoute la variable WEBHOOK_URL = https://ton-projet.up.railway.app\n"
        "Render  : ajoute la variable WEBHOOK_URL = https://farmbot-77xl.onrender.com"
    )

PRISON_EXEMPT_COMMANDS = {
    "start", "help", "bail", "bail_judgment",
    "adminhelp", "give", "take", "setcoins", "userinfo",
    "ban", "unban", "resetuser", "adminadd", "adminremove",
    "adminlist", "userlist", "broadcast", "liberer", "prisonlist", "emprisonner",
    "pause", "resume", "enquete", "richlista",
    "fin", "donate",
    "topactifs",
}

# Commandes accessibles même en étant banni (commandes admin uniquement)
BAN_EXEMPT_COMMANDS = {
    "ban", "unban", "adminhelp", "give", "take", "setcoins", "userinfo",
    "adminadd", "adminremove", "adminlist", "userlist", "broadcast",
    "liberer", "prisonlist", "emprisonner", "pause", "resume",
    "enquete", "richlista", "logs", "suspicious", "grouplist", "groupscan",
    "fin", "donate", "resetuser", "useractivity",
}


import time as _time_ban
_ban_cache: dict[int, tuple] = {}  # user_id → (timestamp, is_banned)
_BAN_CACHE_TTL = 120  # 2 minutes

async def ban_middleware(update: Update, context) -> None:
    """Bloque toutes les interactions des utilisateurs bannis."""
    user = update.effective_user
    if not user:
        return
    now_mono = _time_ban.monotonic()
    cached = _ban_cache.get(user.id)
    if cached is not None and now_mono - cached[0] < _BAN_CACHE_TTL:
        if not cached[1]:
            return
        if await is_admin(user.id):
            return
        if update.message and update.message.text:
            cmd = update.message.text.split()[0].lstrip("/").split("@")[0].lower()
            if cmd in BAN_EXEMPT_COMMANDS:
                return
        if update.callback_query:
            await update.callback_query.answer()
        raise ApplicationHandlerStop()
    try:
        async with AsyncSessionLocal() as _s:
            from database.db import get_user as _gu
            _u = await _gu(_s, user.id)
            is_banned = bool(_u and _u.is_banned)
            _ban_cache[user.id] = (now_mono, is_banned)
            if not is_banned:
                return
        if await is_admin(user.id):
            return
        if update.message and update.message.text:
            cmd = update.message.text.split()[0].lstrip("/").split("@")[0].lower()
            if cmd in BAN_EXEMPT_COMMANDS:
                return
        if update.callback_query:
            await update.callback_query.answer()
        raise ApplicationHandlerStop()
    except ApplicationHandlerStop:
        raise
    except Exception:
        pass


import time as _time
# Cache prison : user_id → (timestamp_check, is_imprisoned, released_at, bail_amount, minutes_left)
_prison_cache: dict[int, tuple] = {}
_PRISON_CACHE_TTL = 60  # re-vérifie en DB max toutes les 60s

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

    now_mono = _time.monotonic()

    # Cache valide → on répond sans DB
    cached = _prison_cache.get(user.id)
    if cached is not None and now_mono - cached[0] < _PRISON_CACHE_TTL:
        is_imprisoned, bail_amount, duration_str = cached[1], cached[2], cached[3]
        if not is_imprisoned:
            return False
        await update.message.reply_text(
            f"🔒 <b>Tu es en prison !</b>\n\n"
            f"La commande <code>/{command}</code> n'est pas disponible.\n"
            f"⏳ Libération dans : <b>{duration_str}</b>\n"
            f"💸 Caution : <b>{_fmt(bail_amount)} 💰</b>\n\n"
            f"Demande à quelqu'un de payer ta caution avec <code>/bail @toi</code>",
            parse_mode="HTML"
        )
        return True

    # Cache expiré ou absent → on tape la DB UNE fois
    try:
        async with AsyncSessionLocal() as session:
            from datetime import datetime
            prison_row_res = await session.execute(
                __import__("sqlalchemy").text("SELECT * FROM crime_prison WHERE user_id = :uid"),
                {"uid": user.id}
            )
            prison_row = prison_row_res.fetchone()
            if not prison_row:
                _prison_cache[user.id] = (now_mono, False, 0, "")
                return False
            now = datetime.utcnow()
            if now >= prison_row.released_at:
                await session.execute(
                    __import__("sqlalchemy").text("DELETE FROM crime_prison WHERE user_id = :uid"),
                    {"uid": user.id}
                )
                await session.commit()
                _prison_cache[user.id] = (now_mono, False, 0, "")
                return False
            minutes_left = max(0, int((prison_row.released_at - now).total_seconds() / 60))
            h = minutes_left // 60
            m = minutes_left % 60
            duration_str = f"{h}h{m:02d}m" if h > 0 else f"{m} minute(s)"
            _prison_cache[user.id] = (now_mono, True, prison_row.bail_amount, duration_str)
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






# Cache en mémoire pour éviter de spammer l'API Telegram à chaque message de groupe
_group_tracking_cache: dict[int, float] = {}
_GROUP_TRACKING_COOLDOWN = 3600  # 1h entre chaque scan API d'un même groupe

async def group_tracking_middleware(update: Update, context) -> None:
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup", "channel"):
        return
    try:
        import time
        now = time.monotonic()
        last_seen = _group_tracking_cache.get(chat.id, 0)

        # Si on a déjà tracké ce groupe récemment, juste mettre à jour last_seen sans appels API
        if now - last_seen < _GROUP_TRACKING_COOLDOWN:
            # Mise à jour silencieuse sans appels API
            return

        _group_tracking_cache[chat.id] = now

        # Appels API seulement toutes les heures par groupe
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
    # Mettre à jour l'activité entreprise en arrière-plan (non bloquant)
    try:
        asyncio.create_task(update_company_activity(user.id))
    except Exception as e:
        logger.debug(f"Erreur update_company_activity: {e}")
    # Incrémenter la progression des contrats bureau (sans throttle, SQL atomique)
    try:
        asyncio.create_task(increment_contract_progress(user.id))
    except Exception as e:
        logger.debug(f"Erreur increment_contract_progress: {e}")

async def on_startup(application: Application):
    await init_db()
    await init_logs_table()
    await init_groups_table()   # ← crée activity_logs si elle n'existe pas
    await init_journal_table()
    await init_crime_tables()
    await init_drain_tables()
    await _ensure_cambriolage_cd_table()
    await init_auction_tables()
    await init_salle_tables()
    await init_company_tables()
    await init_sector_tables()
    await init_finance_tables()
    await init_contract_tables()
    await load_admins_from_db()  # ← charge les admins persistés en DB

    # Migration : colonnes genre et mariage
    from database.db import engine
    from sqlalchemy import text as _text
    async with engine.begin() as _conn:
        for _sql in [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS gender VARCHAR(10)",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS marriage_type VARCHAR(10) DEFAULT 'monogame'",
            "ALTER TABLE pending_requests ADD COLUMN IF NOT EXISTS extra VARCHAR(50)",
            # Index pour accélérer la recherche par @username (parse_target)
            "CREATE INDEX IF NOT EXISTS idx_users_username_lower ON users (LOWER(username))",
        ]:
            try:
                await _conn.execute(_text(_sql))
            except Exception:
                pass

    logger.info("Base de données initialisée.")


async def error_handler(update: object, context):
    from telegram.error import RetryAfter, TimedOut
    import httpx

    err = context.error

    # Flood control ou timeout réseau — on log en warning sans spammer l'utilisateur
    if isinstance(err, RetryAfter):
        wait = min(err.retry_after, 30)  # cap à 30s max, Telegram envoie parfois des valeurs aberrantes
        logger.warning(f"Flood control Telegram : retry in {err.retry_after}s — ignoré (cap {wait}s)")
        return
    if isinstance(err, (TimedOut, httpx.ReadTimeout, httpx.ConnectTimeout)):
        logger.warning(f"Timeout réseau ({type(err).__name__}) — ignoré")
        return

    logger.error("Exception dans un handler :", exc_info=err)
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
        # Incrémenter progression contrats + activité entreprise à chaque commande
        if update.effective_user:
            try:
                asyncio.create_task(increment_contract_progress(update.effective_user.id))
            except Exception:
                pass
            try:
                asyncio.create_task(update_company_activity(update.effective_user.id))
            except Exception:
                pass
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
    # activity_logging_middleware désactivé — génère trop d'egress Supabase
    # app.add_handler(TypeHandler(Update, activity_logging_middleware), group=-1)
    app.add_handler(TypeHandler(Update, group_tracking_middleware),   group=-2)
    app.add_handler(TypeHandler(Update, ban_middleware),              group=-3)
    from telegram.ext import ChatMemberHandler
    app.add_handler(ChatMemberHandler(my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER))

    # ── Général ───────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("help",        help_cmd))
    app.add_handler(CommandHandler("helpentreprise", helpentreprise_cmd))
    app.add_handler(CommandHandler("leaderboard", _prison_checked(leaderboard)))
    app.add_handler(CommandHandler("mode",        mode))
    app.add_handler(CommandHandler("toggle",      toggle))
    app.add_handler(CallbackQueryHandler(nouveautes_callback, pattern=r"^info:"))

    # ── Famille ───────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("marry",         _prison_checked(marry)))
    app.add_handler(CommandHandler("adopt",         _prison_checked(adopt)))
    app.add_handler(CommandHandler("friend",        _prison_checked(friend)))
    app.add_handler(CommandHandler("divorce",       _prison_checked(divorce)))
    app.add_handler(CommandHandler("disown",        _prison_checked(disown)))
    app.add_handler(CommandHandler("unfriend",      _prison_checked(unfriend)))
    app.add_handler(CommandHandler("brother",       _prison_checked(brother)))
    app.add_handler(CommandHandler("sister",        _prison_checked(sister)))
    app.add_handler(CommandHandler("leave",         _prison_checked(leave)))
    app.add_handler(CommandHandler("setsexe",       _prison_checked(setsexe)))
    app.add_handler(CommandHandler("setmariage",    _prison_checked(setmariage)))
    app.add_handler(CommandHandler("setfamilyname", _prison_checked(setfamilyname)))
    app.add_handler(CommandHandler("familyphoto",   _prison_checked(familyphoto)))
    app.add_handler(CallbackQueryHandler(marry_type_callback, pattern=r"^marry_type:"))

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
    app.add_handler(CommandHandler("karmainfo", _prison_checked(karmainfo)))

    # ── Économie ──────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("acc",        _prison_checked(acc)))
    app.add_handler(CommandHandler("daily",      _prison_checked(daily)))
    app.add_handler(CommandHandler("work",       _prison_checked(work)))
    app.add_handler(CommandHandler("pay",        _prison_checked(pay)))
    app.add_handler(CommandHandler("richlist",   _prison_checked(richlist)))
    app.add_handler(CommandHandler("topactifs",  topactifs))
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
    app.add_handler(CallbackQueryHandler(portfolio_callback,  pattern=r"^pf:"))

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
    app.add_handler(CommandHandler("openbank",     _prison_checked(openbank)))
    app.add_handler(CommandHandler("depositbank",  _prison_checked(depositbank)))
    app.add_handler(CommandHandler("withdrawbank", _prison_checked(withdrawbank)))
    app.add_handler(CommandHandler("balancebank",  _prison_checked(balancebank)))
    app.add_handler(CommandHandler("loanbank",     _prison_checked(loanbank)))
    app.add_handler(CommandHandler("repaybank",    _prison_checked(repaybank)))
    app.add_handler(CommandHandler("loansbank",    _prison_checked(loansbank)))

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
    app.add_handler(CommandHandler("admindiplome", admindiplome))
    app.add_handler(CommandHandler("examreset", adminexam_reset_cooldown))
    app.add_handler(CommandHandler("examanciennete", adminexam_set_anciennete))
    app.add_handler(CommandHandler("examcoins", adminexam_give_coins_exam))
    app.add_handler(CommandHandler("examinfo", adminexam_info))
    app.add_handler(CommandHandler("examunlock", adminexam_unlock))

    # ── God Mode ──────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("statsbot",       statsbot))
    app.add_handler(CommandHandler("resetcooldown",  resetcooldown))
    app.add_handler(CommandHandler("addkarma",       addkarma))
    app.add_handler(CommandHandler("setkarma",       setkarma))
    app.add_handler(CommandHandler("wipeinventory",  wipeinventory))
    app.add_handler(CommandHandler("resetbanque",    resetbanque))
    app.add_handler(CommandHandler("kickboite",      kickboite))
    app.add_handler(CommandHandler("deletecompany",  deletecompany))
    app.add_handler(CommandHandler("forcepdg",       forcepdg))
    app.add_handler(CommandHandler("purgeprison",    purgeprison))
    app.add_handler(CommandHandler("freeze",         freeze))
    app.add_handler(CommandHandler("unfreeze",       unfreeze))
    app.add_handler(CommandHandler("inflation",      inflation))
    app.add_handler(CommandHandler("checkuser",      checkuser))
    app.add_handler(CommandHandler("setreputation",  setreputation))
    app.add_handler(CommandHandler("addvalue",       addvalue))
    app.add_handler(CommandHandler("wipeloans",      wipeloans))
    app.add_handler(CommandHandler("broadcastdm",    broadcastdm))
    app.add_handler(CommandHandler("mutecompany",    mutecompany))
    app.add_handler(CommandHandler("unmutecompany",  unmutecompany))
    app.add_handler(CommandHandler("adminboites",    adminboites))
    app.add_handler(CommandHandler("adminboite",     adminboite))
    app.add_handler(CommandHandler("statsusers",     statsusers))
    app.add_handler(CommandHandler("adminparts",     adminparts))
    app.add_handler(CommandHandler("admintransfert", admintransfert))
    app.add_handler(CommandHandler("auditboite",     auditboite))
    app.add_handler(CommandHandler("setvalue",       setvalue))
    app.add_handler(CommandHandler("resetboite",     resetboite))

    # ── Entreprises ───────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("listeboites",   _prison_checked(listeboites_cmd)))
    app.add_handler(CallbackQueryHandler(listeboites_callback, pattern=r"^lb"))
    app.add_handler(CommandHandler("infoboite",     _prison_checked(infoboite_cmd)))
    app.add_handler(CommandHandler("creerboite",    _prison_checked(creerboite_cmd)))
    app.add_handler(CommandHandler("postuler",      _prison_checked(postuler_cmd)))
    app.add_handler(CommandHandler("candidatures",  _prison_checked(candidatures_cmd)))
    app.add_handler(CommandHandler("accepter",      _prison_checked(accepter_cmd)))
    app.add_handler(CommandHandler("refuser",       _prison_checked(refuser_cmd)))
    app.add_handler(CommandHandler("recruter",      _prison_checked(recruter_cmd)))
    app.add_handler(CommandHandler("rejoindre",     _prison_checked(rejoindre_cmd)))
    app.add_handler(CommandHandler("demissionner",  _prison_checked(demissionner_cmd)))
    app.add_handler(CommandHandler("nommer",        _prison_checked(nommer_cmd)))
    app.add_handler(CommandHandler("monentreprise", _prison_checked(monentreprise_cmd)))
    app.add_handler(CommandHandler("depotboite",    _prison_checked(depotboite_cmd)))
    app.add_handler(CommandHandler("retraitboite",  _prison_checked(retraitboite_cmd)))
    app.add_handler(CommandHandler("logsboite",     _prison_checked(logsboite_cmd)))
    app.add_handler(CommandHandler("parts",         _prison_checked(parts_cmd)))
    app.add_handler(CommandHandler("vendreparts",   _prison_checked(vendreparts_cmd)))
    app.add_handler(CommandHandler("acheterparts",  _prison_checked(acheterparts_cmd)))
    app.add_handler(CommandHandler("accepteroffre", _prison_checked(accepteroffre_cmd)))
    app.add_handler(CommandHandler("refuseroffre",  _prison_checked(refuseroffre_cmd)))
    app.add_handler(CommandHandler("licencier",      _prison_checked(licencier_cmd)))
    app.add_handler(CommandHandler("employes",       _prison_checked(employes_cmd)))
    app.add_handler(CommandHandler("dissoudreboite",  _prison_checked(dissoudreboite_cmd)))
    app.add_handler(CommandHandler("annoncerecrutement", _prison_checked(annoncerecrutement_cmd)))
    app.add_handler(CallbackQueryHandler(annoncerecrutement_callback, pattern=r"^annonce:"))
    app.add_handler(CommandHandler("salaireinfo",     _prison_checked(salaireinfo_cmd)))
    app.add_handler(CommandHandler("versersalaires", _prison_checked(versersalaires_cmd)))
    app.add_handler(CommandHandler("presences",      _prison_checked(presences_cmd)))
    app.add_handler(CommandHandler("payeremploye",   _prison_checked(payeremploye_cmd)))
    app.add_handler(CommandHandler("negociercontrat",_prison_checked(negociercontrat_cmd)))
    app.add_handler(CommandHandler("mescontratsauto", _prison_checked(mescontratsauto_cmd)))
    app.add_handler(CommandHandler("claimcontrat",   _prison_checked(claimcontrat_cmd)))
    app.add_handler(CallbackQueryHandler(contract_callback, pattern=r"^cnt_(accept|negoc|refuse):\d+$"))
    app.add_handler(CommandHandler("cederentreprise",_prison_checked(cederentreprise_cmd)))
    app.add_handler(CommandHandler("skipattente",    _prison_checked(skipattente_cmd)))
    app.add_handler(CommandHandler("offresparts",    _prison_checked(offresparts_cmd)))
    app.add_handler(CommandHandler("mesparts",       _prison_checked(mesparts_cmd)))
    app.add_handler(CommandHandler("renommerboite",  _prison_checked(renommerboite_cmd)))
    app.add_handler(CommandHandler("acheterpla",     _prison_checked(acheterpla_cmd)))
    # ── Finances entreprise ────────────────────────────────────────────────────
    app.add_handler(CommandHandler("bilan",           _prison_checked(bilan_cmd)))
    app.add_handler(CommandHandler("emprunterboite",  _prison_checked(emprunterboite_cmd)))
    app.add_handler(CommandHandler("pretboite",       _prison_checked(pretboite_cmd)))
    app.add_handler(CommandHandler("rembourserboite", _prison_checked(rembourserboite_cmd)))
    app.add_handler(CommandHandler("dividendes",      _prison_checked(dividendes_cmd)))
    # ── Secteurs : événements, contrats, classement ───────────────────────────
    app.add_handler(CommandHandler("evenements",      _prison_checked(evenements_cmd)))
    app.add_handler(CommandHandler("classement",      _prison_checked(classement_cmd)))
    app.add_handler(CommandHandler("proposercontrat", _prison_checked(proposercontrat_cmd)))
    app.add_handler(CommandHandler("acceptercontrat", _prison_checked(acceptercontrat_cmd)))
    app.add_handler(CommandHandler("refusercontrat",  _prison_checked(refusercontrat_cmd)))
    app.add_handler(CommandHandler("mescontrats",     _prison_checked(mescontrats_cmd)))

    # ── Agence Fiscale ────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("payerimpots",     _prison_checked(payerimpots_cmd)))
    app.add_handler(CommandHandler("mesimpots",        _prison_checked(mesimpots_cmd)))
    app.add_handler(CommandHandler("startcompet",      startcompet_cmd))   # admin only (géré dans le handler)
    app.add_handler(CommandHandler("compet",           _prison_checked(compet_cmd)))
    app.add_handler(CommandHandler("stopcompet",       stopcompet_cmd))    # admin only (géré dans le handler)
    app.add_handler(CommandHandler("caisse",          caisse_cmd))

    # ── Bureau des Contrats ───────────────────────────────────────────────────
    app.add_handler(CommandHandler("soumettredossier", _prison_checked(soumettredossier_cmd)))
    app.add_handler(CommandHandler("choisircontrat",   _prison_checked(choisircontrat_cmd)))
    app.add_handler(CommandHandler("mescontratsbc",    _prison_checked(mescontratsbc_cmd)))
    app.add_handler(CommandHandler("claimcontratbc",   _prison_checked(claimcontratbc_cmd)))

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
    setup_salle_jobs(app)
    setup_journal_jobs(app)
    app.add_handler(CommandHandler("testjournal", testjournal_cmd))

    # ── Job revenus entreprises (toutes les 24h) ──────────────────────────────
    app.job_queue.run_repeating(
        job_company_revenues,
        interval=timedelta(hours=24),
        first=timedelta(minutes=15),
        name="company_revenues",
    )

    # ── Job expiration des offres de parts (toutes les heures) ───────────────
    app.job_queue.run_repeating(
        job_expire_share_offers,
        interval=timedelta(hours=1),
        first=timedelta(minutes=5),
        name="expire_share_offers",
    )

    # ── Job rapport quotidien 18h (PDG) ─────────────────────────────────────────
    from datetime import time as dt_time
    import pytz
    tz_paris = pytz.timezone("Africa/Abidjan")  # UTC+0 = Lomé/Abidjan
    app.job_queue.run_daily(
        job_daily_report,
        time=dt_time(hour=18, minute=0, tzinfo=tz_paris),
        name="daily_report",
    )

    # ── Job événements sectoriels (toutes les 48h) ─────────────────────────────
    app.job_queue.run_repeating(
        job_sector_events,
        interval=timedelta(hours=48),
        first=timedelta(minutes=30),
        name="sector_events",
    )

    # ── Job expiration contrats (toutes les heures) ──────────────────────────
    app.job_queue.run_repeating(
        job_expire_contracts,
        interval=timedelta(hours=1),
        first=timedelta(minutes=10),
        name="expire_contracts",
    )

    # ── Job classement hebdo (dimanche 20h) ──────────────────────────────────
    from datetime import time as dt_time
    app.job_queue.run_daily(
        job_weekly_ranking_reward,
        time=dt_time(hour=20, minute=0, tzinfo=tz_paris),
        days=(6,),  # dimanche
        name="weekly_ranking_reward",
    )

    # ── Job dividendes hebdo (lundi 9h) ───────────────────────────────────────
    app.job_queue.run_daily(
        job_company_dividends,
        time=dt_time(hour=9, minute=0, tzinfo=tz_paris),
        days=(0,),  # lundi
        name="company_dividends_weekly",
    )

    # ── Job classement quotidien 18h (snapshot + broadcast) ──────────────────────
    app.job_queue.run_daily(
        job_daily_ranking_broadcast,
        time=dt_time(hour=18, minute=0, tzinfo=tz_paris),
        name="daily_ranking_broadcast",
    )

    # ── Jobs contrats automatiques IA ─────────────────────────────────────────
    app.job_queue.run_repeating(
        job_dispatch_contracts,
        interval=timedelta(hours=1),
        first=timedelta(minutes=20),
        name="dispatch_auto_contracts",
    )
    app.job_queue.run_repeating(
        job_check_contracts,
        interval=timedelta(hours=1),
        first=timedelta(minutes=35),
        name="check_auto_contracts",
    )

    # ── Agence Fiscale — factures tous les 2 jours ────────────────────────────
    app.job_queue.run_repeating(
        tax_daily_job,
        interval=timedelta(hours=48),
        first=timedelta(minutes=5),
        name="tax_daily",
    )
    # Vérification impayés toutes les heures
    app.job_queue.run_repeating(
        compet_autoclose_job,
        interval=timedelta(hours=1),
        first=timedelta(minutes=10),
        name="compet_autoclose",
    )
    app.job_queue.run_repeating(
        tax_overdue_job,
        interval=timedelta(hours=1),
        first=timedelta(minutes=10),
        name="tax_overdue_check",
    )

    # ── Bureau des Contrats — vérification toutes les heures ─────────────────
    app.job_queue.run_repeating(
        bureau_check_job,
        interval=timedelta(hours=1),
        first=timedelta(minutes=45),
        name="bureau_contrats_check",
    )

    # ── Serveur aiohttp : /webhook (Telegram) + / (UptimeRobot) ──────────────
    async def health(request):
        return web.Response(text="OK", status=200)

    # Déduplication des updates Telegram — évite de traiter le même message 30 fois
    _seen_updates: dict[int, float] = {}
    _SEEN_TTL = 120  # garder les IDs 2 minutes

    async def telegram_webhook(request):
        import time as _t
        data = await request.json()
        update_id = data.get("update_id")
        now = _t.monotonic()

        # Nettoyage périodique du cache
        expired = [k for k, v in _seen_updates.items() if now - v > _SEEN_TTL]
        for k in expired:
            del _seen_updates[k]

        # Ignorer les doublons
        if update_id and update_id in _seen_updates:
            return web.Response(text="OK")
        if update_id:
            _seen_updates[update_id] = now

        update = Update.de_json(data, app.bot)
        asyncio.ensure_future(app.process_update(update))
        return web.Response(text="OK")

    from api.webapp import setup_webapp_routes

    webserver = web.Application()
    webserver.router.add_get("/health", health)
    webserver.router.add_post("/webhook", telegram_webhook)
    setup_webapp_routes(webserver)

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
