"""
Génère une image PNG de l'arbre généalogique avec Pillow.
Layout :
    Row 0  →  Parents
    Row 1  →  User  +  Époux/se
    Row 2  →  Enfants
    Côté   →  Amis (colonne à droite)
"""
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

from config import PROFILE_COLORS
import io, os

W, H      = 900, 600
NODE_W    = 140
NODE_H    = 50
RADIUS    = 10
PAD       = 20
BG_COLOR  = (18, 18, 30)
LINE_COLOR = (100, 100, 140)
TEXT_COLOR = (240, 240, 255)

COLOR_MAP = {
    "blue":   (52,  152, 219),
    "green":  (46,  204, 113),
    "red":    (231, 76,  60),
    "purple": (155, 89,  182),
    "orange": (230, 126, 34),
    "pink":   (253, 121, 168),
    "gold":   (241, 196, 15),
    "teal":   (26,  188, 156),
}

FONT_SM = FONT_LG = FONT_XS = None

def _load_fonts():
    global FONT_SM, FONT_LG, FONT_XS
    if FONT_SM is not None:
        return
    try:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        ]
        path = next((p for p in candidates if os.path.exists(p)), None)
        if path:
            FONT_SM = ImageFont.truetype(path, 13)
            FONT_LG = ImageFont.truetype(path, 16)
            FONT_XS = ImageFont.truetype(path, 11)
        else:
            FONT_SM = FONT_LG = FONT_XS = ImageFont.load_default()
    except Exception:
        FONT_SM = FONT_LG = FONT_XS = ImageFont.load_default()


def _node_color(profile_color: str) -> tuple:
    return COLOR_MAP.get(profile_color, (52, 152, 219))


def _draw_node(draw, x: int, y: int, label: str, subtitle: str, color: tuple):
    draw.rounded_rectangle([x+3, y+3, x+NODE_W+3, y+NODE_H+3],
                            radius=RADIUS, fill=(10, 10, 20))
    draw.rounded_rectangle([x, y, x+NODE_W, y+NODE_H],
                            radius=RADIUS, fill=color)
    draw.text((x + NODE_W//2, y + 14), label,
              font=FONT_SM, fill=TEXT_COLOR, anchor="mm")
    if subtitle:
        draw.text((x + NODE_W//2, y + 36), subtitle,
                  font=FONT_XS, fill=(220, 220, 255), anchor="mm")


def _center_x(col: int, total_cols: int) -> int:
    available = W - 200
    col_w     = available // max(total_cols, 1)
    return PAD + col * col_w + (col_w - NODE_W) // 2


def render_tree(members: dict) -> bytes:
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow n'est pas installé sur ce serveur.")

    _load_fonts()

    img  = Image.new("RGB", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    draw.text((W//2, 22), "Arbre Genealogique", font=FONT_LG,
              fill=(200, 200, 255), anchor="mm")

    positions = {}

    def draw_node_at(cx: int, cy: int, info: dict, key: str):
        x = cx - NODE_W // 2
        y = cy - NODE_H // 2
        _draw_node(draw, x, y, info["name"], info.get("title", ""),
                   _node_color(info.get("color", "blue")))
        positions[key] = (cx, cy)

    parents = members.get("parents") or []
    for i, p in enumerate(parents[:4]):
        cx = _center_x(i, max(len(parents), 1))
        draw_node_at(cx + NODE_W//2, 90, p, f"parent_{i}")

    user_cx = W // 2 - (NODE_W // 2 + 20 if members.get("spouse") else 0)
    draw_node_at(user_cx, 230, members["user"], "user")

    if members.get("spouse"):
        spouse_cx = user_cx + NODE_W + 40
        draw_node_at(spouse_cx, 230, members["spouse"], "spouse")
        draw.line([(user_cx, 230), (spouse_cx, 230)],
                  fill=(241, 196, 15), width=3)

    for i in range(len(parents[:4])):
        key = f"parent_{i}"
        if key in positions:
            draw.line([positions[key], (user_cx, 230)],
                      fill=LINE_COLOR, width=2)

    children = members.get("children") or []
    for i, c in enumerate(children[:5]):
        cx       = _center_x(i, max(len(children), 1))
        child_cx = cx + NODE_W // 2
        draw_node_at(child_cx, 390, c, f"child_{i}")
        draw.line([(user_cx, 230 + NODE_H//2), (child_cx, 390 - NODE_H//2)],
                  fill=LINE_COLOR, width=2)

    friends  = members.get("friends") or []
    friend_x = W - NODE_W - PAD
    draw.text((friend_x + NODE_W//2, 50), "Amis",
              font=FONT_XS, fill=(180, 180, 220), anchor="mm")
    for i, f in enumerate(friends[:6]):
        fy = 75 + i * (NODE_H + 12)
        _draw_node(draw, friend_x, fy, f["name"], f.get("title", ""),
                   _node_color(f.get("color", "teal")))
        draw.line([(friend_x, fy + NODE_H//2), (user_cx, 230)],
                  fill=(80, 80, 120), width=1)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()
