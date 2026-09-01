"""Builds ffmpeg argument lists for each job kind from ffprobe data.

The downmix pan formulas are lifted verbatim from the proven PowerShell scripts
(scripts/ffmpeg-downmix*.ps1) so MediaForge produces identical audio."""
import os
import re

PAN_71_TO_51 = "pan=5.1|FL=FL|FR=FR|FC=FC|LFE=LFE|BL=BL|BR=BR"
PAN_71_TO_STEREO = "pan=stereo|FL=FL+FC+BL+SL|FR=FR+FC+BR+SR"
PAN_51_TO_STEREO = "pan=stereo|FL=FL+FC+BL|FR=FR+FC+BR"

TEXT_SUB_EXTS = (".srt", ".ass", ".ssa", ".vtt", ".smi")

# ---- audio codec catalogue (the "convert" job) ----
AUDIO_ENCODERS = {"aac": "aac", "ac3": "ac3", "eac3": "eac3",
                  "opus": "libopus", "mp3": "libmp3lame", "flac": "flac"}
AUDIO_CODEC_LABELS = {"aac": "AAC", "ac3": "Dolby Digital (AC3)",
                      "eac3": "Dolby Digital Plus (E-AC3)", "opus": "Opus",
                      "mp3": "MP3", "flac": "FLAC"}
AUDIO_MAX_CHANNELS = {"aac": 8, "ac3": 6, "eac3": 6, "opus": 8, "mp3": 2, "flac": 8}
DEFAULT_BITRATES = {
    "aac":  {1: "96k", 2: "192k", 6: "384k", 8: "512k"},
    "ac3":  {1: "96k", 2: "192k", 6: "448k"},
    "eac3": {1: "96k", 2: "192k", 6: "448k"},
    "opus": {1: "64k", 2: "128k", 6: "256k", 8: "320k"},
    "mp3":  {1: "128k", 2: "192k"},
    "flac": {},  # lossless — no bitrate
}

# ---- video codec catalogue ----
# AV1 uses libaom (present in every common ffmpeg build, incl. Windows
# "essentials" builds and Debian's) rather than SVT-AV1 (full builds only).
VIDEO_ENCODERS = {"h264": "libx264", "h265": "libx265", "av1": "libaom-av1"}
VIDEO_QUALITY = {  # CRF per codec: high quality / balanced / smaller file
    "h264": {"high": 18, "balanced": 21, "small": 24},
    "h265": {"high": 20, "balanced": 23, "small": 27},
    "av1":  {"high": 24, "balanced": 30, "small": 36},
}
VIDEO_SPEED = {
    "h264": {"fast": "fast", "medium": "medium", "slow": "slow"},
    "h265": {"fast": "fast", "medium": "medium", "slow": "slow"},
    "av1":  {"fast": "8", "medium": "6", "slow": "4"},  # libaom -cpu-used
}

# what MP4 can carry without re-encoding
MP4_COPY_VIDEO = {"h264", "hevc", "av1", "mpeg4"}
MP4_COPY_AUDIO = {"aac", "ac3", "eac3", "mp3", "alac"}
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt"}


class BuildError(Exception):
    """A job can't be built from this file (missing track, bad options, …)."""


def sanitize_suffix(suffix: str) -> str:
    s = re.sub(r"[^A-Za-z0-9 ._()\[\]-]", "", (suffix or "").strip())
    return s.strip()


def output_path(input_path: str, suffix: str, ext: str | None = None) -> str:
    """Same folder as the source: insert the required suffix before the
    extension (optionally swapping the extension, e.g. for a container change).
    Separator-agnostic so Windows and POSIX paths both work."""
    slash = max(input_path.rfind("/"), input_path.rfind("\\"))
    dot = input_path.rfind(".")
    if dot <= slash:  # no extension
        dot = len(input_path)
    return input_path[:dot] + suffix + (ext if ext else input_path[dot:])


def audio_streams(probe: dict) -> list[dict]:
    """Audio streams in audio-relative order (a:0, a:1, …) with the fields the
    UI and builders need."""
    out = []
    for s in probe.get("streams", []):
        if s.get("codec_type") != "audio":
            continue
        tags = s.get("tags") or {}
        out.append({
            "a_index": len(out),
            "codec": s.get("codec_name") or "?",
            "channels": int(s.get("channels") or 0),
            "layout": s.get("channel_layout") or "",
            "language": tags.get("language") or "",
            "title": tags.get("title") or "",
            "default": bool((s.get("disposition") or {}).get("default")),
        })
    return out


def subtitle_streams(probe: dict) -> list[dict]:
    out = []
    for s in probe.get("streams", []):
        if s.get("codec_type") != "subtitle":
            continue
        tags = s.get("tags") or {}
        out.append({
            "s_index": len(out),
            "codec": s.get("codec_name") or "?",
            "language": tags.get("language") or "",
            "title": tags.get("title") or "",
            "default": bool((s.get("disposition") or {}).get("default")),
        })
    return out


def duration_ms(probe: dict) -> int:
    try:
        return int(float(probe.get("format", {}).get("duration", 0)) * 1000)
    except (TypeError, ValueError):
        return 0


def _find_by_channels(auds: list[dict]) -> tuple[int, int, int]:
    """Audio-relative index of the first 7.1 (8ch), 5.1 (6ch) and stereo (2ch)."""
    idx71 = idx51 = idx_st = -1
    for a in auds:
        if a["channels"] == 8 and idx71 < 0:
            idx71 = a["a_index"]
        elif a["channels"] == 6 and idx51 < 0:
            idx51 = a["a_index"]
        elif a["channels"] == 2 and idx_st < 0:
            idx_st = a["a_index"]
    return idx71, idx51, idx_st


def _new_track_meta(argv: list[str], out_idx: int, title: str, language: str) -> None:
    argv += [f"-metadata:s:a:{out_idx}", f"title={title}"]
    if language:
        argv += [f"-metadata:s:a:{out_idx}", f"language={language}"]


def _prefix(input_path: str) -> list[str]:
    return ["-hide_banner", "-nostdin", "-n",
            "-progress", "pipe:1", "-nostats",
            "-i", input_path]


# ---------- downmix ----------

def build_downmix(probe: dict, input_path: str, out_path: str, options: dict) -> list[str]:
    """presets:
      71_add   — keep everything, ADD a 5.1 and a stereo downmixed from the 7.1
      51_add   — keep everything, ADD a stereo downmixed from the 5.1
      71_only  — end up with ONLY a 5.1 + stereo (drop the 7.1)
      add_mono — keep everything, ADD a mono downmixed from the first audio track
    """
    preset = options.get("preset")
    auds = audio_streams(probe)
    if not auds:
        raise BuildError("This file has no audio streams.")
    idx71, idx51, idx_st = _find_by_channels(auds)

    argv = _prefix(input_path) + ["-map", "0:v", "-c:v", "copy"]
    n = 0  # output audio index

    if preset == "71_add":
        if idx71 < 0:
            raise BuildError("No 7.1 (8-channel) audio track found in this file.")
        src_lang = auds[idx71]["language"]
        for a in auds:
            argv += ["-map", f"0:a:{a['a_index']}", f"-c:a:{n}", "copy"]
            n += 1
        argv += ["-map", f"0:a:{idx71}", f"-c:a:{n}", "aac",
                 f"-filter:a:{n}", PAN_71_TO_51, f"-b:a:{n}", "384k"]
        _new_track_meta(argv, n, "5.1 Surround (AAC)", src_lang)
        n += 1
        argv += ["-map", f"0:a:{idx71}", f"-c:a:{n}", "aac",
                 f"-filter:a:{n}", PAN_71_TO_STEREO, f"-b:a:{n}", "192k"]
        _new_track_meta(argv, n, "Stereo (AAC)", src_lang)

    elif preset == "51_add":
        if idx51 < 0:
            raise BuildError("No 5.1 (6-channel) audio track found in this file.")
        src_lang = auds[idx51]["language"]
        for a in auds:
            argv += ["-map", f"0:a:{a['a_index']}", f"-c:a:{n}", "copy"]
            n += 1
        argv += ["-map", f"0:a:{idx51}", f"-c:a:{n}", "aac",
                 f"-filter:a:{n}", PAN_51_TO_STEREO, f"-b:a:{n}", "192k"]
        _new_track_meta(argv, n, "Stereo (AAC)", src_lang)

    elif preset == "add_mono":
        # ffmpeg's -ac 1 downmix handles any source layout correctly
        src_lang = auds[0]["language"]
        for a in auds:
            argv += ["-map", f"0:a:{a['a_index']}", f"-c:a:{n}", "copy"]
            n += 1
        argv += ["-map", "0:a:0", f"-c:a:{n}", "aac",
                 f"-ac:a:{n}", "1", f"-b:a:{n}", "96k"]
        _new_track_meta(argv, n, "Mono (AAC)", src_lang)

    elif preset == "71_only":
        if idx71 < 0:
            raise BuildError("No 7.1 (8-channel) audio track found in this file.")
        lang = auds[idx71]["language"]
        if idx51 >= 0 and idx_st >= 0:
            argv += ["-map", f"0:a:{idx51}", "-c:a:0", "copy",
                     "-map", f"0:a:{idx_st}", "-c:a:1", "copy"]
        elif idx51 >= 0:
            argv += ["-map", f"0:a:{idx51}", "-c:a:0", "copy",
                     "-map", f"0:a:{idx51}", "-c:a:1", "aac",
                     "-filter:a:1", PAN_51_TO_STEREO, "-b:a:1", "192k"]
            _new_track_meta(argv, 1, "Stereo (AAC)", auds[idx51]["language"])
        elif idx_st >= 0:
            argv += ["-map", f"0:a:{idx71}", "-c:a:0", "aac",
                     "-filter:a:0", PAN_71_TO_51, "-b:a:0", "384k"]
            _new_track_meta(argv, 0, "5.1 Surround (AAC)", lang)
            argv += ["-map", f"0:a:{idx_st}", "-c:a:1", "copy"]
        else:
            argv += ["-map", f"0:a:{idx71}", "-c:a:0", "aac",
                     "-filter:a:0", PAN_71_TO_51, "-b:a:0", "384k"]
            _new_track_meta(argv, 0, "5.1 Surround (AAC)", lang)
            argv += ["-map", f"0:a:{idx71}", "-c:a:1", "aac",
                     "-filter:a:1", PAN_71_TO_STEREO, "-b:a:1", "192k"]
            _new_track_meta(argv, 1, "Stereo (AAC)", lang)
    else:
        raise BuildError(f"Unknown downmix preset: {preset}")

    argv += ["-map", "0:s?", "-c:s", "copy", out_path]
    return argv


# ---------- combined re-encode / convert ----------

def _encode_audio_args(n: int, codec: str, channels: int, bitrate: str | None,
                       track_ref: str) -> list[str]:
    enc = AUDIO_ENCODERS.get(codec)
    if enc is None:
        raise BuildError(f"Unknown audio codec: {codec}")
    max_ch = AUDIO_MAX_CHANNELS[codec]
    if channels > max_ch:
        raise BuildError(
            f"{AUDIO_CODEC_LABELS[codec]} supports at most {max_ch} channels but "
            f"{track_ref} has {channels} — add a downmixed track instead.")
    args = [f"-c:a:{n}", enc]
    if codec != "flac":
        b = (bitrate or "").strip() or _default_bitrate(codec, channels)
        if b:
            args += [f"-b:a:{n}", b]
    return args


def _default_bitrate(codec: str, channels: int) -> str:
    table = DEFAULT_BITRATES.get(codec, {})
    if channels in table:
        return table[channels]
    for ch in (8, 6, 2, 1):  # nearest sensible layout at or below
        if ch <= channels and ch in table:
            return table[ch]
    return table.get(2, "")


def _layout_args(layout: str, src_ch: int) -> tuple[str | None, int]:
    """(pan filter or None, output channel count) for an added downmix track."""
    if layout == "5.1":
        if src_ch >= 8:
            return PAN_71_TO_51, 6
        if src_ch == 6:
            return None, 6
        raise BuildError("A 5.1 track needs a 5.1 or 7.1 source track.")
    if layout == "stereo":
        if src_ch >= 8:
            return PAN_71_TO_STEREO, 2
        if src_ch == 6:
            return PAN_51_TO_STEREO, 2
        return None, 2
    if layout == "mono":
        return None, 1  # -ac 1 downmix handles any source layout
    raise BuildError(f"Unknown track layout: {layout}")


def video_codec(probe: dict) -> str:
    return next((s.get("codec_name") or "" for s in probe.get("streams", [])
                 if s.get("codec_type") == "video"), "")


def build_convert(probe: dict, input_path: str, out_path: str, options: dict) -> list[str]:
    """The combined job: any mix of video re-encode, container change, per-track
    audio codec conversion/removal, and added downmixed tracks — in one pass.
    Anything left as 'copy' is stream-copied, so single changes stay fast."""
    auds = audio_streams(probe)
    if not auds:
        raise BuildError("This file has no audio streams.")
    container = os.path.splitext(out_path)[1].lower().lstrip(".")
    src_container = os.path.splitext(input_path)[1].lower().lstrip(".")

    video = options.get("video") or {}
    vtarget = video.get("codec", "copy")
    plan = {}
    for t in options.get("audio") or []:
        if isinstance(t, dict) and "index" in t:
            plan[int(t["index"])] = t
    adds = [a for a in (options.get("add") or []) if isinstance(a, dict)]

    changed = (vtarget != "copy" or bool(adds) or container != src_container
               or any(t.get("action") in ("convert", "remove") for t in plan.values()))
    if not changed:
        raise BuildError("Nothing selected to change — pick at least one option "
                         "(video codec, container, an audio conversion/removal, or a new track).")

    # video: only the primary stream (skips embedded cover art)
    argv = _prefix(input_path) + ["-map", "0:v:0"]
    if vtarget == "copy":
        src_v = video_codec(probe)
        if container == "mp4" and src_v not in MP4_COPY_VIDEO:
            raise BuildError(f"MP4 can't hold {src_v.upper() or 'this'} video without "
                             "re-encoding — pick H.264, H.265 or AV1 for the video, "
                             "or keep the MKV container.")
        argv += ["-c:v", "copy"]
    else:
        enc = VIDEO_ENCODERS.get(vtarget)
        if enc is None:
            raise BuildError(f"Unknown video codec: {vtarget}")
        crf = VIDEO_QUALITY[vtarget].get(video.get("quality"),
                                         VIDEO_QUALITY[vtarget]["balanced"])
        speed = VIDEO_SPEED[vtarget].get(video.get("speed"),
                                         VIDEO_SPEED[vtarget]["medium"])
        if vtarget == "av1":
            # libaom constant-quality mode needs -b:v 0; -cpu-used is its speed dial
            argv += ["-c:v", enc, "-crf", str(crf), "-b:v", "0",
                     "-cpu-used", speed, "-row-mt", "1"]
        else:
            argv += ["-c:v", enc, "-crf", str(crf), "-preset", speed]
        if vtarget == "h265" and container == "mp4":
            argv += ["-tag:v", "hvc1"]  # Apple players want this tag

    # existing audio tracks
    n = 0
    for a in auds:
        t = plan.get(a["a_index"]) or {}
        action = t.get("action", "copy")
        if action == "remove":
            continue
        argv += ["-map", f"0:a:{a['a_index']}"]
        if action == "convert":
            codec = t.get("codec", "aac")
            if container == "mp4" and codec not in MP4_COPY_AUDIO:
                raise BuildError(f"{AUDIO_CODEC_LABELS.get(codec, codec)} audio can't go "
                                 "in an MP4 — pick AAC, AC3, E-AC3 or MP3, or keep MKV.")
            argv += _encode_audio_args(n, codec, a["channels"], t.get("bitrate"),
                                       f"track a:{a['a_index']}")
        else:
            if container == "mp4" and a["codec"] not in MP4_COPY_AUDIO:
                raise BuildError(f"MP4 can't hold the {a['codec'].upper()} audio track "
                                 f"(a:{a['a_index']}) — convert it (e.g. to AAC or AC3), "
                                 "remove it, or keep MKV.")
            argv += [f"-c:a:{n}", "copy"]
        n += 1

    # added downmixed tracks
    for ad in adds:
        src = int(ad.get("source", 0))
        if src < 0 or src >= len(auds):
            raise BuildError(f"Added track's source a:{src} doesn't exist.")
        layout = ad.get("layout", "stereo")
        codec = ad.get("codec", "aac")
        if container == "mp4" and codec not in MP4_COPY_AUDIO:
            raise BuildError(f"{AUDIO_CODEC_LABELS.get(codec, codec)} audio can't go "
                             "in an MP4 — pick AAC, AC3, E-AC3 or MP3, or keep MKV.")
        pan, out_ch = _layout_args(layout, auds[src]["channels"])
        argv += ["-map", f"0:a:{src}"]
        argv += _encode_audio_args(n, codec, out_ch, ad.get("bitrate"),
                                   f"the new {layout} track")
        if pan:
            argv += [f"-filter:a:{n}", pan]
        elif out_ch != auds[src]["channels"]:
            argv += [f"-ac:a:{n}", str(out_ch)]
        label = {"5.1": "5.1 Surround", "stereo": "Stereo", "mono": "Mono"}[layout]
        _new_track_meta(argv, n, f"{label} ({AUDIO_CODEC_LABELS.get(codec, codec)})",
                        auds[src]["language"])
        n += 1

    if n == 0:
        raise BuildError("Every audio track was removed — keep, convert or add at least one.")

    # subtitles & attachments
    if container == "mp4":
        text_subs = [s for s in subtitle_streams(probe) if s["codec"] in TEXT_SUB_CODECS]
        for s in text_subs:
            argv += ["-map", f"0:s:{s['s_index']}"]
        if text_subs:
            argv += ["-c:s", "mov_text"]
        # image subs (PGS/VOBSUB) can't live in MP4 and are dropped
    else:
        argv += ["-map", "0:s?", "-c:s", "copy", "-map", "0:t?", "-c:t", "copy"]

    argv.append(out_path)
    return argv


# ---------- subtitle embed ----------

def build_embed_sub(probe: dict, input_path: str, out_path: str, options: dict,
                    sub_path: str) -> list[str]:
    """Add a subtitle file as a soft (selectable) track. Video/audio are copied
    byte-for-byte. Existing subtitle tracks are kept; the new one is appended."""
    sub_ext = os.path.splitext(sub_path)[1].lower()
    if sub_ext not in TEXT_SUB_EXTS:
        raise BuildError(f"Unsupported subtitle type '{sub_ext}'. "
                         f"Use one of: {', '.join(TEXT_SUB_EXTS)}")
    container = os.path.splitext(out_path)[1].lower()
    existing = subtitle_streams(probe)
    new_idx = len(existing)  # output subtitle-relative index of the new track

    argv = _prefix(input_path) + ["-i", sub_path,
                                  "-map", "0:v", "-map", "0:a", "-map", "0:s?", "-map", "1:0",
                                  "-c:v", "copy", "-c:a", "copy"]
    if container == ".mp4":
        # MP4 only supports mov_text for text subtitles
        argv += ["-c:s", "mov_text"]
    else:
        argv += ["-c:s", "copy"]
        # keep .ass/.ssa as-is to preserve styling, convert everything else to srt
        if sub_ext not in (".ass", ".ssa"):
            argv += [f"-c:s:{new_idx}", "srt"]

    language = (options.get("language") or "eng").strip() or "eng"
    argv += [f"-metadata:s:s:{new_idx}", f"language={language}"]
    label = (options.get("label") or "").strip()
    if label:
        argv += [f"-metadata:s:s:{new_idx}", f"title={label}"]
    argv += [f"-disposition:s:{new_idx}", "default" if options.get("make_default") else "0"]
    argv.append(out_path)
    return argv


# ---------- remove audio tracks ----------

def build_remove_audio(probe: dict, input_path: str, out_path: str, options: dict) -> list[str]:
    auds = audio_streams(probe)
    keep = [int(i) for i in options.get("keep", [])]
    if not keep:
        raise BuildError("At least one audio track must be kept.")
    bad = [i for i in keep if i < 0 or i >= len(auds)]
    if bad:
        raise BuildError(f"Audio track index out of range: {bad}")
    if len(keep) >= len(auds):
        raise BuildError("Nothing to remove — every audio track is being kept.")

    argv = _prefix(input_path) + ["-map", "0:v", "-c:v", "copy"]
    for i in sorted(set(keep)):
        argv += ["-map", f"0:a:{i}"]
    argv += ["-c:a", "copy", "-map", "0:s?", "-c:s", "copy", out_path]
    return argv


# ---------- audio <-> video realign ----------

def _fmt_offset(offset_ms: float) -> str:
    return f"{offset_ms / 1000.0:.3f}"


def build_audio_sync(probe: dict, input_path: str, out_path: str, options: dict) -> list[str]:
    """mode 'offset': shift ALL audio by N ms (positive = audio later) — pure remux.
    mode 'auto': re-encode audio with aresample async correction to squeeze/stretch
    it back onto the video's timestamps (fixes progressive drift and gaps)."""
    mode = options.get("mode", "offset")
    auds = audio_streams(probe)
    if not auds:
        raise BuildError("This file has no audio streams.")

    if mode == "offset":
        try:
            off = float(options.get("offset_ms"))
        except (TypeError, ValueError):
            raise BuildError("A millisecond offset is required (e.g. 500 or -250).")
        if off == 0:
            raise BuildError("Offset is 0 ms — nothing to change.")
        return (["-hide_banner", "-nostdin", "-n", "-progress", "pipe:1", "-nostats",
                 "-i", input_path,
                 "-itsoffset", _fmt_offset(off), "-i", input_path,
                 "-map", "0:v", "-map", "1:a", "-map", "0:s?",
                 "-c", "copy", out_path])

    if mode == "auto":
        argv = _prefix(input_path) + ["-map", "0:v", "-c:v", "copy"]
        for n, a in enumerate(auds):
            bitrate = {8: "512k", 6: "384k"}.get(a["channels"], "192k")
            argv += ["-map", f"0:a:{a['a_index']}", f"-c:a:{n}", "aac",
                     f"-filter:a:{n}", "aresample=async=1000:first_pts=0",
                     f"-b:a:{n}", bitrate]
        argv += ["-map", "0:s?", "-c:s", "copy", out_path]
        return argv

    raise BuildError(f"Unknown audio-sync mode: {mode}")


# ---------- subtitle <-> audio realign ----------

def build_sub_sync(probe: dict, input_path: str, out_path: str, options: dict) -> list[str]:
    """Shift every embedded subtitle track by N ms (positive = subtitles later).
    Pure remux — video, audio and subtitle data are copied untouched."""
    if not subtitle_streams(probe):
        raise BuildError("This file has no embedded subtitle tracks to shift.")
    try:
        off = float(options.get("offset_ms"))
    except (TypeError, ValueError):
        raise BuildError("A millisecond offset is required (e.g. 500 or -250).")
    if off == 0:
        raise BuildError("Offset is 0 ms — nothing to change.")
    return (["-hide_banner", "-nostdin", "-n", "-progress", "pipe:1", "-nostats",
             "-i", input_path,
             "-itsoffset", _fmt_offset(off), "-i", input_path,
             "-map", "0:v", "-map", "0:a", "-map", "1:s?",
             "-c", "copy", out_path])


BUILDERS = {
    "downmix": build_downmix,
    "convert": build_convert,
    "remove_audio": build_remove_audio,
    "audio_sync": build_audio_sync,
    "sub_sync": build_sub_sync,
    # embed_sub is special-cased (needs the staged subtitle path)
}


def build(kind: str, probe: dict, input_path: str, out_path: str, options: dict,
          sub_path: str | None = None) -> list[str]:
    if kind == "embed_sub":
        if not sub_path:
            raise BuildError("No subtitle file available for this job.")
        return build_embed_sub(probe, input_path, out_path, options, sub_path)
    fn = BUILDERS.get(kind)
    if fn is None:
        raise BuildError(f"Unknown job kind: {kind}")
    return fn(probe, input_path, out_path, options)
