"""
Générateur de cartes de relation (mariage, adoption, amitié).
Reproduit le style de la carte d'invitation avec photos de profil,
ornements et texte centré.
"""
import io
import math
from datetime import datetime
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageChops
import logging

logger = logging.getLogger(__name__)

# ─── PALETTE & DIMENSIONS ────────────────────────────────────────────────────
W, H = 600, 900
BG_TOP    = (55, 45, 40)
BG_BOTTOM = (20, 18, 15)
ACCENT    = (180, 80, 80)       # rouge-brun (ornements)
GOLD      = (210, 175, 100)
WHITE     = (255, 255, 255)
CREAM     = (240, 230, 210)
DARK      = (30, 25, 20)


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def _gradient_bg(size):
    """Fond dégradé vertical sombre."""
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    for y in range(size[1]):
        t = y / size[1]
        r = int(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t)
        g = int(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t)
        b = int(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t)
        draw.line([(0, y), (size[0], y)], fill=(r, g, b))
    return img


def _mandala_ornament(size=180, color=ACCENT):
    """Dessine un ornement mandala-floral simplifié."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx, cy = size // 2, size // 2
    R = size // 2

    # Cercles concentriques discrets
    for r in range(R, 0, -12):
        alpha = int(80 * (r / R))
        draw.ellipse([cx-r, cy-r, cx+r, cy+r],
                     outline=(*color, alpha), width=1)

    # Pétales rayonnants
    for i in range(16):
        angle = math.radians(i * (360 / 16))
        for petal_r in [0.5, 0.75]:
            px = cx + int(R * petal_r * math.cos(angle))
            py = cy + int(R * petal_r * math.sin(angle))
            pr = int(R * 0.12)
            alpha = 150 if petal_r == 0.5 else 100
            draw.ellipse([px-pr, py-pr, px+pr, py+pr],
                         fill=(*color, alpha))

    # Cercle central
    draw.ellipse([cx-12, cy-12, cx+12, cy+12],
                 fill=(*GOLD, 180))

    # Triangles décoratifs aux diagonales
    for i in range(8):
        angle = math.radians(i * 45 + 22.5)
        x1 = cx + int(R * 0.9 * math.cos(angle))
        y1 = cy + int(R * 0.9 * math.sin(angle))
        x2 = cx + int(R * 0.7 * math.cos(angle - 0.15))
        y2 = cy + int(R * 0.7 * math.sin(angle - 0.15))
        x3 = cx + int(R * 0.7 * math.cos(angle + 0.15))
        y3 = cy + int(R * 0.7 * math.sin(angle + 0.15))
        draw.polygon([(x1, y1), (x2, y2), (x3, y3)], fill=(*ACCENT, 140))

    return img


def _circle_photo(img_bytes: bytes | None, size: int) -> Image.Image:
    """Découpe la photo en cercle avec bordure dorée."""
    result = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    if img_bytes:
        try:
            src = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
            src = src.resize((size, size), Image.LANCZOS)
        except Exception:
            src = None
    else:
        src = None

    # Fond de secours
    if src is None:
        src = Image.new("RGBA", (size, size), (60, 55, 50, 255))
        d = ImageDraw.Draw(src)
        d.text((size//2, size//2), "?", fill=CREAM, anchor="mm")

    # Masque circulaire
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size-1, size-1], fill=255)

    result.paste(src, mask=mask)

    # Bordure dorée
    border_draw = ImageDraw.Draw(result)
    bw = max(3, size // 40)
    border_draw.ellipse([bw//2, bw//2, size-bw//2, size-bw//2],
                        outline=GOLD, width=bw)

    return result


def _get_font(size: int, bold: bool = False):
    """Essaie de charger DejaVu, sinon police par défaut."""
    paths = [
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'Bold' if bold else ''}.ttf",
        f"/usr/share/fonts/dejavu/DejaVuSans{'Bold' if bold else ''}.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _draw_centered(draw, text, y, font, color=WHITE, letter_spacing=3):
    """Texte centré avec espacement optionnel entre lettres."""
    # Calcul largeur totale avec espacement
    total_w = 0
    for ch in text:
        bb = font.getbbox(ch)
        total_w += (bb[2] - bb[0]) + letter_spacing
    total_w -= letter_spacing

    x = (W - total_w) // 2
    for ch in text:
        draw.text((x, y), ch, font=font, fill=color)
        bb = font.getbbox(ch)
        x += (bb[2] - bb[0]) + letter_spacing


def _separator_line(draw, y, color=GOLD):
    """Ligne décorative centrée."""
    lw = 160
    cx = W // 2
    draw.line([(cx - lw, y), (cx - 20, y)], fill=color, width=1)
    draw.line([(cx + 20, y), (cx + lw, y)], fill=color, width=1)
    # Losange central
    draw.polygon([
        (cx, y - 4), (cx + 8, y), (cx, y + 4), (cx - 8, y)
    ], fill=color)


# ─── FONCTION PRINCIPALE ─────────────────────────────────────────────────────

def generate_relation_card(
    name1: str,
    name2: str,
    relation: str,       # "married", "adopted", "friends"
    photo1: bytes | None = None,
    photo2: bytes | None = None,
) -> bytes:
    """
    Génère une carte de relation et retourne les bytes JPEG.

    relation: "married" | "adopted" | "friends"
    """

    # ── Textes selon relation ──
    if relation == "married":
        top_label    = "YOU ARE INVITED TO THE WEDDING OF"
        connector    = "&"
        bottom_label = "MARIAGE"
        emoji_accent = None
    elif relation == "adopted":
        top_label    = "UNE NOUVELLE FAMILLE EST NEE"
        connector    = "adopte"
        bottom_label = "ADOPTION"
        emoji_accent = None
    else:  # friends
        top_label    = "UNE BELLE AMITIE EST NEE"
        connector    = "&"
        bottom_label = "AMITIE"
        emoji_accent = None

    date_str = datetime.utcnow().strftime("%B %d   %I %p").upper()

    # ── Canvas ──
    canvas = _gradient_bg((W, H))
    draw   = ImageDraw.Draw(canvas)

    # ── Ornements mandala (coins) ──
    msize = 210
    mandala = _mandala_ornament(msize, ACCENT)
    # Coin haut-gauche
    canvas.paste(mandala, (-msize//4, -msize//4), mandala)
    # Coin haut-droit (retourné horizontalement)
    canvas.paste(mandala.transpose(Image.FLIP_LEFT_RIGHT),
                 (W - msize + msize//4, -msize//4), mandala)
    # Coin bas (centré, plus petit)
    mbot = _mandala_ornament(300, (40, 35, 30))
    canvas.paste(mbot, (W//2 - 150, H - 200), mbot)

    # ── Photos de profil ──
    photo_size = 190
    gap        = 30
    total_w    = 2 * photo_size + gap
    left_x     = (W - total_w) // 2
    right_x    = left_x + photo_size + gap
    photo_y    = 160

    p1 = _circle_photo(photo1, photo_size)
    p2 = _circle_photo(photo2, photo_size)
    canvas.paste(p1, (left_x, photo_y), p1)
    canvas.paste(p2, (right_x, photo_y), p2)

    # ── Typographie ──
    font_small  = _get_font(14)
    font_name   = _get_font(42, bold=True)
    font_conn   = _get_font(36, bold=True)
    font_date   = _get_font(28, bold=True)
    font_label  = _get_font(13)

    # Ligne supérieure (label catégorie)
    _draw_centered(draw, top_label, photo_y + photo_size + 40, font_small,
                   color=(190, 180, 165), letter_spacing=2)

    # Nom 1
    name1_y = photo_y + photo_size + 70
    _draw_centered(draw, name1.upper(), name1_y, font_name,
                   color=WHITE, letter_spacing=2)

    bb1 = font_name.getbbox(name1.upper())
    name1_h = bb1[3] - bb1[1]

    # Connecteur (&, "adopte"…)
    conn_y = name1_y + name1_h + 8
    _draw_centered(draw, connector.upper(), conn_y, font_conn,
                   color=WHITE, letter_spacing=1)

    bb_conn = font_conn.getbbox(connector.upper())
    conn_h = bb_conn[3] - bb_conn[1]

    # Nom 2
    name2_y = conn_y + conn_h + 8
    _draw_centered(draw, name2.upper(), name2_y, font_name,
                   color=WHITE, letter_spacing=2)

    bb2 = font_name.getbbox(name2.upper())
    name2_h = bb2[3] - bb2[1]

    # Séparateur
    sep_y = name2_y + name2_h + 22
    _separator_line(draw, sep_y)

    # Date
    date_y = sep_y + 18
    _draw_centered(draw, date_str, date_y, font_date,
                   color=WHITE, letter_spacing=3)

    bb_date = font_date.getbbox(date_str)
    date_h = bb_date[3] - bb_date[1]

    # Séparateur bas
    sep2_y = date_y + date_h + 18
    _separator_line(draw, sep2_y)

    # ── Export JPEG ──
    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="JPEG", quality=92)
    buf.seek(0)
    return buf.getvalue()
