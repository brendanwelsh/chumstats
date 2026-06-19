"""Server-side touch-heatmap PNG rendering for the Discord post.

The web dashboard renders the team-hued touch heatmap as an SVG (server.py
`_ball_heatmap_svg` / `_heat_pitch_svg`) using an SVG blur + colour-transfer
filter. Discord can't render inline SVG filters in an embed, so we reproduce
the same look as a raster PNG with Pillow: splat every ball touch, blur it into
a density field, and recolour it through the SAME blue / orange ramps the
dashboard uses (`_RAMP` below mirrors the feComponentTransfer tableValues so
the two surfaces match).

Blue and orange touches render as two side-by-side pitches — never overlapped —
exactly like the dashboard split, because one map with both teams' heat on it is
unreadable.

Pillow is optional (declared in the ``bot`` extra). If it isn't installed, or
there are no touches, `render_match_heatmap_png` returns None and the post goes
out without the image.
"""

from __future__ import annotations

from io import BytesIO

# --- Rocket League world coords -> pitch projection -------------------------
# Mirrors server.py `_build_playback_data.project`: the long axis (Y) maps to
# the pitch's horizontal, the short axis (X) to the vertical.
RL_LEN = 10240   # Y range total (-5120 .. +5120)
RL_WID = 8192    # X range total (-4096 .. +4096)

# --- raster geometry (per pitch) --------------------------------------------
PITCH_W, PITCH_H = 520, 208   # 2.5 : 1, same ratio as the dashboard pitch
PAD = 16
LABEL_H = 24
GAP = 20
MARGIN = 16
# Per-touch Gaussian brush: sigma proportional to the pitch (mirrors the
# dashboard SVG's stdDeviation 13 on its 800px pitch). DENSITY_GAIN mirrors the
# SVG feColorMatrix x2.6 boost — both surfaces accumulate adaptive-opacity
# splats, scale them up, then clamp, so hot zones reach the white tip while
# moderate traffic stays team-coloured.
BRUSH_SIGMA = PITCH_W / 61.0
DENSITY_GAIN = 2.6

# --- palette (matches the dashboard CSS custom properties) ------------------
BG = (10, 13, 20)          # --bg   #0a0d14
FIELD = (19, 24, 38)       # --card #131826
LINE = (60, 66, 80)        # ~ --border-strong over the dark field
NET_BLUE = (45, 125, 255)  # --team-blue #2d7dff
NET_ORNG = (255, 122, 24)  # --team-orng #ff7a18
TEXT_DIM = (165, 173, 186) # --text-dim #a5adba

# --- density colour ramps ---------------------------------------------------
# 8 control points (at i/7, i = 0..7). These MIRROR the feComponentTransfer
# tableValues in server.py `_heat_pitch_svg` — keep them in sync so the Discord
# heatmap matches the dashboard. team 0 = blue, team 1 = orange.
_RAMP = {
    0: {  # blue: deep blue -> cyan -> white-hot
        "r": [0.05, 0.06, 0.10, 0.18, 0.34, 0.58, 0.82, 1.0],
        "g": [0.10, 0.28, 0.46, 0.62, 0.76, 0.88, 0.95, 1.0],
        "b": [0.30, 0.55, 0.78, 0.92, 0.99, 1.0, 1.0, 1.0],
    },
    1: {  # orange: deep orange -> amber -> white-hot
        "r": [0.25, 0.50, 0.74, 0.92, 1.0, 1.0, 1.0, 1.0],
        "g": [0.08, 0.20, 0.36, 0.52, 0.68, 0.82, 0.92, 1.0],
        "b": [0.05, 0.06, 0.09, 0.16, 0.28, 0.48, 0.74, 1.0],
    },
}
_ALPHA = [0.0, 0.42, 0.66, 0.82, 0.90, 0.95, 0.98, 1.0]


def _touch_xyt(t) -> tuple[float, float, int | None]:
    """Pull (x, y, team_num) from a BallTouch dataclass or a plain dict."""
    if isinstance(t, dict):
        return float(t.get("x", 0.0)), float(t.get("y", 0.0)), t.get("team_num")
    return (float(getattr(t, "x", 0.0)), float(getattr(t, "y", 0.0)),
            getattr(t, "team_num", None))


def _project(rx: float, ry: float) -> tuple[int, int]:
    """RL world (X, Y) -> pixel (px, py) inside one PITCH_W x PITCH_H pitch."""
    ry = max(min(ry, RL_LEN / 2), -RL_LEN / 2)
    rx = max(min(rx, RL_WID / 2), -RL_WID / 2)
    px = ((ry + RL_LEN / 2) / RL_LEN) * (PITCH_W - 1)
    py = ((rx + RL_WID / 2) / RL_WID) * (PITCH_H - 1)
    return int(px), int(py)


def _build_lut(stops: list[float], gamma: float = 1.0, scale: float = 1.0) -> list[int]:
    """8 control points -> a 256-entry LUT for Image.point().

    Input intensity i maps to ramp position `scale * (i/255)**gamma`: gamma
    lifts midtones, scale (< 1) keeps the densest core off the white tip.
    """
    n = len(stops) - 1  # 7 segments
    out: list[int] = []
    for i in range(256):
        d = (i / 255.0) ** gamma if gamma != 1.0 else i / 255.0
        pos = min(1.0, scale * d) * n
        lo = int(pos)
        if lo >= n:
            out.append(int(round(stops[n] * 255)))
            continue
        frac = pos - lo
        v = stops[lo] * (1 - frac) + stops[lo + 1] * frac
        out.append(int(round(v * 255)))
    return out


def _density_field(points: list[tuple[int, int]], Image):
    """Accumulate a kernel-density field: each touch adds an adaptive-opacity
    Gaussian bump, the sum is boosted (DENSITY_GAIN) and clamped. This is the
    raster equivalent of the dashboard SVG (opacity splats -> blur -> x2.6 ->
    ramp), so absolute brightness tracks real touch density. Returns an 'L'
    image (or None if empty)."""
    import math
    if not points:
        return None
    n = len(points)
    # Same adaptive per-point opacity the SVG uses, so sparse minis stay warm
    # and dense maps don't saturate everywhere.
    pt_op = max(0.06, min(0.60, 2.4 / (n ** 0.5)))
    sigma = max(4.0, BRUSH_SIGMA)
    R = int(round(2.5 * sigma))
    inv = 1.0 / (2.0 * sigma * sigma)
    kernel = [[pt_op * math.exp(-((dx * dx + dy * dy) * inv))
               for dx in range(-R, R + 1)] for dy in range(-R, R + 1)]
    buf = [0.0] * (PITCH_W * PITCH_H)
    for (x, y) in points:
        for dy in range(-R, R + 1):
            yy = y + dy
            if 0 <= yy < PITCH_H:
                row = kernel[dy + R]
                base = yy * PITCH_W
                for dx in range(-R, R + 1):
                    xx = x + dx
                    if 0 <= xx < PITCH_W:
                        buf[base + xx] += row[dx + R]
    data = bytes(min(255, int(min(1.0, v * DENSITY_GAIN) * 255)) for v in buf)
    return Image.frombytes("L", (PITCH_W, PITCH_H), data)


def _render_pitch(points: list[tuple[int, int]], team: int, label: str,
                  Image, ImageDraw, font):
    """One labelled pitch panel: dark field + lines + nets, with the team's
    density heatmap composited on (lines drawn on top, matching the SVG)."""
    panel_w = PITCH_W + 2 * PAD
    panel_h = LABEL_H + PITCH_H + 2 * PAD
    panel = Image.new("RGB", (panel_w, panel_h), BG)
    draw = ImageDraw.Draw(panel)

    x0, y0 = PAD, LABEL_H + PAD
    x1, y1 = x0 + PITCH_W, y0 + PITCH_H

    # Label above the pitch, in the team colour.
    col = NET_BLUE if team == 0 else NET_ORNG
    draw.text((x0, 4), label, fill=col, font=font)

    # Pitch field.
    draw.rectangle([x0, y0, x1, y1], fill=FIELD, outline=LINE, width=2)

    # Heatmap density, composited within the pitch via the team ramp + alpha.
    field = _density_field(points, Image)
    if field is not None:
        ramp = _RAMP[team]
        r = field.point(_build_lut(ramp["r"]))
        g = field.point(_build_lut(ramp["g"]))
        b = field.point(_build_lut(ramp["b"]))
        heat = Image.merge("RGB", (r, g, b))
        alpha = field.point(_build_lut(_ALPHA))
        panel.paste(heat, (x0, y0), alpha)

    # Lines + nets on TOP of the heat (same order as the dashboard SVG).
    midx = (x0 + x1) // 2
    draw.line([midx, y0, midx, y1], fill=LINE, width=1)
    cr = int(PITCH_W * 0.06)
    cy = (y0 + y1) // 2
    draw.ellipse([midx - cr, cy - cr, midx + cr, cy + cr], outline=LINE, width=1)
    net_h = int(PITCH_H * 0.375)
    net_t = 6
    draw.rectangle([x0 - net_t, cy - net_h // 2, x0, cy + net_h // 2], fill=NET_BLUE)
    draw.rectangle([x1, cy - net_h // 2, x1 + net_t, cy + net_h // 2], fill=NET_ORNG)
    return panel


def render_match_heatmap_png(ball_touches) -> bytes | None:
    """Render the match touch heatmap to PNG bytes, or None.

    `ball_touches` is a list of BallTouch (or dicts) with x / y / team_num.
    Blue and orange touches each get their own pitch, side by side, recoloured
    through the dashboard's team ramps. Returns None when there are no touches
    or Pillow isn't installed.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    if not ball_touches:
        return None

    blue: list[tuple[int, int]] = []
    orng: list[tuple[int, int]] = []
    for t in ball_touches:
        x, y, team = _touch_xyt(t)
        if team == 0:
            blue.append(_project(x, y))
        elif team == 1:
            orng.append(_project(x, y))
    if not blue and not orng:
        return None

    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    panels = []
    if blue:
        panels.append(_render_pitch(blue, 0, "BLUE  -  attacking Orange net",
                                    Image, ImageDraw, font))
    if orng:
        panels.append(_render_pitch(orng, 1, "ORANGE  -  attacking Blue net",
                                    Image, ImageDraw, font))

    total_w = sum(p.width for p in panels) + GAP * (len(panels) - 1) + 2 * MARGIN
    total_h = max(p.height for p in panels) + 2 * MARGIN
    canvas = Image.new("RGB", (total_w, total_h), BG)
    x = MARGIN
    for p in panels:
        canvas.paste(p, (x, MARGIN))
        x += p.width + GAP

    buf = BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
