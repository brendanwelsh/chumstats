"""One field-map component for every top-down pitch map on the site.

Same pitch drawing, size, dot style, header (which way you attack) and legend
on the Compare, Player and Clubs pages. Points arrive already projected into
the playback SVG space (see server._build_playback_data: 880 x 380 viewBox,
pitch 800 x 320 at (40, 30); world +Y = right) and, for career maps, already
flipped so the player attacks right.

Kinds and what a mark means:
  touches  density of ball touches (kickoff touches dropped)
  goals    the scorer's last touch before the goal, line to where it crossed
  shots    each shot on goal at the shooter's last touch
  assists  the assister's last touch before the goal
  saves    the saver's touch that made the save
  passes   arrow from a player's touch to the next touch, by a teammate,
           within PASS_WINDOW seconds and at least PASS_MIN_UU apart
"""

from __future__ import annotations

import html
import math

VB_W, VB_H = 880, 380
PAD_X, PAD_Y = 40.0, 30.0
PITCH_W, PITCH_H = 800, 320
CX, CY = PAD_X + PITCH_W / 2, PAD_Y + PITCH_H / 2
GEOM = {"vb_w": VB_W, "vb_h": VB_H, "pitch_w": PITCH_W, "pitch_h": PITCH_H,
        "pad_x": PAD_X, "pad_y": PAD_Y}

PASS_WINDOW = 4.0      # seconds between the pass touch and the teammate's touch
PASS_MIN_UU = 600.0    # ignore touches closer than this (a shared 50/50, not a pass)

# Map kinds in display order: (key, button label, unit word)
KINDS = [
    ("touches", "Touches", "touches"),
    ("goals", "Goals", "goals"),
    ("shots", "Shots", "shots on goal"),
    ("passes", "Passes", "passes"),
    ("assists", "Assists", "assists"),
    ("saves", "Saves", "saves"),
]

# Kinds the live feed cannot place on a map, with the reason (hover text).
NOT_TRACKED = [
    ("Demos", "Not tracked: the game reports who demolished whom, but not where."),
    ("Boost pads", "Not tracked: the game does not report boost pad pickups."),
    ("Missed shots", "Not tracked: the game only reports shots on goal."),
]


def flip(sx: float, sy: float, team) -> tuple[float, float]:
    """Rotate a point 180 degrees about the centre spot when the player was on
    Orange (team 1), so every match reads as attacking right."""
    if team == 1:
        return round(2 * CX - sx, 1), round(2 * CY - sy, 1)
    return sx, sy


def pitch_svg(oriented: bool = True) -> str:
    """Pitch: turf, outline with cut corners, halfway line, centre circle,
    faint thirds, goal boxes and both goals."""
    x0, y0, x1, y1 = PAD_X, PAD_Y, PAD_X + PITCH_W, PAD_Y + PITCH_H
    c = 34  # corner cut
    outline = (f"M{x0 + c},{y0} L{x1 - c},{y0} L{x1},{y0 + c} L{x1},{y1 - c} "
               f"L{x1 - c},{y1} L{x0 + c},{y1} L{x0},{y1 - c} L{x0},{y0 + c} Z")
    t1, t2 = x0 + PITCH_W / 3, x0 + 2 * PITCH_W / 3
    gb_w, gb_h = 70, 150
    net_h = 112
    left_cls = "fm-net fm-net-own" if oriented else "fm-net fm-net-blue"
    right_cls = "fm-net fm-net-opp" if oriented else "fm-net fm-net-orng"
    return (
        f'<path class="fm-turf" d="{outline}"/>'
        f'<line class="fm-third" x1="{t1:.1f}" y1="{y0}" x2="{t1:.1f}" y2="{y1}"/>'
        f'<line class="fm-third" x1="{t2:.1f}" y1="{y0}" x2="{t2:.1f}" y2="{y1}"/>'
        f'<line class="fm-line" x1="{CX}" y1="{y0}" x2="{CX}" y2="{y1}"/>'
        f'<circle class="fm-line" cx="{CX}" cy="{CY}" r="46" fill="none"/>'
        f'<circle class="fm-spot" cx="{CX}" cy="{CY}" r="3"/>'
        f'<rect class="fm-line" x="{x0}" y="{CY - gb_h / 2}" width="{gb_w}" height="{gb_h}" fill="none"/>'
        f'<rect class="fm-line" x="{x1 - gb_w}" y="{CY - gb_h / 2}" width="{gb_w}" height="{gb_h}" fill="none"/>'
        f'<rect class="{left_cls}" x="{x0 - 9}" y="{CY - net_h / 2}" width="9" height="{net_h}" rx="2"/>'
        f'<rect class="{right_cls}" x="{x1}" y="{CY - net_h / 2}" width="9" height="{net_h}" rx="2"/>'
    )


def _r(n: int) -> float:
    """Dot radius shrinks as the count grows so dense maps stay readable."""
    if n <= 30:
        return 5.0
    if n <= 120:
        return 4.2
    if n <= 400:
        return 3.4
    return 2.8


def dots_svg(points: list, cls: str = "fm-dot", *, team_colours: bool = False,
             numbered: bool = False) -> str:
    """Dots with a soft halo. team_colours: Blue/Orange per point (single-match
    maps, as played). numbered: a larger dot carrying the point's "n"."""
    r = 10.5 if numbered else _r(len(points))
    out = []
    for p in points:
        tip = html.escape(p.get("tip") or "")
        t = f"<title>{tip}</title>" if tip else ""
        c = cls
        if team_colours and p.get("team") in (0, 1):
            c += f' fm-t{p["team"]}'
        num = (f'<text class="fm-num" x="{p["sx"]:.1f}" y="{p["sy"]:.1f}" dy="3.5" '
               f'text-anchor="middle">{p["n"]}</text>') if numbered and p.get("n") else ""
        out.append(f'<circle class="fm-halo" cx="{p["sx"]:.1f}" cy="{p["sy"]:.1f}" r="{r * 1.9:.1f}"/>'
                   f'<circle class="{c}" cx="{p["sx"]:.1f}" cy="{p["sy"]:.1f}" r="{r:.1f}">{t}</circle>{num}')
    return "".join(out)


def _arrowhead(x1, y1, x2, y2, size=9.0) -> str:
    ang = math.atan2(y2 - y1, x2 - x1)
    a1, a2 = ang + math.pi - 0.42, ang + math.pi + 0.42
    return (f"{x2:.1f},{y2:.1f} {x2 + size * math.cos(a1):.1f},{y2 + size * math.sin(a1):.1f} "
            f"{x2 + size * math.cos(a2):.1f},{y2 + size * math.sin(a2):.1f}")


def arrows_svg(segs: list) -> str:
    """Pass arrows: from (sx, sy) to (ex, ey)."""
    w = 2.2 if len(segs) <= 60 else (1.6 if len(segs) <= 200 else 1.2)
    out = []
    for s in segs:
        tip = html.escape(s.get("tip") or "")
        out.append(
            f'<g class="fm-pass"><title>{tip}</title>'
            f'<line x1="{s["sx"]:.1f}" y1="{s["sy"]:.1f}" x2="{s["ex"]:.1f}" y2="{s["ey"]:.1f}" '
            f'stroke-width="{w}"/>'
            f'<polygon points="{_arrowhead(s["sx"], s["sy"], s["ex"], s["ey"])}"/></g>')
    return "".join(out)


def goals_svg(goals: list, numbered: bool = False) -> str:
    """Goal: dot at the scorer's last touch; a faint line to the point on the
    goal line where the ball crossed (when the game reported it)."""
    lines = "".join(
        f'<line class="fm-goal-path" x1="{g["sx"]:.1f}" y1="{g["sy"]:.1f}" '
        f'x2="{g["ex"]:.1f}" y2="{g["ey"]:.1f}"/>'
        f'<circle class="fm-goal-cross" cx="{g["ex"]:.1f}" cy="{g["ey"]:.1f}" r="2.6"/>'
        for g in goals if g.get("ex") is not None)
    return lines + dots_svg(goals, "fm-dot fm-dot-goal", team_colours=numbered,
                            numbered=numbered)


def svg(inner: str, *, oriented: bool = True, compact: bool = False) -> str:
    cls = "fm-svg fm-compact" if compact else "fm-svg"
    return (f'<svg viewBox="0 0 {VB_W} {VB_H}" class="{cls}" role="img" '
            f'xmlns="http://www.w3.org/2000/svg">{pitch_svg(oriented)}{inner}</svg>')


def head_html(oriented: bool = True, t0: str = "Blue", t1: str = "Orange") -> str:
    """Strip above the pitch naming both goals and the attacking direction."""
    if oriented:
        return ('<div class="fm-head"><span class="fm-end fm-own">Your goal</span>'
                '<span class="fm-dir">You attack this way <b aria-hidden="true">&#8594;</b></span>'
                '<span class="fm-end fm-opp">Their goal</span></div>')
    return (f'<div class="fm-head"><span class="fm-end fm-blue">{html.escape(t0)} goal</span>'
            f'<span class="fm-dir">Blue attacks <b aria-hidden="true">&#8594;</b></span>'
            f'<span class="fm-end fm-orng">{html.escape(t1)} goal</span></div>')


def legend_html(items: list) -> str:
    """items: (swatch class, text)."""
    return ('<div class="fm-legend">' + "".join(
        f'<span class="fm-leg"><i class="fm-sw {c}"></i>{t}</span>' for c, t in items)
        + '</div>')


LEGENDS = {
    "touches": [("fm-sw-heat", "Fewer to more touches")],
    "goals": [("fm-sw-goal", "Scorer's last touch"), ("fm-sw-path", "Where it crossed the line")],
    "shots": [("fm-sw-dot", "Shooter's last touch")],
    "passes": [("fm-sw-pass", "Touch to a teammate's next touch within 4 s")],
    "assists": [("fm-sw-dot", "Assister's last touch before the goal")],
    "saves": [("fm-sw-dot", "Touch that made the save")],
}

FLIP_NOTE = "Each match flipped so you always attack right."

# Heat layer alpha by density level (sparse -> dense): the turf shows through
# where touches are rare.
HEAT_ALPHA = "0 0.16 0.3 0.45 0.6 0.74 0.87 1"
