"""Listen on /ws for 8s to see what events are flowing from the RL ingest."""
import asyncio, json
import websockets

async def main():
    try:
        async with websockets.connect("ws://127.0.0.1:5050/ws") as ws:
            print("connected, listening for 8s...")
            seen = {}
            end = asyncio.get_event_loop().time() + 8
            while asyncio.get_event_loop().time() < end:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    d = json.loads(msg)
                    t = d.get("type", "?")
                    seen[t] = seen.get(t, 0) + 1
                except asyncio.TimeoutError:
                    continue
            print("event counts over 8s:", seen)
            if not seen:
                print("(no events at all -- RL not sending or no active match)")
    except Exception as e:
        print(f"ws error: {type(e).__name__}: {e}")

asyncio.run(main())
