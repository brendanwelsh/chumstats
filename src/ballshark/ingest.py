"""Live ingest: connect to the RL Stats API TCP socket and run the pipeline.

The socket emits concatenated UTF-8 JSON envelopes:
    {"Event":"...","Data":"<json-string>"}
with no delimiters. We accumulate bytes and use a brace-aware scanner
(string + escape aware) to find object boundaries, identical in shape to
what we did in capture.ps1 but in Python.

Pipeline per envelope:
    raw bytes -> JSON object -> Envelope -> dispatch to MatchAggregator
                                         -> append to raw_events (DB)
On MatchEnded + MatchDestroyed sequence, MatchAggregator emits a
MatchSummary, which we persist via Store.save_match and feed to the in-memory
SessionTracker.

This module is the lone owner of socket lifecycle: it reconnects on close
(useful because RL closes the socket between sessions / on quit).
"""

from __future__ import annotations

import json
import socket
import time
from collections.abc import Callable, Iterator
from typing import Any

from .models import Envelope
from .session import MatchAggregator, MatchSummary, SessionTracker
from .store import Store

# Idle-based finalization: how often the recv loop wakes to run closure checks,
# and how long a match may go without ANY events before we treat it as over.
SOCKET_POLL_INTERVAL = 1.0
MATCH_IDLE_TIMEOUT = 30.0


def _find_complete_json(buf: str, start: int = 0) -> tuple[int, int] | None:
    """Find the next complete JSON object in `buf` starting at or after `start`.
    Returns (object_start_index, object_end_index_inclusive) or None if no
    complete object is available yet.

    Brace-depth scanner that respects strings and escapes."""
    i = start
    n = len(buf)
    # Skip whitespace
    while i < n and buf[i] in " \r\n\t":
        i += 1
    if i >= n:
        return None
    if buf[i] != "{":
        # Not the start of an object - skip one char so we don't loop.
        return _find_complete_json(buf, i + 1)

    depth = 0
    in_str = False
    esc = False
    j = i
    while j < n:
        c = buf[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i, j
        j += 1
    return None


def iter_socket_envelopes(host: str = "127.0.0.1", port: int = 49123,
                          recv_bytes: int = 65536,
                          poll_timeout: float | None = None
                          ) -> Iterator[tuple[Envelope | None, dict[str, Any] | None]]:
    """Yield (Envelope, raw_outer_dict) as objects arrive on the TCP socket.
    If `poll_timeout` is set, yields a (None, None) sentinel whenever no data
    arrives within that window so the caller can run idle/closure checks.
    Caller is responsible for handling KeyboardInterrupt etc."""
    sock = socket.create_connection((host, port))
    sock.settimeout(poll_timeout)
    buf = ""
    try:
        while True:
            try:
                chunk = sock.recv(recv_bytes)
            except socket.timeout:
                yield None, None
                continue
            if not chunk:
                return
            try:
                buf += chunk.decode("utf-8")
            except UnicodeDecodeError:
                buf += chunk.decode("utf-8", errors="replace")

            while True:
                hit = _find_complete_json(buf)
                if hit is None:
                    break
                start, end = hit
                obj_str = buf[start:end + 1]
                buf = buf[end + 1:]
                try:
                    obj = json.loads(obj_str)
                except json.JSONDecodeError:
                    continue
                try:
                    env = Envelope.model_validate(obj)
                except Exception:
                    continue
                yield env, obj
    finally:
        try:
            sock.close()
        except Exception:
            pass


def run_live(
    store: Store,
    session: SessionTracker,
    host: str = "127.0.0.1",
    port: int = 49123,
    on_match: Callable[[MatchSummary], None] | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
    on_status: Callable[[bool], None] | None = None,
    reconnect_delay: float = 2.0,
) -> None:
    """Connect to RL, ingest forever. Reconnects on socket close.

    Finalization happens on three triggers so we never lose a completed match
    even when the user leaves early:
      1. Next MatchCreated arrives (most common - between matches in a session).
      2. MatchDestroyed arrives (clean exit through the post-game screen).
      3. Socket closes with an ended agg (user quit RL without going through
         the full post-game screen). Most common for the "leave matches a lot
         when done" workflow.
    """
    # Shared helper used by all three finalization paths.
    def _commit(agg: MatchAggregator, reason: str, force: bool = False) -> None:
        if not agg:
            return
        s = agg.finalize(force=force)
        if not s:
            return
        try:
            store.save_match(s)
        except Exception as e:
            print(f"[ingest] save_match failed ({reason}): {e}")
            return
        session.add(s)
        print(f"[ingest] finalized match ({reason}): "
              f"{s.team0_name} {s.team0_score} - {s.team1_score} {s.team1_name}")
        if on_match:
            try:
                on_match(s)
            except Exception as e:
                print(f"[ingest] on_match callback failed: {e}")

    while True:
        agg: MatchAggregator | None = None
        try:
            print(f"[ingest] connecting to {host}:{port}...")
            connected = False
            last_event = time.time()
            for env, _outer in iter_socket_envelopes(
                    host, port, poll_timeout=SOCKET_POLL_INTERVAL):
                if not connected:
                    connected = True
                    print("[ingest] connected to Rocket League Stats API")
                    if on_status:
                        on_status(True)

                # Idle poll tick (no data this interval): run closure checks so a
                # match that ended but never got MatchDestroyed/next-MatchCreated
                # (user sitting on a screen) still gets finalized.
                if env is None:
                    if (agg is not None and agg.ended
                            and (time.time() - last_event) > MATCH_IDLE_TIMEOUT):
                        _commit(agg, "idle timeout")
                        agg = None
                    continue

                last_event = time.time()
                received_at = time.time()
                try:
                    event_name, raw, parsed = env.parse_payload()
                except Exception:
                    continue

                if event_name == "MatchCreated":
                    if agg is not None:
                        _commit(agg, "next match started")
                    agg = MatchAggregator()

                # If we missed MatchCreated (server restarted mid-match), spawn
                # an aggregator on demand the first time we see an event with a
                # MatchGuid. This used to silently drop the whole match.
                if agg is None and event_name in ("MatchInitialized", "UpdateState",
                                                   "RoundStarted", "CountdownBegin",
                                                   "GoalScored", "BallHit",
                                                   "ClockUpdatedSeconds"):
                    payload_guid = raw.get("MatchGuid") if isinstance(raw, dict) else None
                    if payload_guid:
                        agg = MatchAggregator()
                        agg.match_guid = payload_guid
                        print(f"[ingest] adopted in-progress match {payload_guid} "
                              f"(MatchCreated was missed)")

                if agg is not None:
                    agg.handle(event_name, parsed, raw=raw)

                match_id = agg.match_guid if agg else ""
                store.save_raw_event(
                    received_at,
                    match_id or None,
                    event_name,
                    json.dumps(raw, separators=(",", ":")),
                )

                if on_event:
                    on_event(event_name, raw)

                if event_name == "MatchDestroyed" and agg is not None:
                    _commit(agg, "MatchDestroyed")
                    agg = None

            print("[ingest] socket closed - reconnecting")
        except ConnectionRefusedError:
            print(f"[ingest] connection refused; is RL running with PacketSendRate > 0?")
        except KeyboardInterrupt:
            # User Ctrl-C: salvage an ended match, then bail (finally still runs,
            # so clear agg to avoid a double-commit).
            if agg is not None:
                _commit(agg, "shutdown")
                agg = None
            print("[ingest] interrupted")
            return
        except Exception as e:
            print(f"[ingest] error: {e}")
        finally:
            # Socket dropped (RL closed): RL is gone, so force-finalize a match
            # that had real play even if MatchEnded/Destroyed were missed — it's
            # as complete as it will ever be. On localhost a socket close means
            # RL quit, not a transient blip.
            if agg is not None:
                _commit(agg, "socket closed", force=True)
            agg = None
            if on_status:
                on_status(False)

        time.sleep(reconnect_delay)
