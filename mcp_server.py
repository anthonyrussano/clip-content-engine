"""Local stdio MCP adapter for Content Engine.

Requires optional mcp Python package; launch with clip-content-engine mcp.
No HTTP listener is created and no video or copy is posted to any service.
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

import ce_api
import engine

mcp = FastMCP("clip-content-engine")


@mcp.tool()
def ce_fetch_transcript(url: str, out_dir: str = "", transcriber: str = "captions",
                        whisper_model: str = "") -> dict[str, Any]:
    """Fetch a YouTube video's English transcript and save its JSON file.

    transcriber: "captions" (YouTube), "whisper" (local transcription of the
    audio track, needs the whisper extra) or "auto" (captions, then Whisper).
    """
    return ce_api.fetch_summary(url, out_dir=out_dir or None, transcriber=transcriber,
                                whisper_model=whisper_model or ce_api.transcribe.DEFAULT_WHISPER_MODEL)


@mcp.tool()
def ce_score_moments(
    transcript_path: str, count: int = 6,
    min_duration: float = 45, max_duration: float = 60,
    stride: float = 60, llm_model: str = "",
    llm_base_url: str = "", llm_required: bool = False,
    allow_remote_llm: bool = False,
) -> dict[str, Any]:
    """Rank a local transcript into non-overlapping clip candidates."""
    clips, scoring = ce_api.score_clips(
        ce_api.load_segments(transcript_path), count=count,
        min_duration=min_duration, max_duration=max_duration, stride=stride,
        llm_model=llm_model or None, llm_base_url=llm_base_url or None,
        llm_required=llm_required, allow_remote_llm=allow_remote_llm,
    )
    return {"clips": clips, "scoring": scoring}


@mcp.tool()
def ce_write_copy(
    clip_text: str, platforms: list[str] | None = None,
    url: str = "", title: str = "",
) -> dict[str, str]:
    """Draft first-pass copy for X, Facebook, Shorts, TikTok and Instagram."""
    return engine.write_copy(clip_text, platforms=platforms, url=url, title=title)


@mcp.tool()
def ce_cut_clips(
    video_path: str, clips: list[dict[str, Any]],
    transcript_path: str = "", out_dir: str = "",
    subs: bool = True, framing: str = "auto", faces: str = "auto",
) -> list[dict[str, Any]]:
    """Render selected clips from a local source video using ffmpeg.

    Portrait framing: "auto" tracks speakers and fits slides/animation,
    "track" always follows motion, "fit" shows the whole frame. A clip's own
    crop_x (0-1) or framing key overrides it. faces="auto" follows detected
    faces when the faces extra is installed; "off" uses motion only.
    """
    segments = ce_api.load_segments(transcript_path) if transcript_path else None
    return engine.cut_clips(
        video_path, clips, out_dir=out_dir or None, segments=segments, subs=subs,
        default_framing=framing, faces=faces,
    )


@mcp.tool()
def ce_run(
    url: str, count: int = 6, platforms: list[str] | None = None,
    out_dir: str = "", min_duration: float = 45,
    max_duration: float = 60, stride: float = 60,
    llm_model: str = "", llm_base_url: str = "",
    llm_required: bool = False, allow_remote_llm: bool = False,
    transcriber: str = "captions", whisper_model: str = "",
) -> dict[str, Any]:
    """Get a transcript (captions, whisper or auto), rank candidate clips and draft social copy."""
    return ce_api.run_pipeline(
        url, count=count, platforms=platforms, out_dir=out_dir or None,
        min_duration=min_duration, max_duration=max_duration, stride=stride,
        llm_model=llm_model or None, llm_base_url=llm_base_url or None,
        llm_required=llm_required, allow_remote_llm=allow_remote_llm,
        transcriber=transcriber,
        whisper_model=whisper_model or ce_api.transcribe.DEFAULT_WHISPER_MODEL,
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
