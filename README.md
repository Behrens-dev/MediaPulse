# PlexPulse

A self-hosted Plex monitoring and notification app — a Tautulli-style dashboard with the
extra features Tautulli doesn't have. Runs as a single lightweight Docker container,
designed for TrueNAS SCALE.

## Features

**📡 Activity**
- Live view of every current stream: user, title, player, LAN/WAN, bitrate,
  play/pause/buffering state, direct play vs. transcode (with transcode speed), and progress.
- Full watch history recorded automatically (who watched what, when, on which device,
  how far they got, whether it transcoded). Filter by user, media type, title, and date range.

**📚 Libraries**
- Every Plex library with counts (movies / shows / seasons / episodes / artists / albums / tracks)
  and lifetime play totals.
- **Deep media info with per-file audio tracks** — the killer feature. A "Sync media info" scan
  caches container, video codec, resolution, file size, bitrate **and every audio track in the
  file** (codec, channels, language). Then you can:
  - See an "Audio health" report: how many items have **no stereo track**, how many have
    multiple audio tracks, codec/channel-layout breakdowns.
  - Filter the library for things like *"MKVs with a TrueHD 7.1 track but no 2.0 track"* or
    *"anything with only AC3"* — perfect for planning ffmpeg re-encodes.

**✉️ Notifications** (SMTP email, works with Gmail app passwords, etc.)
- **Newsletter** — "what's new on the server" recently-added digest. Send on demand or on an
  automatic monthly schedule (pick day of month + hour), with an optional personal note.
- **Recommendations** — search your own libraries, pick titles, add a personal note per title
  ("this one's a must-watch!"), and send a styled "My recommended watch list" email to family.
- **Maintenance window** — pick a start and end date/time plus an optional reason and send a
  clean "server will be down" notice.
- **Server down/up alerts** — PlexPulse pings your server every minute and emails you (or a
  separate admin list) when it goes down and again when it recovers.
- Sent log of every email with success/failure.

## Quick start (any machine with Docker)

```bash
docker compose up -d --build
```

Open `http://<host>:8181`, go to **Settings**, enter your Plex URL
(e.g. `http://192.168.1.50:32400`) and your `X-Plex-Token`, click **Test connection**, then **Save**.

> Finding your token: play any item in Plex Web → **⋮ → Get Info → View XML** → copy the
> `X-Plex-Token=` value from the URL.

Then set up email under **Notifications → Email settings** (for Gmail use
`smtp.gmail.com`, port 587, STARTTLS, and an [app password](https://myaccount.google.com/apppasswords)).

Finally, open **Libraries**, click into each library, and hit **Sync media info** once to build
the audio-track database (it re-uses the cache afterwards; re-run it whenever you add lots of media).

## Installing on TrueNAS SCALE

TrueNAS SCALE 24.10+ ("Electric Eel" and newer) runs Docker natively.

### Option A — Custom App (recommended)

1. Build and push the image somewhere your NAS can pull from, e.g. GitHub Container Registry:
   ```bash
   docker build -t ghcr.io/<your-github-user>/plexpulse:latest .
   docker push ghcr.io/<your-github-user>/plexpulse:latest
   ```
   (Or build directly on the NAS via SSH: clone this folder and `docker build -t plexpulse .`)

2. In the TrueNAS UI: **Apps → Discover Apps → ⋮ → Install via YAML**, and paste:

   ```yaml
   services:
     plexpulse:
       image: ghcr.io/<your-github-user>/plexpulse:latest   # or plexpulse:latest if built on the NAS
       restart: unless-stopped
       ports:
         - "8181:8181"
       volumes:
         - /mnt/<your-pool>/apps/plexpulse:/config
       environment:
         TZ: America/New_York
   ```

3. Create the dataset/folder `/mnt/<your-pool>/apps/plexpulse` first (Datasets page), so the
   SQLite database survives app updates.

4. Open `http://<truenas-ip>:8181` and configure as in Quick start.

### Option B — Custom App form UI

**Apps → Discover Apps → Custom App**, then:
- Image repository: your pushed image (e.g. `ghcr.io/<user>/plexpulse`), tag `latest`
- Port: container `8181` → host `8181`
- Storage: host path `/mnt/<pool>/apps/plexpulse` → mount path `/config`
- Environment (optional): `TZ`, `PLEX_URL`, `PLEX_TOKEN`

## Configuration reference

Everything is configurable in the UI and stored in `/config/plexpulse.db`. Environment
variables (all optional):

| Variable | Default | Purpose |
| --- | --- | --- |
| `PLEX_URL` | — | Pre-seed the Plex server URL (UI value wins once saved) |
| `PLEX_TOKEN` | — | Pre-seed the Plex token |
| `PLEXPULSE_PORT` | `8181` | Listen port (also change the Docker port mapping) |
| `PLEXPULSE_POLL_SECONDS` | `15` | Live-session poll interval |
| `PLEXPULSE_HEALTH_SECONDS` | `60` | Server health check interval |
| `PLEXPULSE_HEALTH_THRESHOLD` | `3` | Consecutive failures before a "down" email |
| `TZ` | UTC | Container timezone (affects newsletter schedule + email timestamps) |

## Notes

- **Lifetime plays** shown per library combine Plex's own `viewCount` (captured during media
  sync) with plays PlexPulse has recorded itself. Tautulli-style history starts accumulating
  from the moment PlexPulse is running — it cannot see plays from before it was installed.
- Watch history is recorded by polling `/status/sessions`; a play shorter than the poll
  interval (15 s) can be missed.
- The web UI has no login — run it on your LAN / behind your existing reverse proxy or VPN.
  Your Plex token is stored in the SQLite DB inside the `/config` volume.
- Music libraries sync per-track; very large music libraries take a while on first sync.

## Development

```bash
pip install -r requirements.txt
set PLEXPULSE_DATA=./data      # Windows (use export on Linux/macOS)
python -m uvicorn app.main:app --reload --port 8181
```
