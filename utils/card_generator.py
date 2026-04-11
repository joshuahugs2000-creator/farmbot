"""
Génère des cartes de relation avec 3 styles distincts tirés aléatoirement.
Style 0 : Élégant Champagne/Or
Style 1 : Nuit Étoilée (noir/bleu/or)
Style 2 : Royal Blanc/Noir
"""
import io, math, random
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
import logging

logger = logging.getLogger(__name__)
W, H = 640, 960


def _font(size, bold=False):
    candidates = [
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'-Bold' if bold else ''}.ttf",
        f"/usr/share/fonts/truetype/liberation/LiberationSans{'-Bold' if bold else '-Regular'}.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _centered(draw, text, y, font, color, max_w=W-60):
    """Dessine du texte centré, tronqué si trop long."""
    while True:
        bb = font.getbbox(text)
        tw = bb[2] - bb[0]
        if tw <= max_w or len(text) < 4:
            break
        text = text[:-1]
    bb  = font.getbbox(text)
    tw  = bb[2] - bb[0]
    draw.text(((W - tw) // 2, y), text, font=font, fill=color)


def _circle_avatar(img_bytes, size, border_color, bg_color):
    result = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if img_bytes:
        try:
            src = Image.open(io.BytesIO(img_bytes)).convert("RGBA").resize((size, size), Image.LANCZOS)
        except Exception:
            src = None
    else:
        src = None
    if src is None:
        src = Image.new("RGBA", (size, size), (*bg_color, 255))
        d   = ImageDraw.Draw(src)
        f   = _font(size // 3, bold=True)
        d.text((size // 2, size // 2), "?", font=f, fill=(200, 200, 200, 200), anchor="mm")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    result.paste(src, mask=mask)
    # Bordure
    bd = ImageDraw.Draw(result)
    bd.ellipse([2, 2, size - 3, size - 3], outline=(*border_color, 255), width=4)
    return result


# ─── STYLE 0 : Champagne / Or ────────────────────────────────────────────────

def _style_champagne(name1, name2, relation, photo1, photo2) -> bytes:
    CREAM = (252, 245, 228)
    GOLD  = (197, 158, 64)
    WINE  = (110, 28, 42)
    DARK  = (40, 20, 15)

    img  = Image.new("RGB", (W, H), CREAM)
    draw = ImageDraw.Draw(img)

    # Fond dégradé vertical
    for y in range(H):
        t = y / H
        r = int(252 - t * 40)
        g = int(245 - t * 60)
        b = int(228 - t * 80)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    # Bordures dorées doubles
    draw.rectangle([14, 14, W-14, H-14], outline=GOLD, width=2)
    draw.rectangle([20, 20, W-20, H-20], outline=(*GOLD[:3], 100), width=1)

    # Ornements coins (rosaces)
    for cx, cy in [(0, 0), (W, 0), (0, H), (W, H)]:
        for i in range(8):
            a  = math.radians(i * 45)
            px = cx + int(55 * math.cos(a))
            py = cy + int(55 * math.sin(a))
            draw.ellipse([px-6, py-6, px+6, py+6], fill=(*GOLD[:3], 180))
        draw.ellipse([cx-8, cy-8, cx+8, cy+8], fill=GOLD)

    # Titre haut
    f_small = _font(13)
    f_title = _font(16, bold=True)
    f_name  = _font(46, bold=True)
    f_conn  = _font(28)
    f_date  = _font(22)

    LABELS = {"married": "INVITATION AU MARIAGE", "adopted": "ACTE D'ADOPTION", "friends": "ACTE D'AMITIÉ"}
    CONN   = {"married": "❤  &  ❤", "adopted": "adopte", "friends": "&"}

    top_y = 50
    _centered(draw, LABELS.get(relation, ""), top_y, f_small, GOLD)

    # Séparateur
    def sep(y):
        draw.line([(80, y), (W//2-30, y)], fill=GOLD, width=1)
        draw.line([(W//2+30, y), (W-80, y)], fill=GOLD, width=1)
        draw.polygon([(W//2, y-6), (W//2+8, y), (W//2, y+6), (W//2-8, y)], fill=GOLD)

    sep(top_y + 22)

    # Avatars
    av_size = 180
    gap     = 50
    lx      = W // 2 - av_size - gap // 2
    rx      = W // 2 + gap // 2
    av_y    = 90

    p1 = _circle_avatar(photo1, av_size, GOLD, (180, 150, 120))
    p2 = _circle_avatar(photo2, av_size, GOLD, (180, 150, 120))
    base = img.convert("RGBA")
    base.paste(p1, (lx, av_y), p1)
    base.paste(p2, (rx, av_y), p2)
    img  = base.convert("RGB")
    draw = ImageDraw.Draw(img)

    # Noms
    ty = av_y + av_size + 28
    _centered(draw, name1.upper(), ty, f_name, DARK)
    ty += 56
    _centered(draw, CONN.get(relation, "&"), ty, f_conn, WINE)
    ty += 40
    _centered(draw, name2.upper(), ty, f_name, DARK)
    ty += 60

    sep(ty); ty += 20
    date_str = datetime.now().strftime("%d %B %Y").upper()
    _centered(draw, date_str, ty, f_date, DARK)
    ty += 36
    sep(ty)

    # Bas
    _centered(draw, "FamTree Bot", H - 45, f_small, GOLD)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    buf.seek(0)
    return buf.getvalue()


# ─── STYLE 1 : Nuit Étoilée ───────────────────────────────────────────────────

def _style_night(name1, name2, relation, photo1, photo2) -> bytes:
    NAVY  = (8, 10, 30)
    BLUE  = (30, 50, 120)
    GOLD  = (220, 180, 60)
    WHITE = (235, 240, 255)
    CYAN  = (100, 200, 255)

    img  = Image.new("RGB", (W, H), NAVY)
    draw = ImageDraw.Draw(img)

    # Fond dégradé radial simulé
    for y in range(H):
        t = (y / H)
        r = int(8  + t * 22)
        g = int(10 + t * 20)
        b = int(30 + t * 60)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    # Étoiles
    rng = random.Random(7)
    for _ in range(200):
        sx = rng.randint(0, W-1)
        sy = rng.randint(0, H-1)
        sz = rng.randint(1, 3)
        bright = rng.randint(140, 255)
        draw.ellipse([sx-sz, sy-sz, sx+sz, sy+sz], fill=(bright, bright, 255))

    # Bordures
    draw.rectangle([12, 12, W-12, H-12], outline=GOLD, width=2)
    draw.rectangle([18, 18, W-18, H-18], outline=(*CYAN[:3], 80), width=1)

    # Lune décorative (haut gauche)
    draw.ellipse([30, 28, 90, 88], fill=(220, 210, 80))
    draw.ellipse([50, 22, 105, 82], fill=(18, 22, 55))

    f_small = _font(13)
    f_name  = _font(44, bold=True)
    f_conn  = _font(26)
    f_date  = _font(20)

    LABELS = {"married": "✦  UNION CÉLESTE  ✦", "adopted": "✦  ADOPTION  ✦", "friends": "✦  AMITIÉ ÉTERNELLE  ✦"}
    CONN   = {"married": "♥ unis pour toujours ♥", "adopted": "accueille dans sa famille", "friends": "♦ amis pour la vie ♦"}

    top_y = 46
    _centered(draw, LABELS.get(relation, ""), top_y, f_small, GOLD)

    def sep(y):
        draw.line([(70, y), (W-70, y)], fill=CYAN, width=1)
        draw.ellipse([W//2-4, y-4, W//2+4, y+4], fill=GOLD)

    sep(top_y + 22)

    # Avatars avec halo
    av_size = 175
    gap     = 45
    lx      = W // 2 - av_size - gap // 2
    rx      = W // 2 + gap // 2
    av_y    = 88

    # Halos lumineux
    for cx, r_mult in [(lx + av_size//2, 1), (rx + av_size//2, 1)]:
        for rr in range(av_size//2+25, av_size//2+5, -4):
            alpha = max(0, 60 - (rr - av_size//2) * 5)
            draw.ellipse([cx-rr, av_y+av_size//2-rr, cx+rr, av_y+av_size//2+rr],
                         outline=(100, 160, 255, alpha), width=1)

    p1 = _circle_avatar(photo1, av_size, GOLD, (20, 30, 80))
    p2 = _circle_avatar(photo2, av_size, GOLD, (20, 30, 80))
    base = img.convert("RGBA")
    base.paste(p1, (lx, av_y), p1)
    base.paste(p2, (rx, av_y), p2)
    img  = base.convert("RGB")
    draw = ImageDraw.Draw(img)

    ty = av_y + av_size + 30
    _centered(draw, name1.upper(), ty, f_name, WHITE)
    ty += 54
    _centered(draw, CONN.get(relation, "&"), ty, f_conn, CYAN)
    ty += 38
    _centered(draw, name2.upper(), ty, f_name, WHITE)
    ty += 58

    sep(ty); ty += 20
    date_str = datetime.now().strftime("%d %B %Y").upper()
    _centered(draw, date_str, ty, f_date, GOLD)
    ty += 34
    sep(ty)

    _centered(draw, "✦  FamTree Bot  ✦", H - 44, f_small, CYAN)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    buf.seek(0)
    return buf.getvalue()


# ─── STYLE 2 : Royal Blanc/Noir ───────────────────────────────────────────────

def _style_royal(name1, name2, relation, photo1, photo2) -> bytes:
    BLACK = (12, 12, 12)
    WHITE = (245, 245, 245)
    GOLD  = (212, 170, 50)
    GREY  = (160, 160, 160)
    RED   = (180, 30, 30)

    img  = Image.new("RGB", (W, H), BLACK)
    draw = ImageDraw.Draw(img)

    # Bande blanche centrale
    draw.rectangle([0, H//2-20, W, H//2+20], fill=(25, 25, 25))

    # Grille subtile
    for x in range(0, W, 40):
        draw.line([(x, 0), (x, H)], fill=(30, 30, 30), width=1)
    for y in range(0, H, 40):
        draw.line([(0, y), (W, y)], fill=(30, 30, 30), width=1)

    # Bordure or triple
    draw.rectangle([10, 10, W-10, H-10], outline=GOLD, width=3)
    draw.rectangle([17, 17, W-17, H-17], outline=WHITE, width=1)
    draw.rectangle([22, 22, W-22, H-22], outline=GOLD, width=1)

    # Coins croisés
    for cx, cy, dx, dy in [(30, 30, 1, 1), (W-30, 30, -1, 1), (30, H-30, 1, -1), (W-30, H-30, -1, -1)]:
        draw.line([(cx, cy), (cx+dx*40, cy)], fill=GOLD, width=2)
        draw.line([(cx, cy), (cx, cy+dy*40)], fill=GOLD, width=2)
        draw.ellipse([cx-5, cy-5, cx+5, cy+5], fill=GOLD)

    f_small = _font(13)
    f_big   = _font(18, bold=True)
    f_name  = _font(44, bold=True)
    f_conn  = _font(22)
    f_date  = _font(20)

    LABELS = {"married": "ACTE DE MARIAGE", "adopted": "ACTE D'ADOPTION", "friends": "ACTE D'AMITIÉ"}
    CONN   = {"married": "◆ ÉPOUX ◆", "adopted": "◆ PARENT & ENFANT ◆", "friends": "◆ AMIS ◆"}

    top_y = 48
    _centered(draw, LABELS.get(relation, ""), top_y, f_big, GOLD)

    def sep(y, color=GOLD):
        draw.line([(60, y), (W-60, y)], fill=color, width=1)

    sep(top_y + 26)

    av_size = 172
    gap     = 48
    lx      = W // 2 - av_size - gap // 2
    rx      = W // 2 + gap // 2
    av_y    = 95

    # Fond blanc derrière les avatars
    for off in range(8, 0, -2):
        draw.rectangle([lx-off, av_y-off, lx+av_size+off, av_y+av_size+off],
                        outline=(*GOLD[:3], off * 20), width=1)
        draw.rectangle([rx-off, av_y-off, rx+av_size+off, av_y+av_size+off],
                        outline=(*GOLD[:3], off * 20), width=1)

    p1 = _circle_avatar(photo1, av_size, GOLD, (50, 50, 50))
    p2 = _circle_avatar(photo2, av_size, GOLD, (50, 50, 50))
    base = img.convert("RGBA")
    base.paste(p1, (lx, av_y), p1)
    base.paste(p2, (rx, av_y), p2)
    img  = base.convert("RGB")
    draw = ImageDraw.Draw(img)

    ty = av_y + av_size + 30
    _centered(draw, name1.upper(), ty, f_name, WHITE)
    ty += 54
    _centered(draw, CONN.get(relation, "&"), ty, f_conn, RED)
    ty += 36
    _centered(draw, name2.upper(), ty, f_name, WHITE)
    ty += 58

    sep(ty); ty += 20
    date_str = datetime.now().strftime("%d %B %Y").upper()
    _centered(draw, date_str, ty, f_date, GREY)
    ty += 34
    sep(ty)

    _centered(draw, "FamTree Bot", H - 44, f_small, GREY)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    buf.seek(0)
    return buf.getvalue()


# ─── DISPATCHER ──────────────────────────────────────────────────────────────

_STYLES = [_style_champagne, _style_night, _style_royal]


def generate_relation_card(name1: str, name2: str, relation: str,
                            photo1=None, photo2=None) -> bytes:
    """Tire un style aléatoire et génère la carte."""
    style = random.choice(_STYLES)
    try:
        return style(name1, name2, relation, photo1, photo2)
    except Exception as e:
        logger.error(f"Erreur génération carte (style {style.__name__}): {e}")
        # Fallback sur style 0
        return _style_champagne(name1, name2, relation, photo1, photo2)
