from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import ContextTypes

from database.db import AsyncSessionLocal, get_garden, plant, harvest_plant, get_settings, get_user, adjust_karma
from database.models import Garden
from utils.helpers import ensure_user, is_group
from config import PLANT_TYPES, GARDEN_SLOTS, CURRENCY


def _plant_status(g: Garden) -> str:
    grow_time = PLANT_TYPES[g.plant_type]["grow_time"]
    ready_at  = g.planted_at + timedelta(seconds=grow_time)
    now       = datetime.utcnow()
    emoji     = PLANT_TYPES[g.plant_type]["emoji"]
    if now >= ready_at:
        return f"Slot {g.slot+1}: {emoji} {g.plant_type} — Pret a recolter! (/harvest {g.slot+1})"
    remaining = int((ready_at - now).total_seconds() / 60)
    return f"Slot {g.slot+1}: {emoji} {g.plant_type} — {remaining} min restantes"


async def garden(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return await update.message.reply_text("Commande de groupe uniquement.")
    user     = await ensure_user(update.effective_user)
    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        settings = await get_settings(session, group_id)
        if not settings.garden_enabled:
            return await update.message.reply_text("Le jardin est desactive dans ce groupe.")
        plants = await get_garden(session, user.user_id, group_id)
        u      = await get_user(session, user.user_id)
        coins  = u.coins if u else 0

    lines = [f"Jardin de {update.effective_user.first_name}  {CURRENCY} : {coins:,}\n"]
    for slot_i in range(GARDEN_SLOTS):
        g = next((p for p in plants if p.slot == slot_i), None)
        if g:
            lines.append(_plant_status(g))
        else:
            plant_list = "|".join(PLANT_TYPES.keys())
            lines.append(f"Slot {slot_i+1}: [Vide]  /plant {slot_i+1} {plant_list}")

    lines.append(f"\nPlantes disponibles : {', '.join(PLANT_TYPES.keys())}")
    await update.message.reply_text("\n".join(lines))


async def plant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return await update.message.reply_text("Commande de groupe uniquement.")
    if not context.args or len(context.args) < 2:
        return await update.message.reply_text(
            f"Usage : /plant [slot 1-{GARDEN_SLOTS}] [plante]\nPlantes : {', '.join(PLANT_TYPES.keys())}"
        )
    try:
        slot = int(context.args[0]) - 1
        assert 0 <= slot < GARDEN_SLOTS
    except (ValueError, AssertionError):
        return await update.message.reply_text(f"Slot invalide (1-{GARDEN_SLOTS}).")

    plant_type = context.args[1].lower()
    if plant_type not in PLANT_TYPES:
        return await update.message.reply_text(
            f"Plante inconnue. Disponibles : {', '.join(PLANT_TYPES.keys())}"
        )

    user     = await ensure_user(update.effective_user)
    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        existing = await get_garden(session, user.user_id, group_id)
        if any(g.slot == slot for g in existing):
            return await update.message.reply_text("Ce slot est deja occupe. Recoltez d'abord.")
        await plant(session, user.user_id, group_id, slot, plant_type)

    info  = PLANT_TYPES[plant_type]
    hours = info["grow_time"] // 3600
    mins  = (info["grow_time"] % 3600) // 60
    await update.message.reply_text(
        f"{info['emoji']} {plant_type} plante dans le slot {slot+1} !\n"
        f"Pret dans {hours}h{mins:02d}m — Valeur : {info['value']} {CURRENCY}"
    )


async def harvest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return await update.message.reply_text("Commande de groupe uniquement.")

    user     = await ensure_user(update.effective_user)
    group_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        plants = await get_garden(session, user.user_id, group_id)
        now    = datetime.utcnow()
        ready  = [
            g for g in plants
            if now >= g.planted_at + timedelta(seconds=PLANT_TYPES[g.plant_type]["grow_time"])
        ]

        if context.args:
            try:
                slot  = int(context.args[0]) - 1
                ready = [g for g in ready if g.slot == slot]
            except ValueError:
                pass

        if not ready:
            return await update.message.reply_text("Rien a recolter pour l'instant !")

        total = 0
        lines = []
        for g in ready:
            coins = await harvest_plant(session, g.id)
            total += coins
            lines.append(f"  {PLANT_TYPES[g.plant_type]['emoji']} {g.plant_type} → +{coins:,} {CURRENCY}")

        # 🎯 Karma : +1 tous les 5 récoltes
        from sqlalchemy import text as _text
        await session.execute(
            _text("UPDATE users SET harvest_count = COALESCE(harvest_count, 0) + :n WHERE user_id = :uid"),
            {"n": len(ready), "uid": user.user_id}
        )
        await session.commit()
        # Récupérer le nouveau harvest_count
        r = await session.execute(_text("SELECT harvest_count FROM users WHERE user_id = :uid"), {"uid": user.user_id})
        row = r.fetchone()
        harvest_count = row[0] if row else 0
        karma_msg = ""
        if harvest_count > 0 and harvest_count % 5 == 0:
            await adjust_karma(session, user.user_id, +1)
            karma_msg = "\n⭐ +1 karma (5 récoltes atteintes) !"

    await update.message.reply_text(
        "Recolte terminee !\n" + "\n".join(lines) + f"\nTotal : {total:,} {CURRENCY}" + karma_msg
    )
