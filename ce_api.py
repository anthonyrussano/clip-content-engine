"""Agent-neutral API shared by the CLI, MCP server and Hermes integration."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import engine
from llm_ranking import rank_moments


def load_segments(transcript_path: str) -> list[dict[str, Any]]:
    with open(transcript_path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("segments")
    if not isinstance(data, list):
        raise ValueError("Transcript must be a JSON array or an object containing segments")
    for segment in data:
        if not isinstance(segment, dict) or not all(k in segment for k in ("start", "duration", "text")):
            raise ValueError("Each transcript segment needs start, duration and text")
    return data


def score_clips(
    segments: list[dict[str, Any]], *, count: int = 6,
    min_duration: float = 45, max_duration: float = 60,
    stride: float = 60, llm_model: str | None = None,
    llm_base_url: str | None = None,
    llm_required: bool = False, allow_remote_llm: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    key = os.getenv("CONTENT_ENGINE_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    return rank_moments(
        segments, count=count, min_duration=min_duration,
        max_duration=max_duration, stride=stride, model=llm_model,
        base_url=llm_base_url, api_key=key, required=llm_required,
        allow_remote=allow_remote_llm,
    )


def run_pipeline(
    url: str, *, count: int = 6, platforms: list[str] | None = None,
    out_dir: str | None = None, min_duration: float = 45,
    max_duration: float = 60, stride: float = 60,
    llm_model: str | None = None, llm_base_url: str | None = None,
    llm_required: bool = False, allow_remote_llm: bool = False,
) -> dict[str, Any]:
    started = time.monotonic()
    t = engine.fetch_transcript(url, out_dir=out_dir)
    clips, scoring = score_clips(
        t["segments"], count=count, min_duration=min_duration,
        max_duration=max_duration, stride=stride, llm_model=llm_model,
        llm_base_url=llm_base_url, llm_required=llm_required,
        allow_remote_llm=allow_remote_llm,
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
            "total": c["total"], "text": c["text"][:600], "copy": draft,
        }
        for optional in ("llm_reason", "scoring_source", "heuristic_scores"):
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
        },
        "clips": output, "copy": copy, "scoring": scoring,
        "elapsed_seconds": round(time.monotonic() - started, 1),
    }


def fetch_summary(url: str, out_dir: str | None = None) -> dict[str, Any]:
    t = engine.fetch_transcript(url, out_dir=out_dir)
    return {k: t[k] for k in (
        "video_id", "title", "channel", "duration", "segment_count",
        "transcript_path", "transcript_txt", "work_dir",
    )} | {"preview": t["segments"][:5]}
