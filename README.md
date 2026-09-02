# MediaPulse & MediaForge

Two free, self-hosted companion apps for a home Plex server. They share the same dark
"control room" UI theme and the same lightweight stack (FastAPI + SQLite + vanilla JS),
but they are **two separate applications**, shipped as **two separate Docker containers**:

| App | What it does | Port | Image |
| --- | --- | --- | --- |
| **[MediaPulse](#mediapulse)** (this folder) | Tautulli-style **monitoring & notifications**: live activity, watch history, per-file audio-track analysis, email newsletters/alerts | `8181` | `ghcr.io/behrens-dev/mediapulse:latest` |
| **[MediaForge](mediaforge/README.md)** (`mediaforge/`) | **ffmpeg toolbox**: downmix 7.1 → 5.1/stereo, embed subtitles, remove audio tracks, repair audio/subtitle sync — run against files straight out of your Plex library | `8191` | `ghcr.io/behrens-dev/mediaforge:latest` |

They work great together — MediaPulse's audio-track analysis *finds* the files that need
work (e.g. "movies with 7.1 but no stereo track"), and MediaForge *fixes* them — but each
runs fine without the other. The `scripts/` folder holds the original interactive
PowerShell/ffmpeg scripts that MediaForge's jobs were built from.

> Every push to `main` builds and publishes **both** images to GHCR via GitHub Actions.

## Who this is for (supported setups)

These apps were built around one specific home-lab setup and are tested against it:

- **The apps run as Docker containers**, designed for **TrueNAS SCALE 24.10+** ("Electric
  Eel" and newer, which runs Docker natively) — though any Docker host works.
- **The Plex server** can be:
  - a container on the same TrueNAS/Docker host, **or**
  - a **separate Windows machine** (Plex for Windows) — the tested reference setup.

What that means per app:

- **MediaPulse** only talks to Plex over HTTP, so it works with *any* Plex server it can
  reach on the network — Windows, Linux, NAS, wherever.
- **MediaForge** must reach the actual media *files*, so it supports exactly three layouts:
  1. media on storage the MediaForge container can mount → jobs run **in the container**;
  2. Plex on a **Linux** server reachable by SSH → jobs run **on that server**;
  3. Plex on a **Windows** server reachable by SSH (OpenSSH) → jobs run **on that server**.

  If your media lives somewhere MediaForge can't mount **and** the Plex machine can't run
  SSH, MediaForge can't help you yet.

Other assumptions baked in: LAN use (neither app has a login page — put them behind your
firewall/VPN/reverse proxy), a single Plex server, and SMTP email for notifications.

---

# MediaPulse

A 100% free self-hosted Plex monitoring and notification app — a Tautulli-style dashboard with the
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
    *"anything with only AC3"* — perfect for planning ffmpeg re-encodes (then queue them
    in [MediaForge](mediaforge/README.md)).
  - **Multi-level sorting** (Airtable-style): stack sort rules like *file size high → low,
    then title A → Z* across title, size, added date, bitrate, resolution, container,
    video codec, audio channels, year, and duration.

**✉️ Notifications** (SMTP email, works with Gmail app passwords, etc.)
- **Recently added** — digest with poster art. Movies show poster + summary; TV shows show the
  series poster + series summary with a bulleted list of exactly which episodes were added
  (S08E07 · Baja, …); music groups new tracks by album. Send on demand or on an automatic
  monthly schedule, with an optional personal note.
- **Recommendations** — search your own libraries, pick titles (poster art included), add a
  personal note per title, and send a styled "My recommended watch list" email to family.
- **Maintenance window** — start/end date-time, optional reason, optional header image.
- **Outage alerts** — a "we know it's down" notice with optional message, ETA, and image.
  Can also send itself automatically once the server has been unreachable for a configurable
  delay (1 min – 12 h).
- **Server down/up alerts** — admin-facing email the moment the server goes down and again
  when it recovers (separate recipient list supported).
- Sent log of every email with success/failure.

**👥 Users**
- Everyone with access to your server (owner, shared users, Plex Home/managed users) with
  their email, library access, play counts, and last-watched — plus an alias field so you can
  note who's actually who.

**🔔 Update notice** (both apps)
- Every Sunday at midnight the app compares the commit it was built from against this
  repo's `main` branch; when a newer image exists, an "⬆ Update available" badge appears
  in the sidebar. Purely informational — updating is still a manual stop/start of the app
  (with `pull_policy: always`). A Settings toggle disables the check entirely, and a
  "Check now" button runs it on demand.

**💾 Backup & restore**
- One-click JSON backup of all settings (Plex connection, email config, schedules, alert
  options), the full watch history, user aliases, and the sent-email log. Restore it into a
  fresh container to migrate to a new system with everything intact — only the library
  media-info cache is excluded (it rebuilds with one "Sync media info" click). The backup
  file contains your Plex token and SMTP password, so store it somewhere safe.

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

Every push to `main` automatically builds and publishes the image to
`ghcr.io/behrens-dev/mediapulse:latest` via GitHub Actions, so the NAS just pulls it.

### Option A — Install via YAML (recommended)

1. Create the dataset/folder `/mnt/<your-pool>/apps/mediapulse` first (Datasets page), so the
   SQLite database survives app updates.

2. In the TrueNAS UI: **Apps → Discover Apps → ⋮ → Install via YAML**, and paste:

   ```yaml
   services:
     mediapulse:
       image: ghcr.io/behrens-dev/mediapulse:latest
       pull_policy: always
       restart: unless-stopped
       ports:
         - "8181:8181"
       volumes:
         - /mnt/<your-pool>/apps/mediapulse:/config
       environment:
         TZ: America/New_York
   ```

3. Open `http://<truenas-ip>:8181` and configure as in Quick start.

To update later: push to `main` (or wait for the Actions build), then in TrueNAS stop and
start the app — `pull_policy: always` makes it grab the newest `latest` image on start.

### Option B — Custom App form UI

**Apps → Discover Apps → Custom App**, then:
- Image repository: `ghcr.io/behrens-dev/mediapulse`, tag `latest`
- Port: container `8181` → host `8181`
- Storage: host path `/mnt/<pool>/apps/mediapulse` → mount path `/config`
- Environment (optional): `TZ`, `PLEX_URL`, `PLEX_TOKEN`

For installing **MediaForge** on TrueNAS, see [mediaforge/README.md](mediaforge/README.md) —
same pattern, different port/volume, plus a one-time SSH setup if your Plex server is a
separate machine.

## Configuration reference

Everything is configurable in the UI and stored in `/config/mediapulse.db`. Environment
variables (all optional):

| Variable | Default | Purpose |
| --- | --- | --- |
| `PLEX_URL` | — | Pre-seed the Plex server URL (UI value wins once saved) |
| `PLEX_TOKEN` | — | Pre-seed the Plex token |
| `MEDIAPULSE_PORT` | `8181` | Listen port (also change the Docker port mapping) |
| `MEDIAPULSE_POLL_SECONDS` | `15` | Live-session poll interval |
| `MEDIAPULSE_HEALTH_SECONDS` | `60` | Server health check interval |
| `MEDIAPULSE_HEALTH_THRESHOLD` | `3` | Consecutive failures before a "down" email |
| `TZ` | UTC | Container timezone (affects newsletter schedule + email timestamps) |

## Notes

- **Renamed from PlexPulse** (and MediaForge from PlexCode) to steer clear of Plex's
  trademark. Existing installs keep working: the old `plexpulse.db` database file and
  `PLEXPULSE_*` environment variables are still recognized. After renaming the GitHub repo,
  point the TrueNAS app at the new image name (`ghcr.io/behrens-dev/mediapulse:latest`).
- **Lifetime plays** shown per library combine Plex's own `viewCount` (captured during media
  sync) with plays MediaPulse has recorded itself. Tautulli-style history starts accumulating
  from the moment MediaPulse is running — it cannot see plays from before it was installed.
- Watch history is recorded by polling `/status/sessions`; a play shorter than the poll
  interval (15 s) can be missed.
- The web UI has no login — run it on your LAN / behind your existing reverse proxy or VPN.
  Your Plex token is stored in the SQLite DB inside the `/config` volume.
- Music libraries sync per-track; very large music libraries take a while on first sync.

## Development

```bash
pip install -r requirements.txt
set MEDIAPULSE_DATA=./data      # Windows (use export on Linux/macOS)
python -m uvicorn app.main:app --reload --port 8181
```

MediaForge development works the same way from the `mediaforge/` folder — see its README.
