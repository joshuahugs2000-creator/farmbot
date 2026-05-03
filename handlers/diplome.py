"""
handlers/diplome.py — Système de diplômes avec examens générés par Groq.

Commandes :
  /diplome        — voir ses diplômes et lancer un examen
  /mondomaine     — voir son domaine de spécialisation

Flux d'un examen :
  1. /diplome → bouton "Passer le Bac / Licence / ..."
  2. Pour Licence → choisir un domaine (définitif)
  3. Bot appelle Groq → génère N questions QCM
  4. Questions posées une par une via boutons inline
  5. Résultat → diplôme accordé ou cooldown
"""

import json
import os
import logging
import httpx
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from sqlalchemy import text

from database.db import AsyncSessionLocal, get_user
from utils.helpers import ensure_user

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


# ── Configuration ──────────────────────────────────────────────────────────────

DOMAINS: dict[str, tuple[str, str]] = {
    "finance":      ("📈", "Finance"),
    "informatique": ("💻", "Informatique"),
    "marketing":    ("📣", "Marketing"),
    "droit":        ("⚖️",  "Droit"),
    "management":   ("🏢", "Management"),
    "agriculture":  ("🌾", "Agriculture"),
    "securite":     ("🛡️", "Sécurité"),
}

EXAMS: dict[str, dict] = {
    "bac":     {"emoji": "📄", "label": "Bac",     "n": 10, "required": 7,  "cost": 0,          "cooldown_fail": 6},
    "licence": {"emoji": "🎓", "label": "Licence", "n": 10, "required": 8,  "cost": 500_000,    "cooldown_fail": 12},
    "master":  {"emoji": "🏅", "label": "Master",  "n": 10, "required": 8,  "cost": 5_000_000,  "cooldown_fail": 24},
    "mba":     {"emoji": "👑", "label": "MBA",      "n": 10, "required": 10, "cost": 50_000_000, "cooldown_fail": 24},
}

WORK_BONUS: dict[str, int] = {
    "bac": 10, "licence": 25, "master": 50, "mba": 100,
}

LEVEL_ORDER = ["none", "bac", "licence", "master", "mba"]


# ── Questions de secours (si Groq échoue) ────────────────────────────────────

FALLBACK: dict[str, list] = {
    "bac": [
        {"question": "Quelle est la capitale de la France ?",                      "choices": ["A. Lyon", "B. Marseille", "C. Paris", "D. Bordeaux"],          "correct": 2},
        {"question": "Combien font 15 % de 200 ?",                                 "choices": ["A. 25", "B. 30", "C. 35", "D. 20"],                            "correct": 1},
        {"question": "Quel est le plus grand océan du monde ?",                    "choices": ["A. Atlantique", "B. Indien", "C. Arctique", "D. Pacifique"],    "correct": 3},
        {"question": "En quelle année a eu lieu la Révolution française ?",        "choices": ["A. 1789", "B. 1799", "C. 1776", "D. 1815"],                    "correct": 0},
        {"question": "Quel est le symbole chimique de l'or ?",                     "choices": ["A. Or", "B. Au", "C. Ag", "D. Go"],                            "correct": 1},
        {"question": "Combien de continents y a-t-il sur Terre ?",                 "choices": ["A. 5", "B. 6", "C. 7", "D. 8"],                               "correct": 2},
        {"question": "Qui a peint la Joconde ?",                                   "choices": ["A. Picasso", "B. Michel-Ange", "C. Raphaël", "D. Léonard de Vinci"], "correct": 3},
        {"question": "Quelle est la vitesse de la lumière ?",                      "choices": ["A. 300 000 km/s", "B. 150 000 km/s", "C. 500 000 km/s", "D. 200 000 km/s"], "correct": 0},
        {"question": "Quel gaz représente ~78 % de l'atmosphère terrestre ?",      "choices": ["A. Oxygène", "B. Hydrogène", "C. Azote", "D. CO₂"],            "correct": 2},
        {"question": "Combien de secondes dans une heure ?",                       "choices": ["A. 3 000", "B. 3 600", "C. 6 000", "D. 1 200"],                "correct": 1},
    ],
    "finance": [
        {"question": "Qu'est-ce qu'un dividende ?",                                "choices": ["A. Un impôt sur les bénéfices", "B. Une part des bénéfices versée aux actionnaires", "C. Un prêt bancaire", "D. Une cotisation sociale"], "correct": 1},
        {"question": "Que mesure le taux d'intérêt ?",                            "choices": ["A. Le risque d'une action", "B. Le coût de l'emprunt", "C. Le rendement d'un bien immobilier", "D. La croissance du PIB"], "correct": 1},
        {"question": "Qu'est-ce qu'une action en bourse ?",                       "choices": ["A. Un titre de créance", "B. Une part du capital d'une entreprise", "C. Un contrat d'assurance", "D. Un dépôt bancaire"], "correct": 1},
        {"question": "Qu'est-ce qu'une obligation ?",                              "choices": ["A. Un titre de propriété d'une entreprise", "B. Un titre de dette émis par une société ou un État", "C. Un contrat à terme", "D. Une devise étrangère"], "correct": 1},
        {"question": "Que signifie le sigle ROI ?",                               "choices": ["A. Return On Investment", "B. Rate Of Inflation", "C. Risk Of Insolvency", "D. Ratio Of Income"], "correct": 0},
        {"question": "Qu'est-ce que la liquidité d'un actif ?",                   "choices": ["A. Sa profitabilité", "B. Sa facilité à être converti en cash", "C. Son niveau de risque", "D. Sa durée de vie"], "correct": 1},
        {"question": "Qu'est-ce que le PIB ?",                                    "choices": ["A. Produit Intérieur Brut", "B. Prix Indicatif Bancaire", "C. Plan d'Investissement Boursier", "D. Profit Inter-Bancaire"], "correct": 0},
        {"question": "Qu'est-ce qu'une plus-value ?",                             "choices": ["A. Un bénéfice réalisé lors de la vente d'un actif", "B. Une taxe sur les revenus", "C. Un intérêt composé", "D. Un remboursement de TVA"], "correct": 0},
        {"question": "Quel organisme régule les marchés financiers en France ?",   "choices": ["A. La Banque de France", "B. L'AMF", "C. La BCE", "D. Le FMI"], "correct": 1},
        {"question": "Qu'est-ce qu'un bilan comptable ?",                         "choices": ["A. Un état des flux de trésorerie", "B. Un document listant actifs et passifs", "C. Un compte de résultat", "D. Un plan de financement"], "correct": 1},
    ],
    "informatique": [
        {"question": "Qu'est-ce qu'une boucle 'for' ?",                           "choices": ["A. Une condition logique", "B. Une structure répétant un bloc N fois", "C. Une fonction récursive", "D. Un type de données"], "correct": 1},
        {"question": "Que signifie HTTP ?",                                        "choices": ["A. HyperText Transfer Protocol", "B. High Tech Transmission Process", "C. Hybrid Text Transport Protocol", "D. Hyper Transfer Text Program"], "correct": 0},
        {"question": "Quelle est la base du système binaire ?",                   "choices": ["A. 8", "B. 10", "C. 2", "D. 16"],                              "correct": 2},
        {"question": "Qu'est-ce qu'une base de données relationnelle ?",          "choices": ["A. Une base stockant des fichiers multimédia", "B. Une base organisant les données en tables liées", "C. Une base distribuée sur plusieurs serveurs", "D. Une base en mémoire vive"], "correct": 1},
        {"question": "Que fait la commande 'git commit' ?",                       "choices": ["A. Supprime l'historique", "B. Envoie le code sur GitHub", "C. Enregistre les modifications localement", "D. Fusionne deux branches"], "correct": 2},
        {"question": "Qu'est-ce que le CPU ?",                                    "choices": ["A. Central Processing Unit", "B. Computer Power Unit", "C. Core Program Utility", "D. Central Programmable Unit"], "correct": 0},
        {"question": "Quelle est la différence entre RAM et ROM ?",               "choices": ["A. La RAM est permanente, la ROM est volatile", "B. La RAM est volatile, la ROM est permanente", "C. Les deux sont identiques", "D. La RAM est plus lente"], "correct": 1},
        {"question": "Qu'est-ce qu'une API ?",                                    "choices": ["A. Application Protocol Interface", "B. Application Programming Interface", "C. Advanced Program Integration", "D. Automated Protocol Interface"], "correct": 1},
        {"question": "Quel langage est principalement utilisé pour les pages web ?", "choices": ["A. Python", "B. Java", "C. HTML/CSS", "D. C++"],           "correct": 2},
        {"question": "Que signifie 'SQL' ?",                                      "choices": ["A. Structured Query Language", "B. Simple Query Loop", "C. System Queue Logic", "D. Structured Queue Link"], "correct": 0},
    ],
}

FALLBACK["marketing"] = [
    {"question": "Qu'est-ce que le marketing mix ?",                              "choices": ["A. Un cocktail de boissons", "B. Les 4P : Produit, Prix, Place, Promotion", "C. Un logiciel de gestion", "D. Une technique de vente directe"], "correct": 1},
    {"question": "Que signifie CRM ?",                                            "choices": ["A. Customer Relationship Management", "B. Creative Revenue Model", "C. Content Reach Metrics", "D. Corporate Resource Management"], "correct": 0},
    {"question": "Qu'est-ce qu'un buyer persona ?",                               "choices": ["A. Un concurrent fictif", "B. Le profil type du client idéal", "C. Un outil de publicité payante", "D. Un contrat de vente"], "correct": 1},
    {"question": "Que mesure le NPS (Net Promoter Score) ?",                      "choices": ["A. Le chiffre d'affaires net", "B. La satisfaction et fidélité client", "C. Le trafic sur un site web", "D. Le coût d'acquisition"], "correct": 1},
    {"question": "Qu'est-ce que le SEO ?",                                        "choices": ["A. Social Engagement Optimization", "B. Search Engine Optimization", "C. Sales & Export Operations", "D. System Email Outreach"], "correct": 1},
    {"question": "Qu'est-ce qu'un entonnoir de conversion ?",                     "choices": ["A. Un outil de comptabilité", "B. Le parcours client de la découverte à l'achat", "C. Un rapport financier", "D. Une technique de packaging"], "correct": 1},
    {"question": "Que désigne le terme 'branding' ?",                             "choices": ["A. La gestion des stocks", "B. La construction de l'identité et image de marque", "C. Le recrutement commercial", "D. La fixation des prix"], "correct": 1},
    {"question": "Qu'est-ce que le coût d'acquisition client (CAC) ?",            "choices": ["A. Le prix de revient d'un produit", "B. Le coût moyen pour acquérir un nouveau client", "C. Le salaire des commerciaux", "D. Le budget publicitaire total"], "correct": 1},
    {"question": "Qu'est-ce qu'une étude de marché ?",                           "choices": ["A. Un bilan comptable", "B. Une analyse de l'offre, la demande et la concurrence", "C. Un plan de financement", "D. Un contrat fournisseur"], "correct": 1},
    {"question": "Que signifie B2B ?",                                            "choices": ["A. Back to Basics", "B. Business to Business", "C. Brand to Brand", "D. Budget to Budget"], "correct": 1},
]

FALLBACK["droit"] = [
    {"question": "Qu'est-ce qu'un contrat synallagmatique ?",                     "choices": ["A. Un contrat unilatéral", "B. Un contrat avec obligations réciproques", "C. Un contrat verbal", "D. Un contrat international"], "correct": 1},
    {"question": "Que signifie la présomption d'innocence ?",                    "choices": ["A. Toute personne est coupable jusqu'à preuve du contraire", "B. Toute personne est innocente jusqu'à preuve du contraire", "C. Le juge décide seul de la culpabilité", "D. Le procureur a toujours raison"], "correct": 1},
    {"question": "Qu'est-ce qu'une personne morale ?",                           "choices": ["A. Une personne physique respectueuse", "B. Une entité juridique (société, association...)", "C. Un mineur sous tutelle", "D. Un expert judiciaire"], "correct": 1},
    {"question": "Qu'est-ce que la force majeure en droit ?",                    "choices": ["A. Un avantage contractuel", "B. Un événement imprévisible, irrésistible et extérieur exonérant la responsabilité", "C. Une clause pénale", "D. Un recours en appel"], "correct": 1},
    {"question": "Qu'est-ce qu'un acte notarié ?",                               "choices": ["A. Un document rédigé par un avocat", "B. Un acte authentique rédigé par un notaire", "C. Un jugement du tribunal", "D. Un contrat verbal enregistré"], "correct": 1},
    {"question": "Que désigne la 'prescription' en droit ?",                     "choices": ["A. Une ordonnance médicale", "B. L'extinction d'un droit après un délai légal", "C. Une loi récente", "D. Un avertissement officiel"], "correct": 1},
    {"question": "Qu'est-ce que le droit de la responsabilité civile ?",         "choices": ["A. Le droit pénal appliqué aux civils", "B. L'obligation de réparer le préjudice causé à autrui", "C. Le droit des contrats publics", "D. Le droit électoral"], "correct": 1},
    {"question": "Qu'est-ce qu'une clause pénale ?",                             "choices": ["A. Une peine de prison prévue au contrat", "B. Une indemnité forfaitaire prévue en cas d'inexécution", "C. Une amende fiscale", "D. Une sanction disciplinaire"], "correct": 1},
    {"question": "Que signifie 'in solidum' ?",                                  "choices": ["A. Solidarité familiale", "B. Responsabilité conjointe et solidaire de plusieurs débiteurs", "C. Un contrat en bonne et due forme", "D. Un jugement définitif"], "correct": 1},
    {"question": "Qu'est-ce que la jurisprudence ?",                             "choices": ["A. L'ensemble des lois votées", "B. L'ensemble des décisions de justice qui font référence", "C. Le règlement intérieur d'une entreprise", "D. Les traités internationaux"], "correct": 1},
]

FALLBACK["management"] = [
    {"question": "Qu'est-ce que le management participatif ?",                   "choices": ["A. La prise de décision uniquement par le PDG", "B. L'implication des employés dans les décisions", "C. La gestion automatisée des équipes", "D. Le management à distance"], "correct": 1},
    {"question": "Que désigne le terme 'KPI' ?",                                 "choices": ["A. Key Performance Indicator", "B. Knowledge Process Integration", "C. Key Project Initiative", "D. Knowledge Performance Index"], "correct": 0},
    {"question": "Qu'est-ce que la méthode SMART pour les objectifs ?",          "choices": ["A. Simple, Mesurable, Atteignable, Réaliste, Temporel", "B. Spécifique, Mesurable, Atteignable, Réaliste, Temporel", "C. Stratégique, Mesurable, Ambitieux, Rapide, Traçable", "D. Simple, Motivant, Ambitieux, Réaliste, Technologique"], "correct": 1},
    {"question": "Qu'est-ce que la délégation en management ?",                  "choices": ["A. Transférer une tâche à un subordonné tout en gardant la responsabilité", "B. Se décharger complètement d'une tâche", "C. Recruter un consultant externe", "D. Supprimer un poste"], "correct": 0},
    {"question": "Qu'est-ce qu'un organigramme ?",                               "choices": ["A. Un tableau de bord financier", "B. La représentation graphique de la hiérarchie d'une organisation", "C. Un plan de communication", "D. Un outil de planification de projet"], "correct": 1},
    {"question": "Que mesure le taux d'absentéisme ?",                           "choices": ["A. Le nombre de démissions", "B. La part du temps de travail perdu par les absences", "C. Le retard des livraisons", "D. Le taux de rotation des stocks"], "correct": 1},
    {"question": "Qu'est-ce que la méthode Agile ?",                             "choices": ["A. Une approche de management rigide et planifiée", "B. Une approche itérative et flexible de gestion de projet", "C. Un logiciel de gestion RH", "D. Une technique de recrutement"], "correct": 1},
    {"question": "Qu'est-ce que le turnover en entreprise ?",                    "choices": ["A. Le chiffre d'affaires annuel", "B. Le taux de renouvellement du personnel", "C. La rotation des stocks", "D. Le changement de direction"], "correct": 1},
    {"question": "Qu'est-ce qu'un plan de formation ?",                          "choices": ["A. Le planning des congés", "B. Le document définissant les actions de développement des compétences", "C. Le budget marketing", "D. Le règlement intérieur"], "correct": 1},
    {"question": "Que désigne le leadership transformationnel ?",                "choices": ["A. La gestion administrative quotidienne", "B. Un style de leadership inspirant le changement et la vision", "C. Le management par les chiffres", "D. La supervision stricte des équipes"], "correct": 1},
]

FALLBACK["agriculture"] = [
    {"question": "Qu'est-ce que l'agroforesterie ?",                             "choices": ["A. L'agriculture en forêt uniquement", "B. La combinaison d'arbres, cultures et/ou élevage sur la même parcelle", "C. La culture hors-sol", "D. La gestion des forêts tropicales"], "correct": 1},
    {"question": "Que signifie pH du sol en agriculture ?",                      "choices": ["A. Le taux d'humidité", "B. L'acidité ou alcalinité du sol", "C. La proportion d'humus", "D. La température moyenne du sol"], "correct": 1},
    {"question": "Qu'est-ce que la rotation des cultures ?",                    "choices": ["A. Cultiver la même plante chaque année", "B. Alterner différentes cultures sur une même parcelle", "C. Arroser en tournant autour du champ", "D. Retourner la terre mécaniquement"], "correct": 1},
    {"question": "Qu'est-ce qu'un intrant agricole ?",                           "choices": ["A. Un revenu agricole", "B. Une ressource utilisée dans la production (semence, engrais, pesticide...)", "C. Une subvention gouvernementale", "D. Un équipement de récolte"], "correct": 1},
    {"question": "Que désigne l'agriculture de précision ?",                     "choices": ["A. L'agriculture manuelle traditionnelle", "B. L'utilisation de technologies (GPS, drones, capteurs) pour optimiser les rendements", "C. L'agriculture en serre fermée", "D. La production artisanale"], "correct": 1},
    {"question": "Qu'est-ce que la jachère ?",                                   "choices": ["A. Une mauvaise herbe invasive", "B. Laisser une terre au repos pour la régénérer", "C. Un type de labour profond", "D. Une technique d'irrigation"], "correct": 1},
    {"question": "Qu'est-ce que le compostage ?",                               "choices": ["A. Une technique de taille des arbres", "B. La décomposition de matières organiques pour produire un amendement naturel", "C. Un procédé de conservation des semences", "D. Un système d'irrigation goutte à goutte"], "correct": 1},
    {"question": "Que signifie 'agriculture raisonnée' ?",                       "choices": ["A. Agriculture 100% bio", "B. Agriculture minimisant l'impact environnemental tout en restant rentable", "C. Agriculture uniquement pour autoconsommation", "D. Agriculture intensive maximisant le rendement"], "correct": 1},
    {"question": "Qu'est-ce que l'élevage extensif ?",                           "choices": ["A. L'élevage en bâtiment avec forte densité", "B. L'élevage avec de grands espaces et faible densité animale", "C. L'élevage hors-sol", "D. L'élevage de volailles uniquement"], "correct": 1},
    {"question": "Qu'est-ce que la permaculture ?",                              "choices": ["A. La culture permanente sans repos du sol", "B. Un système de conception agricole imitant les écosystèmes naturels", "C. La monoculture intensive", "D. L'agriculture aquatique"], "correct": 1},
]

FALLBACK["securite"] = [
    {"question": "Qu'est-ce qu'une attaque par phishing ?",                      "choices": ["A. Une intrusion physique dans un bâtiment", "B. Une tentative de tromper un utilisateur pour voler ses données", "C. Un virus informatique qui chiffre les fichiers", "D. Une panne de réseau planifiée"], "correct": 1},
    {"question": "Que signifie 'RGPD' ?",                                        "choices": ["A. Règlement Général sur la Protection des Données", "B. Réseau de Gestion des Protocoles Digitaux", "C. Règles Générales de Prévention des Dommages", "D. Registre Global des Personnes et Données"], "correct": 0},
    {"question": "Qu'est-ce qu'un pare-feu (firewall) ?",                        "choices": ["A. Un logiciel antivirus", "B. Un système filtrant le trafic réseau selon des règles de sécurité", "C. Un protocole de chiffrement", "D. Un serveur de sauvegarde"], "correct": 1},
    {"question": "Qu'est-ce que l'authentification à deux facteurs (2FA) ?",     "choices": ["A. Un double mot de passe identique", "B. Une vérification d'identité combinant deux méthodes distinctes", "C. Un accès partagé entre deux utilisateurs", "D. Une sauvegarde automatique des données"], "correct": 1},
    {"question": "Que désigne un ransomware ?",                                  "choices": ["A. Un logiciel espion", "B. Un malware qui chiffre les données et demande une rançon", "C. Un outil de surveillance réseau", "D. Un virus de messagerie"], "correct": 1},
    {"question": "Qu'est-ce qu'un audit de sécurité ?",                         "choices": ["A. Une réunion d'équipe hebdomadaire", "B. Une évaluation systématique des failles et mesures de sécurité", "C. Un contrat de maintenance informatique", "D. Un rapport financier annuel"], "correct": 1},
    {"question": "Que signifie 'chiffrement de bout en bout' ?",                 "choices": ["A. Le chiffrement du serveur uniquement", "B. Les données sont chiffrées de l'expéditeur au destinataire sans décryptage intermédiaire", "C. Un chiffrement partiel des métadonnées", "D. Le chiffrement du canal réseau uniquement"], "correct": 1},
    {"question": "Qu'est-ce qu'une politique de sécurité des systèmes d'information (PSSI) ?", "choices": ["A. Un antivirus d'entreprise", "B. L'ensemble des règles et procédures pour protéger le SI", "C. Un plan de reprise d'activité", "D. Un contrat de prestation informatique"], "correct": 1},
    {"question": "Qu'est-ce qu'une vulnérabilité zero-day ?",                    "choices": ["A. Une faille connue depuis longtemps et non corrigée", "B. Une faille inconnue du fabricant, exploitée avant tout correctif", "C. Un bug qui apparaît le premier jour du déploiement", "D. Une faille corrigée en urgence"], "correct": 1},
    {"question": "Que désigne le principe du 'moindre privilège' ?",             "choices": ["A. Donner le maximum d'accès à tous les utilisateurs", "B. N'accorder à chaque utilisateur que les droits strictement nécessaires à sa tâche", "C. Restreindre l'accès uniquement aux administrateurs", "D. Supprimer tous les comptes non utilisés"], "correct": 1},
]

# Pour les domaines encore sans fallback, on utilise les questions Bac
for _d in DOMAINS:
    if _d not in FALLBACK:
        FALLBACK[_d] = list(FALLBACK["bac"])  # copie, pas une référence


# ── Helpers ───────────────────────────────────────────────────────────────────

def _user_level(u) -> str:
    if getattr(u, "diplome_mba",     False): return "mba"
    if getattr(u, "diplome_master",  False): return "master"
    if getattr(u, "diplome_licence", False): return "licence"
    if getattr(u, "diplome_bac",     False): return "bac"
    return "none"


def _next_level(current: str) -> str | None:
    idx = LEVEL_ORDER.index(current)
    return LEVEL_ORDER[idx + 1] if idx < len(LEVEL_ORDER) - 1 else None


# ── Génération Groq ───────────────────────────────────────────────────────────

def _build_prompt(level: str, domain_label: str, level_label: str, n: int, seed: int) -> str:
    """Construit le prompt Groq selon le niveau."""
    import random
    if level == "bac":
        themes = [
            "géographie mondiale", "histoire", "sciences naturelles",
            "mathématiques de base", "culture générale", "économie de base",
            "littérature", "sports et records", "gastronomie et culture",
            "technologie et inventions", "politique mondiale", "astronomie",
            "biologie", "physique", "chimie de base", "philosophie",
        ]
        random.shuffle(themes)
        themes_choisis = ", ".join(themes[:5])
        return (
            f"[SEED:{seed}] Génère EXACTEMENT {n} questions QCM de culture générale niveau Bac. "
            f"Thèmes obligatoires : {themes_choisis}. Questions VARIÉES et DIFFÉRENTES à chaque fois. "
            f"RÉPONDS UNIQUEMENT avec le JSON brut, aucun texte avant ou après, aucun markdown. "
            f'Format strict : [{{"question":"...","choices":["A. ...","B. ...","C. ...","D. ..."],"correct":0}},...] '
            f"Génère exactement {n} objets dans le tableau. Le champ correct est l'index 0-3 de la bonne réponse."
        )
    else:
        hardness = {"licence": "intermédiaire", "master": "avancé", "mba": "expert et pointu"}[level]
        sous_themes = {
            "finance": "comptabilité, marchés financiers, fiscalité, trésorerie, gestion des risques",
            "informatique": "algorithmes, réseaux, bases de données, cybersécurité, architecture logicielle",
            "marketing": "stratégie marketing, branding, digital, pricing, comportement consommateur",
            "droit": "droit des contrats, droit du travail, droit commercial, procédure civile",
            "management": "leadership, gestion de projet, RH, stratégie d'entreprise, organisation",
            "agriculture": "agronomie, élevage, agroéconomie, développement durable, techniques agricoles",
            "securite": "cybersécurité, sécurité physique, gestion des risques, RGPD, audit",
        }.get(level if level == "bac" else domain_label.lower(), "concepts professionnels avancés")
        return (
            f"[SEED:{seed}] Génère EXACTEMENT {n} questions QCM niveau {hardness} en {domain_label} ({level_label}). "
            f"Sous-thèmes à couvrir : {sous_themes}. Questions PROFESSIONNELLES, PRÉCISES et VARIÉES. "
            f"RÉPONDS UNIQUEMENT avec le JSON brut, aucun texte avant ou après, aucun markdown. "
            f'Format strict : [{{"question":"...","choices":["A. ...","B. ...","C. ...","D. ..."],"correct":0}},...] '
            f"Génère exactement {n} objets dans le tableau. Le champ correct est l'index 0-3 de la bonne réponse."
        )


def _parse_groq_raw(raw: str) -> list:
    """Extrait et parse le JSON d'une réponse Groq, même si elle contient du markdown."""
    raw = raw.strip()

    # Cas 1 : déjà propre
    if raw.startswith("["):
        return json.loads(raw)

    # Cas 2 : bloc ```json ... ```
    if "```" in raw:
        import re
        match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.DOTALL)
        if match:
            return json.loads(match.group(1))

    # Cas 3 : chercher le premier '[' et le dernier ']'
    start = raw.find("[")
    end   = raw.rfind("]")
    if start != -1 and end != -1 and end > start:
        return json.loads(raw[start:end + 1])

    raise ValueError(f"Impossible d'extraire un tableau JSON de la réponse Groq: {raw[:200]}")


def _validate_questions(questions: list, n: int) -> list:
    """Valide et normalise les questions. Lève ValueError si trop peu."""
    valid = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        if "question" not in q or "choices" not in q or "correct" not in q:
            continue
        choices = q["choices"]
        if not isinstance(choices, list) or len(choices) < 2:
            continue
        # Normaliser à 4 choix si besoin
        while len(choices) < 4:
            choices.append("D. —")
        q["choices"] = choices[:4]
        try:
            correct = int(q["correct"])
            if not (0 <= correct <= 3):
                correct = 0
            q["correct"] = correct
        except Exception:
            q["correct"] = 0
        valid.append(q)

    if len(valid) < max(1, n // 2):
        raise ValueError(f"Trop peu de questions valides : {len(valid)}/{n}")

    # Si on a moins que n, on duplique les dernières (rare)
    while len(valid) < n:
        valid.append(valid[-1].copy())

    return valid[:n]


async def _groq_questions(level: str, domain: str, n: int) -> list | None:
    if not GROQ_API_KEY:
        logger.warning("GROQ_API_KEY non définie — fallback local activé")
        return None

    import random
    domain_label = DOMAINS.get(domain, ("", domain.capitalize()))[1]
    level_label  = EXAMS[level]["label"]

    for attempt in range(2):  # 2 tentatives
        seed   = random.randint(1000, 9999)
        prompt = _build_prompt(level, domain_label, level_label, n, seed)
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                resp = await client.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "model":       "llama-3.3-70b-versatile",
                        "messages":    [{"role": "user", "content": prompt}],
                        "temperature": 0.9 + attempt * 0.05,
                        "max_tokens":  7000,
                    },
                )
            if resp.status_code != 200:
                logger.error(f"Groq HTTP {resp.status_code}: {resp.text[:300]}")
                continue

            data = resp.json()
            raw  = data["choices"][0]["message"]["content"]
            logger.info(f"Groq raw (attempt {attempt+1}): {raw[:120]}...")

            questions = _parse_groq_raw(raw)
            validated = _validate_questions(questions, n)
            logger.info(f"Groq OK — {len(validated)} questions générées pour {level}/{domain}")
            return validated

        except Exception as e:
            logger.error(f"Groq tentative {attempt+1} échouée ({level}/{domain}): {e}")

    logger.error(f"Groq complètement échoué pour {level}/{domain} — fallback local")
    return None


# ── /diplome ──────────────────────────────────────────────────────────────────

async def diplome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update.effective_user)
    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.user_id)

    current = _user_level(u)
    domain  = getattr(u, "diplome_domain", None)
    d_emoji, d_label = DOMAINS.get(domain, ("🎓", "—")) if domain else ("—", "—")

    # Cooldown
    cd = getattr(u, "exam_cooldown", None)
    cd_active = cd and cd > datetime.utcnow()
    cd_line = ""
    if cd_active:
        delta = cd - datetime.utcnow()
        h = int(delta.total_seconds() // 3600)
        m = int((delta.total_seconds() % 3600) // 60)
        cd_line = f"\n⏳ <b>Prochain examen dans :</b> {h}h{m:02d}m"

    # Affichage des niveaux
    def _status(lvl):
        return "✅" if getattr(u, f"diplome_{lvl}", False) else "⬜"

    bonus_line = ""
    if current != "none":
        bonus_line = f"\n💰 Bonus /work actif : <b>+{WORK_BONUS.get(current, 0)}%</b>"

    lines = [
        "🎓 <b>VOS DIPLÔMES</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"{_status('bac')}  📄 <b>Bac</b>  — Gratuit  (7/10 requis)",
        f"{_status('licence')}  🎓 <b>Licence</b>  — {_fmt(500_000)} 💰  (8/10 requis)",
        f"{_status('master')}  🏅 <b>Master</b>  — {_fmt(5_000_000)} 💰  (8/10 requis)",
        f"{_status('mba')}  👑 <b>MBA</b>  — {_fmt(50_000_000)} 💰  (10/10 requis — parfait ✨)",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"{'📌' if domain else '—'} <b>Domaine :</b> {d_emoji + ' ' + d_label if domain else '—  (choisi à la Licence)'}",
        bonus_line,
        cd_line,
    ]

    next_lvl = _next_level(current)
    keyboard = None

    if next_lvl and not cd_active:
        info     = EXAMS[next_lvl]
        cost_str = f"— {_fmt(info['cost'])} 💰" if info["cost"] else "— Gratuit"

        if next_lvl == "bac":
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"📝 Passer le {info['label']}  {cost_str}", callback_data="exam:begin:bac:bac")
            ]])
        elif next_lvl == "licence" and not domain:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"📝 Passer la {info['label']}  {cost_str}", callback_data="exam:domain:licence")
            ]])
        elif next_lvl in ("master", "mba") and domain:
            # Domaine verrouillé — obligatoire de repasser dans le même domaine
            d_em, d_lb = DOMAINS.get(domain, ("🎓", domain))
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"📝 Passer le {info['label']}  {d_em} {d_lb}  {cost_str}",
                    callback_data=f"exam:begin:{next_lvl}:{domain}"
                )
            ]])
        else:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"📝 Passer le {info['label']}  {cost_str}", callback_data=f"exam:begin:{next_lvl}:{domain or 'general'}")
            ]])
    elif current == "mba":
        lines.append("\n🏆 Tu as tous les diplômes ! Félicitations.")

    await update.message.reply_text(
        "\n".join(l for l in lines if l is not None),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# ── Callbacks ─────────────────────────────────────────────────────────────────

async def diplome_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    uid     = query.from_user.id
    parts   = query.data.split(":")   # exam:action:...
    action  = parts[1]

    if action == "domain":
        # Afficher les choix de domaine
        level = parts[2]
        rows  = []
        row   = []
        for key, (emoji, label) in DOMAINS.items():
            row.append(InlineKeyboardButton(f"{emoji} {label}", callback_data=f"exam:begin:{level}:{key}"))
            if len(row) == 2:
                rows.append(row); row = []
        if row:
            rows.append(row)
        await query.edit_message_text(
            "🎓 <b>Choisissez votre domaine de spécialisation</b>\n\n"
            "⚠️ Ce choix est <b>définitif</b> — il s'appliquera à votre Licence, Master et MBA.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )

    elif action == "begin":
        level  = parts[2]
        domain = parts[3]
        await _start_exam(query, context, uid, level, domain)

    elif action == "answer":
        level  = parts[2]
        domain = parts[3]
        q_idx  = int(parts[4])
        answer = int(parts[5])
        await _handle_answer(query, context, uid, level, domain, q_idx, answer)


# ── Déroulement de l'examen ───────────────────────────────────────────────────

async def _start_exam(query, context, uid: int, level: str, domain: str):
    async with AsyncSessionLocal() as session:
        row = await session.execute(text("SELECT * FROM users WHERE user_id = :uid"), {"uid": uid})
        u   = row.fetchone()

    if not u:
        return await query.edit_message_text("❌ Compte introuvable. Fais /start d'abord.")

    # Cooldown
    cd = getattr(u, "exam_cooldown", None)
    if cd and cd > datetime.utcnow():
        delta = cd - datetime.utcnow()
        h = int(delta.total_seconds() // 3600)
        m = int((delta.total_seconds() % 3600) // 60)
        return await query.edit_message_text(f"⏳ Examen disponible dans <b>{h}h{m:02d}m</b>.", parse_mode=ParseMode.HTML)

    # Ordre des niveaux
    current = _user_level(u)
    if _next_level(current) != level:
        return await query.edit_message_text("❌ Tu dois obtenir le diplôme précédent d'abord.")

    # Verif domaine verrouille (Master / MBA dans le meme domaine que la Licence)
    saved_domain = getattr(u, "diplome_domain", None)
    if level in ("master", "mba") and saved_domain and domain != saved_domain:
        d_em, d_lb = DOMAINS.get(saved_domain, ("🎓", saved_domain))
        return await query.edit_message_text(
            f"❌ Tu dois passer le {level.capitalize()} dans ton domaine de Licence : "
            f"<b>{d_em} {d_lb}</b>",
            parse_mode=ParseMode.HTML,
        )

    # Ancienneté Master (20 jours)
    if level == "master" and u.created_at:
        days = (datetime.utcnow() - u.created_at).days
        if days < 20:
            return await query.edit_message_text(
                f"❌ Le Master requiert <b>20 jours</b> d'ancienneté.\nTu en as {days}/20.",
                parse_mode=ParseMode.HTML,
            )

    # Coût
    info = EXAMS[level]
    cost = info["cost"]
    if cost > 0:
        if u.coins < cost:
            return await query.edit_message_text(
                f"❌ Il te faut <b>{_fmt(cost)} 💰</b> pour cet examen.\nTon solde : <b>{_fmt(u.coins)} 💰</b>",
                parse_mode=ParseMode.HTML,
            )
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("UPDATE users SET coins = CAST(coins AS BIGINT) - :c WHERE user_id = :uid"),
                {"c": cost, "uid": uid},
            )
            await session.commit()

    # Générer les questions
    await query.edit_message_text(
        f"🔄 <b>Génération de l'examen en cours…</b>\n"
        f"{info['emoji']} {info['label']}"
        + (f"  ·  {DOMAINS.get(domain, ('',''))[1]}" if level != 'bac' else ""),
        parse_mode=ParseMode.HTML,
    )

    questions = await _groq_questions(level, domain, info["n"])
    if not questions:
        # Fallback local
        fb_key    = domain if domain in FALLBACK else "bac"
        questions = FALLBACK.get(fb_key, FALLBACK["bac"])[: info["n"]]
        if not questions:
            # Rembourser
            if cost > 0:
                async with AsyncSessionLocal() as session:
                    await session.execute(
                        text("UPDATE users SET coins = CAST(coins AS BIGINT) + :c WHERE user_id = :uid"),
                        {"c": cost, "uid": uid},
                    )
                    await session.commit()
            return await query.edit_message_text("❌ Impossible de générer l'examen. Réessaie dans quelques minutes.")

    # Stocker la session
    context.user_data[f"exam_{uid}"] = {
        "level":     level,
        "domain":    domain,
        "questions": questions,
        "score":     0,
        "total":     info["n"],
    }

    await _show_question(query, context, uid, 0)


QUESTION_TIMEOUT = 20  # secondes par question


def _timer_bar(remaining: int, total: int = QUESTION_TIMEOUT) -> str:
    """Barre de progression du timer."""
    filled = round((remaining / total) * 10)
    bar = "🟩" * filled + "⬜" * (10 - filled)
    return bar


async def _show_question(query, context, uid: int, q_idx: int):
    data = context.user_data.get(f"exam_{uid}")
    if not data:
        return

    import asyncio

    q      = data["questions"][q_idx]
    level  = data["level"]
    domain = data["domain"]
    total  = data["total"]
    score  = data["score"]
    info   = EXAMS[level]

    buttons = [
        [InlineKeyboardButton(choice, callback_data=f"exam:answer:{level}:{domain}:{q_idx}:{i}")]
        for i, choice in enumerate(q["choices"])
    ]

    # Marquer la question active + générer un token unique pour ce tour
    import time
    turn_token = time.monotonic()
    data["current_q"]    = q_idx
    data["turn_token"]   = turn_token

    def _build_text(remaining: int) -> str:
        bar = _timer_bar(remaining)
        return (
            f"{info['emoji']} <b>{info['label']}</b>  ·  Question {q_idx + 1}/{total}\n"
            f"✅ Score : {score}/{q_idx}  |  ⏱ {bar} <b>{remaining}s</b>\n\n"
            f"❓ <b>{q['question']}</b>"
        )

    async def _edit_msg(msg_obj, text, markup=None):
        """Édite peu importe si c'est un CallbackQuery ou un Message."""
        try:
            if hasattr(msg_obj, "edit_message_text"):
                await msg_obj.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            else:
                await msg_obj.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception:
            pass

    # Affichage initial
    await _edit_msg(query, _build_text(QUESTION_TIMEOUT), InlineKeyboardMarkup(buttons))

    # Récupérer l'objet message pour le countdown
    # On a besoin du vrai Message pour edit_text dans la tâche de fond
    try:
        if hasattr(query, "message"):
            msg = query.message   # CallbackQuery → .message
        else:
            msg = query           # déjà un Message
    except Exception:
        msg = query

    # Countdown en tâche de fond
    async def _countdown():
        try:
            for remaining in range(QUESTION_TIMEOUT - 5, 0, -5):
                await asyncio.sleep(5)
                current_data = context.user_data.get(f"exam_{uid}")
                # Stopper si : session perdue, question changée, ou token différent
                if (not current_data
                        or current_data.get("current_q") != q_idx
                        or current_data.get("turn_token") != turn_token):
                    return
                try:
                    await msg.edit_text(
                        _build_text(remaining),
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup(buttons),
                    )
                except Exception:
                    pass

            # Dernier tick — vérifier une ultime fois
            await asyncio.sleep(5)
            current_data = context.user_data.get(f"exam_{uid}")
            if (not current_data
                    or current_data.get("current_q") != q_idx
                    or current_data.get("turn_token") != turn_token):
                return  # Le joueur a répondu entre-temps

            # Temps écoulé → question ratée
            current_data["current_q"]  = -1
            current_data["turn_token"] = None
            next_idx = q_idx + 1

            try:
                await msg.edit_text(
                    f"⏰ <b>Temps écoulé !</b>\n\n"
                    f"❌ Question {q_idx + 1} ratée — pas de réponse.\n"
                    f"Score : {current_data['score']}/{q_idx + 1}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

            await asyncio.sleep(2)
            if next_idx >= current_data["total"]:
                await _finish_exam(msg, context, uid, current_data)
            else:
                await _show_question(msg, context, uid, next_idx)

        except Exception as e:
            logger.debug(f"Countdown error q{q_idx}: {e}")

    asyncio.create_task(_countdown())


async def _handle_answer(query, context, uid: int, level: str, domain: str, q_idx: int, answer: int):
    data = context.user_data.get(f"exam_{uid}")
    if not data:
        return await query.edit_message_text("❌ Session expirée. Refais /diplome pour recommencer.")

    # Ignorer si cette question a déjà été traitée (double-clic ou timer)
    if data.get("current_q") != q_idx:
        try:
            await query.answer("⚠️ Réponse déjà enregistrée !", show_alert=False)
        except Exception:
            pass
        return

    # Verrouiller immédiatement pour stopper le countdown
    data["current_q"]  = -1
    data["turn_token"] = None

    correct = int(data["questions"][q_idx]["correct"])
    is_correct = (answer == correct)
    if is_correct:
        data["score"] += 1

    # Feedback visuel rapide
    emoji = "✅" if is_correct else "❌"
    bonne = data["questions"][q_idx]["choices"][correct]
    try:
        await query.edit_message_text(
            f"{emoji} <b>{'Bonne réponse !' if is_correct else 'Mauvaise réponse...'}</b>\n"
            f"{'✔️' if is_correct else f'La bonne réponse était : <b>{bonne}</b>'}\n\n"
            f"Score : {data['score']}/{q_idx + 1}",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    import asyncio
    await asyncio.sleep(1.5)

    next_idx = q_idx + 1
    if next_idx >= data["total"]:
        await _finish_exam(query, context, uid, data)
    else:
        await _show_question(query, context, uid, next_idx)


async def _finish_exam(query, context, uid: int, data: dict):
    """
    query peut être un CallbackQuery (réponse bouton) ou un Message (timeout timer).
    On normalise l'appel edit pour supporter les deux cas.
    """
    level    = data["level"]
    domain   = data["domain"]
    score    = data["score"]
    total    = data["total"]
    info     = EXAMS[level]
    required = info["required"]
    success  = score >= required

    async def _edit(text: str, **kwargs):
        """Édite le message peu importe si c'est un CallbackQuery ou un Message."""
        try:
            if hasattr(query, "edit_message_text"):
                # CallbackQuery
                await query.edit_message_text(text, **kwargs)
            else:
                # Message (appelé depuis le timer)
                await query.edit_text(text, **kwargs)
        except Exception as e:
            logger.warning(f"_finish_exam edit error: {e}")

    async with AsyncSessionLocal() as session:
        params = {"uid": uid}
        sets   = []

        if success:
            sets.append(f"diplome_{level} = TRUE")
            # Sauvegarder le domaine à la Licence (définitif)
            if level == "licence" and domain not in ("bac", "general", None):
                sets.append("diplome_domain = :dom")
                params["dom"] = domain
            sets.append("exam_cooldown = NULL")
        else:
            cd_dt = datetime.utcnow() + timedelta(hours=info["cooldown_fail"])
            sets.append("exam_cooldown = :cd")
            params["cd"] = cd_dt

        await session.execute(
            text(f"UPDATE users SET {', '.join(sets)} WHERE user_id = :uid"),
            params,
        )
        await session.commit()

    context.user_data.pop(f"exam_{uid}", None)

    if success:
        bonus = WORK_BONUS.get(level, 0)
        d_str = ""
        if level != "bac" and domain not in ("bac", "general", None):
            d_str = f"  ·  {DOMAINS.get(domain, ('',''))[1]}"
        await _edit(
            f"🎉 <b>FÉLICITATIONS !</b>\n\n"
            f"✅ Diplôme obtenu : <b>{info['emoji']} {info['label']}{d_str}</b>\n"
            f"📊 Score : <b>{score}/{total}</b>\n\n"
            f"💰 Bonus /work permanent : <b>+{bonus}%</b>\n"
            f"🏆 Badge visible sur ton /me !\n\n"
            f"Tape /diplome pour voir ta progression.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await _edit(
            f"❌ <b>ÉCHEC</b>\n\n"
            f"Score : <b>{score}/{total}</b>  (minimum requis : <b>{required}/{total}</b>)\n\n"
            f"⏳ Nouveau tentative disponible dans <b>{info['cooldown_fail']}h</b>.\n"
            f"Tape /diplome pour voir le cooldown.",
            parse_mode=ParseMode.HTML,
        )
