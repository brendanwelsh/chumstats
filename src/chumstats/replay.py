"""Replay a captured .jsonl file through the parser/aggregator.

This is a dev tool. It lets us iterate on the pipeline without playing
matches live. Two modes:

  - "fast": yields events as fast as we can read the file. Good for tests.
  - "realtime": tries to match the original tick cadence (~30Hz). Good for
    poking the overlay/Discord output and seeing how it'd look live.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

from .models import Envelope, parse_envelope_obj


def iter_jsonl(path: str | Path) -> Iterator[Envelope]:
    """Yield Envelopes from a .jsonl capture file."""
    p = Path(path)
    with p.open("r", encoding="utf-8-sig") as f:  # utf-8-sig swallows leading BOM
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                yield parse_envelope_obj(obj)
            except Exception:
                # Tolerate malformed lines - we want every other event.
                continue


def iter_parsed(path: str | Path):
    """Yield (event_name, raw_dict, parsed_or_None) for each line.

    A payload that fails its typed model degrades to parsed=None instead of
    raising — live ingest drops such events individually, and one bad line
    must not abort the whole replay."""
    for env in iter_jsonl(path):
        try:
            yield env.parse_payload()
        except Exception:
            try:
                raw = json.loads(env.data) if env.data else {}
            except ValueError:
                continue
            yield env.event, raw, None


def iter_for_aggregator(path: str | Path):
    """Yield (event_name, raw, parsed) triples ready for MatchAggregator/run_aggregation."""
    for event_name, raw, parsed in iter_parsed(path):
        yield event_name, raw, parsed


def replay_realtime(path: str | Path, hz: float = 30.0):
    """Like iter_parsed but sleeps between ticks to approximate live cadence.
    Sleep is based on UpdateState count, not wall clock from the file."""
    interval = 1.0 / hz
    next_tick = time.monotonic()
    for tup in iter_parsed(path):
        event_name = tup[0]
        if event_name == "UpdateState":
            now = time.monotonic()
            if now < next_tick:
                time.sleep(next_tick - now)
            next_tick += interval
        yield tup
