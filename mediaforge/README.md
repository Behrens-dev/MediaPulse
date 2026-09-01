# MediaForge

An ffmpeg toolbox for your Plex library, in the same spirit (and theme) as MediaPulse.
Search your Plex library, pick a file, and queue ffmpeg jobs against it — running either
**inside the MediaForge container** or **on the Plex server itself over SSH**.

## What it can do

| Job | What happens |
|---|---|
| **Downmix audio** | `7.1 → add 5.1 + stereo`, `5.1 → add stereo`, or `normalize to only 5.1 + stereo (drop the 7.1)` — the same pan formulas as the proven `scripts/ffmpeg-downmix*.ps1` scripts. New tracks are AAC (5.1 @ 384k, stereo @ 192k); everything else is stream-copied. |
| **Embed subtitles** | Adds an `.srt/.ass/.ssa/.vtt/.smi` file as a soft, selectable track (off by default unless you say otherwise), with your choice of track name and language. No re-encode. |
| **Remove audio** | Shows every audio track (codec, channels, language) — uncheck the ones you don't want (that mono track, the 7.1, the wrong-language stereo) and the rest are copied untouched. |
| **Audio ↔ video sync** | *Auto-repair drift* (re-times audio onto the video's timestamps via `aresample=async`) or *fixed offset* in milliseconds (pure remux). |
| **Subtitle sync** | Shift every embedded subtitle track by a fixed offset so captions line up with the audio (pure remux). |

Every job writes to **the same folder as the source file** and **requires a filename
suffix** (e.g. `_encoded`) so the original can never be overwritten and names never
collide. Partial output is deleted automatically if a job fails or is canceled.

## Where jobs run

Pick in **Settings → Where ffmpeg jobs run**:

- **In the MediaForge container** — ffmpeg ships with the image. Mount your media into the
  container (see `docker-compose.yml`) and add a path mapping
  (`/mnt/pool/media → /media`) so MediaForge can find the files Plex reports.
- **On the Plex server (SSH)** — jobs execute on the server that owns the files.
  Requires an account or service account with SSH access (password or private key)
  and ffmpeg installed on the server. MediaForge verifies credentials before saving and
  has a *Test ffmpeg* button for both modes. Both **Linux and Windows** Plex servers are
  supported — pick the server OS in Settings. For Windows: install the built-in
  *OpenSSH Server* feature (Settings → System → Optional features; on newer builds use
  `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0` from an admin
  PowerShell), and make sure `ffmpeg`/`ffprobe` are on the **system** PATH (so the SSH
  session can find them). Both cmd and PowerShell default SSH shells work — MediaForge
  detects which one it's talking to. Plex on Windows reports paths like `Z:\Movies\...`,
  which the SSH session sees identically — so usually **no path mappings are needed**.

## Run it

```bash
cd mediaforge
docker compose up -d --build
```

Open `http://<host>:8191`, set your Plex URL + token in Settings, choose an execution
mode, and start queuing jobs. Job progress, logs, cancel, and retry live on the
**Jobs** page.

### TrueNAS Scale notes

- Plex reports paths as *its* container sees them (e.g. `/data/Movies/...`). Whichever
  execution mode you use, add a path mapping from that prefix to the path the mode can
  actually open (host path for SSH, mount path for container mode).
- For SSH mode, a dedicated service account that only has read/write access to the media
  datasets is the safest setup.

## Development

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8191
```

Config/state lives in `MEDIAFORGE_DATA` (default `/config`): the SQLite DB and staged
subtitle uploads.
