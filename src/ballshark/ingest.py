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
be lowered (see `ballshark setup --rate`) to reduce the packet pressure.

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

from .models import Envelope
from .session import MatchAggregator, MatchSummary, SessionTracker
from .store import Store

# Idle-based finalization: how often the worker wakes to run closure checks,
# and how long a match may go without ANY events before we treat it as over.
SOCKET_POLL_INTERVAL = 1.0
MATCH_IDLE_TIMEOUT = 30.0

# Reader-thread recv() timeout. Small so the reader notices a stop request
# promptly; otherwise it just blocks waiting for RL packets. This is NOT an
# idle/closure timer — that's the worker's job via the queue.get() timeout.
READER_RECV_TIMEOUT = 1.0

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


def _drain_buffer(buf: str) -> tuple[list[tuple[Envelope, dict[str, Any]]], str]:
    """Pull every complete envelope out of `buf`, returning
    (list_of_(Envelope, raw_outer_dict), unconsumed_remainder).

    Runs on the WORKER thread — JSON decoding and pydantic validation are part
    of the "slow work" we keep off the recv path. Malformed objects are skipped
    individually so one bad packet can't wedge the stream."""
    out: list[tuple[Envelope, dict[str, Any]]] = []
    while True:
        hit = _find_complete_json(buf)
        if hit is None:
            return out, buf
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
        out.append((env, obj))


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

    Finalization happens on three triggers so we never lose a completed match
    even when the user leaves early:
      1. Next MatchCreated arrives (most common - between matches in a session).
      2. MatchDestroyed arrives (clean exit through the post-game screen).
      3. Socket closes with an ended agg (user quit RL without going through
         the full post-game screen). Most common for the "leave matches a lot
         when done" workflow.
    """
    # Shared helper used by all finalization paths. Runs on the WORKER thread,
    # never on the recv path, so a slow save_match / on_match cannot back up the
    # socket.
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

    def _process(env: Envelope, agg: MatchAggregator | None) -> MatchAggregator | None:
        """Handle one parsed envelope; returns the (possibly new/cleared) agg."""
        received_at = time.time()
        try:
            event_name, raw, parsed = env.parse_payload()
        except Exception:
            return agg

        if event_name == "MatchCreated":
            if agg is not None:
                _commit(agg, "next match started")
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

        return agg

    while True:
        agg: MatchAggregator | None = None
        sock: socket.socket | None = None
        reader: threading.Thread | None = None
        stop = threading.Event()
        q: queue.Queue = queue.Queue()
        try:
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
                name="ballshark-recv", daemon=True,
            )
            reader.start()

            print("[ingest] connected to Rocket League Stats API")
            if on_status:
                on_status(True)

            buf = ""
            last_event = time.time()
            while True:
                try:
                    kind, payload = q.get(timeout=SOCKET_POLL_INTERVAL)
                except queue.Empty:
                    # Idle tick (no data this interval): run closure checks so a
                    # match that ended but never got MatchDestroyed / next
                    # MatchCreated (user sitting on a screen) still finalizes.
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
                for env, _obj in envs:
                    last_event = time.time()
                    agg = _process(env, agg)

        except ConnectionRefusedError:
            print("[ingest] connection refused; is RL running with PacketSendRate > 0?")
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
