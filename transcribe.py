"""Local media download and Whisper transcription.

Whisper support is optional: ``pip install '.[whisper]'`` installs
faster-whisper. Audio is decoded with ffmpeg in fixed-length chunks so memory
stays flat on long videos, and transcription runs entirely on this machine.
Every output path is confined to the plugin work dir, like the rest of the
engine.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from . import engine
except ImportError:  # standalone module install
    import engine

# The ORIGINAL audio track first: many videos also carry auto-dubbed tracks,
# and plain "bestaudio" can pick a dub. Plain HTTP (DASH) before HLS.
AUDIO_FORMAT = ("ba[format_note*=original][protocol^=http]/ba[language^=en][protocol^=http]"
                "/ba[protocol^=http]/ba/b")
DEFAULT_WHISPER_MODEL = "small.en"
_MEDIA_SUFFIXES = {".mp4", ".mkv", ".webm", ".m4a", ".opus", ".mp3", ".ogg", ".aac", ".wav"}


def _video_format(max_height: int) -> str:
    return (f"bv*[height<={max_height}]+ba[format_note*=original]/"
            f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b")


def download_media(url: str, out_dir: Optional[str] = None, audio_only: bool = False,
                   max_height: int = 1080, timeout: float = 3600) -> Dict[str, Any]:
    """Download a YouTube video (or only its original audio track) into the work dir.

    Returns {video_id, title, channel, duration, path, work_dir}.
    """
    if type(max_height) is not int or not 144 <= max_height <= 4320:
        raise ValueError("max_height must be an integer from 144 to 4320")
    yt = engine._ytdlp()
    url = engine.validate_video_url(url)
    out_dir = engine._resolve_out_dir(out_dir)
    name = "audio" if audio_only else "source"
    fmt = AUDIO_FORMAT if audio_only else _video_format(max_height)
    cmd = [yt, *engine._youtube_options(), "-f", fmt, "--no-part", "--quiet", "--no-warnings",
           "--print", "after_move:%(.{id,title,channel,duration_string,filepath})j"]
    if not audio_only:
        cmd += ["--merge-output-format", "mp4"]
    # A staging directory keeps a failed download from leaving partial files behind.
    with tempfile.TemporaryDirectory(prefix="media-", dir=out_dir) as stage:
        cmd += ["-o", os.path.join(stage, name + ".%(ext)s"), "--", url]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(f"yt-dlp download failed: {r.stderr[-500:]}")
        lines = [l for l in r.stdout.splitlines() if l.strip().startswith("{")]
        info = json.loads(lines[-1]) if lines else {}
        files = [f for f in Path(stage).iterdir()
                 if f.is_file() and f.name.startswith(name + ".") and f.suffix in _MEDIA_SUFFIXES]
        if not files:
            raise RuntimeError("yt-dlp reported success but produced no media file")
        produced = files[0]
        dest = Path(out_dir) / produced.name
        if dest.is_symlink():
            raise ValueError("Refusing to overwrite a symlink in the work dir")
        for old in Path(out_dir).glob(name + ".*"):
            if old.suffix in _MEDIA_SUFFIXES and old.is_file() and not old.is_symlink():
                old.unlink()
        shutil.move(str(produced), dest)
    return {
        "video_id": info.get("id") or engine.video_id(url),
        "title": info.get("title") or "",
        "channel": info.get("channel") or "",
        "duration": info.get("duration_string") or "",
        "path": str(dest),
        "work_dir": out_dir,
    }


def _media_duration(path: str) -> float:
    r = subprocess.run(
        [engine._find_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
         "-of", "json", path], capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise ValueError("Cannot read media file: " + r.stderr[-400:])
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Cannot read media duration")


def _pcm_chunk(path: str, start: float, length: float):
    """16 kHz mono float32 samples for one chunk, decoded by ffmpeg."""
    import numpy as np

    r = subprocess.run(
        [engine._ffmpeg(), "-v", "error", "-nostdin", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
         "-i", path, "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
        capture_output=True, timeout=max(120, length),
    )
    if r.returncode != 0:
        raise RuntimeError("ffmpeg audio decode failed: " + r.stderr.decode(errors="replace")[-400:])
    return np.frombuffer(r.stdout, dtype=np.float32)


_SENTENCE_END = re.compile(r"[.!?][\"')\]]*$")


def _join_words(words: List[Dict[str, Any]]) -> str:
    # Whisper words carry their own leading space; pieces such as "-led" do not.
    text = " ".join("".join(w["word"] for w in words).split())
    return re.sub(r"(\w) -(?=\w)", r"\1-", text)  # "spirit -led" -> "spirit-led"


def words_to_segments(words: List[Dict[str, Any]], max_chars: int = 42,
                      max_seconds: float = 4.0, max_gap: float = 0.8) -> List[Dict[str, Any]]:
    """Group timed words into short caption cues that never span a sentence end.

    Short cues read well as burned-in subtitles, and ending every sentence on a
    cue boundary lets clip selection start and stop on complete sentences.
    """
    segments: List[Dict[str, Any]] = []
    cur: List[Dict[str, Any]] = []

    def flush() -> None:
        if not cur:
            return
        text = _join_words(cur)
        if text:
            start = cur[0]["start"]
            if segments:
                start = max(start, segments[-1]["start"])
            end = max(cur[-1]["end"], start)
            segments.append({"start": round(float(start), 2), "duration": round(float(end - start), 2),
                             "text": text})
        cur.clear()

    for i, w in enumerate(words):
        if not w["word"].strip():
            continue
        cur.append(w)
        token = w["word"].strip()
        chars = len(_join_words(cur))
        nxt = words[i + 1] if i + 1 < len(words) else None
        if nxt is not None and nxt["word"][:1] not in (" ", "") and not _SENTENCE_END.search(token):
            continue  # the next piece continues this word (e.g. "spirit" + "-led")
        if (_SENTENCE_END.search(token)
                or chars >= max_chars
                or (chars >= 24 and token.endswith((",", ";", ":")))
                or cur[-1]["end"] - cur[0]["start"] >= max_seconds
                or (nxt is not None and nxt["start"] - w["end"] > max_gap)):
            flush()
    flush()
    return segments


def transcribe_file(media_path: str, model: str = DEFAULT_WHISPER_MODEL, language: str = "en",
                    out_dir: Optional[str] = None, compute_type: str = "int8",
                    threads: int = 0, chunk_seconds: float = 1800,
                    beam_size: int = 5, batch_size: int = 8) -> Dict[str, Any]:
    """Transcribe a local audio or video file with faster-whisper on the CPU.

    Writes transcript.json (the same segment format as caption fetches) and
    transcript.txt to the work dir. Returns the same keys as
    ``engine.fetch_transcript`` minus the YouTube metadata.

    batch_size > 1 uses faster-whisper's batched pipeline, which transcribes
    VAD-split speech regions in parallel (about 1.7x faster for small.en on
    a 32-thread CPU, with the same output quality in local tests). Use 1 for
    the sequential decoder.
    """
    try:
        from faster_whisper import BatchedInferencePipeline, WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "Whisper transcription needs faster-whisper: pip install 'clip-content-engine[whisper]'"
        ) from exc
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", model or "") or ".." in model:
        raise ValueError("Invalid Whisper model name")
    if not re.fullmatch(r"[a-z]{2,3}", language or ""):
        raise ValueError("language must be a two- or three-letter code such as 'en'")
    if model.endswith(".en") and language != "en":
        raise ValueError("English-only (.en) models need language='en'")
    if type(batch_size) is not int or not 1 <= batch_size <= 64:
        raise ValueError("batch_size must be an integer from 1 to 64")
    if not 60 <= chunk_seconds <= 7200:
        raise ValueError("chunk_seconds must be between 60 and 7200")
    path = str(Path(media_path).expanduser().resolve(strict=True))
    out_dir = engine._resolve_out_dir(out_dir)
    total = _media_duration(path)
    started = time.monotonic()
    whisper = WhisperModel(model, device="cpu", compute_type=compute_type,
                           cpu_threads=int(threads) if threads else min(os.cpu_count() or 4, 16))
    words: List[Dict[str, Any]] = []
    offset = 0.0
    while offset < total:
        audio = _pcm_chunk(path, offset, chunk_seconds)
        if audio.size < 16000 // 2:  # under half a second of audio left
            break
        if batch_size > 1:
            pieces, _ = BatchedInferencePipeline(model=whisper).transcribe(
                audio, language=language, beam_size=beam_size, word_timestamps=True,
                batch_size=batch_size,
            )
        else:
            pieces, _ = whisper.transcribe(
                audio, language=language, beam_size=beam_size, vad_filter=True,
                word_timestamps=True, condition_on_previous_text=False,
            )
        for piece in pieces:
            for w in piece.words or []:
                words.append({"start": float(offset + w.start), "end": float(offset + w.end),
                              "word": str(w.word)})
        offset += chunk_seconds
    segments = words_to_segments(words)
    if not segments:
        raise RuntimeError("Whisper produced no speech for this file")
    engine.validate_segments(segments)
    result = engine.write_transcript_files(segments, out_dir)
    result.update({
        "segment_count": len(segments),
        "work_dir": out_dir,
        "segments": segments,
        "transcript_source": f"whisper:{model}" + (f":batch{batch_size}" if batch_size > 1 else ""),
        "media_seconds": round(total, 1),
        "transcribe_seconds": round(time.monotonic() - started, 1),
    })
    return result


def transcribe_url(url: str, out_dir: Optional[str] = None, model: str = DEFAULT_WHISPER_MODEL,
                   language: str = "en", keep_audio: bool = False) -> Dict[str, Any]:
    """Download only the original audio track and transcribe it locally."""
    media = download_media(url, out_dir=out_dir, audio_only=True)
    try:
        t = transcribe_file(media["path"], model=model, language=language, out_dir=media["work_dir"])
    finally:
        if not keep_audio:
            Path(media["path"]).unlink(missing_ok=True)
    t.update({k: media[k] for k in ("video_id", "title", "channel", "duration")})
    if keep_audio:
        t["audio_path"] = media["path"]
    return t
