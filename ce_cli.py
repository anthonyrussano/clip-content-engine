"""Standalone Content Engine command line interface (no Hermes required)."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import ce_api
import engine
import transcribe

SCORING_PRESETS = {
    "standard": dict(count=6, min_duration=45, max_duration=60, stride=60),
    "long-form": dict(count=12, min_duration=30, max_duration=90, stride=30),
}


def _scoring_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--preset", choices=SCORING_PRESETS, default="standard",
                        help="long-form: up to 12 clips, 30–90 seconds, scanning every 30 seconds")
    parser.add_argument("--count", type=int, help="Maximum clips; overrides the preset")
    parser.add_argument("--min-duration", type=float, help="Minimum candidate duration; overrides the preset")
    parser.add_argument("--max-duration", type=float, help="Maximum candidate duration; overrides the preset")
    parser.add_argument("--stride", type=float, help="Seconds between candidate starts; overrides the preset")
    parser.add_argument("--llm-model", help="Enable local or OpenAI-compatible model ranking")
    parser.add_argument("--llm-url", help="Default: http://127.0.0.1:1234/v1")
    parser.add_argument("--llm-required", action="store_true",
                        help="Fail instead of falling back to heuristic scoring")
    parser.add_argument("--allow-remote-llm", action="store_true",
                        help="Allow transcript submission to an HTTPS remote model")
    parser.add_argument("--llm-pool", type=int,
                        help="Candidate limit (default 120); capped pools sample across the full timeline")
    parser.add_argument("--llm-batch", type=int, default=12,
                        help="Candidates per model request (default 12)")
    parser.add_argument("--no-llm-refine", action="store_true",
                        help="Keep the scored windows instead of letting the model "
                             "choose each clip's start and end sentences")


def _transcript_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--transcriber", choices=ce_api.TRANSCRIBERS, default="captions",
                        help="captions (YouTube), whisper (local audio transcription), "
                             "or auto (captions, then Whisper if they fail)")
    parser.add_argument("--whisper-model", default=transcribe.DEFAULT_WHISPER_MODEL,
                        help="faster-whisper model, e.g. base.en, small.en, large-v3-turbo")


def _score_kwargs(args: argparse.Namespace) -> dict:
    settings = {key: getattr(args, key) if getattr(args, key) is not None else value
                for key, value in SCORING_PRESETS[args.preset].items()}
    return dict(
        **settings,
        llm_model=args.llm_model, llm_base_url=args.llm_url,
        llm_required=args.llm_required, allow_remote_llm=args.allow_remote_llm,
        llm_pool=args.llm_pool, llm_batch=args.llm_batch, llm_refine=not args.no_llm_refine,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clip-content-engine",
        description="Generate short-form video clips and copy without Hermes."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch", help="Fetch YouTube captions and metadata")
    fetch.add_argument("url")
    fetch.add_argument("--out-dir")
    _transcript_args(fetch)

    download = sub.add_parser("download", help="Download a YouTube video (or its audio) for cutting")
    download.add_argument("url")
    download.add_argument("--out-dir")
    download.add_argument("--audio-only", action="store_true")
    download.add_argument("--max-height", type=int, default=1080)

    trans = sub.add_parser("transcribe", help="Transcribe a local audio/video file with Whisper")
    trans.add_argument("media_path")
    trans.add_argument("--out-dir")
    trans.add_argument("--whisper-model", default=transcribe.DEFAULT_WHISPER_MODEL)
    trans.add_argument("--language", default="en")

    score = sub.add_parser("score", help="Score an existing transcript JSON")
    score.add_argument("transcript_path")
    _scoring_args(score)

    prepare = sub.add_parser("prepare", help="Create editable selections from score/run JSON")
    prepare.add_argument("scored_file")
    prepare.add_argument("--out-dir")

    gallery = sub.add_parser("gallery", help="Build a local video gallery and contact sheets")
    gallery.add_argument("--renders-file", required=True)
    gallery.add_argument("--clips-file", required=True)
    gallery.add_argument("--out-dir")
    gallery.add_argument("--verify", action="store_true", help="Decode every rendered clip to check for errors")

    run = sub.add_parser("run", help="Fetch, score, and draft platform copy")
    run.add_argument("url")
    run.add_argument("--out-dir")
    run.add_argument("--platforms", default="x,facebook,shorts,tiktok")
    _transcript_args(run)
    _scoring_args(run)

    copy = sub.add_parser("copy", help="Draft copy from text or a UTF-8 file")
    text_source = copy.add_mutually_exclusive_group(required=True)
    text_source.add_argument("--text")
    text_source.add_argument("--text-file")
    copy.add_argument("--url", default="")
    copy.add_argument("--title", default="")
    copy.add_argument("--platforms", default="x,facebook,shorts,tiktok")

    cut = sub.add_parser("cut", help="Cut clips described by a JSON list")
    cut.add_argument("video_path")
    cut.add_argument("--clips-file", required=True)
    cut.add_argument("--transcript-file")
    cut.add_argument("--out-dir")
    cut.add_argument("--no-subs", action="store_true")
    cut.add_argument("--framing", choices=engine.FRAMINGS, default="auto",
                     help="Portrait framing when a clip sets no crop_x: auto (track speakers, "
                          "fit slides and animation), track, or fit")
    cut.add_argument("--faces", choices=("auto", "off"), default="auto",
                     help="Follow detected faces in portrait clips when the faces extra is "
                          "installed (auto), or plan framing from motion only (off)")

    sub.add_parser("mcp", help="Launch the optional local stdio MCP server")
    return parser


def execute(args: argparse.Namespace):
    if args.command in ("prepare", "gallery"):
        import review_batch
        if args.command == "prepare":
            return review_batch.prepare(args.scored_file, args.out_dir)
        return review_batch.gallery(args.renders_file, args.clips_file, args.out_dir, args.verify)
    if args.command == "fetch":
        return ce_api.fetch_summary(args.url, out_dir=args.out_dir, transcriber=args.transcriber,
                                    whisper_model=args.whisper_model)
    if args.command == "download":
        return transcribe.download_media(args.url, out_dir=args.out_dir, audio_only=args.audio_only,
                                         max_height=args.max_height)
    if args.command == "transcribe":
        return ce_api.transcribe_summary(args.media_path, out_dir=args.out_dir,
                                         whisper_model=args.whisper_model, language=args.language)
    if args.command == "score":
        segments = ce_api.load_segments(args.transcript_path)
        clips, scoring = ce_api.score_clips(segments, **_score_kwargs(args))
        return {"clips": clips, "scoring": scoring}
    if args.command == "run":
        return ce_api.run_pipeline(
            args.url, out_dir=args.out_dir,
            platforms=[p.strip() for p in args.platforms.split(",") if p.strip()],
            transcriber=args.transcriber, whisper_model=args.whisper_model,
            **_score_kwargs(args),
        )
    if args.command == "copy":
        text = args.text if args.text is not None else Path(args.text_file).read_text(encoding="utf-8")
        return engine.write_copy(
            text, platforms=[p.strip() for p in args.platforms.split(",") if p.strip()],
            url=args.url, title=args.title,
        )
    if args.command == "cut":
        clips = json.loads(Path(args.clips_file).read_text(encoding="utf-8"))
        if not isinstance(clips, list):
            raise ValueError("clips file must contain a JSON array")
        segments = ce_api.load_segments(args.transcript_file) if args.transcript_file else None
        return engine.cut_clips(
            args.video_path, clips, out_dir=args.out_dir,
            segments=segments, subs=not args.no_subs, default_framing=args.framing,
            faces=args.faces,
        )
    raise ValueError("Unsupported command: " + args.command)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "mcp":
            try:
                from mcp_server import main as serve
            except ImportError as exc:
                raise RuntimeError("MCP support not installed; pip install '.[mcp]'") from exc
            serve()
            return 0
        result = execute(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "cut" and any(row.get("error") or not row.get("output") for row in result):
            return 1
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print("clip-content-engine: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
