"""
Script d'annonce globale — Your Family ❤️
Usage : railway run python send_announce.py
"""
import asyncio
import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

# ── Config ────────────────────────────────────────────────────────────────────
TITLE = "🎟️ Les Tickets, c'est quoi ?"
ICON  = "🎟️"
BODY  = (
    "Les Tickets sont la nouvelle monnaie premium de Your Family !\n\n"
    "🛍️ Ils servent à acheter des Packs exclusifs contenant :\n"
    "• 💰 De l'argent\n"
    "• 🧪 Des objets rares & ultra-utiles\n"
    "• ⚡ Des boosters (×2 ta banque, ×2 tes gains…)\n"
    "• 🛡️ Protection contre les vols\n"
    "• 🆘 Survie aux crises économiques\n\n"
    "Les Packs arrivent bientôt. Garde tes Tickets précieusement ! 🎯"
)
# ──────────────────────────────────────────────────────────────────────────────

async def main():
    db_url = os.environ["DATABASE_URL"].replace(
        "postgresql://", "postgresql+asyncpg://"
    ).replace(
        "postgres://", "postgresql+asyncpg://"
    )

    engine = create_async_engine(db_url, echo=False)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as session:
        # Créer la table si elle n'existe pas
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS user_notifications (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT NOT NULL,
                icon       VARCHAR(10)  DEFAULT '📢',
                title      VARCHAR(255) NOT NULL,
                body       TEXT         NOT NULL,
                is_read    BOOLEAN      DEFAULT FALSE,
                created_at TIMESTAMP    DEFAULT NOW()
            )
        """))

        # Récupérer tous les joueurs non bannis
        rows = (await session.execute(
            text("SELECT user_id FROM users WHERE is_banned = FALSE OR is_banned IS NULL")
        )).fetchall()

        count = 0
        for (uid,) in rows:
            await session.execute(text("""
                INSERT INTO user_notifications (user_id, icon, title, body)
                VALUES (:uid, :icon, :title, :body)
            """), {"uid": uid, "icon": ICON, "title": TITLE, "body": BODY})
            count += 1

        await session.commit()
        print(f"✅ Notif envoyée à {count} joueurs.")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
