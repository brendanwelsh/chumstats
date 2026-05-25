import sqlite3, json
from collections import Counter
c = sqlite3.connect("data/carball.db")
ctr = Counter()
for (p,) in c.execute("SELECT payload FROM raw_events WHERE event = 'StatfeedEvent'"):
    try:
        inner = json.loads(p)
    except Exception:
        continue
    ctr[f'{inner.get("EventName","")} / {inner.get("Type","")}'] += 1
for k, v in sorted(ctr.items(), key=lambda x: -x[1]):
    print(f"  {k:<40} {v}")
