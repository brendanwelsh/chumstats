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

from chumstats import ingest
from chumstats.session import SessionTracker

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


def _forfeit_stream() -> bytes:
    """A forfeited / left-early match: like _match_stream but the client never
    receives MatchEnded — the player left before the game broadcast its result,
    so the stream ends on MatchDestroyed ALONE. The fixed ingest must salvage it
    by inferring the winner from the final 1-0 score; pre-fix it was discarded.
    UpdateState carries a goal so the match has meaningful play (not an abort)."""
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
        + _envelope("MatchDestroyed", {"MatchGuid": GUID})  # NB: no MatchEnded
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


def test_forfeit_without_matchended_is_salvaged_on_destroy(tmp_path):
    """Regression for the dropped-forfeit bug: a match that ends on MatchDestroyed
    with NO preceding MatchEnded (user left / forfeited) must still be SAVED, with
    the winner inferred from the final scores. Before the fix the MatchDestroyed
    path called finalize(force=False), which returned None and silently discarded
    every abandoned game."""
    from chumstats.store import Store

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    store = Store(str(tmp_path / "forfeit.db"))
    session = SessionTracker(self_name="Me")
    matched: list = []
    done = threading.Event()

    def serve() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        conn.sendall(_forfeit_stream())
        done.wait(timeout=15)
        try:
            conn.close()
        except OSError:
            pass

    srv = threading.Thread(target=serve, name="fake-rl", daemon=True)
    srv.start()

    def on_match(s) -> None:
        matched.append(s)
        done.set()

    worker = threading.Thread(
        target=lambda: ingest.run_live(
            store, session, host="127.0.0.1", port=port,
            on_match=on_match, reconnect_delay=60.0,
        ),
        name="ingest-under-test", daemon=True,
    )
    worker.start()
    try:
        assert _wait_for(lambda: len(matched) == 1, timeout=15), (
            "forfeit (MatchDestroyed without MatchEnded) was not saved"
        )
        s = matched[0]
        assert s.winner_team_num == 0          # 1 > 0 -> team 0 inferred
        assert (s.team0_score, s.team1_score) == (1, 0)
        # Sanity: the stream genuinely carried no MatchEnded.
        with store._conn() as c:
            ended = c.execute(
                "SELECT COUNT(*) FROM raw_events WHERE event='MatchEnded'"
            ).fetchone()[0]
            n_matches = c.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        assert ended == 0
        assert n_matches == 1
    finally:
        done.set()
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


# --- _drain_buffer parser (the brace-scanner + inline payload parse) ---------

from chumstats.ingest import _drain_buffer
from chumstats.models import MatchEnded, UpdateState


def test_drain_buffer_parses_concatenated_envelopes():
    """Two back-to-back envelopes (no delimiter, exactly like the wire) drain to
    (event_name, raw_dict, typed_payload) triples with the right models."""
    buf = (_match_stream()).decode("utf-8")  # 4 concatenated envelopes
    out, rem = _drain_buffer(buf)
    assert rem == ""  # everything consumed
    names = [ev for ev, _raw, _p in out]
    assert names == ["MatchCreated", "UpdateState", "MatchEnded", "MatchDestroyed"]
    # Inner payload is decoded to a dict, and modelled events get a typed object.
    by_name = {ev: (raw, p) for ev, raw, p in out}
    assert by_name["UpdateState"][0]["MatchGuid"] == GUID
    assert isinstance(by_name["UpdateState"][1], UpdateState)
    assert isinstance(by_name["MatchEnded"][1], MatchEnded)
    assert by_name["MatchEnded"][1].winner_team_num == 0


def test_drain_buffer_keeps_partial_trailing_object():
    """A trailing, not-yet-complete object stays in the remainder for the next
    recv chunk; complete ones ahead of it are still drained."""
    full = _envelope("MatchCreated", {"MatchGuid": GUID}).decode("utf-8")
    partial = full[:-5]  # chop the closing braces off the second object
    out, rem = _drain_buffer(full + partial)
    assert [ev for ev, _r, _p in out] == ["MatchCreated"]
    assert rem == partial  # exact bytes preserved for reassembly


def test_drain_buffer_skips_malformed_without_wedging():
    """A junk byte between objects and an envelope whose Data isn't valid JSON
    are both skipped individually — the good events on either side survive."""
    good1 = _envelope("MatchCreated", {"MatchGuid": GUID}).decode("utf-8")
    bad_inner = '{"Event":"UpdateState","Data":"{not json}"}'
    good2 = _envelope("MatchEnded", {"MatchGuid": GUID, "WinnerTeamNum": 1}).decode("utf-8")
    out, rem = _drain_buffer(good1 + "\n\t" + bad_inner + good2)
    assert [ev for ev, _r, _p in out] == ["MatchCreated", "MatchEnded"]


def test_live_ingest_persists_every_raw_event_batched(tmp_path):
    """End-to-end against a REAL Store: the batched raw_events path must persist
    every envelope (in order) and save the match — i.e. batching changes timing,
    not content."""
    from chumstats.store import Store

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    store = Store(str(tmp_path / "live.db"))
    session = SessionTracker(self_name="Me")
    matched: list = []
    done = threading.Event()

    def serve() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        conn.sendall(_match_stream())
        done.wait(timeout=15)
        try:
            conn.close()
        except OSError:
            pass

    srv = threading.Thread(target=serve, name="fake-rl", daemon=True)
    srv.start()

    def on_match(s) -> None:
        matched.append(s)
        done.set()

    worker = threading.Thread(
        target=lambda: ingest.run_live(
            store, session, host="127.0.0.1", port=port,
            on_match=on_match, reconnect_delay=60.0,
        ),
        name="ingest-under-test", daemon=True,
    )
    worker.start()
    try:
        assert _wait_for(lambda: len(matched) == 1, timeout=15), "match never finalized"
        # _commit flushes raw_events before save_match/on_match, so by the time
        # on_match fired all four envelopes must be on disk, in arrival order.
        with store._conn() as c:
            rows = c.execute(
                "SELECT event FROM raw_events ORDER BY received_at ASC, id ASC"
            ).fetchall()
            events = [r[0] for r in rows]
            n_matches = c.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        assert events == ["MatchCreated", "UpdateState", "MatchEnded", "MatchDestroyed"]
        assert n_matches == 1
        assert matched[0].team0_score == 1
    finally:
        done.set()
        try:
            listener.close()
        except OSError:
            pass
