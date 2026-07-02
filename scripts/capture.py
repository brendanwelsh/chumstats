"""
Standalone capture script for the Rocket League Stats API.

Connects to 127.0.0.1:49123 (the local TCP socket RL opens when
DefaultStatsAPI.ini has PacketSendRate > 0), writes every byte to a
timestamped .bin file AND a parsed .jsonl file, and prints event names
to stdout so you can see it's working.

Usage:
    1. Make sure Rocket League is FULLY CLOSED.
    2. Launch Rocket League and load into the main menu.
    3. In a separate terminal, run:  python scripts/capture.py
       (If it prints "connection refused", RL hasn't opened the socket yet
        - the socket only opens once the game has loaded.)
    4. Play 1-2 matches. Casual / Ranked / Private all work.
    5. Press Ctrl+C to stop. Send the .bin and .jsonl files.

Requires only the Python standard library.
"""

from __future__ import annotations

import json
import pathlib
import socket
import sys
import time

HOST = "127.0.0.1"
PORT = 49123
RECV_BYTES = 65536

OUT_DIR = pathlib.Path(__file__).resolve().parents[1] / "captures"
OUT_DIR.mkdir(exist_ok=True)

stamp = time.strftime("%Y%m%d_%H%M%S")
raw_path = OUT_DIR / f"rl_{stamp}.bin"
jsonl_path = OUT_DIR / f"rl_{stamp}.jsonl"


def connect_with_retry(host: str, port: int, attempts: int = 30, delay: float = 1.0) -> socket.socket:
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            s = socket.create_connection((host, port), timeout=5)
            s.settimeout(None)
            return s
        except (ConnectionRefusedError, OSError) as e:
            last_err = e
            print(f"  [{i + 1}/{attempts}] waiting for RL socket on {host}:{port}...", flush=True)
            time.sleep(delay)
    raise RuntimeError(f"could not connect to {host}:{port}: {last_err}")


def main() -> int:
    print(f"connecting to {HOST}:{PORT} ...", flush=True)
    sock = connect_with_retry(HOST, PORT)
    print("connected.")
    print(f"  raw   -> {raw_path}")
    print(f"  jsonl -> {jsonl_path}")
    print("press Ctrl+C to stop.\n", flush=True)

    decoder = json.JSONDecoder()
    text_buf = ""
    event_count = 0
    type_counts: dict[str, int] = {}

    try:
        with raw_path.open("wb") as raw_f, jsonl_path.open("w", encoding="utf-8") as jsonl_f:
            while True:
                chunk = sock.recv(RECV_BYTES)
                if not chunk:
                    print("\nsocket closed by RL.", flush=True)
                    break

                raw_f.write(chunk)
                raw_f.flush()

                try:
                    text_buf += chunk.decode("utf-8")
                except UnicodeDecodeError:
                    text_buf += chunk.decode("utf-8", errors="replace")

                idx = 0
                while idx < len(text_buf):
                    remainder = text_buf[idx:].lstrip()
                    if not remainder:
                        idx = len(text_buf)
                        break
                    leading_ws = len(text_buf) - idx - len(remainder)
                    try:
                        obj, end = decoder.raw_decode(remainder)
                    except json.JSONDecodeError:
                        break

                    idx += leading_ws + end

                    jsonl_f.write(json.dumps(obj, separators=(",", ":")) + "\n")
                    jsonl_f.flush()

                    name = (
                        obj.get("event")
                        or obj.get("Event")
                        or obj.get("name")
                        or (next(iter(obj.keys())) if isinstance(obj, dict) and obj else "unknown")
                    )
                    type_counts[name] = type_counts.get(name, 0) + 1
                    event_count += 1

                    if name != "UpdateState" or type_counts[name] % 30 == 1:
                        print(f"  #{event_count:<6} {name}", flush=True)

                text_buf = text_buf[idx:]
    except KeyboardInterrupt:
        print("\nstopped by user.", flush=True)
    finally:
        try:
            sock.close()
        except Exception:
            pass

    print(f"\ncaptured {event_count} events.")
    if type_counts:
        print("event type counts:")
        for k, v in sorted(type_counts.items(), key=lambda kv: -kv[1]):
            print(f"  {v:>6}  {k}")
    print("\nfiles written:")
    print(f"  {raw_path}")
    print(f"  {jsonl_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
