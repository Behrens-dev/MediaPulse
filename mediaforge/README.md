# MediaForge

An ffmpeg toolbox for your Plex library, and the sibling app of
[MediaPulse](../README.md) (same theme, separate container). Search your Plex library,
pick a file, and queue ffmpeg jobs against it — running either **inside the MediaForge
container** or **on the Plex server itself over SSH** (Linux *or* Windows).

> **Scope:** MediaForge is designed around a specific home-lab case — the app runs as a
> Docker container (built for TrueNAS SCALE 24.10+, any Docker host works), and the media
> files live either on storage the container can mount, or on a Plex server (Linux or
> Windows) that MediaForge can reach over SSH. The tested reference setup is MediaForge
> on TrueNAS with Plex on a separate Windows 11 machine. If neither of those layouts fits
> your setup, MediaForge can't reach your files.

## What it can do

| Job | What happens |
|---|---|
| **Downmix audio** | `7.1 → add 5.1 + stereo`, `5.1 → add stereo`, `normalize to only 5.1 + stereo (drop the 7.1)`, or `add a mono track` — the same pan formulas as the proven `scripts/ffmpeg-downmix*.ps1` scripts. New tracks are AAC (5.1 @ 384k, stereo @ 192k, mono @ 96k) and named ("5.1 Surround (AAC)", "Stereo (AAC)", …); everything else is stream-copied. |
| **Re-encode / convert** | The combined job — mix and match in one pass: re-encode video (H.264 / H.265 / AV1, quality + speed presets), change container (MKV ↔ MP4, with compatibility checks), convert any audio track's codec (AAC, AC3/Dolby Digital, E-AC3, Opus, MP3, FLAC), remove tracks, and add downmixed 5.1/stereo/mono tracks. Anything left on "keep" is stream-copied, so a single small change never re-encodes the whole file. |
| **Embed subtitles** | Adds an `.srt/.ass/.ssa/.vtt/.smi` file as a soft, selectable track (off by default unless you say otherwise), with your choice of track name and language. Upload the file through the browser or point at a path on the server. No re-encode. |
| **Remove audio** | Shows every audio track (codec, channels, language) — uncheck the ones you don't want (that mono track, the 7.1, the wrong-language stereo) and the rest are copied untouched. |
| **Audio ↔ video sync** | *Auto-repair drift* (re-times audio onto the video's timestamps via `aresample=async`) or *fixed offset* in milliseconds (pure remux). |
| **Subtitle sync** | Shift every embedded subtitle track by a fixed offset so captions line up with the audio (pure remux). |

Every job writes to **the same folder as the source file** and **requires a filename
suffix** (e.g. `_encoded`) so the original can never be overwritten and names never
collide. Partial output is deleted automatically if a job fails or is canceled. The
**Jobs** page shows live progress, full ffmpeg logs, cancel, and retry.

**Run now or schedule:** each job can run immediately (next in queue) or be *held until*
a chosen date & time — stack up several held jobs and they all release at that moment
(e.g. overnight), still running one at a time in the order queued. Scheduled jobs can be
started early or canceled from the Jobs page.

**Update notice:** every Sunday at midnight the app checks whether a newer image has been
published (sidebar badge only — updating stays a manual stop/start). Toggle it off in
Settings, or use "Check now".

## Where jobs run

Pick in **Settings → Where ffmpeg jobs run**:

### 1. In the MediaForge container

ffmpeg ships with the image. Use this when the media files live on storage the container
can mount (e.g. a TrueNAS dataset). Mount the media into the container (see
`docker-compose.yml`) and add a **path mapping** so MediaForge can translate the paths
Plex reports into container paths — e.g. `/mnt/pool/media → /media`.

### 2. On the Plex server (SSH)

Jobs execute on the machine that owns the files. Requires an account or **service
account** with SSH access (password or private key) and ffmpeg installed on that server.
Pick the **server operating system** (Linux or Windows) in Settings — file
checks/uploads go over SFTP and both cmd and PowerShell SSH shells are auto-detected, so
no shell tuning is needed. MediaForge validates credentials before saving, and the
**Test ffmpeg** button verifies the whole chain (SSH login + ffmpeg on PATH) for either mode.

When Plex runs **on the same machine the SSH account logs into**, the paths Plex reports
are the same paths the session sees — so **no path mappings are needed at all**.

#### Windows Plex server — one-time setup

Tested on Windows 11 (including 25H2). On the Plex machine, in an **admin PowerShell**:

1. Install the built-in OpenSSH Server (the Settings → Optional features page doesn't
   always list it; the command always works):

   ```powershell
   Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
   ```

   If Features-on-Demand is broken on your build, use the standalone build instead:
   `winget install Microsoft.OpenSSH.Preview`.

2. Start it and set it to start with Windows:

   ```powershell
   Start-Service sshd; Set-Service sshd -StartupType Automatic
   ```

3. Confirm the firewall rule exists (the install normally creates it):

   ```powershell
   Get-NetFirewallRule -Name *OpenSSH-Server* | Select-Object Name, Enabled
   ```

   If it's missing:

   ```powershell
   New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22
   ```

4. Make sure `ffmpeg`/`ffprobe` are on the **system** PATH (Settings → System → About →
   Advanced system settings → Environment Variables → *System* `Path`) — a user-only PATH
   entry isn't always visible to SSH sessions.

5. **Give the SSH account NTFS access to the media folder** — this is the step everyone
   hits. Jobs need read *and* write (outputs are saved next to the originals):

   ```powershell
   icacls "D:\Plex_Bin" /grant "<ssh-username>:(OI)(CI)M"
   ```

   (Add `/T` at the end if some subfolders have inheritance disabled. GUI equivalent:
   folder Properties → Security → Edit → add the user with *Modify*.)

6. Sanity-check from any other machine before touching MediaForge:

   ```bash
   ssh <username>@<plex-ip> "dir \"D:\Plex_Bin\""
   ```

   If that lists your media folders, MediaForge will work. `Access is denied` means
   step 5 isn't done; `'ffmpeg' is not recognized` later means step 4 isn't done.

## Installing on TrueNAS SCALE

Create the config dataset/folder first (e.g. `/mnt/<pool>/apps/mediaforge`), then
**Apps → Discover Apps → ⋮ → Install via YAML**, name it `mediaforge`:

```yaml
services:
  mediaforge:
    image: ghcr.io/behrens-dev/mediaforge:latest
    pull_policy: always
    restart: unless-stopped
    ports:
      - '8191:8191'
    environment:
      TZ: America/New_York
    volumes:
      - '/mnt/<pool>/apps/mediaforge:/config'
      # Only for "in the container" mode — mount your media:
      # - '/mnt/<pool>/media:/media'
```

In **SSH mode the media mount is not needed at all** — the container never touches the
files. Note the quoted volume lines: quoting is the reliable way to handle spaces in
pool/dataset names.

Open `http://<truenas-ip>:8191` → Settings → enter the Plex URL + token, pick the
execution mode, **Test ffmpeg**, Save. Then search your library on the Convert page —
the tracks table should say *(ffprobe, on the Plex server)* or *(… in the container)*.

To update: push to `main` (both images rebuild via GitHub Actions), then stop/start the
app — `pull_policy: always` pulls the newest image on start.

## Path mappings

Plex reports file paths **as the Plex server sees them** (`D:\Plex_Bin\Plex Movies\...`
on Windows, `/data/movies/...` in a Linux container). If the execution mode sees the
files at a different prefix, add a mapping in Settings (longest match wins). Windows-style
prefixes are matched case-insensitively and backslash-tolerant, so
`D:\Plex_Bin → /media` and `/mnt/pool/media → /media` both work. Leave mappings empty
when the paths already line up (the usual case for SSH mode).

## Troubleshooting

- **Which build am I running?** The sidebar footer shows the version
  (e.g. `⚡ runs on Plex server · v0.3.1`), and `http://<host>:8191/api/status` returns it
  as JSON. No version shown = a pre-0.3.0 image; stop/start the app to re-pull.
- **"File not found at … — checked over SFTP on … / inside the MediaForge container"** —
  the error names exactly where it looked. Wrong place → fix the execution mode or path
  mappings in Settings. Right place → the SSH account can't see the file: check NTFS
  permissions (`icacls` step above) or the media mount.
- **Windows quirks handled automatically:** drive paths are tried as both `D:/…` and
  `/D:/…` against Windows OpenSSH's SFTP server, existence checks fall back to the login
  shell, and cmd vs PowerShell default shells are auto-detected — no server-side
  configuration needed beyond the setup steps above.
- **TrueNAS can't pull the image** — new GHCR packages default to private. GitHub profile
  → Packages → `mediaforge` → Package settings → change visibility to Public.

## Development

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8191
```

Config/state lives in `MEDIAFORGE_DATA` (default `/config`): the SQLite DB (`mediaforge.db`)
and staged subtitle uploads.
