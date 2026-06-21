"""Identity resolution from match data (rename-safe, account-ID keyed)."""

from __future__ import annotations

from chumstats.identity import resolve_self_in_match

STEAM = "Steam|76561197985273611|0"


def test_steam_autocorrects_name_keeps_id():
    # We already have the account ID; the name changed since setup -> auto-correct.
    players = [
        {"name": "@NewTag", "primary_id": STEAM, "ticks_total": 100},
        {"name": "Opp", "primary_id": "Steam|1|0", "ticks_total": 0},
    ]
    name, pid, locked = resolve_self_in_match(players, "@OldName", STEAM)
    assert name == "@NewTag"
    assert pid == STEAM
    assert locked is False


def test_epic_first_match_locks_id():
    # No ID yet (Epic typed a name once) -> capture it from the match.
    players = [
        {"name": "Me", "primary_id": "Epic|abc", "ticks_total": 50},
        {"name": "Friend", "primary_id": "Epic|def", "ticks_total": 50},
    ]
    name, pid, locked = resolve_self_in_match(players, "Me", "")
    assert pid == "Epic|abc"
    assert locked is True
    assert name == "Me"


def test_name_match_prefers_own_team():
    # An opponent shares the name but has no telemetry; pick the teammate (us).
    players = [
        {"name": "Dup", "primary_id": "Epic|opp", "ticks_total": 0},
        {"name": "Dup", "primary_id": "Epic|self", "ticks_total": 80},
    ]
    name, pid, locked = resolve_self_in_match(players, "Dup", "")
    assert pid == "Epic|self"
    assert locked is True


def test_no_self_in_match_leaves_unchanged():
    players = [{"name": "Other", "primary_id": "Steam|9|0", "ticks_total": 10}]
    assert resolve_self_in_match(players, "Me", "") == ("Me", "", False)


def test_never_locks_onto_a_bot():
    players = [{"name": "Me", "primary_id": "Unknown|0|0", "ticks_total": 0}]
    name, pid, locked = resolve_self_in_match(players, "Me", "")
    assert pid == ""          # did not lock to the shared bot id
    assert locked is False
    assert name == "Me"
