"""Standalone Content Engine command line interface (no Hermes required)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import ce_api
import engine


def _scoring_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--count", type=int, default=6)
    parser.add_argument("--min-duration", type=float, default=45)
    parser.add_argument("--max-duration", type=float, default=60)
    parser.add_argument("--stride", type=float, default=60)
    parser.add_argument("--llm-model", help="Enable local or OpenAI-compatible model ranking")
    parser.add_argument("--llm-url", help="Default: http://127.0.0.1:1234/v1")
    parser.add_argument("--llm-required", action="store_true",
                        help="Fail instead of falling back to heuristic scoring")
    parser.add_argument("--allow-remote-llm", action="store_true",
                        help="Allow transcript submission to an HTTPS remote model")


def _score_kwargs(args: argparse.Namespace) -> dict:
    return dict(
        count=args.count, min_duration=args.min_duration,
        max_duration=args.max_duration, stride=args.stride,
        llm_model=args.llm_model, llm_base_url=args.llm_url,
        llm_required=args.llm_required, allow_remote_llm=args.allow_remote_llm,
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

    score = sub.add_parser("score", help="Score an existing transcript JSON")
    score.add_argument("transcript_path")
    _scoring_args(score)

    run = sub.add_parser("run", help="Fetch, score, and draft platform copy")
    run.add_argument("url")
    run.add_argument("--out-dir")
    run.add_argument("--platforms", default="x,facebook,shorts,tiktok")
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

    sub.add_parser("mcp", help="Launch the optional local stdio MCP server")
    return parser


def execute(args: argparse.Namespace):
    if args.command == "fetch":
        return ce_api.fetch_summary(args.url, out_dir=args.out_dir)
    if args.command == "score":
        segments = ce_api.load_segments(args.transcript_path)
        clips, scoring = ce_api.score_clips(segments, **_score_kwargs(args))
        return {"clips": clips, "scoring": scoring}
    if args.command == "run":
        return ce_api.run_pipeline(
            args.url, out_dir=args.out_dir,
            platforms=[p.strip() for p in args.platforms.split(",") if p.strip()],
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
            segments=segments, subs=not args.no_subs,
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
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print("clip-content-engine: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
