"""Live ingest: connect to the RL Stats API TCP socket and run the pipeline.

The socket emits concatenated UTF-8 JSON envelopes:
    {"Event":"...","Data":"<json-string>"}
with no delimiters. We accumulate bytes and use a brace-aware scanner
(string + escape aware) to find object boundaries, identical in shape to
what we did in capture.ps1 but in Python.

Pipeline per envelope:
    raw bytes -> JSON object + typed payload -> dispatch to MatchAggregator
                                             -> buffer for raw_events (DB)
Raw events are flushed to SQLite in batches (one transaction per ~hundreds of
packets) rather than one tiny transaction per packet. On MatchEnded +
MatchDestroyed sequence, MatchAggregator emits a MatchSummary, which we persist
via Store.save_match and feed to the in-memory SessionTracker.

THREADING / WHY IT'S SPLIT IN TWO
---------------------------------
RL streams ~30 stat packets/sec over the loopback socket. If the SAME thread
that recv()s also does the slow work — JSON parsing, aggregation, the SQLite
writes, and the on_match upload to the central host — then any stall in that
slow work (slow disk, network latency to the upload target) stops the socket
from being drained. RL's TCP send buffer then fills, RL's game thread blocks on
send(), and the GAME FREEZES (Windows logs it as AppHangXProcB1, a cross-process
hang). This is a real bug we hit.

So the socket drain is fully decoupled from all slow work:

  * a READER thread (`_socket_reader`) does NOTHING but recv() and push raw
    bytes onto an unbounded queue.Queue. It never parses, never touches disk,
    never makes a network call — so it can never block, and RL's sender can
    never back up.
  * the WORKER (`run_live`'s own loop) consumes that queue and does all the
    parsing / aggregation / persistence / callbacks. If it stalls, the queue
    just grows in memory; the reader keeps the socket empty regardless.

Defense in depth: we also request a large SO_RCVBUF so the kernel itself buffers
a burst even before our queue sees it, and the RL Stats API `PacketSendRate` can
be lowered (see `chumstats setup --rate`) to reduce the packet pressure.

This module is the lone owner of socket lifecycle: it reconnects on close
(useful because RL closes the socket between sessions / on quit).
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

from .models import EVENT_MODEL
from .session import MatchAggregator, MatchSummary, SessionTracker
from .store import Store

# Idle-based finalization: how often the worker wakes to run closure checks,
# and how long a match may go without ANY events before we treat it as over.
SOCKET_POLL_INTERVAL = 1.0
MATCH_IDLE_TIMEOUT = 30.0

# Raw-event persistence is batched: instead of one SQLite transaction per
# envelope (~30 Hz UpdateState + clock + ball-hit => tens of tiny commits per
# second, each re-opening a connection), the worker buffers rows and flushes
# them with a single executemany. We flush when the buffer hits this many rows,
# on every idle tick, and always before a match is finalized (so the syncer and
# any reader see a complete, persisted match). Bounds memory mid-match while
# collapsing hundreds of transactions into a handful.
RAW_BATCH_MAX = 256

# Reader-thread recv() timeout. Small so the reader notices a stop request
# promptly; otherwise it just blocks waiting for RL packets. This is NOT an
# idle/closure timer — that's the worker's job via the queue.get() timeout.
READER_RECV_TIMEOUT = 1.0

# A refused connect means nothing is listening on the Stats API port — RL is
# closed, or an RL update reset PacketSendRate to 0. That state can persist for
# days, so retries back off exponentially (reconnect_delay doubling up to this
# cap) and the refusal is logged once, then summarized at most every
# REFUSED_LOG_INTERVAL seconds instead of two lines per attempt forever.
RECONNECT_DELAY_MAX = 30.0
REFUSED_LOG_INTERVAL = 300.0

# Defense in depth: ask the OS for a fat receive buffer. Even before our reader
# thread pulls bytes off, the kernel can soak up a burst without back-pressuring
# RL's sender. The queue decoupling is the real fix; this just adds slack.
DEFAULT_SO_RCVBUF = 4 * 1024 * 1024  # 4 MiB (kernel may clamp to its own max)

# Queue protocol (reader -> worker). Unbounded queue => reader's put never
# blocks, which is the whole point.
_DATA = "data"      # ("data", bytes)   - a recv chunk
_CLOSED = "closed"  # ("closed", None)  - peer closed cleanly
_ERROR = "error"    # ("error", exc)    - recv raised; worker should reconnect


def _find_complete_json(buf: str, start: int = 0) -> tuple[int, int] | None:
    """Find the next complete JSON object in `buf` starting at or after `start`.
    Returns (object_start_index, object_end_index_inclusive) or None if no
    complete object is available yet.

    Brace-depth scanner that respects strings and escapes."""
    i = start
    n = len(buf)
    # Skip anything that isn't the start of an object: whitespace between
    # concatenated envelopes, or stray bytes from a malformed packet. Iterative
    # (not recursive) so a buffer full of leading non-'{' bytes can't blow the
    # recursion limit or pay a stack frame per skipped byte.
    while i < n and buf[i] != "{":
        i += 1
    if i >= n:
        return None

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


def _drain_buffer(buf: str) -> tuple[list[tuple[str, dict[str, Any], Any]], str]:
    """Pull every complete envelope out of `buf`, returning
    (list_of_(event_name, raw_payload_dict, parsed_or_None), unconsumed_remainder).

    Runs on the WORKER thread — JSON decoding and pydantic validation are part
    of the "slow work" we keep off the recv path. Malformed objects are skipped
    individually so one bad packet can't wedge the stream.

    Each envelope is parsed all the way through here (outer object + inner Data
    payload + the typed model), so the worker loop gets ready-to-use triples and
    we never build a throwaway `Envelope` instance per packet. We scan with a
    moving cursor and slice the remainder exactly once instead of reslicing the
    whole buffer per object (that was quadratic when many envelopes arrive in one
    recv chunk)."""
    out: list[tuple[str, dict[str, Any], Any]] = []
    pos = 0
    while True:
        hit = _find_complete_json(buf, pos)
        if hit is None:
            return out, buf[pos:]
        start, end = hit
        pos = end + 1
        try:
            obj = json.loads(buf[start:end + 1])
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        event = obj.get("Event")
        data = obj.get("Data")
        # Outer shape requires string Event + Data (Data is itself JSON text).
        if not isinstance(event, str) or not isinstance(data, str):
            continue
        try:
            raw = json.loads(data) if data else {}
        except ValueError:
            continue
        model = EVENT_MODEL.get(event)
        parsed = None
        if model is not None:
            try:
                parsed = model.model_validate(raw)
            except Exception:
                # A payload that fails its model is dropped wholesale — same as
                # the old path, where parse_payload() raising skipped the event
                # before it reached aggregation or the raw_events archive.
                continue
        out.append((event, raw, parsed))


def _socket_reader(sock: socket.socket, out: queue.Queue, stop: threading.Event,
                   recv_bytes: int) -> None:
    """Reader thread target. Its ONLY job: pull bytes off the RL socket as fast
    as the kernel delivers them and hand them to the worker via `out`.

    It performs no parsing, no disk I/O, and no callbacks — nothing that could
    block — so RL's TCP send buffer can never back up and stall RL's game thread.
    `out` is unbounded, so `put` never blocks the reader either.

    Pushes onto `out` per the queue protocol: ("data", bytes) for each recv
    chunk, ("closed", None) on a clean peer close, ("error", exc) if recv
    raised. Returns after a terminal marker so the worker can reconnect."""
    while not stop.is_set():
        try:
            chunk = sock.recv(recv_bytes)
        except socket.timeout:
            continue  # just a wakeup so we can re-check `stop`
        except OSError as e:
            out.put((_ERROR, e))
            return
        if not chunk:
            out.put((_CLOSED, None))
            return
        out.put((_DATA, chunk))


def run_live(
    store: Store,
    session: SessionTracker,
    host: str = "127.0.0.1",
    port: int = 49123,
    on_match: Callable[[MatchSummary], None] | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
    on_status: Callable[[bool], None] | None = None,
    reconnect_delay: float = 2.0,
    recv_bytes: int = 65536,
    so_rcvbuf: int | None = DEFAULT_SO_RCVBUF,
) -> None:
    """Connect to RL, ingest forever. Reconnects on socket close.

    A dedicated reader thread drains the socket into a queue; this function is
    the worker that consumes that queue and does all the slow work, so the
    socket is always read promptly regardless of disk/network latency (see the
    module docstring for the freeze this prevents).

    Finalization happens on three triggers so we never lose a match even when
    the user leaves early. Each one force-salvages an un-ended-but-played match
    from its final scores (see finalize(force=True)), so a forfeit / early-leave
    that never got a MatchEnded is still recorded instead of discarded:
      1. Next MatchCreated arrives (left a match and requeued).
      2. MatchDestroyed arrives (exit to menu — clean post-game OR a forfeit;
         a leaving client gets MatchDestroyed but never the final MatchEnded).
      3. Socket closes (user quit RL). Most common for the "leave matches a lot
         when done" workflow.
    """
    # Pending raw_events, flushed in one transaction instead of one per packet.
    # Lives across reconnects; the finally clause flushes whatever's left.
    raw_batch: list[tuple[float, str | None, str, str]] = []

    def _flush_raw() -> None:
        """Persist buffered raw_events in a single transaction. Best-effort: a DB
        error drops the batch with a log rather than tearing down ingest (the
        match itself is still saved via save_match, and raw_events are an archive
        we can rebuild). Prefers the bulk path but falls back to per-row writes so
        a duck-typed store without save_raw_events_bulk still works."""
        if not raw_batch:
            return
        rows = raw_batch[:]
        raw_batch.clear()
        try:
            bulk = getattr(store, "save_raw_events_bulk", None)
            if bulk is not None:
                bulk(rows)
            else:
                for r in rows:
                    store.save_raw_event(*r)
        except Exception as e:
            print(f"[ingest] save_raw_events failed (dropped {len(rows)}): {e}")

    # Shared helper used by all finalization paths. Runs on the WORKER thread,
    # never on the recv path, so a slow save_match / on_match cannot back up the
    # socket.
    def _commit(agg: MatchAggregator, reason: str, force: bool = False) -> None:
        # Flush first so the finalized match's raw_events are on disk before the
        # syncer (or any reader) goes looking for them in save_match/on_match.
        _flush_raw()
        if not agg:
            return
        ended_cleanly = agg.ended
        s = agg.finalize(force=force)
        if not s:
            return
        # Private lobbies reuse the same MatchGuid for post-game segments
        # (rematch screen, lobby resets). A force-salvaged segment carrying the
        # guid of a match we already saved would INSERT OR REPLACE junk over the
        # real result — only a real MatchEnded may overwrite an existing row.
        has_match = getattr(store, "has_match", None)
        if not ended_cleanly and has_match is not None:
            try:
                if has_match(s.match_id):
                    print(f"[ingest] skipped salvage of {s.match_id} ({reason}): "
                          f"match already recorded (guid reused by lobby)")
                    return
            except Exception:
                pass
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

    def _process(event_name: str, raw: dict[str, Any], parsed: Any,
                 agg: MatchAggregator | None) -> MatchAggregator | None:
        """Handle one already-parsed envelope; returns the (possibly new/cleared)
        agg. Parsing now happens in _drain_buffer, so this is pure dispatch."""
        received_at = time.time()

        if event_name == "MatchCreated":
            if agg is not None:
                # The previous match never got a MatchDestroyed before this new
                # one started => the user left it early and requeued. force=True
                # salvages it from its final scores (no-op when it ended cleanly).
                _commit(agg, "next match started", force=not agg.ended)
            agg = MatchAggregator()

        # If we missed MatchCreated (server restarted mid-match), spawn an
        # aggregator on demand the first time we see an event with a MatchGuid.
        # This used to silently drop the whole match.
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
            agg.handle(event_name, parsed, raw=raw, received_at=received_at)

        match_id = agg.match_guid if agg else ""
        raw_batch.append((
            received_at,
            match_id or None,
            event_name,
            json.dumps(raw, separators=(",", ":")),
        ))
        if len(raw_batch) >= RAW_BATCH_MAX:
            _flush_raw()

        if on_event:
            on_event(event_name, raw)

        if event_name == "MatchDestroyed" and agg is not None:
            # MatchDestroyed with no preceding MatchEnded = the user forfeited /
            # left before the game broadcast its result (a leaving client never
            # receives MatchEnded). force=True salvages it from the final team
            # scores, the same way the socket-close path does, so a real abandoned
            # game isn't silently dropped. finalize() still returns None for a
            # genuine abort (no tick state / no play, or a tied score with no
            # inferable winner), so empty lobby-cancels stay dropped. When the
            # match DID end cleanly, force is a no-op (winner already known).
            _commit(agg, "MatchDestroyed", force=not agg.ended)
            agg = None

        return agg

    # Reconnect state. `refused_streak` counts consecutive refused connects;
    # it drives both the backoff delay and the log rate-limit. `last_status`
    # dedupes on_status so subscribers see transitions, not a False per retry.
    refused_streak = 0
    last_refused_log = 0.0
    last_status: bool | None = None

    def _set_status(connected: bool) -> None:
        # Only fire on change. Also swallow callback errors: this runs inside
        # the connection `finally`, where an exception would kill ingest.
        nonlocal last_status
        if connected == last_status:
            return
        last_status = connected
        if on_status:
            try:
                on_status(connected)
            except Exception as e:
                print(f"[ingest] on_status callback failed: {e}")

    while True:
        agg: MatchAggregator | None = None
        sock: socket.socket | None = None
        reader: threading.Thread | None = None
        stop = threading.Event()
        q: queue.Queue = queue.Queue()
        delay = reconnect_delay
        try:
            if refused_streak == 0:
                print(f"[ingest] connecting to {host}:{port}...")
            sock = socket.create_connection((host, port))
            if so_rcvbuf:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, so_rcvbuf)
                except OSError:
                    pass  # best effort; not all stacks honor the request
            sock.settimeout(READER_RECV_TIMEOUT)
            reader = threading.Thread(
                target=_socket_reader, args=(sock, q, stop, recv_bytes),
                name="chumstats-recv", daemon=True,
            )
            reader.start()

            if refused_streak:
                print(f"[ingest] connected to Rocket League Stats API "
                      f"(after {refused_streak} refused attempts)")
            else:
                print("[ingest] connected to Rocket League Stats API")
            refused_streak = 0
            _set_status(True)

            buf = ""
            last_event = time.time()
            while True:
                try:
                    kind, payload = q.get(timeout=SOCKET_POLL_INTERVAL)
                except queue.Empty:
                    # Idle tick (no data this interval): flush any buffered
                    # raw_events so a quiet stretch can't leave recent packets
                    # unpersisted, then run closure checks so a match that ended
                    # but never got MatchDestroyed / next MatchCreated (user
                    # sitting on a screen) still finalizes.
                    _flush_raw()
                    if (agg is not None and agg.ended
                            and (time.time() - last_event) > MATCH_IDLE_TIMEOUT):
                        _commit(agg, "idle timeout")
                        agg = None
                    continue

                if kind == _CLOSED:
                    print("[ingest] socket closed - reconnecting")
                    break
                if kind == _ERROR:
                    raise payload  # surface to the except below -> reconnect

                # kind is _DATA: append bytes and drain every complete envelope.
                try:
                    buf += payload.decode("utf-8")
                except UnicodeDecodeError:
                    buf += payload.decode("utf-8", errors="replace")
                envs, buf = _drain_buffer(buf)
                for event_name, raw, parsed in envs:
                    last_event = time.time()
                    agg = _process(event_name, raw, parsed, agg)

        except ConnectionRefusedError:
            refused_streak += 1
            now = time.time()
            if refused_streak == 1:
                print("[ingest] connection refused; is RL running with "
                      "PacketSendRate > 0? (retrying with backoff, logging at "
                      f"most every {REFUSED_LOG_INTERVAL / 60:.0f} min)")
                last_refused_log = now
            elif now - last_refused_log >= REFUSED_LOG_INTERVAL:
                print(f"[ingest] Stats API still refusing connections "
                      f"({refused_streak} attempts so far); RL is closed or "
                      f"PacketSendRate=0 — run `chumstats setup` to re-enable")
                last_refused_log = now
            # 2s, 4s, 8s, ... capped. min() on the exponent so a days-long
            # outage can't overflow 2**streak.
            delay = min(reconnect_delay * (2 ** min(refused_streak - 1, 16)),
                        RECONNECT_DELAY_MAX)
        except KeyboardInterrupt:
            # User Ctrl-C (only reachable when run_live is on the main thread):
            # salvage an ended match, then bail. `finally` still runs, so clear
            # agg to avoid a double-commit.
            if agg is not None:
                _commit(agg, "shutdown")
                agg = None
            print("[ingest] interrupted")
            return
        except Exception as e:
            print(f"[ingest] error: {e}")
        finally:
            # Stop the reader and release the socket before we (maybe) reconnect.
            stop.set()
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            if reader is not None:
                reader.join(timeout=2.0)
            # Persist any buffered raw_events (including between-match events with
            # no agg) before we drop this connection.
            _flush_raw()
            # Socket dropped (RL closed): RL is gone, so force-finalize a match
            # that had real play even if MatchEnded/Destroyed were missed — it's
            # as complete as it will ever be. On localhost a socket close means
            # RL quit, not a transient blip.
            if agg is not None:
                _commit(agg, "socket closed", force=True)
            agg = None
            _set_status(False)

        time.sleep(delay)
