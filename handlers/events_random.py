"""
Événements aléatoires automatiques :
  🎁 Coffre mystère  — le premier à faire /open gagne 5 000–200 000 $
  ⭐ Heure dorée     — gains casino x2 pendant 1h
"""
import random
import logging
from datetime import datetime, timedelta, timezone

from telegram.ext import Application, ContextTypes

from database.db import AsyncSessionLocal, add_coins
from utils.helpers import ensure_user

logger = logging.getLogger(__name__)

# ─── ÉTAT GLOBAL ──────────────────────────────────────────────────────────────
_active_chests: dict = {}
_golden_hours: dict = {}


def golden_multiplier(group_id: int) -> float:
    """Retourne 2.0 si l'heure dorée est active pour ce groupe, sinon 1.0."""
    exp = _golden_hours.get(group_id)
    if exp and datetime.now(timezone.utc) < exp:
        return 2.0
    return 1.0


# ─── COFFRE MYSTÈRE ───────────────────────────────────────────────────────────

async def _spawn_chest(context: ContextTypes.DEFAULT_TYPE):
    group_id = context.job.data["group_id"]
    amount   = random.randint(5_000, 200_000)

    _active_chests[group_id] = {
        "active":  True,
        "amount":  amount,
        "expires": datetime.now(timezone.utc) + timedelta(seconds=60),
    }

    fmt = f"{amount:,}".replace(",", " ")
    await context.bot.send_message(
        chat_id=group_id,
        text=(
            "🎁 <b>Un coffre mystère est apparu !</b>\n\n"
            f"Il contient <b>{fmt} $</b>\n\n"
            "Le premier à taper /open le remporte !\n"
            "⏳ Disparaît dans 60 secondes…"
        ),
        parse_mode="HTML",
    )

    context.job_queue.run_once(
        _expire_chest,
        when=60,
        data={"group_id": group_id},
        name=f"chest_expire_{group_id}",
    )


async def _expire_chest(context: ContextTypes.DEFAULT_TYPE):
    group_id = context.job.data["group_id"]
    chest    = _active_chests.get(group_id)
    if chest and chest["active"]:
        _active_chests[group_id]["active"] = False
        await context.bot.send_message(
            chat_id=group_id,
            text="⌛ Le coffre mystère a disparu… Personne ne l'a ouvert !",
        )


async def open_chest_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    """/open — Ouvrir le coffre mystère actif."""
    if not update.effective_chat or update.effective_chat.type not in ("group", "supergroup"):
        return await update.message.reply_text("Cette commande est uniquement disponible dans un groupe !")

    group_id = update.effective_chat.id
    chest    = _active_chests.get(group_id)

    if not chest or not chest["active"]:
        return await update.message.reply_text("Il n'y a pas de coffre mystère actif pour le moment !")

    if datetime.now(timezone.utc) > chest["expires"]:
        _active_chests[group_id]["active"] = False
        return await update.message.reply_text("⌛ Trop tard, le coffre a expiré !")

    _active_chests[group_id]["active"] = False
    amount = chest["amount"]
    user   = await ensure_user(update.effective_user)

    async with AsyncSessionLocal() as session:
        new_bal = await add_coins(session, user.user_id, amount)

    fmt_amount = f"{amount:,}".replace(",", " ")
    fmt_bal    = f"{new_bal:,}".replace(",", " ")

    await update.message.reply_text(
        f"🎉 <b>{update.effective_user.first_name}</b> a ouvert le coffre mystère !\n"
        f"💰 Gain : <b>{fmt_amount} $</b>\n"
        f"Solde : {fmt_bal} $",
        parse_mode="HTML",
    )


# ─── HEURE DORÉE ──────────────────────────────────────────────────────────────

async def _spawn_golden_hour(context: ContextTypes.DEFAULT_TYPE):
    group_id = context.job.data["group_id"]
    expires  = datetime.now(timezone.utc) + timedelta(hours=1)
    _golden_hours[group_id] = expires

    await context.bot.send_message(
        chat_id=group_id,
        text=(
            "⭐ <b>HEURE DORÉE activée !</b>\n\n"
            "Tous les gains au casino sont <b>×2</b> pendant <b>1 heure</b> !\n"
            "🎰 /blackjack  |  🎡 /roulette  |  🎰 /slots  |  🏇 /race"
        ),
        parse_mode="HTML",
    )

    context.job_queue.run_once(
        _end_golden_hour,
        when=3600,
        data={"group_id": group_id},
        name=f"golden_end_{group_id}",
    )


async def _end_golden_hour(context: ContextTypes.DEFAULT_TYPE):
    group_id = context.job.data["group_id"]
    _golden_hours.pop(group_id, None)
    await context.bot.send_message(
        chat_id=group_id,
        text="⌛ L'Heure Dorée est terminée. Les gains reviennent à la normale.",
    )


# ─── SCHEDULER PRINCIPAL ──────────────────────────────────────────────────────

_known_groups: set = set()


def register_group(group_id: int):
    """Enregistre un groupe pour qu'il reçoive des événements."""
    _known_groups.add(group_id)


async def _schedule_events(context: ContextTypes.DEFAULT_TYPE):
    if not _known_groups:
        return

    for group_id in list(_known_groups):
        if random.random() > 0.40:
            continue

        if golden_multiplier(group_id) == 2.0:
            event = "chest"
        else:
            event = random.choices(["chest", "golden"], weights=[60, 40], k=1)[0]

        job_data = {"group_id": group_id}

        if event == "chest":
            chest = _active_chests.get(group_id)
            if chest and chest["active"]:
                continue
            context.job_queue.run_once(
                _spawn_chest, when=0, data=job_data,
                name=f"chest_spawn_{group_id}",
            )
        else:
            context.job_queue.run_once(
                _spawn_golden_hour, when=0, data=job_data,
                name=f"golden_spawn_{group_id}",
            )


def setup_random_events(app: Application):
    """Appeler dans main() pour activer les événements aléatoires (toutes les 4h)."""
    app.job_queue.run_repeating(
        _schedule_events,
        interval=14_400,
        first=300,
        name="random_events_scheduler",
    )
    logger.info("Événements aléatoires activés.")
