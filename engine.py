"""Content Engine core.

Fetch a YouTube transcript, score clip moments, write per-platform social
copy, and cut clips with ffmpeg. No API keys, no model tokens. yt-dlp and
ffmpeg are found on PATH with shutil.which. Every URL is restricted to
YouTube hosts and every output path is confined to the plugin work dir.
"""

from __future__ import annotations

import json
import math
import numbers
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Tool discovery and safety boundaries
# ---------------------------------------------------------------------------


def _find_tool(name: str) -> str:
    """Locate a helper binary on PATH. No machine-specific absolute paths."""
    path = shutil.which(name)
    if not path:
        raise RuntimeError(
            f"{name} was not found. Install {name} and make sure it is on PATH."
        )
    return path


def _ytdlp() -> str:
    # An absolute CLI invocation must still use its own environment's dependency.
    sibling = Path(sys.executable).with_name("yt-dlp.exe" if os.name == "nt" else "yt-dlp")
    if sibling.is_file():
        return str(sibling)
    return _find_tool("yt-dlp")


def _youtube_options() -> List[str]:
    options = ["--no-playlist"]
    # yt-dlp enables Deno by default; explicitly enable Node on Node-only hosts.
    if not shutil.which("deno") and shutil.which("node"):
        options += ["--js-runtimes", "node"]
    return options


def _ffmpeg() -> str:
    return _find_tool("ffmpeg")


_ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_video_url(url: str) -> str:
    """Return a normalized YouTube URL, or raise ValueError.

    Only https URLs on YouTube's own hosts are accepted. A leading '-' is
    rejected so the value can never be parsed as a yt-dlp option, and any
    userinfo or port is rejected so the authority cannot be redirected.
    """
    if not isinstance(url, str):
        raise ValueError("url must be a string")
    u = url.strip()
    if not u or u.startswith("-"):
        raise ValueError("invalid url: must not be empty or start with '-'")
    p = urlparse(u)
    if p.scheme != "https":
        raise ValueError("invalid url: https is required")
    host = (p.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise ValueError(f"invalid url: {host!r} is not a YouTube host")
    if p.username or p.password or p.port:
        raise ValueError("invalid url: unexpected credentials or port")
    return u


def _hermes_home() -> Path:
    try:
        import hermes_constants
        return Path(hermes_constants.get_hermes_home())
    except Exception:
        pass
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "hermes"
    return Path.home() / ".hermes"


def _plugin_work_dir() -> Path:
    """Return a private work directory without requiring a Hermes installation.

    CONTENT_ENGINE_HOME has priority for standalone CLI/MCP use. Existing
    Hermes installations retain their original plugin-data location.
    """
    custom = os.environ.get("CONTENT_ENGINE_HOME")
    if custom:
        base = Path(custom).expanduser().resolve()
    elif os.environ.get("HERMES_HOME"):
        base = (_hermes_home() / "plugin-data" / "content-engine").resolve()
    else:
        try:
            import hermes_constants
        except ImportError:
            if os.name == "nt":
                base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "clip-content-engine"
            else:
                base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "clip-content-engine"
            base = base.resolve()
        else:
            base = (Path(hermes_constants.get_hermes_home()) / "plugin-data" / "content-engine").resolve()
    base.mkdir(parents=True, exist_ok=True)
    return base


def _resolve_out_dir(out_dir: Optional[str]) -> str:
    """Confine every output path to the plugin work dir.

    Absolute paths are allowed only when they already resolve inside the work
    dir. Traversal ('..') and outside-absolute paths are rejected, so a label
    or out_dir can never write .srt/.mp4 files elsewhere on the machine.
    """
    base = _plugin_work_dir()
    if not out_dir:
        return str(Path(tempfile.mkdtemp(prefix="content-engine-", dir=str(base))))
    if re.match(r"^[A-Za-z]:[/\\]", str(out_dir)):
        raise ValueError("Windows drive paths are not allowed as out_dir")
    requested = Path(str(out_dir))
    if not requested.is_absolute():
        requested = base / requested
    resolved = requested.resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        raise ValueError(
            "out_dir must resolve inside the plugin work dir: " + str(base)
        )
    resolved.mkdir(parents=True, exist_ok=True)
    return str(resolved)


def _safe_label(label: Any) -> str:
    """Limit a clip label to [A-Za-z0-9_-] so it cannot escape or inject."""
    s = str(label or "clip")
    if not _SAFE_NAME_RE.fullmatch(s):
        raise ValueError(
            "label must contain only letters, digits, '_' or '-' (got " + repr(s) + ")"
        )
    return s


def _escape_filter_path(path: str) -> str:
    """Escape a filename for an ffmpeg filter argument."""
    out = []
    for ch in path:
        if ch in ("\\", ":", "'", "[", "]", ",", ";"):
            out.append("\\")
        out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------

_VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")


def video_id(url: str) -> str:
    m = _VIDEO_ID_RE.search(url)
    return m.group(1) if m else url.strip()


def validate_segments(segments: Any) -> List[Dict[str, Any]]:
    """Validate transcript data before candidate generation or rendering."""
    if not isinstance(segments, list):
        raise ValueError("Transcript segments must be a JSON array")
    previous = -1.0
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError("Each transcript segment must be an object")
        for key in ("start", "duration"):
            value = segment.get(key)
            if isinstance(value, bool) or not isinstance(value, numbers.Real) or not math.isfinite(value):
                raise ValueError(f"Segment {key} must be a finite number")
        if segment["start"] < previous or segment["start"] < 0 or segment["duration"] < 0:
            raise ValueError("Segments need sorted nonnegative starts and nonnegative durations")
        if not isinstance(segment.get("text"), str):
            raise ValueError("Segment text must be a string")
        previous = segment["start"]
    return segments


def validate_scoring(min_duration: float, max_duration: float, count: int, stride: float) -> None:
    if (type(count) is not int or not 1 <= count <= 20
            or any(type(v) not in (int, float) or not math.isfinite(v)
                   for v in (min_duration, max_duration, stride))
            or min_duration <= 0 or max_duration < min_duration or stride <= 0):
        raise ValueError("Invalid count, duration, or stride")


def _strip_vtt(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return (s.replace("&amp;", "&").replace("&lt;", "<")
             .replace("&gt;", ">").replace("&nbsp;", " ")).strip()


def _parse_vtt(path: str) -> List[Dict[str, Any]]:
    """Parse YouTube VTT, handling both plain captions and auto-caption
    rolling cues (which repeat the previous line plus new karaoke-tagged words).
    """
    with open(path, encoding="utf-8") as f:
        content = f.read()
    segments: List[Dict[str, Any]] = []
    prev_text = ""
    prev_line = ""  # last visible line of the previous cue, emitted or not
    for block in re.split(r"\n\s*\n", content):
        lines = block.strip().split("\n")
        if len(lines) < 2:
            continue
        timing_idx = None
        for i, line in enumerate(lines):
            if "-->" in line:
                timing_idx = i
                break
        if timing_idx is None:
            continue
        m = re.match(
            r"(\d+):(\d+):([\d.]+)\s*-->\s*(\d+):(\d+):([\d.]+)",
            lines[timing_idx].strip(),
        )
        if not m:
            continue
        h1, m1, s1, h2, m2, s2 = m.groups()
        start = int(h1) * 3600 + int(m1) * 60 + float(s1)
        end = int(h2) * 3600 + int(m2) * 60 + float(s2)
        dur = end - start
        text_lines = lines[timing_idx + 1:]
        raw = "\n".join(text_lines)
        # Auto-caption rolling cues carry word-level karaoke tags. The last
        # line holds the NEW spoken words; earlier lines are the previous
        # rolling line repeated. Plain captions join all lines.
        visible = [_strip_vtt(l) for l in text_lines if _strip_vtt(l)]
        if re.search(r"<c>|<\d\d:\d\d:", raw):
            text = _strip_vtt(text_lines[-1]) if text_lines else ""
        else:
            # Rolling cues whose new line is a single word carry no karaoke
            # tags; drop the repeated previous line so words are not doubled.
            fresh = visible[1:] if len(visible) > 1 and visible[0] == prev_line else visible
            text = " ".join(fresh)
        if visible:
            prev_line = visible[-1]
        text = text.strip()
        # Skip 10ms "clear" cues and exact consecutive repeats.
        if not text or dur < 0.12 or text == prev_text:
            continue
        segments.append({"start": round(start, 2), "duration": round(dur, 2), "text": text})
        prev_text = text
    return segments


def fetch_transcript(url: str, out_dir: Optional[str] = None) -> Dict[str, Any]:
    """Download the transcript and metadata for a YouTube video.

    Returns {video_id, title, channel, duration, segment_count, transcript_path,
    transcript_txt, segments}.
    """
    yt = _ytdlp()
    url = validate_video_url(url)
    out_dir = _resolve_out_dir(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    vid = video_id(url)

    meta = subprocess.run(
        [yt, *_youtube_options(), "--skip-download", "--dump-single-json", "--", url],
        capture_output=True, text=True, timeout=120,
    )
    if meta.returncode != 0:
        raise RuntimeError(f"yt-dlp metadata failed: {meta.stderr[-500:]}")
    metadata = json.loads(meta.stdout)
    if metadata.get("_type") in ("playlist", "multi_video"):
        raise ValueError("A single YouTube video is required")
    vid = metadata.get("id") or vid
    title = metadata.get("title") or ""
    channel = metadata.get("channel") or ""
    duration = metadata.get("duration_string") or ""
    # Prefer one original English track; do not request dozens of translations.
    manual = metadata.get("subtitles") or {}
    automatic = metadata.get("automatic_captions") or {}
    tracks = {k: v for k, v in manual.items() if k == "en" or k.startswith("en-")}
    tracks = tracks or automatic
    english = sorted(k for k in tracks if k == "en" or k.startswith("en-"))
    language = next((k for k in ("en-orig", "en", "en-US", "en-GB") if k in tracks),
                    english[0] if english else "en")
    # A fresh staging directory prevents a failed request from reusing old captions.
    with tempfile.TemporaryDirectory(prefix="captions-", dir=out_dir) as stage:
        sub = subprocess.run(
            [yt, *_youtube_options(), "--skip-download", "--write-subs", "--write-auto-subs",
             "--sub-lang", language, "--sub-format", "vtt", "-o",
             os.path.join(stage, "transcript"), "--", url],
            capture_output=True, text=True, timeout=300,
        )
        vtt_files = sorted(Path(stage).glob("*.vtt"))
        if sub.returncode != 0 or not vtt_files:
            raise RuntimeError(
                "No transcript found. The video may have no English captions, or yt-dlp "
                f"could not fetch them. yt-dlp said: {sub.stderr[-500:]}"
            )
        segments = _parse_vtt(str(vtt_files[0]))
        if segments:
            # Remove captions saved by an earlier fetch so only the current track remains.
            for old in Path(out_dir).glob("transcript*.vtt"):
                if old.is_file() and not old.is_symlink():
                    old.unlink()
            shutil.copy2(vtt_files[0], Path(out_dir) / vtt_files[0].name)
    if not segments:
        raise RuntimeError("Transcript file was empty or unreadable.")

    files = write_transcript_files(segments, out_dir)
    return {
        "video_id": vid,
        "title": title,
        "channel": channel,
        "duration": duration,
        "segment_count": len(segments),
        "transcript_path": files["transcript_path"],
        "transcript_txt": files["transcript_txt"],
        "work_dir": out_dir,
        "segments": segments,
        "transcript_source": f"captions:{language}",
    }


def write_transcript_files(segments: List[Dict[str, Any]], out_dir: str) -> Dict[str, str]:
    """Write transcript.json and a readable transcript.txt (with hours past 60 minutes)."""
    transcript_path = os.path.join(out_dir, "transcript.json")
    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump(segments, f, ensure_ascii=False, indent=2)

    def stamp(t: float) -> str:
        h, rem = divmod(int(t), 3600)
        return (f"{h}:" if h else "") + f"{rem // 60:02d}:{rem % 60:02d}"

    txt_path = os.path.join(out_dir, "transcript.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(f"{stamp(s['start'])} | {s['text']}" for s in segments))
    return {"transcript_path": transcript_path, "transcript_txt": txt_path}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

_HOOK_KEYWORDS = [
    "secret", "never", "first", "evidence", "proof", "hidden", "revealed",
    "unknown", "mystery", "discovered", "actually", "real", "truth", "shocking",
    "nobody", "everyone", "always", "nothing", "everything",
]
_VISUAL_KEYWORDS = [
    "look", "see", "show", "image", "photo", "video", "footage", "picture",
    "screen", "graphic", "watch", "view", "display", "frame", "camera",
]
_NUMBER_RE = re.compile(r"\b\d[\d,.]*\b")
# Prayers, greetings and housekeeping rarely work as standalone clips.
_LOW_VALUE_RE = re.compile(
    r"\b(let'?s pray|let us pray|in jesus'? name|amen|bow (?:our|your) heads|"
    r"announcements?|welcome (?:back|everyone|to)|good morning|before we (?:begin|start)|"
    r"turn (?:in your bibles|with me) to|subscribe|like and subscribe|see you next week)\b",
    re.I)
_NAME_RE = re.compile(r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})+\b")


_SENTENCE_END_RE = re.compile(r"[.!?][\"')\]]*$")
# Below this share of punctuated segment endings, transcripts are treated as unpunctuated.
_MIN_SENTENCE_END_RATIO = 0.15


def _sentence_ends(segments: List[Dict[str, Any]]) -> List[bool]:
    return [bool(_SENTENCE_END_RE.search(s["text"].strip())) for s in segments]


def _first_sentence_text(text: str, fallback: str) -> str:
    m = re.match(r"\s*(.+?[.!?])(?=\s|$)", text, re.S)
    return " ".join((m.group(1) if m else fallback).split())[:300]


def _make_candidates(segments: List[Dict[str, Any]], lo: float, hi: float, stride: float) -> List[Dict[str, Any]]:
    """Segment-aligned candidate windows, ported from ClipBot 5000.

    When the transcript is punctuated, windows start after a sentence end and
    finish on one when possible, so clips do not begin or stop mid-thought.
    """
    cands: List[Dict[str, Any]] = []
    i = 0
    n = len(segments)
    ends = _sentence_ends(segments)
    snap = n > 0 and sum(ends) / n >= _MIN_SENTENCE_END_RATIO

    def clean_start(k: int) -> bool:
        return k == 0 or ends[k - 1]

    while i < n:
        if snap:
            # Move forward to the next sentence start within one stride.
            k = i
            while k < n and not clean_start(k) and segments[k]["start"] < segments[i]["start"] + stride:
                k += 1
            if k < n and clean_start(k):
                i = k
        start = segments[i]["start"]
        j = i
        while j < n and (segments[j]["start"] + segments[j]["duration"] - start) < lo:
            j += 1
        best_j = None
        sentence_j = None
        while j < n and (segments[j]["start"] + segments[j]["duration"] - start) <= hi:
            best_j = j
            if ends[j]:
                sentence_j = j
            j += 1
        if snap and sentence_j is not None:
            best_j = sentence_j
        if best_j is not None:
            end = segments[best_j]["start"] + segments[best_j]["duration"]
            text = " ".join(s["text"] for s in segments[i:best_j + 1])
            cand = {
                "start": round(start, 2),
                "end": round(end, 2),
                "duration": round(end - start, 2),
                "text": text,
                "first": _first_sentence_text(text, segments[i]["text"]) if snap else segments[i]["text"].strip(),
                "last": segments[best_j]["text"].strip(),
            }
            if snap:
                cand["clean_start"] = clean_start(i)
                cand["clean_end"] = ends[best_j]
            cands.append(cand)
        target = start + stride
        while i < n and segments[i]["start"] < target:
            i += 1
        if i >= n:
            break
    out: List[Dict[str, Any]] = []
    seen = set()
    for c in cands:
        key = round(c["start"], 0)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    for idx, c in enumerate(out, 1):
        c["id"] = f"C{idx:02d}"
    return out


def _score_candidate(text: str, first: str, duration: float,
                     clean: Optional[bool] = None) -> Dict[str, int]:
    """Deterministic 1-5 scoring on four criteria, max 20.

    ``clean`` is True when the window starts and ends on sentence boundaries,
    False when a punctuated transcript could not be snapped, and None when the
    transcript has no usable punctuation.
    """
    first_lower = first.lower()
    text_lower = text.lower()

    hook = 1
    if _NUMBER_RE.search(first):
        hook += 1
    if any(w in first_lower for w in _HOOK_KEYWORDS):
        hook += 1
    if _NAME_RE.search(first):
        hook += 1
    if len(first.split()) >= 8:
        hook += 1
    hook = min(hook, 5)

    completeness = 2
    words = len(text.split())
    if words >= 40:
        completeness += 1
    if words >= 80:
        completeness += 1
    if any(w in text_lower for w in ["because", "so", "therefore", "which means"]):
        completeness += 1
    if clean is True:
        completeness += 1
    elif clean is False:
        completeness -= 1
    completeness = max(1, min(completeness, 5))

    standalone = 2
    if not re.search(r"\b(this|that|it|they|them|he|she)\b", first_lower):
        standalone += 1
    if _NUMBER_RE.search(text):
        standalone += 1
    if _NAME_RE.search(text):
        standalone += 1
    if len(_LOW_VALUE_RE.findall(text)) >= 1:
        standalone -= 2
        hook -= 1
    standalone = max(1, min(standalone, 5))
    hook = max(1, hook)

    visual = 1
    if any(w in text_lower for w in _VISUAL_KEYWORDS):
        visual += 2
    if _NUMBER_RE.search(text):
        visual += 1
    if any(w in text_lower for w in ["here", "below", "above", "left", "right"]):
        visual += 1
    visual = min(visual, 5)

    length_bonus = 0
    if 45 <= duration <= 65:
        length_bonus = 1

    total = hook + completeness + standalone + visual + length_bonus
    return {
        "hook": hook,
        "completeness": completeness,
        "standalone": standalone,
        "visual": visual,
        "length_bonus": length_bonus,
        "total": total,
    }


def _clean(c: Dict[str, Any]) -> Optional[bool]:
    if "clean_start" not in c:
        return None
    return bool(c["clean_start"] and c["clean_end"])


def score_moments(
    segments: List[Dict[str, Any]],
    min_duration: float = 45,
    max_duration: float = 60,
    count: int = 6,
    stride: float = 60,
) -> List[Dict[str, Any]]:
    """Generate candidates, score them, and return the top N non-overlapping."""
    validate_scoring(min_duration, max_duration, count, stride)
    validate_segments(segments)
    cands = _make_candidates(segments, min_duration, max_duration, stride)
    for c in cands:
        c["scores"] = _score_candidate(c["text"], c["first"], c["duration"], _clean(c))
        c["total"] = c["scores"]["total"]
    cands.sort(key=lambda x: x["total"], reverse=True)

    picked: List[Dict[str, Any]] = []
    for c in cands:
        overlap = False
        for p in picked:
            if not (c["end"] <= p["start"] or c["start"] >= p["end"]):
                overlap = True
                break
        if overlap:
            continue
        picked.append(c)
        if len(picked) >= count:
            break
    return picked


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------

def _excerpt(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text.rstrip(".,;:")
    return text[:limit].rsplit(" ", 1)[0].rstrip(".,;:")


def _first_sentence(text: str) -> str:
    """First complete sentence with at least 4 words, capped."""
    for m in re.finditer(r"[^.!?]+[.!?]", text):
        s = m.group(0).strip()
        if len(s.split()) >= 4:
            return _excerpt(s, 120)
    return _excerpt(text, 120)


def _key_fact(text: str, hook: str) -> str:
    """A sentence with a number or name that is meaningfully different from the hook."""
    best = ""
    for s in re.split(r"(?<=[.!?])\s+", text):
        s = " ".join(s.split())
        if not s or s == hook:
            continue
        if (_NUMBER_RE.search(s) or _NAME_RE.search(s)) and len(s) <= 160:
            if len(s) > len(best):
                best = s
    return _excerpt(best, 120) if best else ""


def write_copy(
    clip_text: str,
    platforms: Optional[List[str]] = None,
    url: str = "",
    title: str = "",
) -> Dict[str, str]:
    """Deterministic first-draft social copy per platform."""
    platforms = platforms or ["x", "facebook", "shorts", "tiktok"]
    hook = _first_sentence(clip_text)
    fact = _key_fact(clip_text, hook)
    # Auto-caption text has no sentence breaks, so hook and fact can overlap.
    # Never ship the same words twice.
    if fact and (fact in hook or hook in fact or fact[:30] in hook):
        fact = ""
    body = f"{hook} {fact}".strip() if fact else hook
    out: Dict[str, str] = {}
    for p in platforms:
        p = p.lower()
        if p == "x":
            out[p] = f"{body} {url}".strip()
        elif p == "facebook":
            out[p] = f"{body}\n\n{url}".strip()
        elif p in ("shorts", "youtube_shorts", "youtube"):
            out[p] = body
        elif p == "tiktok":
            out[p] = f"{body} Link in bio."
        elif p == "instagram":
            out[p] = f"{body} Link in bio."
        else:
            out[p] = f"{body} {url}".strip()
    return out


# ---------------------------------------------------------------------------
# Cutting
# ---------------------------------------------------------------------------

def _fmt_srt_time(s: float) -> str:
    total = round(s * 1000)
    seconds, ms = divmod(total, 1000)
    minutes, sec = divmod(seconds, 60)
    h, m = divmod(minutes, 60)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def _make_srt(segments: List[Dict[str, Any]], start: float, end: float, path: str) -> int:
    in_range = []
    for seg in segments:
        ss, ee = seg["start"], seg["start"] + seg["duration"]
        if ee <= start + 0.01 or ss >= end - 0.01:
            continue
        in_range.append((max(ss, start), min(ee, end), seg["text"]))
    idx = 1
    lines: List[str] = []
    for i, (rs, re, text) in enumerate(in_range):
        if i + 1 < len(in_range):
            re = min(re, in_range[i + 1][0])
        re = min(re, end)
        rs = max(rs, start)
        rel_s = rs - start
        rel_e = re - start
        if rel_e - rel_s < 0.3:
            continue
        if rel_e - rel_s > 4.0:
            rel_e = rel_s + 4.0
        lines += [str(idx), f"{_fmt_srt_time(rel_s)} --> {_fmt_srt_time(rel_e)}", text.strip(), ""]
        idx += 1
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return idx - 1


FRAMINGS = ("auto", "track", "fit")


def cut_clips(
    video_path: str,
    clips: List[Dict[str, Any]],
    out_dir: Optional[str] = None,
    segments: Optional[List[Dict[str, Any]]] = None,
    subs: bool = True,
    default_framing: str = "auto",
    faces: str = "auto",
) -> List[Dict[str, Any]]:
    """Cut clips from a source video with ffmpeg, burning subtitles.

    Portrait clips accept ``crop_x``: a fixed 0-1 horizontal position for the
    centre of the crop, or "auto" (default) to plan framing per camera shot.
    ``framing`` (per clip, or ``default_framing``) steers auto mode: "auto"
    tracks speakers and fits slides and full-frame animation, "track" always
    follows motion, and "fit" shows the whole frame over a blurred copy.
    ``faces`` "auto" uses face detection when the faces extra is installed;
    "off" plans framing from motion only.
    """
    ffmpeg = _ffmpeg()
    if faces not in ("auto", "off"):
        raise ValueError("faces must be 'auto' or 'off'")
    video_path = str(Path(video_path).expanduser().resolve(strict=True))
    if not clips or not isinstance(clips, list):
        raise ValueError("clips must be a nonempty array")
    if segments is not None:
        validate_segments(segments)
    source = _probe_video(video_path)
    out_dir = _resolve_out_dir(out_dir)
    # Validate the whole batch before writing any outputs.
    plan: List[Dict[str, Any]] = []
    labels = set()
    for c in clips:
        if not isinstance(c, dict):
            raise ValueError("Each clip must be an object")
        start, end = float(c["start"]), float(c["end"])
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError("Clip times must be finite with 0 <= start < end")
        if end > source["duration"] + 0.05:
            raise ValueError(f"Clip end exceeds source duration ({source['duration']:.3f}s)")
        label = _safe_label(c.get("label") or f"clip_{int(start)}")
        if label in labels:
            raise ValueError("Clip labels must be unique")
        labels.add(label)
        orient = c.get("orientation", "h")
        if orient not in ("h", "v"):
            raise ValueError("orientation must be 'h' or 'v'")
        crop_x = c.get("crop_x", "auto")
        if crop_x != "auto" and (type(crop_x) not in (int, float) or not 0 <= crop_x <= 1):
            raise ValueError("crop_x must be 'auto' or a number from 0 to 1")
        framing = c.get("framing", default_framing)
        if framing not in FRAMINGS:
            raise ValueError("framing must be one of: " + ", ".join(FRAMINGS))
        srt = os.path.join(out_dir, f"{label}.srt")
        out = os.path.join(out_dir, f"{label}_{orient}.mp4")
        if any(Path(f).is_symlink() or Path(f).resolve() == Path(video_path) for f in (srt, out)):
            raise ValueError("Output files must not be symlinks or overwrite the source")
        plan.append({"label": label, "orient": orient, "start": start, "end": end,
                     "crop_x": crop_x, "framing": framing, "srt": srt, "out": out})

    results: List[Dict[str, Any]] = []
    for item in plan:
        label, orient, start, end = item["label"], item["orient"], item["start"], item["end"]
        srt, out = item["srt"], item["out"]
        dur = end - start
        n = 0
        if subs and segments:
            n = _make_srt(segments, start, end, srt)
        crop_used = None
        shots: List[Dict[str, Any]] = []
        if orient == "v":
            if item["crop_x"] == "auto":
                shots = _auto_crop_shots(video_path, start, dur, source, framing=item["framing"],
                                         faces=faces == "auto")
            else:
                x = round(float(item["crop_x"]), 3)
                shots = [{"t": 0.0, "x": x, "mode": "crop", "subject": "manual", "path": [(0.0, x)]}]
            crop_used = shots[0]["x"] if len(shots) == 1 else _crop_median(shots, dur)
            graph = _portrait_graph(shots)
        else:
            graph = ("[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
                     "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1[v]")
        sub_arg = _escape_filter_path(os.path.basename(srt))
        if subs and segments and os.path.exists(srt) and os.path.getsize(srt) > 0:
            # Fit captions to the final canvas so portrait cropping cannot cut them off.
            graph = graph[:-3] + (f",subtitles={sub_arg}:force_style='FontSize=20,"
                                  "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                                  "Outline=2,Shadow=1'[v]")
        cmd = [
            ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", video_path, "-t", f"{dur:.3f}",
            "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=out_dir)
        row = {
            "label": label,
            "orientation": orient,
            "start": start,
            "end": end,
            "duration": round(dur, 2),
            "subs": n if subs else 0,
            "output": out if r.returncode == 0 else None,
            "error": r.stderr[-400:] if r.returncode != 0 else None,
        }
        if crop_used is not None:
            row["crop_x"] = crop_used
            if item["crop_x"] == "auto":
                row["crop_shots"] = [{"t": sh["t"], "x": sh["x"], "mode": sh["mode"],
                                      "subject": sh.get("subject", "motion"),
                                      "moves": max(0, len(sh["path"]) - 1)} for sh in shots]
            if shots[0].get("warning"):
                row["warning"] = shots[0]["warning"]
        results.append(row)
    return results


def _probe_video(video_path: str) -> Dict[str, Any]:
    probe = subprocess.run(
        [_find_tool("ffprobe"), "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,width,height", "-of", "json", video_path],
        capture_output=True, text=True, timeout=30,
    )
    if probe.returncode != 0:
        raise ValueError("Cannot read source video: " + probe.stderr[-400:])
    info = json.loads(probe.stdout)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("Source file has no video stream")
    try:
        duration = float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Cannot read source video duration")
    return {"duration": duration, "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0)}


def _crop_median(shots: List[Dict[str, Any]], dur: float) -> float:
    """Duration-weighted median crop position, reported as a single summary value."""
    spans = []
    for i, shot in enumerate(shots):
        end = shots[i + 1]["t"] if i + 1 < len(shots) else dur
        spans.append((shot["x"], max(0.0, end - shot["t"])))
    spans.sort()
    half, acc = sum(w for _, w in spans) / 2, 0.0
    for x, w in spans:
        acc += w
        if acc >= half:
            return x
    return spans[-1][0]


def _linear_expr(path: List[Any]) -> str:
    """ffmpeg expression for a piecewise-linear x(t) through (t, x) points."""
    expr = str(path[-1][1])
    for (t0, x0), (t1, x1) in reversed(list(zip(path, path[1:]))):
        if x0 == x1:
            seg = str(x0)
        else:
            seg = f"{x0}+({x1 - x0:.4f})*(t-{t0})/{max(t1 - t0, 0.01):.3f}"
        expr = f"if(lt(t\\,{t1})\\,{seg}\\,{expr})"
    return expr


def _portrait_graph(shots: List[Dict[str, Any]]) -> str:
    """filter_complex for a 1080x1920 portrait clip, ending in label [v].

    Crop shots follow each shot's camera path. Shots marked "fit" (slides and
    other still frames) show the whole frame over a blurred copy instead, so
    on-screen text is not cut off.
    """
    crop_shots = [sh for sh in shots if sh["mode"] == "crop"] or [
        {"t": 0.0, "x": 0.5, "path": [(0.0, 0.5)]}]
    # Camera position over the whole clip: shot paths joined at cut times.
    x_expr = _linear_expr(crop_shots[-1]["path"])
    for i in range(len(crop_shots) - 2, -1, -1):
        x_expr = (f"if(lt(t\\,{crop_shots[i + 1]['t']})\\,"
                  f"{_linear_expr(crop_shots[i]['path'])}\\,{x_expr})")
    crop = ("scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920:min(max(({x_expr})*iw-540\\,0)\\,iw-1080):0,setsar=1")
    fits = []
    for i, sh in enumerate(shots):
        if sh["mode"] == "fit":
            end = shots[i + 1]["t"] if i + 1 < len(shots) else 1e9
            fits.append(f"between(t\\,{sh['t']}\\,{end})")
    if not fits:
        return f"[0:v]{crop}[v]"
    return (
        f"[0:v]split=3[a][b][c];[a]{crop}[cropped];"
        # Blur at quarter size: a blurred background loses nothing and renders ~4x cheaper.
        "[b]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
        "boxblur=6:2,eq=brightness=-0.12,scale=1080:1920,setsar=1[bg];"
        "[c]scale=1080:-2,setsar=1[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[fitted];"
        f"[cropped][fitted]overlay=0:0:enable='{'+'.join(fits)}'[v]"
    )


# Motion spread (10th-90th percentile of crop votes, as a share of the frame width)
# above which a shot is treated as full-frame content such as animation or a
# screencast. Measured 2026-11-16: speakers 0.08-0.44 (walking the whole stage),
# 3Blue1Brown-style animation 0.57-0.65.
_FULL_FRAME_SPREAD = 0.5


def _auto_crop_shots(video_path: str, start: float, dur: float, source: Dict[str, Any],
                     fps: int = 6, min_shot: float = 1.0,
                     framing: str = "auto", faces: bool = True) -> List[Dict[str, Any]]:
    """Plan portrait framing for each camera shot in a clip.

    Samples small grayscale frames. A large whole-frame change marks a camera
    cut. Within a shot, each frame pair votes for the crop-width window with
    the most motion (the speaker). A virtual camera then follows the votes: it
    holds still while the speaker stays near the middle of the crop and glides
    over one second when they move away. Shots with almost no motion (slides,
    still graphics) are marked "fit" so the whole frame is shown, and so are
    shots whose motion is spread across the frame (animation, screencasts)
    unless framing="track". framing="fit" fits every shot.

    With faces=True and the optional faces extra installed, a shot where the
    YuNet detector finds a face in enough sampled frames follows the largest
    face instead of motion, and is never fitted for wide motion spread. Each
    shot reports "subject": "face", "motion" or "none" (fitted).

    Returns [{"t": shot start, "x": median centre, "mode": "crop"|"fit",
    "path": [(t, x), ...]}], always starting at t=0. Times are seconds from
    the clip start; x is 0-1 across the source width.
    """
    w, h = source.get("width") or 0, source.get("height") or 0
    centre_only = [{"t": 0.0, "x": 0.5, "mode": "crop", "path": [(0.0, 0.5)]}]
    if w <= 0 or h <= 0:
        return centre_only
    cols = 96
    rows = max(2, int(round(cols * h / w / 2)) * 2)
    # Fraction of the scaled width that the 9:16 crop keeps.
    keep = (1080 / 1920) * h / w
    if keep >= 0.98:
        return centre_only
    win = max(1, int(round(cols * keep)))
    r = subprocess.run(
        [_ffmpeg(), "-v", "error", "-nostdin", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
         "-i", video_path, "-an", "-vf", f"fps={fps},scale={cols}:{rows},format=gray",
         "-f", "rawvideo", "-"],
        capture_output=True, timeout=max(60, dur * 4),
    )
    size = cols * rows
    data = r.stdout
    frames = [data[k:k + size] for k in range(0, len(data) - size + 1, size)]
    if r.returncode != 0 or len(frames) < 3:
        return centre_only
    cuts = [0.0]
    votes: List[List[Any]] = [[]]
    pairs = [0]
    for idx, (a, b) in enumerate(zip(frames, frames[1:]), 1):
        energy = [0] * cols
        total = 0
        for y in range(rows):
            off = y * cols
            for x in range(cols):
                d = abs(a[off + x] - b[off + x])
                if d > 8:  # ignore compression noise
                    energy[x] += d
                    total += d
        mean = total / size
        t = idx / fps
        if mean > 20:  # camera cut or large camera move: start a new shot
            if t - cuts[-1] < min_shot:  # flash or dissolve: restart the current shot here
                if len(cuts) > 1:
                    cuts[-1] = t
            else:
                cuts.append(t)
                votes.append([])
                pairs.append(0)
            continue
        pairs[-1] += 1
        if mean < 0.05:  # static frame
            continue
        window = sum(energy[:win])
        best, best_x = window, 0
        for x in range(1, cols - win + 1):
            window += energy[x + win - 1] - energy[x - 1]
            if window > best:
                best, best_x = window, x
        if best > 0:
            # Centre on the motion inside the window, not the window edge, so ties don't bias left.
            span = range(best_x, best_x + win)
            votes[-1].append((t, (sum(energy[x] * (x + 0.5) for x in span) / best) / cols))
    face_list: Optional[List[Any]] = None
    face_warning: Optional[str] = None
    face_fps = 3
    if faces and framing != "fit":
        face_mod = None
        try:
            from . import faces as face_mod
        except ImportError:
            try:
                import faces as face_mod
            except ImportError:  # optional module missing from an older install: motion only
                face_warning = "face detection module not installed"
        if face_mod is not None:
            face_list, face_warning = face_mod.try_face_votes(video_path, start, dur, source)
    dead_zone = keep * 0.22
    shots: List[Dict[str, Any]] = []
    for i, (t0, shot_votes) in enumerate(zip(cuts, votes)):
        t1 = cuts[i + 1] if i + 1 < len(cuts) else dur
        static = len(shot_votes) < 3 or len(shot_votes) < 0.1 * max(pairs[i], 1)
        if face_list and framing != "fit":
            shot_faces = [(t, x) for t, x in face_list if t0 <= t < t1]
            expected = max(1.0, (t1 - t0) * face_fps)
            # Still shots (slides) need a face in most frames, so a photo on a slide stays fitted.
            needed = max(3, (0.6 if static else 0.3) * expected)
            if len(shot_faces) >= needed:
                fx = sorted(x for _, x in shot_faces)
                median = round(fx[len(fx) // 2], 3)
                shots.append({"t": round(t0, 2), "x": median, "mode": "crop", "subject": "face",
                              "path": _camera_path(shot_faces, t0, t1, median, dead_zone)})
                continue
        xs = sorted(x for _, x in shot_votes)
        spread = (xs[int(len(xs) * 0.9)] - xs[int(len(xs) * 0.1)]) if len(xs) >= 10 else 0.0
        if (framing == "fit" or static
                or (framing == "auto" and spread > _FULL_FRAME_SPREAD)):
            # No subject to follow (slide, still frame or full-frame animation): show it all.
            shots.append({"t": round(t0, 2), "x": 0.5, "mode": "fit", "subject": "none",
                          "path": [(round(t0, 2), 0.5)]})
            continue
        median = round(xs[len(xs) // 2], 3)
        path = _camera_path(shot_votes, t0, t1, median, dead_zone=dead_zone)
        shots.append({"t": round(t0, 2), "x": median, "mode": "crop", "subject": "motion",
                      "path": path})
    merged: List[Dict[str, Any]] = []
    for sh in shots:  # neighbouring "fit" shots look identical; join them
        if merged and sh["mode"] == "fit" and merged[-1]["mode"] == "fit":
            continue
        merged.append(sh)
    merged = merged or centre_only
    if face_warning:
        merged[0]["warning"] = face_warning
    return merged


def _camera_path(votes: List[Any], t0: float, t1: float, start_x: float,
                 dead_zone: float, step: float = 1.0, glide: float = 1.0) -> List[Any]:
    """Smooth (t, x) keyframes that follow motion votes without jitter."""
    cam = start_x
    points = [(round(t0, 2), round(cam, 3))]
    t = t0 + step
    while t < t1:
        near = sorted(x for vt, x in votes if t - step <= vt <= t + step)
        if len(near) >= 3:
            target = near[len(near) // 2]
            if abs(target - cam) > dead_zone:
                # Hold until now, then glide to the subject.
                if points[-1][0] < round(t - glide, 2):
                    points.append((round(t - glide, 2), round(cam, 3)))
                cam = target
                points.append((round(t, 2), round(cam, 3)))
        t += step
    return points


def _auto_crop_x(video_path: str, start: float, dur: float, source: Dict[str, Any]) -> float:
    """Single crop centre for a clip: the duration-weighted median over its shots."""
    shots = _auto_crop_shots(video_path, start, dur, source)
    return shots[0]["x"] if len(shots) == 1 else _crop_median(shots, dur)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run(
    url: str,
    count: int = 6,
    platforms: Optional[List[str]] = None,
    out_dir: Optional[str] = None,
    min_duration: float = 45,
    max_duration: float = 60,
) -> Dict[str, Any]:
    """Full pipeline: fetch transcript, score moments, write copy.

    Returns {video, transcript, clips, copy}. Clips carry the text needed for
    the agent to write final copy; the copy block is a deterministic first draft.
    """
    t0 = time.time()
    transcript = fetch_transcript(url, out_dir=out_dir)
    segments = transcript["segments"]
    clips = score_moments(segments, min_duration, max_duration, count)
    copy: Dict[str, Any] = {}
    for c in clips:
        c["copy"] = write_copy(c["text"], platforms, url=url, title=transcript.get("title", ""))
        copy[c["id"]] = c["copy"]
    return {
        "video": {
            "video_id": transcript["video_id"],
            "title": transcript["title"],
            "channel": transcript["channel"],
            "duration": transcript["duration"],
            "url": url,
        },
        "transcript": {
            "segment_count": transcript["segment_count"],
            "path": transcript["transcript_path"],
            "txt_path": transcript["transcript_txt"],
            "work_dir": transcript["work_dir"],
        },
        "clips": [
            {
                "id": c["id"],
                "start": c["start"],
                "end": c["end"],
                "duration": c["duration"],
                "scores": c["scores"],
                "total": c["total"],
                "text": c["text"][:600],
                "copy": c["copy"],
            }
            for c in clips
        ],
        "copy": copy,
        "elapsed_seconds": round(time.time() - t0, 1),
    }
