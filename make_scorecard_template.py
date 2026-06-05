"""
Generate a BLANK StatGolf scorecard template (static/scorecard.png).

This is just a starting point — open static/scorecard.png in any image editor and
restyle it however you like. The app draws ONLY the round's numbers on top of it, at the
cell positions defined by CARD_LAYOUT in templates/index.html. The geometry below MUST
match that CARD_LAYOUT; if you move the grid in your design, update CARD_LAYOUT to match
(the numbers are drawn centered on the coordinates there).

Run:  python make_scorecard_template.py
"""
from PIL import Image, ImageDraw, ImageFont

W = H = 1080

# ── palette ──
GREEN_DEEP = (14, 61, 38)
CREAM      = (244, 236, 210)
CREAM_DK   = (231, 218, 176)
GOLD       = (201, 168, 76)
GOLD_DK    = (156, 126, 51)
INK        = (36, 59, 44)

# ── grid geometry (keep in sync with CARD_LAYOUT in index.html) ──
GRID_LEFT  = 90
GRID_RIGHT = 990
LABEL_W    = 270
GRID_TOP   = 300
ROW_H      = 86
COL_W      = (GRID_RIGHT - (GRID_LEFT + LABEL_W)) / 4.0      # H1,H2,H3,TOT
ROWS = ["HOLE", "DISTANCE", "PAR", "SCORE", "+/-"]           # header + 4 value rows
COLS = ["H1", "H2", "H3", "TOT"]


def font(sz, bold=True):
    for n in (["arialbd.ttf", "Arialbd.ttf", "DejaVuSans-Bold.ttf"] if bold
              else ["arial.ttf", "Arial.ttf", "DejaVuSans.ttf"]):
        try:
            return ImageFont.truetype(n, sz)
        except Exception:
            pass
    return ImageFont.load_default()


img = Image.new("RGB", (W, H), GREEN_DEEP)
d = ImageDraw.Draw(img)


def rrect(x, y, w, h, r, fill=None, outline=None, width=1):
    d.rounded_rectangle([x, y, x + w, y + h], radius=r, fill=fill, outline=outline, width=width)


def ctext(cx, cy, s, f, fill):
    bb = d.textbbox((0, 0), s, font=f)
    d.text((cx - (bb[2] - bb[0]) / 2, cy - (bb[3] - bb[1]) / 2 - bb[1]), s, font=f, fill=fill)


def ltext(x, cy, s, f, fill):
    bb = d.textbbox((0, 0), s, font=f)
    d.text((x, cy - (bb[3] - bb[1]) / 2 - bb[1]), s, font=f, fill=fill)


# card body + border
m = 46
rrect(m, m, W - 2 * m, H - 2 * m, 22, fill=CREAM)
rrect(m + 6, m + 6, W - 2 * m - 12, H - 2 * m - 12, 16, outline=GOLD, width=8)

# header (edit/replace this freely in your design)
d.text((90, 96), "StatGolf", font=font(76), fill=GREEN_DEEP)
d.text((90, 188), "Daily Sports Golf", font=font(34, False), fill=GOLD_DK)
d.line([90, 280, 990, 280], fill=GOLD, width=3)

# grid
def col_x(i):
    return GRID_LEFT + LABEL_W + i * COL_W

for r, label in enumerate(ROWS):
    y = GRID_TOP + r * ROW_H
    header = (r == 0)
    # label cell
    fill = GOLD if header else (CREAM if r % 2 else CREAM_DK)
    d.rectangle([GRID_LEFT, y, GRID_LEFT + LABEL_W, y + ROW_H], fill=fill, outline=GOLD_DK, width=2)
    ltext(GRID_LEFT + 18, y + ROW_H / 2, label, font(30), GREEN_DEEP if header else INK)
    # value cells — left BLANK for the app to fill, except the header row (H1/H2/H3/TOT)
    for i in range(4):
        x = col_x(i)
        tot = (i == 3)
        cfill = GOLD_DK if (header and tot) else GOLD if header else (CREAM if r % 2 else CREAM_DK)
        d.rectangle([x, y, x + COL_W, y + ROW_H], fill=cfill, outline=GOLD_DK, width=2)
        if header:
            ctext(x + COL_W / 2, y + ROW_H / 2, COLS[i], font(30), CREAM if tot else GREEN_DEEP)

# footer band with link (edit freely)
fy = H - m - 86
rrect(m + 12, fy, W - 2 * m - 24, 74, 14, fill=GREEN_DEEP)
ctext(W / 2, fy + 37, "Play today at  statgolf.com", font(44), GOLD)

img.save("static/scorecard.png")
print("wrote static/scorecard.png")
print("Grid coords for CARD_LAYOUT:")
print("  firstColCenterX =", round(col_x(0) + COL_W / 2, 1))
print("  colStep         =", round(COL_W, 1))
for r, label in enumerate(ROWS):
    if r == 0:
        continue
    print(f"  row '{label}' centerY = {GRID_TOP + r * ROW_H + ROW_H // 2}")
