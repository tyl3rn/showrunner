"""
post_card.py

Renders a Reddit-post-style intro card as a transparent PNG: fake user with
a generated avatar, the story headline, and 99+ upvotes / 99+ comments. The
card is overlaid on the video for the first few seconds (while the narrator
reads the title) to hook viewers.

Only the HEADLINE is shown -- no post body. Users and avatars are invented.

    python3 post_card.py --title "TIFU by ..." --subreddit tifu --out card.png
"""
import argparse
import random
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CARD_WIDTH = 960
PADDING = 44
AVATAR_SIZE = 92

FAKE_USERS = [
    "Throwaway_Tales42", "MidnightConfessor", "RegretfulRaccoon",
    "AnonAndOnAndOn", "CasualChaosClub", "SirStoriesALot",
    "DefinitelyNotOP", "PanicAtTheCostco", "GrandmaSaidNo",
    "LowkeyLoreKeeper", "SnackAtMidnight", "UnpaidNarrator",
]

AVATAR_COLORS = [
    (255, 87, 51), (52, 152, 219), (46, 204, 113), (155, 89, 182),
    (241, 196, 15), (230, 126, 34), (26, 188, 156), (231, 76, 60),
]

DARK = (26, 26, 27)
GRAY = (120, 124, 126)
UPVOTE_ORANGE = (255, 69, 0)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Windows fonts first, DejaVu (Linux/Pi) as fallback."""
    candidates = (
        ["C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
        if bold else
        ["C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _rounded_card(width: int, height: int) -> Image.Image:
    card = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle([0, 0, width - 1, height - 1], radius=36, fill=(255, 255, 255, 255))
    return card


def _draw_avatar(draw: ImageDraw.ImageDraw, x: int, y: int, username: str):
    color = random.choice(AVATAR_COLORS)
    draw.ellipse([x, y, x + AVATAR_SIZE, y + AVATAR_SIZE], fill=color)
    # simple "snoo-like" face: two eyes + smile keeps it friendly and avoids
    # using any real logo
    ex = x + AVATAR_SIZE * 0.30
    ey = y + AVATAR_SIZE * 0.38
    r = AVATAR_SIZE * 0.07
    for cx in (ex, x + AVATAR_SIZE * 0.70):
        draw.ellipse([cx - r, ey - r, cx + r, ey + r], fill=(255, 255, 255))
    draw.arc(
        [x + AVATAR_SIZE * 0.28, y + AVATAR_SIZE * 0.42,
         x + AVATAR_SIZE * 0.72, y + AVATAR_SIZE * 0.78],
        start=20, end=160, fill=(255, 255, 255), width=5,
    )


def _draw_verified_badge(draw: ImageDraw.ImageDraw, x: int, y: int, size: int):
    """Blue verification circle with a white check, twitter/reddit-premium vibes."""
    draw.ellipse([x, y, x + size, y + size], fill=(29, 155, 240))
    s = size
    draw.line(
        [(x + s * 0.26, y + s * 0.52), (x + s * 0.44, y + s * 0.70), (x + s * 0.76, y + s * 0.32)],
        fill=(255, 255, 255), width=max(3, s // 8), joint="curve",
    )


def _draw_star(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, fill):
    import math
    pts = []
    for k in range(10):
        rad = r if k % 2 == 0 else r * 0.45
        ang = -math.pi / 2 + k * math.pi / 5
        pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
    draw.polygon(pts, fill=fill)


def _draw_heart(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, fill):
    draw.ellipse([cx - r, cy - r * 0.9, cx, cy + r * 0.1], fill=fill)
    draw.ellipse([cx, cy - r * 0.9, cx + r, cy + r * 0.1], fill=fill)
    draw.polygon([(cx - r * 0.95, cy - r * 0.15), (cx + r * 0.95, cy - r * 0.15), (cx, cy + r)], fill=fill)


def _draw_awards_row(draw: ImageDraw.ImageDraw, x: int, y: int, count_font):
    """A row of reddit-award-looking badges + a total, invented like the users."""
    size = 44
    badges = [
        ((255, 196, 37), "star"),    # gold
        ((192, 199, 206), "star"),   # silver
        ((255, 102, 119), "heart"),  # heart award
        ((148, 87, 235), "star"),    # premium purple
    ]
    n_badges = random.randint(2, 4)
    for i, (bg, icon) in enumerate(badges[:n_badges]):
        bx = x + i * (size + 12)
        draw.ellipse([bx, y, bx + size, y + size], fill=bg)
        cx, cy = bx + size / 2, y + size / 2
        if icon == "star":
            _draw_star(draw, cx, cy, size * 0.30, (255, 255, 255))
        else:
            _draw_heart(draw, cx, cy - 2, size * 0.26, (255, 255, 255))
    total = random.randint(23, 480)
    draw.text((x + n_badges * (size + 12) + 6, y + 4), f"+{total} awards",
              font=count_font, fill=GRAY)


def _draw_up_arrow(draw: ImageDraw.ImageDraw, x: int, y: int, size: int, color):
    w = size
    draw.polygon(
        [(x + w // 2, y), (x + w, y + w // 2), (x + int(w * 0.72), y + w // 2),
         (x + int(w * 0.72), y + w), (x + int(w * 0.28), y + w),
         (x + int(w * 0.28), y + w // 2), (x, y + w // 2)],
        fill=color,
    )


def _draw_comment_bubble(draw: ImageDraw.ImageDraw, x: int, y: int, size: int, color):
    draw.rounded_rectangle([x, y, x + size, y + int(size * 0.78)], radius=size // 4, outline=color, width=5)
    draw.polygon(
        [(x + int(size * 0.22), y + int(size * 0.74)),
         (x + int(size * 0.42), y + int(size * 0.74)),
         (x + int(size * 0.22), y + size)],
        fill=color,
    )


def generate_card(title: str, subreddit: str, out_path: str, seed: int | None = None):
    if seed is not None:
        random.seed(seed)
    username = random.choice(FAKE_USERS)

    font_user = _font(34, bold=True)
    font_meta = _font(30)
    font_title = _font(48, bold=True)
    font_counts = _font(36, bold=True)
    font_awards = _font(28)

    # wrap the headline to fit the card
    wrapped = textwrap.wrap(title, width=34)
    line_height = 60
    title_block = line_height * len(wrapped)

    awards_h = 44 + 22  # badge row + spacing
    height = PADDING + AVATAR_SIZE + 18 + awards_h + title_block + 30 + 56 + PADDING
    card = _rounded_card(CARD_WIDTH, height)
    draw = ImageDraw.Draw(card)

    # header row: avatar + username + verified badge + subreddit
    _draw_avatar(draw, PADDING, PADDING, username)
    tx = PADDING + AVATAR_SIZE + 26
    name_text = f"u/{username}"
    draw.text((tx, PADDING + 8), name_text, font=font_user, fill=DARK)
    name_w = draw.textlength(name_text, font=font_user)
    _draw_verified_badge(draw, int(tx + name_w + 12), PADDING + 10, 34)
    draw.text((tx, PADDING + 50), f"r/{subreddit} • 6h", font=font_meta, fill=GRAY)

    # awards row under the username block
    ay = PADDING + AVATAR_SIZE + 18
    _draw_awards_row(draw, PADDING, ay, font_awards)

    # headline
    ty = ay + awards_h
    for line in wrapped:
        draw.text((PADDING, ty), line, font=font_title, fill=DARK)
        ty += line_height

    # footer row: 99+ upvotes, 99+ comments
    fy = ty + 30
    _draw_up_arrow(draw, PADDING, fy, 46, UPVOTE_ORANGE)
    draw.text((PADDING + 62, fy + 2), "99+", font=font_counts, fill=DARK)
    _draw_comment_bubble(draw, PADDING + 210, fy + 2, 46, GRAY)
    draw.text((PADDING + 272, fy + 2), "99+", font=font_counts, fill=DARK)

    card.save(out_path)
    print(f"Wrote {out_path} ({CARD_WIDTH}x{height}, user u/{username})")


def main():
    ap = argparse.ArgumentParser(description="Render a fake Reddit post card PNG.")
    ap.add_argument("--title", required=True)
    ap.add_argument("--subreddit", default="tifu")
    ap.add_argument("--out", default="card.png")
    ap.add_argument("--seed", type=int, default=None, help="fix the random user/avatar")
    args = ap.parse_args()
    generate_card(args.title, args.subreddit, args.out, args.seed)


if __name__ == "__main__":
    main()
