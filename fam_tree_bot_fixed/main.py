import logging
from datetime import time

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
)

from config import BOT_TOKEN
from database import init_db

# Handlers
from handlers.misc    import start, help_cmd, leaderboard, mode, toggle
from handlers.family  import (
    marry, adopt, friend, divorce, disown, unfriend,
    setfamilyname, leave, familyphoto,
    request_callback, leave_callback,
)
from handlers.tree    import tree, bigtree
from handlers.garden  import garden, plant_cmd, harvest
from handlers.waifu   import waifu, upvote, downvote
from handlers.profile import me, setpic, customize, color_callback, titles
from handlers.events  import check_anniversaries

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def on_startup(app: Application):
    await init_db()
    logger.info("✅ Base de données initialisée.")


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )

    # ── Général ──────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("help",        help_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("mode",        mode))
    app.add_handler(CommandHandler("toggle",      toggle))

    # ── Famille ──────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("marry",          marry))
    app.add_handler(CommandHandler("adopt",          adopt))
    app.add_handler(CommandHandler("friend",         friend))
    app.add_handler(CommandHandler("divorce",        divorce))
    app.add_handler(CommandHandler("disown",         disown))
    app.add_handler(CommandHandler("unfriend",       unfriend))
    app.add_handler(CommandHandler("setfamilyname",  setfamilyname))
    app.add_handler(CommandHandler("leave",          leave))
    app.add_handler(CommandHandler("familyphoto",    familyphoto))

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

    # ── Callbacks inline ─────────────────────────────────────────────────────
    app.add_handler(CallbackQueryHandler(request_callback, pattern=r"^req:"))
    app.add_handler(CallbackQueryHandler(leave_callback,   pattern=r"^leave:"))
    app.add_handler(CallbackQueryHandler(color_callback,   pattern=r"^color:"))

    # ── Job quotidien : anniversaires ─────────────────────────────────────────
    app.job_queue.run_daily(
        check_anniversaries,
        time=time(hour=8, minute=0),   # 08:00 UTC chaque matin
        name="anniversary_check",
    )

    logger.info("🤖 Bot démarré.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
