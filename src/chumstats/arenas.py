"""Canonical Rocket League arena id -> friendly display-name mapping.

Single source of truth for every surface (dashboard / match / history / bot /
live overlay). RL ships two id conventions (RLBot CamelCase package names like
``Stadium_P`` and ballchasing's lower-cased ``stadium_p``); UPK names are
case-insensitive, so we always normalise the id to lower-case before lookup.

Names reconciled from RLBot's game_map_dict + RLMapChanger + the RL wiki.
``neotokyo_arcade_p`` / ``beach_night_grs_p`` / ``stadium_10a_p`` are added as
verified variants. The remaining opaque capture-only ids (``paname_*``, ``uf_*``,
``mall_*``) are NOT in any public map list, so we render them as "Unknown arena"
(NOT a fabricated title-case like "UF Night", which read as a real map but isn't)
and log them once so the table can be extended if they're ever identified.

Notes baked in from research:
- Psyonix labels BOTH fog and rain weather variants "(Stormy)".
- "standard" vs original-footprint duplicates share one in-game display name.
- Mode arenas (Hoops / Dropshot) are not soccar variants.
"""

from __future__ import annotations

import logging

log = logging.getLogger("chumstats.arenas")

# Keys MUST be lower-case (lookup lower-cases the incoming id).
ARENA_NICE = {
    # --- DFH Stadium ---
    "stadium_p":            "DFH Stadium",
    "stadium_day_p":        "DFH Stadium (Day)",
    "stadium_foggy_p":      "DFH Stadium (Stormy)",
    "stadium_winter_p":     "DFH Stadium (Snowy)",
    "stadium_race_day_p":   "DFH Stadium (Circuit)",
    # --- Mannfield ---
    "eurostadium_p":        "Mannfield",
    "eurostadium_night_p":  "Mannfield (Night)",
    "eurostadium_rainy_p":  "Mannfield (Stormy)",
    "eurostadium_dusk_p":   "Mannfield (Dusk)",
    "eurostadium_snownight_p": "Mannfield (Snowy)",
    # --- Beckwith Park ---
    "park_p":               "Beckwith Park",
    "park_night_p":         "Beckwith Park (Midnight)",
    "park_rainy_p":         "Beckwith Park (Stormy)",
    "park_snowy_p":         "Beckwith Park (Snowy)",
    # --- Urban Central ---
    "trainstation_p":       "Urban Central",
    "trainstation_night_p": "Urban Central (Night)",
    "trainstation_dawn_p":  "Urban Central (Dawn)",
    "haunted_trainstation_p": "Urban Central (Haunted)",
    # --- Utopia Coliseum ---
    "utopiastadium_p":      "Utopia Coliseum",
    "utopiastadium_dusk_p": "Utopia Coliseum (Dusk)",
    "utopiastadium_snow_p": "Utopia Coliseum (Snowy)",
    "utopiastadium_lux_p":  "Utopia Coliseum (Gilded)",
    # --- Champions Field ---
    "cs_p":                 "Champions Field",
    "cs_day_p":             "Champions Field (Day)",
    "cs_hw_p":              "Rivals Arena",
    # --- Wasteland ---
    "wasteland_p":          "Wasteland",
    "wasteland_s_p":        "Wasteland (Standard)",
    "wasteland_night_p":    "Wasteland (Night)",
    "wasteland_night_s_p":  "Wasteland (Standard, Night)",
    "wasteland_grs_p":      "Wasteland (Pitched)",
    # --- Neo Tokyo ---
    "neotokyo_standard_p":  "Neo Tokyo",
    "neotokyo_p":           "Neo Tokyo",
    "neotokyo_toon_p":      "Neo Tokyo (Comic)",
    "neotokyo_hax_p":       "Neo Tokyo (Hacked)",
    # --- AquaDome ---
    "underwater_p":         "AquaDome",
    "underwater_grs_p":     "AquaDome (Pitched)",
    # --- Starbase ARC ---
    "arc_standard_p":       "Starbase ARC",
    "arc_p":                "Starbase ARC",
    "arc_darc_p":           "Starbase ARC (Aftermath)",
    # --- Farmstead ---
    "farm_p":               "Farmstead",
    "farm_night_p":         "Farmstead (Night)",
    "farm_grs_p":           "Farmstead (Pitched)",
    "farm_hw_p":            "Farmstead (Spooky)",
    "farm_upsidedown_p":    "Farmstead (The Upside Down)",
    # --- Salty Shores ---
    "beach_p":              "Salty Shores",
    "beach_night_p":        "Salty Shores (Night)",
    # --- Other soccar ---
    "music_p":              "Neon Fields",
    "outlaw_p":             "Deadeye Canyon",
    "outlaw_oasis_p":       "Deadeye Canyon (Oasis)",
    "street_p":             "Sovereign Heights",
    "ff_dusk_p":            "Estadio Vida (Dusk)",
    "ff_p":                 "Estadio Vida",
    "chn_stadium_p":        "Forbidden Temple",
    "chn_stadium_day_p":    "Forbidden Temple (Day)",
    "chinastadium_p":       "Forbidden Temple",
    "fni_stadium_p":        "Forbidden Temple (Fire & Ice)",
    "throwbackstadium_p":   "Throwback Stadium",
    "throwbackhockey_p":    "Throwback Stadium (Snowy)",
    # --- Mode arenas ---
    "shattershot_p":        "Core 707 (Dropshot)",
    "hoopsstadium_p":       "Dunk House (Hoops)",
    # --- Variant ids confirmed from capture data (2026-06) ---
    "neotokyo_arcade_p":    "Neo Tokyo (Arcade)",
    "beach_night_grs_p":    "Salty Shores (Night, Pitched)",
    "stadium_10a_p":        "DFH Stadium (10th Anniversary)",
}

_unknown_logged: set[str] = set()


def arena_nice(arena: str | None) -> str:
    """Friendly arena name for an internal id. Unknown ids fall back to a clean
    title-cased form (trailing ``_P`` and underscores stripped) and are logged
    once so the table can be extended after verification."""
    if not arena:
        return "Unknown arena"
    key = arena.lower()
    name = ARENA_NICE.get(key)
    if name is not None:
        return name
    if key not in _unknown_logged:
        _unknown_logged.add(key)
        log.info("unmapped arena id %r -> shown as 'Unknown arena'", arena)
    # Don't fabricate a friendly name from an opaque internal id — e.g.
    # "UF_Night_P" title-cased to "UF Night", which reads like a real map but
    # isn't. Show a neutral label; the id is logged so the table can be extended
    # once the map is identified.
    return "Unknown arena"
