"""Mock RL Stats API server: replays a recorded match's raw_events back as the
concatenated-JSON envelope stream Rocket League emits, so the full live-ingest
pipeline can be exercised end-to-end without RL running.

usage: python scripts/mock_emitter.py <chumstats.db> <match_id_prefix> <port>

Point an ingest instance at it with a throwaway DB (NOT 49123 — the real
tracker connects there and would swallow the replay into the live DB):

    python -m chumstats.cli --db test.db run --port 49555 \
        --no-bot --no-sync --no-server

The event window is sliced by time, not match_id, because lifecycle envelopes
(MatchCreated/MatchDestroyed) are archived before/after the guid is known and
carry a NULL match_id.
"""
import json
import socket
import sqlite3
import sys
import time

db, match_prefix, port = sys.argv[1], sys.argv[2], int(sys.argv[3])

con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
full = con.execute("SELECT DISTINCT match_id FROM raw_events WHERE match_id LIKE ?",
                   (match_prefix + "%",)).fetchone()[0]
lo, hi = con.execute("SELECT MIN(received_at), MAX(received_at) FROM raw_events "
                     "WHERE match_id=?", (full,)).fetchone()
rows = con.execute("SELECT event, payload FROM raw_events WHERE received_at "
                   "BETWEEN ? AND ? ORDER BY id", (lo - 10, hi + 30)).fetchall()
con.close()

stream = b"".join(json.dumps({"Event": e, "Data": p}).encode("utf-8")
                  for e, p in rows)
print(f"[mock] match {full}: {len(rows)} envelopes, {len(stream)} bytes; "
      f"listening on 127.0.0.1:{port}", flush=True)

srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", port))
srv.listen(1)
conn, addr = srv.accept()
print(f"[mock] client connected from {addr}", flush=True)

CHUNK = 32768
for i in range(0, len(stream), CHUNK):
    conn.sendall(stream[i:i + CHUNK])
    time.sleep(0.02)

print("[mock] stream sent; holding socket 3s then closing", flush=True)
time.sleep(3)
conn.close()
srv.close()
print("[mock] done", flush=True)
