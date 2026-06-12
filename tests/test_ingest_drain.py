"""Regression test for the RL-freeze bug.

Background: RL streams ~30 stat packets/sec over a loopback TCP socket. If the
thread that drains that socket also does the slow finalize work (SQLite writes +
the upload to the central host), then a stall in the slow work stops the socket
being read. RL's TCP send buffer fills, RL's game thread blocks on send(), and
the GAME FREEZES (Windows: AppHangXProcB1, a cross-process hang).

The fix decouples the socket drain (a reader thread that only recv()s) from all
slow work (a worker that parses / aggregates / persists / uploads). This test
proves it: a fake server streams a match then blasts a large burst of packets
while the finalizer (`save_match`) is wedged. With the decoupled drain the burst
is fully received even though the worker is stuck; a single-threaded drain would
deadlock here (the socket stops being read, the server's send() blocks, and the
burst never completes) — so this test fails if the decoupling regresses.
"""

from __future__ import annotations

import json
import socket
import threading

from ballshark import ingest
from ballshark.session import SessionTracker

GUID = "test-guid-0001"

# Tiny socket buffers + a burst far larger than they can hold => if the worker
# stops reading, the server's send() MUST block. 2 MiB clears any platform's
# default/minimum socket buffer with margin, while staying trivially fast on
# loopback.
SMALL_BUF = 8192
BURST_BYTES = 2 * 1024 * 1024


def _envelope(event: str, data_obj: dict) -> bytes:
    """Build one wire envelope: {"Event":..,"Data":"<json-string>"} (Data is a
    JSON-encoded string, exactly like the real RL Stats API)."""
    inner = json.dumps(data_obj, separators=(",", ":"))
    outer = json.dumps({"Event": event, "Data": inner}, separators=(",", ":"))
    return outer.encode("utf-8")


def _match_stream() -> bytes:
    """A minimal but VALID match: MatchCreated -> UpdateState -> MatchEnded ->
    MatchDestroyed. MatchDestroyed triggers finalize + save_match on the worker.
    UpdateState must carry Game (teams/scores) and at least one player so
    MatchAggregator.finalize() actually returns a summary."""
    update = {
        "MatchGuid": GUID,
        "Players": [
            {"Name": "Me", "PrimaryId": "Steam|123|0", "TeamNum": 0,
             "Score": 100, "Goals": 1, "Speed": 1500, "Boost": 50},
        ],
        "Game": {
            "Teams": [
                {"Name": "Blue", "TeamNum": 0, "Score": 1},
                {"Name": "Orange", "TeamNum": 1, "Score": 0},
            ],
            "Arena": "TestArena",
        },
    }
    return (
        _envelope("MatchCreated", {"MatchGuid": GUID})
        + _envelope("UpdateState", update)
        + _envelope("MatchEnded", {"MatchGuid": GUID, "WinnerTeamNum": 0})
        + _envelope("MatchDestroyed", {"MatchGuid": GUID})
    )


def _burst() -> bytes:
    """A large stream of cheap, valid packets sent right after MatchDestroyed.
    MatchPaused is a no-op for the aggregator and isn't adopted as a new match,
    so processing it later (after release) is cheap and side-effect-free."""
    pad = "x" * 1400
    one = _envelope("MatchPaused", {"MatchGuid": GUID, "pad": pad})
    return one * ((BURST_BYTES // len(one)) + 1)


class SlowFinalizeStore:
    """Stand-in Store whose save_match() blocks — simulating a slow disk or a
    slow/hung finalize. save_raw_event is instant. The duck-typed surface is
    exactly what run_live touches: save_raw_event() and save_match()."""

    def __init__(self) -> None:
        self.entered = threading.Event()   # set the instant save_match begins
        self.release = threading.Event()   # test sets this to let it return
        self.saved: list = []
        self.raw_count = 0

    def save_raw_event(self, *args, **kwargs) -> None:
        self.raw_count += 1

    def save_match(self, summary) -> None:
        self.entered.set()
        # Block here while the fake server keeps streaming. The reader thread
        # must keep draining the socket the entire time.
        self.release.wait(timeout=30)
        self.saved.append(summary)


def _serve_once(listener: socket.socket, all_sent: threading.Event,
                done: threading.Event) -> None:
    """Accept one client, send the match + the burst, signal when the burst's
    send() fully returns, then hold the connection open until told to close."""
    try:
        conn, _ = listener.accept()
    except OSError:
        return
    try:
        try:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SMALL_BUF)
        except OSError:
            pass
        conn.sendall(_match_stream())
        # sendall() returns only once every byte is handed to the kernel. With a
        # tiny send buffer and a stalled reader it would block here forever.
        conn.sendall(_burst())
        all_sent.set()
        done.wait(timeout=30)
    finally:
        try:
            conn.close()
        except OSError:
            pass


def test_drain_keeps_reading_while_finalize_is_slow():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    store = SlowFinalizeStore()
    session = SessionTracker(self_name="Me")
    uploads: list = []

    all_sent = threading.Event()
    server_done = threading.Event()

    server = threading.Thread(
        target=_serve_once, args=(listener, all_sent, server_done),
        name="fake-rl-server", daemon=True,
    )
    server.start()

    def run() -> None:
        ingest.run_live(
            store, session,
            host="127.0.0.1", port=port,
            on_match=uploads.append,   # the "upload to welsh-macmini", runs post-save
            # Force a tiny client receive buffer so the burst genuinely exceeds
            # the kernel's headroom — without the reader thread the drain would
            # stall and the server's send() would block.
            so_rcvbuf=SMALL_BUF,
            reconnect_delay=60.0,      # don't busy-reconnect after we close
        )

    worker = threading.Thread(target=run, name="ingest-under-test", daemon=True)
    worker.start()

    try:
        # 1) The worker reaches the slow finalize and wedges there.
        assert store.entered.wait(timeout=10), "worker never reached save_match()"

        # 2) THE ASSERTION: even though the worker is stuck in save_match, the
        # reader thread keeps draining, so the server streams the whole burst.
        # A single-threaded drain would deadlock and this would time out.
        assert all_sent.wait(timeout=10), (
            "socket drain stalled while the finalizer was slow — the recv loop "
            "is back-pressuring RL (the freeze bug)"
        )

        # 3) Release the finalizer; the pipeline finishes end-to-end.
        store.release.set()
        assert store.entered.is_set()
        # save_match completes and the on_match "upload" fires.
        deadline_ok = _wait_for(lambda: len(store.saved) == 1 and len(uploads) == 1,
                                timeout=10)
        assert deadline_ok, "pipeline did not finalize the match after release"
        assert store.saved[0].team0_score == 1
        assert uploads[0].winner_team_num == 0
    finally:
        store.release.set()
        server_done.set()
        try:
            listener.close()
        except OSError:
            pass


def _wait_for(pred, timeout: float) -> bool:
    """Poll pred() until true or timeout. Avoids importing time.sleep loops in
    the test body."""
    end = threading.Event()
    t = threading.Timer(timeout, end.set)
    t.daemon = True
    t.start()
    try:
        while not end.is_set():
            if pred():
                return True
            end.wait(0.02)
        return pred()
    finally:
        t.cancel()
