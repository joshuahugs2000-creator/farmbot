# 🌳 FamTree Bot

Réplique complète de @fam_tree_bot avec fonctionnalités supplémentaires.

## Fonctionnalités

### Base (@fam_tree_bot)
- 💍 Mariage, adoption, amitié
- 🌳 Arbre généalogique visuel (image Pillow)
- 🌱 Jardin virtuel (planter, récolter, coins)
- ✨ Waifu du jour
- 🏆 Leaderboard
- 👤 Profil personnalisable (photo, couleur)
- ⚙️ Modes global / groupe

### Fonctionnalités supplémentaires
- 🏠 Nom de famille / clan partagé
- 🎂 Anniversaires de mariage automatiques (annonce chaque année)
- ⭐ Système de karma (+1 / -1 par jour)
- 📸 Photo de famille composite (grille automatique)
- 🕊️ Héritage (coins transmis à la famille lors d'un /leave)
- 👑 Titres dynastiques (Citoyen → Noble → Chevalier → Duc → Roi)

## Installation

### 1. Prérequis
```bash
python 3.11+
PostgreSQL
```

### 2. Cloner et installer
```bash
cd fam_tree_bot
pip install -r requirements.txt
```

### 3. Configuration
```bash
cp .env.example .env
# Édite .env avec ton token et l'URL de ta base de données
```

**`.env` :**
```
BOT_TOKEN=ton_token_botfather
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/fam_tree_bot
```

### 4. Créer la base de données
```bash
createdb fam_tree_bot
# Les tables sont créées automatiquement au démarrage
```

### 5. Lancer
```bash
python main.py
```

## Commandes

| Commande | Description |
|---|---|
| `/marry` | Demande en mariage (répondre ou mentionner) |
| `/adopt` | Adopter un membre |
| `/friend` | Ajouter un ami |
| `/divorce` | Divorcer |
| `/disown` | Désavouer un enfant |
| `/unfriend` | Retirer un ami |
| `/setfamilyname <nom>` | Définir le nom de clan |
| `/leave` | Quitter + transmettre l'héritage |
| `/tree` | Arbre généalogique (image) |
| `/bigtree` | Arbre du groupe (texte) |
| `/garden` | Voir ton jardin |
| `/plant <slot> <plante>` | Planter (rose/sunflower/cherry/apple/diamond) |
| `/harvest [slot]` | Récolter |
| `/waifu` | Waifu du jour |
| `/upvote` | +1 karma (répondre au message) |
| `/downvote` | -1 karma (répondre au message) |
| `/me` | Profil complet |
| `/setpic` | Photo de profil (répondre à une photo) |
| `/customize` | Couleur du profil |
| `/titles` | Liste des titres dynastiques |
| `/familyphoto` | Photo de famille composite |
| `/leaderboard` | Top familles |
| `/mode` | Basculer global ↔ groupe |
| `/toggle garden\|waifu` | Activer/désactiver une fonctionnalité |

## Déploiement Railway / Render

1. Ajoute les variables d'environnement `BOT_TOKEN` et `DATABASE_URL`
2. Commande de démarrage : `python main.py`
3. PostgreSQL est fourni nativement par Railway

## Structure du projet

```
fam_tree_bot/
├── main.py              # Point d'entrée
├── config.py            # Constantes
├── requirements.txt
├── .env.example
├── database/
│   ├── models.py        # Modèles SQLAlchemy
│   └── db.py            # Toutes les opérations DB
├── handlers/
│   ├── family.py        # marry/adopt/friend/divorce...
│   ├── tree.py          # /tree /bigtree
│   ├── garden.py        # Jardin virtuel
│   ├── waifu.py         # Waifu + karma
│   ├── profile.py       # Profil utilisateur
│   ├── misc.py          # start/help/leaderboard...
│   └── events.py        # Jobs automatiques
└── utils/
    ├── helpers.py        # Utilitaires
    └── tree_renderer.py  # Génération d'image Pillow
```
