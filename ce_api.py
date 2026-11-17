"""Agent-neutral API shared by the CLI, MCP server and Hermes integration."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

try:
    from . import engine, transcribe
    from .llm_ranking import rank_moments
except ImportError:  # standalone module install
    import engine
    import transcribe
    from llm_ranking import rank_moments

TRANSCRIBERS = ("captions", "whisper", "auto")


def load_segments(transcript_path: str) -> list[dict[str, Any]]:
    with open(transcript_path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("segments")
    if not isinstance(data, list):
        raise ValueError("Transcript must be a JSON array or an object containing segments")
    if all(isinstance(s, dict) and type(s.get("start")) in (int, float) for s in data):
        data = sorted(data, key=lambda s: s["start"])  # stable; edited files may be out of order
    return engine.validate_segments(data)


def get_transcript(url: str, out_dir: str | None = None, transcriber: str = "captions",
                   whisper_model: str = transcribe.DEFAULT_WHISPER_MODEL) -> dict[str, Any]:
    """Captions from YouTube, local Whisper, or captions with a Whisper fallback ("auto")."""
    if transcriber not in TRANSCRIBERS:
        raise ValueError("transcriber must be one of: " + ", ".join(TRANSCRIBERS))
    if transcriber == "whisper":
        return transcribe.transcribe_url(url, out_dir=out_dir, model=whisper_model)
    try:
        return engine.fetch_transcript(url, out_dir=out_dir)
    except RuntimeError as exc:
        if transcriber != "auto" or "metadata failed" in str(exc):
            raise
        # No usable captions (missing, or rate limited): transcribe the audio locally.
        t = transcribe.transcribe_url(url, out_dir=out_dir, model=whisper_model)
        t["caption_error"] = str(exc)[-300:]
        return t


def score_clips(
    segments: list[dict[str, Any]], *, count: int = 6,
    min_duration: float = 45, max_duration: float = 60,
    stride: float = 60, llm_model: str | None = None,
    llm_base_url: str | None = None,
    llm_required: bool = False, allow_remote_llm: bool = False,
    llm_pool: int | None = None, llm_batch: int = 12, llm_refine: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    key = os.getenv("CONTENT_ENGINE_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    return rank_moments(
        segments, count=count, min_duration=min_duration,
        max_duration=max_duration, stride=stride, model=llm_model,
        base_url=llm_base_url, api_key=key, required=llm_required,
        allow_remote=allow_remote_llm, pool_size=llm_pool, batch_size=llm_batch,
        refine=llm_refine,
    )


def run_pipeline(
    url: str, *, count: int = 6, platforms: list[str] | None = None,
    out_dir: str | None = None, min_duration: float = 45,
    max_duration: float = 60, stride: float = 60,
    llm_model: str | None = None, llm_base_url: str | None = None,
    llm_required: bool = False, allow_remote_llm: bool = False,
    transcriber: str = "captions", whisper_model: str = transcribe.DEFAULT_WHISPER_MODEL,
    llm_pool: int | None = None, llm_batch: int = 12, llm_refine: bool = True,
) -> dict[str, Any]:
    started = time.monotonic()
    t = get_transcript(url, out_dir=out_dir, transcriber=transcriber, whisper_model=whisper_model)
    clips, scoring = score_clips(
        t["segments"], count=count, min_duration=min_duration,
        max_duration=max_duration, stride=stride, llm_model=llm_model,
        llm_base_url=llm_base_url, llm_required=llm_required,
        allow_remote_llm=allow_remote_llm, llm_pool=llm_pool, llm_batch=llm_batch,
        llm_refine=llm_refine,
    )
    output = []
    copy = {}
    for c in clips:
        draft = engine.write_copy(
            c["text"], platforms=platforms, url=url, title=t.get("title", "")
        )
        row = {
            "id": c["id"], "start": c["start"], "end": c["end"],
            "duration": c["duration"], "scores": c["scores"],
            "total": c["total"], "text": c["text"], "copy": draft,
        }
        for optional in ("llm_reason", "scoring_source", "heuristic_scores",
                         "refine_reason", "window"):
            if optional in c:
                row[optional] = c[optional]
        output.append(row)
        copy[c["id"]] = draft
    return {
        "video": {
            "video_id": t["video_id"], "title": t["title"],
            "channel": t["channel"], "duration": t["duration"], "url": url,
        },
        "transcript": {
            "segment_count": t["segment_count"], "path": t["transcript_path"],
            "txt_path": t["transcript_txt"], "work_dir": t["work_dir"],
            "source": t.get("transcript_source", ""),
        },
        "clips": output, "copy": copy, "scoring": scoring,
        "elapsed_seconds": round(time.monotonic() - started, 1),
    }


def fetch_summary(url: str, out_dir: str | None = None, transcriber: str = "captions",
                  whisper_model: str = transcribe.DEFAULT_WHISPER_MODEL) -> dict[str, Any]:
    t = get_transcript(url, out_dir=out_dir, transcriber=transcriber, whisper_model=whisper_model)
    return {k: t[k] for k in (
        "video_id", "title", "channel", "duration", "segment_count",
        "transcript_path", "transcript_txt", "work_dir",
    )} | {k: t[k] for k in ("transcript_source", "caption_error", "transcribe_seconds") if k in t} \
        | {"preview": t["segments"][:5]}


def transcribe_summary(media_path: str, out_dir: str | None = None,
                       whisper_model: str = transcribe.DEFAULT_WHISPER_MODEL,
                       language: str = "en") -> dict[str, Any]:
    t = transcribe.transcribe_file(media_path, model=whisper_model, language=language, out_dir=out_dir)
    return {k: v for k, v in t.items() if k != "segments"} | {"preview": t["segments"][:5]}
