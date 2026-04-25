"""
Arbre généalogique avec vraies photos de profil Telegram.
- Si photo dispo → affichée en carré arrondi
- Sinon → avatar coloré avec initiale
- Taille décroissante par génération
"""
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

import io, os

BG_COLOR   = (173, 216, 230)
LINE_COLOR = (80,  80,  120)
TEXT_COLOR = (40,  40,   80)
WHITE      = (255, 255, 255)

AVATAR_SIZES = [80, 68, 56, 46]
NAME_SIZES   = [13, 12, 11, 10]
GEN_GAP      = 115

COLOR_MAP = {
    "blue":   (70,  130, 180),
    "green":  (60,  179, 113),
    "red":    (220,  80,  60),
    "purple": (148,  80, 180),
    "orange": (220, 120,  40),
    "pink":   (240, 100, 150),
    "gold":   (200, 170,  30),
    "teal":   (40,  180, 160),
}

_FONT_CACHE = {}

def _get_font(size):
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    # NotoSansCJK en priorité → supporte japonais, coréen, chinois, cyrillique, arabe, etc.
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    path = next((p for p in candidates if os.path.exists(p)), None)
    try:
        f = ImageFont.truetype(path, size) if path else ImageFont.load_default()
    except Exception:
        f = ImageFont.load_default()
    _FONT_CACHE[size] = f
    return f


def _apply_round_mask(img, size, radius_ratio=6):
    """Applique un masque carré arrondi."""
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size-1, size-1], radius=size//radius_ratio, fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    src = img.convert("RGBA").resize((size, size), Image.LANCZOS)
    out.paste(src, mask=mask)
    # Contour blanc
    border = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bd = ImageDraw.Draw(border)
    bw = max(2, size // 22)
    bd.rounded_rectangle([bw//2, bw//2, size-1-bw//2, size-1-bw//2],
                         radius=size//radius_ratio, outline=WHITE, width=bw)
    out = Image.alpha_composite(out, border)
    return out


def _make_avatar_from_photo(photo_bytes, size):
    """Charge une vraie photo et l'adapte."""
    try:
        img = Image.open(io.BytesIO(photo_bytes))
        # Crop carré centré
        w, h = img.size
        s = min(w, h)
        left = (w - s) // 2
        top  = (h - s) // 2
        img  = img.crop((left, top, left + s, top + s))
        return _apply_round_mask(img, size)
    except Exception:
        return None


def _make_avatar_initiale(name, profile_color, size):
    """Avatar coloré avec initiale si pas de photo."""
    color = COLOR_MAP.get(profile_color, (70, 130, 180))
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([0, 0, size-1, size-1], fill=color)
    initial = name[0].upper() if name else "?"
    font = _get_font(max(10, size // 3))
    bbox = draw.textbbox((0, 0), initial, font=font)
    tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
    draw.text(((size-tw)//2, (size-th)//2 - 1), initial, font=font, fill=WHITE)
    return _apply_round_mask(img, size)


def _draw_member(canvas, draw, cx, cy, node, gen):
    size = AVATAR_SIZES[min(gen, 3)]
    ns   = NAME_SIZES[min(gen, 3)]

    # Choisir avatar : vraie photo ou initiale
    photo_bytes = node.get("photo")
    if photo_bytes:
        av = _make_avatar_from_photo(photo_bytes, size)
    else:
        av = None
    if av is None:
        av = _make_avatar_initiale(node["name"], node.get("color", "blue"), size)

    x, y = cx - size//2, cy
    canvas.paste(av, (x, y), av)

    # Nom sous l'avatar
    name = node["name"][:14]
    font = _get_font(ns)
    bbox = draw.textbbox((0, 0), name, font=font)
    tw   = bbox[2] - bbox[0]
    draw.text((cx - tw//2, y + size + 3), name, font=font, fill=TEXT_COLOR)
    return (cx, y, y + size)


def _line(draw, x1, y1, x2, y2):
    draw.line([(x1, y1), (x2, y2)], fill=LINE_COLOR, width=2)


def render_tree(members):
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow n'est pas installe.")

    parents  = members.get("parents")  or []
    user     = members["user"]
    spouse   = members.get("spouse")
    children = members.get("children") or []
    friends  = members.get("friends")  or []

    gens = []
    if parents:
        gens.append(parents)
    gens.append([user] + ([spouse] if spouse else []))
    if children:
        gens.append(children)

    friend_w  = 110 if friends else 0
    HPAD      = 60
    NODE_SLOT = 115
    max_n     = max(len(g) for g in gens)
    content_w = max(max_n * NODE_SLOT, 320)
    W = content_w + HPAD * 2 + friend_w
    H = 60 + len(gens) * (AVATAR_SIZES[0] + 28 + GEN_GAP) + 50
    W = max(W, 500)
    H = max(H, 380)

    canvas = Image.new("RGBA", (W, H), BG_COLOR + (255,))
    draw   = ImageDraw.Draw(canvas)

    # Titre
    tfont = _get_font(18)
    title = f"Arbre de {user['name']}"
    bbox  = draw.textbbox((0, 0), title, font=tfont)
    tw    = bbox[2] - bbox[0]
    draw.text(((W - friend_w - tw)//2, 14), title, font=tfont, fill=(100, 60, 180))

    zone_w  = W - friend_w - HPAD * 2
    title_h = 50

    rows = []
    for gi, gnodes in enumerate(gens):
        gen_idx = min(gi + (1 if parents else 2), 3)
        n       = len(gnodes)
        slot_w  = max(zone_w // max(n, 1), NODE_SLOT)
        total_w = slot_w * n
        sx      = HPAD + (zone_w - total_w)//2 + slot_w//2
        y_top   = title_h + gi * (AVATAR_SIZES[0] + 28 + GEN_GAP)
        row = []
        for ni, node in enumerate(gnodes):
            cx  = sx + ni * slot_w
            pos = _draw_member(canvas, draw, cx, y_top, node, gen_idx)
            row.append(pos)
        rows.append(row)

    # Lignes parents → user
    if len(rows) >= 2 and parents:
        pr = rows[0]
        ur = rows[1]
        py = pr[0][2] + 10
        if len(pr) > 1:
            _line(draw, pr[0][0], py, pr[-1][0], py)
        pmid_x = (pr[0][0] + pr[-1][0])//2
        umid_x = (ur[0][0] + ur[-1][0])//2 if len(ur) > 1 else ur[0][0]
        _line(draw, pmid_x, py, umid_x, ur[0][1])

    # Lignes user+conjoint → enfants
    ur_idx = 1 if parents else 0
    if ur_idx < len(rows):
        ur = rows[ur_idx]
        if len(ur) == 2:
            uy = ur[0][1] + (ur[0][2] - ur[0][1])//2
            _line(draw, ur[0][0], uy, ur[1][0], uy)
        cr_idx = ur_idx + 1
        if cr_idx < len(rows):
            cr     = rows[cr_idx]
            join_x = (ur[0][0] + ur[-1][0])//2
            join_y = ur[0][2] + 8
            top_y  = cr[0][1]
            mid_y  = (join_y + top_y)//2
            _line(draw, join_x, join_y, join_x, mid_y)
            if len(cr) > 1:
                _line(draw, cr[0][0], mid_y, cr[-1][0], mid_y)
            for cx, ctop, _ in cr:
                _line(draw, cx, mid_y, cx, ctop)

    # Amis colonne droite
    if friends and friend_w:
        fxc  = W - friend_w//2
        ffnt = _get_font(11)
        bbox = draw.textbbox((0, 0), "Amis", font=ffnt)
        draw.text((fxc - (bbox[2]-bbox[0])//2, title_h + 5), "Amis",
                  font=ffnt, fill=(100, 100, 160))
        for i, f in enumerate(friends[:8]):
            _draw_member(canvas, draw, fxc, title_h + 28 + i * 60, f, 3)

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.read()
