"""Builds ffmpeg argument lists for each job kind from ffprobe data.

The downmix pan formulas are lifted verbatim from the proven PowerShell scripts
(scripts/ffmpeg-downmix*.ps1) so MediaForge produces identical audio."""
import os
import re

PAN_71_TO_51 = "pan=5.1|FL=FL|FR=FR|FC=FC|LFE=LFE|BL=BL|BR=BR"
PAN_71_TO_STEREO = "pan=stereo|FL=FL+FC+BL+SL|FR=FR+FC+BR+SR"
PAN_51_TO_STEREO = "pan=stereo|FL=FL+FC+BL|FR=FR+FC+BR"

TEXT_SUB_EXTS = (".srt", ".ass", ".ssa", ".vtt", ".smi")


class BuildError(Exception):
    """A job can't be built from this file (missing track, bad options, …)."""


def sanitize_suffix(suffix: str) -> str:
    s = re.sub(r"[^A-Za-z0-9 ._()\[\]-]", "", (suffix or "").strip())
    return s.strip()


def output_path(input_path: str, suffix: str) -> str:
    """Same folder as the source: insert the required suffix before the
    extension. Separator-agnostic so Windows and POSIX paths both work."""
    slash = max(input_path.rfind("/"), input_path.rfind("\\"))
    dot = input_path.rfind(".")
    if dot <= slash:  # no extension
        dot = len(input_path)
    return input_path[:dot] + suffix + input_path[dot:]


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
