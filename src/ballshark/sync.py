"""Client-side sync: POST each finalized MatchSummary to a central ballshark server.

Hooked into the live pipeline alongside the Discord bot and overlay broadcaster.
Failures are logged but never crash the ingest — local DB stays authoritative
on each friend's machine; the central server is the group-view aggregator.

See `docs/multi-user-network.md` and `server.py` `MatchSummaryUpload` for the wire format.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .session import MatchSummary, AGGREGATOR_VERSION

log = logging.getLogger("ballshark.sync")

RETRY_DELAYS = (1.0, 2.0, 4.0)  # seconds between attempts


def _player_dict(p, is_mvp: bool) -> dict:
    return {
        "primary_id": p.primary_id, "name": p.name, "team_num": p.team_num,
        "goals": p.goals, "shots": p.shots, "assists": p.assists, "saves": p.saves,
        "demos": p.demos, "touches": p.touches, "score": p.score,
        "is_bot": bool(p.is_bot), "is_mvp": bool(is_mvp), "platform": p.platform,
        "ticks_total": p.ticks_total, "ticks_on_wall": p.ticks_on_wall,
        "ticks_on_ground": p.ticks_on_ground, "ticks_in_air": p.ticks_in_air,
        "ticks_boosting": p.ticks_boosting, "ticks_supersonic": p.ticks_supersonic,
        "ticks_zero_boost": p.ticks_zero_boost, "ticks_full_boost": p.ticks_full_boost,
        "speed_sum": p.speed_sum, "speed_max": p.speed_max, "boost_used": p.boost_used,
    }


def build_payload(summary: MatchSummary, owner_primary_id: str) -> dict | None:
    """Convert a MatchSummary to the upload schema. Returns None if `owner_primary_id`
    isn't in the match (e.g., friend was spectating / not playing this game)."""
    from dataclasses import asdict
    my_p = next((p for p in summary.players if p.primary_id == owner_primary_id), None)
    if my_p is None:
        return None
    others = [p for p in summary.players if p.primary_id != owner_primary_id]
    return {
        "match_id": summary.match_id,
        "parser_version": AGGREGATOR_VERSION,
        "started_at": summary.started_at, "ended_at": summary.ended_at,
        "arena": summary.arena,
        "team0_score": summary.team0_score, "team1_score": summary.team1_score,
        "team0_name": summary.team0_name, "team1_name": summary.team1_name,
        "team0_color": summary.color_primary.get(0, ""),
        "team1_color": summary.color_primary.get(1, ""),
        "winner_team_num": summary.winner_team_num,
        "is_online": bool(summary.is_online),
        "crossbar_hits": summary.crossbar_hits,
        "duration_seconds": summary.duration_seconds,
        "my_row": _player_dict(my_p, summary.is_mvp.get(my_p.primary_id, False)),
        "other_rows": [_player_dict(p, summary.is_mvp.get(p.primary_id, False))
                       for p in others],
        # Match-level extras — needed for heatmaps + per-goal detail on central.
        "ball_touches": [asdict(b) for b in summary.ball_touches],
        "goal_events":  list(summary.goal_events),
    }


class MatchSyncer:
    """Enqueue MatchSummary → POST to central server. Drained by an async task."""

    def __init__(self, remote_url: str, api_key: str, owner_primary_id: str,
                 store=None) -> None:
        self.remote_url = remote_url.rstrip("/") + "/api/v1/match-summary"
        self.api_key = api_key
        self.owner_primary_id = owner_primary_id
        self.store = store      # optional; if set, we attach lifecycle raw_events
        self.queue: asyncio.Queue[MatchSummary] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None

    def enqueue(self, summary: MatchSummary) -> None:
        """Thread-safe enqueue from the ingest thread."""
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(self.queue.put_nowait, summary)
        else:
            # Pre-loop or post-loop drop: log and discard. Local DB still has it.
            log.warning("syncer queue not ready; dropped match %s", summary.match_id)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        print(f"[sync] enabled -> {self.remote_url}")
        while True:
            summary = await self.queue.get()
            try:
                await self._post_with_retry(summary)
            except Exception:
                log.exception("sync drain loop error")
            finally:
                self.queue.task_done()

    async def _post_with_retry(self, summary: MatchSummary) -> None:
        payload = build_payload(summary, self.owner_primary_id)
        if payload is None:
            log.info("skipping sync for %s: owner primary_id not in match", summary.match_id)
            return
        # Attach lifecycle raw_events from the local DB if we have store access.
        # Excludes UpdateState (the 30 Hz firehose) — see docstring above.
        if self.store is not None:
            payload["raw_events"] = self._fetch_lifecycle_events(summary.match_id)
        body = json.dumps(payload).encode("utf-8")
        last_err: Exception | None = None
        for attempt, delay in enumerate([0.0, *RETRY_DELAYS]):
            if delay:
                await asyncio.sleep(delay)
            try:
                resp = await asyncio.to_thread(self._do_post, body)
                status = resp.get("status_code", 0)
                if 200 <= status < 300:
                    body_json = resp.get("body") or {}
                    print(f"[sync] uploaded {summary.match_id[:8]}... "
                          f"(created={body_json.get('created_match')}, "
                          f"others+{body_json.get('others_inserted', 0)})")
                    return
                if status in (401, 403):
                    log.error("sync rejected (status %s): %s — check BALLSHARK_API_KEY", status, resp.get("body"))
                    return  # don't retry auth failures
                last_err = RuntimeError(f"HTTP {status}: {resp.get('body')}")
            except (URLError, OSError) as e:
                last_err = e
                log.warning("sync attempt %d failed: %s", attempt + 1, e)
        log.error("sync gave up on %s after %d attempts: %s",
                  summary.match_id, 1 + len(RETRY_DELAYS), last_err)

    def _fetch_lifecycle_events(self, match_id: str) -> list[dict]:
        """Read the match's raw events from local DB, skipping the firehose."""
        try:
            with self.store._conn() as c:
                rows = c.execute(
                    "SELECT event, payload FROM raw_events "
                    "WHERE match_id = ? AND event != 'UpdateState' "
                    "ORDER BY received_at ASC, id ASC",
                    (match_id,),
                ).fetchall()
            return [{"event": r["event"], "payload": r["payload"]} for r in rows]
        except Exception:
            log.exception("failed to fetch lifecycle events for %s", match_id)
            return []

    def _do_post(self, body: bytes) -> dict:
        req = Request(
            self.remote_url, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Ballshark-Key": self.api_key,
            },
        )
        try:
            with urlopen(req, timeout=10) as r:
                raw = r.read().decode("utf-8") or "{}"
                return {"status_code": r.status, "body": json.loads(raw)}
        except HTTPError as e:
            raw = e.read().decode("utf-8") or "{}"
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = {"raw": raw}
            return {"status_code": e.code, "body": parsed}
