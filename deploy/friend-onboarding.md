# Friend onboarding — joining the chumstats network

## What the host does

1. On the central server, provision them:

   ```bash
   chumstats --db ~/chumstats/data/central.db admin create-user \
     --primary-id 'Steam|76561...|0' \
     --name '@TheirIngameName'
   ```

   This prints a 64-char API key.

2. Send the friend two things over Discord DM:
   - **Server URL** (e.g. `https://stats.your-domain.com`, or `http://<server-host>:5050` on a VPN/LAN)
   - **API key** (the 64-char string)

3. Send them the **Chumstats.zip** (built via `deploy/windows/build.ps1`).

## What the friend does

1. **Unzip Chumstats.zip** anywhere (Documents / Desktop).
2. **Double-click `Chumstats.exe`**.
3. **Wizard pops up.** Walk through 4 short steps:
   - Welcome
   - Paste the server URL + API key, click **Test Connection** to verify
   - Confirm their in-game name (auto-detected from the API key)
   - Click **Enable Stats API** (writes `PacketSendRate=30` to RL's config)
4. **Done.** Tray icon appears. Restart Rocket League if it was running.
5. Play normally — every finalized match auto-uploads to the central server.

The friend never touches:
- A `.env` file
- A command line
- Python (the .exe bundles everything)

## Updating later

Right-click tray icon → **Settings…** to change server URL, API key, or name.

## Backfill existing local matches (optional)

If the friend ran an earlier dev install of chumstats locally, their previous
matches live in their old DB. To push them into the central server:

```powershell
# From wherever their old data\chumstats.db is
.\Chumstats.exe --cli --db data\chumstats.db push-history `
  --primary-id Steam|76561...|0 --dry-run
# review count, re-run without --dry-run
```

(Bundle exposes the CLI via the `--cli` sentinel — same binary, different mode.)

## How identity works

- The API key the host provisioned is tied to ONE `primary_id`. The friend's
  uploads are accepted only when their `my_row.primary_id` matches.
- If they spoof another friend's primary_id, the server returns 403.
- Opponent player rows from their uploads use first-writer-wins, so they
  can't overwrite anyone else's authoritative stats.
- If they ever change their in-game NAME, no action needed — the name
  on the central server updates automatically on the next upload. Their
  historical matches stay attached via `primary_id`.

## Privacy

What gets uploaded per match:
- Match metadata (arena, score, duration, winner, online/offline)
- Their full player row (goals, saves, demos, ticks, boost, speed)
- Opponent player rows (same fields)

What does NOT get uploaded:
- Replay files
- Raw 30 Hz position/state ticks (those stay in their local DB)
- Anything outside a finalized RL match
