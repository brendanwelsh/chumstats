"""Server-side goal-map PNG for the Discord post.

ONE top-down pitch showing where every goal was scored: each goal is a numbered,
team-coloured marker placed at its impact location (where the ball crossed the
line), with a legend mapping each number to the player who scored it. Goals into
the blue net cluster on the left, goals into the orange net on the right.

This replaces the earlier touch-density heatmap in the post. Pillow is optional
(the ``bot`` extra); if it isn't installed, or the match has no located goals,
`render_goal_map_png` returns None and the post still goes out (the text "Goal
timeline" field carries the same who/when/score).
"""

from __future__ import annotations

import math
from io import BytesIO

# --- Rocket League world coords -> pitch projection -------------------------
# Same mapping as server.py / the touch heatmap: the long axis (Y) -> pitch
# horizontal, the short axis (X) -> pitch vertical.
RL_LEN = 10240   # Y range total (-5120 .. +5120); the nets sit at +/-Y
RL_WID = 8192    # X range total (-4096 .. +4096)

# --- raster geometry --------------------------------------------------------
PITCH_W, PITCH_H = 560, 224   # 2.5 : 1
MARGIN = 16
TITLE_H = 26
NET_T = 7
DISC_R = 11

# --- palette (matches the dashboard CSS) ------------------------------------
BG = (10, 13, 20)          # --bg
FIELD = (19, 24, 38)       # --card
LINE = (60, 66, 80)        # ~ --border-strong on the dark field
TEAM_BLUE = (45, 125, 255) # --team-blue
TEAM_ORNG = (255, 122, 24) # --team-orng
TEXT = (232, 237, 243)     # --text
TEXT_DIM = (165, 173, 186) # --text-dim
GOLD = (255, 209, 102)     # viewer-goal ring


def _team_color(team: int | None):
    return TEAM_ORNG if team == 1 else TEAM_BLUE


def _project(rx: float, ry: float) -> tuple[int, int]:
    """RL world (X, Y) -> pixel inside one PITCH_W x PITCH_H pitch."""
    ry = max(min(ry, RL_LEN / 2), -RL_LEN / 2)
    rx = max(min(rx, RL_WID / 2), -RL_WID / 2)
    px = ((ry + RL_LEN / 2) / RL_LEN) * (PITCH_W - 1)
    py = ((rx + RL_WID / 2) / RL_WID) * (PITCH_H - 1)
    return int(px), int(py)


def _load_font(size: int):
    """Truetype if available (nicer sizing), else PIL's bitmap default."""
    from PIL import ImageFont
    for name in ("arialbd.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _declutter(cx: int, cy: int, placed: list[tuple[int, int]],
               x0: int, y0: int) -> tuple[int, int]:
    """Nudge a marker off any already-placed marker so stacked goals (e.g. two
    goals that crossed the line at the same spot) fan out instead of overlapping."""
    step = DISC_R * 2 + 2
    for k in range(40):
        ok = all((cx - px) ** 2 + (cy - py) ** 2 >= (DISC_R * 2) ** 2 for px, py in placed)
        if ok:
            break
        ang = 2.399963 * k          # golden-angle spiral
        ring = 1 + k // 8
        cx = int(x0 + max(DISC_R, min(PITCH_W - DISC_R,
                 (cx - x0) + math.cos(ang) * step * ring)))
        cy = int(y0 + max(DISC_R, min(PITCH_H - DISC_R,
                 (cy - y0) + math.sin(ang) * step * ring)))
    return cx, cy


def render_goal_map_png(goal_events, team0_name: str = "Blue",
                        team1_name: str = "Orange",
                        viewer_name: str | None = None) -> bytes | None:
    """Render the goal map to PNG bytes, or None if there are no goals / no
    Pillow. `goal_events` are the dicts the aggregator stores (scorer,
    scorer_team, impact_location)."""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    goals = goal_events or []
    if not goals:
        return None

    f_title = _load_font(15)
    f_num = _load_font(13)
    f_leg = _load_font(13)

    # ---- canvas layout: title, pitch, legend ----
    W = PITCH_W + 2 * MARGIN
    px0, py0 = MARGIN, MARGIN + TITLE_H
    px1, py1 = px0 + PITCH_W, py0 + PITCH_H

    n = len(goals)
    cols = 1 if n <= 4 else (2 if n <= 10 else 3)
    rows = math.ceil(n / cols)
    row_h = 18
    legend_top = py1 + 14
    H = legend_top + rows * row_h + MARGIN

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    d.text((MARGIN, MARGIN // 2 + 2), "GOALS  -  where each was scored",
           fill=TEXT, font=f_title)

    # ---- pitch base ----
    d.rectangle([px0, py0, px1, py1], fill=FIELD, outline=LINE, width=2)
    midx = (px0 + px1) // 2
    cy = (py0 + py1) // 2
    d.line([midx, py0, midx, py1], fill=LINE, width=1)
    cr = int(PITCH_W * 0.06)
    d.ellipse([midx - cr, cy - cr, midx + cr, cy + cr], outline=LINE, width=1)
    net_h = int(PITCH_H * 0.375)
    d.rectangle([px0 - NET_T, cy - net_h // 2, px0, cy + net_h // 2], fill=TEAM_BLUE)
    d.rectangle([px1, cy - net_h // 2, px1 + NET_T, cy + net_h // 2], fill=TEAM_ORNG)
    # Net captions (which team defends which end).
    d.text((px0 + 4, py0 + 3), (team0_name or "Blue")[:14], fill=TEAM_BLUE, font=f_num)
    rt = (team1_name or "Orange")[:14]
    rtw = d.textlength(rt, font=f_num)
    d.text((px1 - 4 - rtw, py0 + 3), rt, fill=TEAM_ORNG, font=f_num)

    # ---- goal markers ----
    placed: list[tuple[int, int]] = []
    for i, g in enumerate(goals, 1):
        team = g.get("scorer_team")
        loc = g.get("impact_location")
        if loc and len(loc) >= 2:
            gx, gy = _project(float(loc[0]), float(loc[1]))
        else:  # no location -> park at the net this goal went into
            gx, gy = _project(0.0, RL_LEN / 2 if team == 0 else -RL_LEN / 2)
        cx, cy2 = _declutter(px0 + gx, py0 + gy, placed, px0, py0)
        placed.append((cx, cy2))
        col = _team_color(team)
        is_you = bool(viewer_name and g.get("scorer") == viewer_name)
        ring = GOLD if is_you else (245, 248, 252)
        d.ellipse([cx - DISC_R, cy2 - DISC_R, cx + DISC_R, cy2 + DISC_R],
                  fill=col, outline=ring, width=3 if is_you else 2)
        num = str(i)
        tw = d.textlength(num, font=f_num)
        d.text((cx - tw / 2, cy2 - 8), num, fill=(255, 255, 255), font=f_num)

    # ---- legend: number -> scorer (in scoring order, team-coloured) ----
    col_w = PITCH_W // cols
    for idx, g in enumerate(goals):
        c = idx // rows
        r = idx % rows
        lx = MARGIN + c * col_w
        ly = legend_top + r * row_h
        team = g.get("scorer_team")
        col = _team_color(team)
        d.ellipse([lx, ly + 3, lx + 11, ly + 14], fill=col)
        name = (g.get("scorer") or "?")
        is_you = bool(viewer_name and name == viewer_name)
        label = f"{idx + 1}. {name}"[:22]
        d.text((lx + 16, ly + 1), label, fill=(GOLD if is_you else TEXT), font=f_leg)

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
