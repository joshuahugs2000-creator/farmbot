import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Railway et Render fournissent DATABASE_URL en postgresql:// ou postgres://
# SQLAlchemy async nécessite postgresql+asyncpg://  → on corrige automatiquement
_db_url = os.getenv("DATABASE_URL", "postgresql+asyncpg://localhost/fam_tree_bot")
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql+asyncpg://", 1)
elif _db_url.startswith("postgresql://"):
    _db_url = _db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
# Certains hébergeurs ajoutent ?sslmode=require — asyncpg utilise ssl=True à la place
if "?sslmode=require" in _db_url:
    _db_url = _db_url.replace("?sslmode=require", "")
    _db_url = _db_url + "?ssl=true" if "?" not in _db_url.split("@")[-1] else _db_url + "&ssl=true"
DATABASE_URL = _db_url

REQUEST_TIMEOUT = 120  # secondes avant expiration demande (2 minutes)

PLANT_TYPES = {
    "rose":      {"emoji": "🌹", "grow_time": 3600,  "value": 10},
    "sunflower": {"emoji": "🌻", "grow_time": 7200,  "value": 25},
    "cherry":    {"emoji": "🍒", "grow_time": 14400, "value": 60},
    "apple":     {"emoji": "🍎", "grow_time": 28800, "value": 120},
    "diamond":   {"emoji": "💎", "grow_time": 86400, "value": 500},
}
GARDEN_SLOTS = 5

PROFILE_COLORS = {
    "blue":   "#3498db",
    "green":  "#2ecc71",
    "red":    "#e74c3c",
    "purple": "#9b59b6",
    "orange": "#e67e22",
    "pink":   "#fd79a8",
    "gold":   "#f1c40f",
    "teal":   "#1abc9c",
}

# (taille_famille_min, karma_min) → titre
TITLES = [
    (0,   0,  "👤 Citoyen"),
    (3,   0,  "🧑 Noble"),
    (5,  10,  "⚔️ Chevalier"),
    (8,  25,  "🏰 Duc"),
    (12, 50,  "👑 Roi"),
]

MOODS = [
    "😊 Joyeux", "😴 Fatigué", "😤 En colère", "🤔 Pensif",
    "😎 Confiant", "🥰 Amoureux", "😢 Triste", "🎉 Festif",
]

INHERITANCE_SHARE = 0.8  # 80 % des coins transmis à la famille

CURRENCY = "$"  # Symbole de la monnaie affiché aux joueurs
