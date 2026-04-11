"""
Générateur de cartes de relation (mariage, adoption, amitié).
Style chaud champagne/bordeaux avec ornements floraux et bordure dorée.
"""
import io
import math
import random
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
import logging

logger = logging.getLogger(__name__)

W, H = 600, 900

GOLD  = (212, 175, 80)
DARK  = (50,  25,  20)
CREAM = (255, 248, 235)
ROSE  = (180, 60,  70)
WINE  = (120, 30,  45)


def _get_font(size, bold=False):
    paths = [
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'Bold' if bold else ''}.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _draw_centered(draw, text, y, font, color, letter_spacing=2):
    total_w = sum(font.getbbox(ch)[2] - font.getbbox(ch)[0] + letter_spacing
                  for ch in text) - letter_spacing
    x = (W - total_w) // 2
    for ch in text:
        draw.text((x, y), ch, font=font, fill=color)
        x += font.getbbox(ch)[2] - font.getbbox(ch)[0] + letter_spacing


def _separator(draw, y):
    lw = 150; cx = W // 2
    draw.line([(cx - lw, y), (cx - 25, y)], fill=GOLD, width=1)
    draw.line([(cx + 25, y), (cx + lw, y)], fill=GOLD, width=1)
    draw.polygon([(cx, y-5), (cx+9, y), (cx, y+5), (cx-9, y)], fill=GOLD)


def _gradient_bg():
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        if t < 0.5:
            t2 = t / 0.5
            r = int(245 + (200-245)*t2)
            g = int(235 + (180-235)*t2)
            b = int(210 + (150-210)*t2)
        else:
            t2 = (t-0.5)/0.5
            r = int(200 + (80 -200)*t2)
            g = int(180 + (40 -180)*t2)
            b = int(150 + (50 -150)*t2)
        draw.line([(0, y), (W, y)], fill=(r, g, b))
    return img


def _add_noise(img):
    rng = random.Random(42)
    noise = Image.new("RGBA", (W, H), (0,0,0,0))
    nd = ImageDraw.Draw(noise)
    for _ in range(8000):
        x = rng.randint(0, W-1); y = rng.randint(0, H-1)
        nd.point((x, y), fill=(255, 255, 255, rng.randint(0, 18)))
    base = img.convert("RGBA")
    base.alpha_composite(noise)
    return base.convert("RGB")


def _draw_corner(draw, cx, cy, flip=False):
    sign = -1 if flip else 1
    for i in range(12):
        angle = math.radians(i * 30 + (15 if flip else 0))
        for r_pct, alpha, size in [(0.88, 200, 13), (0.65, 160, 9), (0.44, 110, 6)]:
            R = 90
            px = cx + int(R * r_pct * math.cos(angle)) * sign
            py = cy + int(R * r_pct * math.sin(angle))
            color = ROSE if i % 3 == 0 else WINE
            draw.ellipse([px-size, py-size, px+size, py+size], fill=(*color, alpha))
    for i in range(6):
        angle = math.radians(i * 60); R = 50
        px = cx + int(R * math.cos(angle)) * sign
        py = cy + int(R * math.sin(angle))
        draw.polygon([
            (cx + int(R*0.9*math.cos(angle))*sign, cy + int(R*0.9*math.sin(angle))),
            (px-7, py-4), (px+7, py+4),
        ], fill=(*GOLD, 190))
    draw.ellipse([cx-13, cy-13, cx+13, cy+13], fill=(*GOLD, 230))
    draw.ellipse([cx-6,  cy-6,  cx+6,  cy+6],  fill=(*CREAM, 255))


def _draw_bottom_ornament(draw):
    cx, cy = W // 2, H - 75
    for i in range(8):
        angle = math.radians(i * 45); R = 42
        px = cx + int(R * math.cos(angle)); py = cy + int(R * math.sin(angle))
        draw.ellipse([px-6, py-6, px+6, py+6], fill=(*ROSE, 160))
    draw.ellipse([cx-10, cy-10, cx+10, cy+10], fill=(*GOLD, 220))
    draw.ellipse([cx-4,  cy-4,  cx+4,  cy+4],  fill=(*CREAM, 255))


def _circle_photo(img_bytes, size):
    result = Image.new("RGBA", (size, size), (0,0,0,0))
    src = None
    if img_bytes:
        try:
            src = Image.open(io.BytesIO(img_bytes)).convert("RGBA").resize((size, size), Image.LANCZOS)
        except Exception:
            pass
    if src is None:
        src = Image.new("RGBA", (size, size), (200, 180, 150, 255))
        d = ImageDraw.Draw(src)
        font = _get_font(size // 3, bold=True)
        d.text((size//2, size//2), "?", font=font, fill=(*DARK, 180), anchor="mm")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size-1, size-1], fill=255)
    result.paste(src, mask=mask)
    bd = ImageDraw.Draw(result)
    bd.ellipse([2, 2, size-3, size-3], outline=(*GOLD, 255), width=3)
    bd.ellipse([7, 7, size-8, size-8], outline=(*CREAM, 100), width=1)
    return result


def generate_relation_card(name1, name2, relation, photo1=None, photo2=None):
    """
    Génère une carte de relation et retourne les bytes JPEG.
    relation: "married" | "adopted" | "friends"
    """
    TOP_LABELS = {
        "married": "YOU ARE INVITED TO THE WEDDING OF",
        "adopted": "UNE NOUVELLE FAMILLE EST NEE",
        "friends": "UNE BELLE AMITIE EST NEE",
    }
    CONNECTORS = {"married": "&", "adopted": "ADOPTE", "friends": "&"}

    top_label = TOP_LABELS.get(relation, "")
    connector = CONNECTORS.get(relation, "&")
    date_str  = datetime.now().strftime("%B %d   %I %p").upper()

    canvas = _gradient_bg()
    canvas = _add_noise(canvas)
    draw   = ImageDraw.Draw(canvas)

    # Bordure intérieure
    bx, by = 18, 18
    draw.rectangle([bx,   by,   W-bx,   H-by  ], outline=(*GOLD, 200), width=1)
    draw.rectangle([bx+5, by+5, W-bx-5, H-by-5], outline=(*GOLD, 70),  width=1)

    # Ornements coins
    _draw_corner(draw, 0, 0, flip=False)
    _draw_corner(draw, W, 0, flip=True)

    # Photos circulaires
    photo_size = 178
    gap = 44
    total_pw = 2 * photo_size + gap
    lx = (W - total_pw) // 2
    rx = lx + photo_size + gap
    photo_y = 128

    p1 = _circle_photo(photo1, photo_size)
    p2 = _circle_photo(photo2, photo_size)
    canvas_rgba = canvas.convert("RGBA")
    canvas_rgba.paste(p1, (lx, photo_y), p1)
    canvas_rgba.paste(p2, (rx, photo_y), p2)
    canvas = canvas_rgba.convert("RGB")
    draw   = ImageDraw.Draw(canvas)

    # Polices
    f_tiny = _get_font(12)
    f_name = _get_font(44, bold=True)
    f_conn = _get_font(30, bold=True)
    f_date = _get_font(25, bold=True)

    text_y = photo_y + photo_size + 28

    _draw_centered(draw, top_label, text_y, f_tiny, GOLD, letter_spacing=3)
    text_y += 26

    _draw_centered(draw, name1.upper(), text_y, f_name, DARK, letter_spacing=2)
    text_y += f_name.getbbox("A")[3] + 6

    _draw_centered(draw, connector, text_y, f_conn, WINE, letter_spacing=2)
    text_y += f_conn.getbbox("A")[3] + 6

    _draw_centered(draw, name2.upper(), text_y, f_name, DARK, letter_spacing=2)
    text_y += f_name.getbbox("A")[3] + 22

    _separator(draw, text_y); text_y += 18
    _draw_centered(draw, date_str, text_y, f_date, DARK, letter_spacing=3)
    text_y += f_date.getbbox("A")[3] + 18
    _separator(draw, text_y)

    _draw_bottom_ornament(draw)

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=94)
    buf.seek(0)
    return buf.getvalue()
