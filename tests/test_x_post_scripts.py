"""Offline checks for the X posting helpers in scripts/x-post.

A fake Browse binary records each call, so no browser or network is used.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POST_SH = ROOT / "scripts" / "x-post" / "post.sh"
MK_PY = ROOT / "scripts" / "x-post" / "mk.py"

FAKE_BROWSE = """#!/bin/sh
printf '%s\\n' "$*" >> "$BROWSE_LOG"
case "$*" in
  *attachments*) echo "Ready" ;;
  *innerText.length*) echo "250" ;;
esac
"""

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")


def _posts(tmp_path: Path, text: object = "x" * 200) -> Path:
    path = tmp_path / "final_posts.json"
    path.write_text(json.dumps({"clip_06": text}), encoding="utf-8")
    return path


def _env(tmp_path: Path, posts: Path, **extra: str) -> dict:
    browse = tmp_path / "browse"
    browse.write_text(FAKE_BROWSE, encoding="utf-8")
    browse.chmod(0o755)
    clips = tmp_path / "clips"
    clips.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update(
        BROWSE=str(browse),
        BROWSE_LOG=str(tmp_path / "calls.log"),
        POSTS_JSON=str(posts),
        X_POST_CLIP_DIR=str(clips),
        X_POST_SETTLE_SECONDS="0",
    )
    env.update(extra)
    return env


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_mk_builds_main_world_paste(tmp_path):
    posts = _posts(tmp_path, 'Quote "here"\n@PalmerLuckey')
    result = subprocess.run(
        [sys.executable, str(MK_PY), "clip_06"],
        env={**os.environ, "POSTS_JSON": str(posts)},
        capture_output=True, text=True, check=True,
    )
    request = json.loads(result.stdout)
    assert request["world"] == "main"
    assert "ClipboardEvent" in request["expression"]
    assert json.dumps('Quote "here"\n@PalmerLuckey') in request["expression"]


def test_mk_rejects_non_string_text(tmp_path):
    posts = _posts(tmp_path, {"text": "not a plain string"})
    result = subprocess.run(
        [sys.executable, str(MK_PY), "clip_06"],
        env={**os.environ, "POSTS_JSON": str(posts)},
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "plain string" in result.stderr


def test_dry_run_fills_composer_without_clicking(tmp_path):
    env = _env(tmp_path, _posts(tmp_path), X_POST_DRY_RUN="1", X_POST_TAB_PREFIX="Test_Run")
    (Path(env["X_POST_CLIP_DIR"]) / "clip_06.mp4").write_bytes(b"video")
    result = subprocess.run(["bash", str(POST_SH), "clip_06"], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert any(call.startswith("upload") for call in calls)
    assert not any(call.startswith("click") for call in calls)
    assert '"id":"test-run-clip-06"' in calls[0]


def test_explicit_run_clicks_post(tmp_path):
    env = _env(tmp_path, _posts(tmp_path))
    (Path(env["X_POST_CLIP_DIR"]) / "clip_06.mp4").write_bytes(b"video")
    result = subprocess.run(["bash", str(POST_SH), "clip_06"], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert _calls(tmp_path)[-1].startswith("click")
    assert "posted-click" in result.stdout


def test_short_text_stops_before_click(tmp_path):
    env = _env(tmp_path, _posts(tmp_path))
    browse = Path(env["BROWSE"])
    browse.write_text(FAKE_BROWSE.replace('echo "250"', 'echo "12"'), encoding="utf-8")
    (Path(env["X_POST_CLIP_DIR"]) / "clip_06.mp4").write_bytes(b"video")
    result = subprocess.run(["bash", str(POST_SH), "clip_06"], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert "text-missing" in result.stderr
    assert not any(call.startswith("click") for call in _calls(tmp_path))


def test_oversized_clip_is_rejected_before_browser_work(tmp_path):
    env = _env(tmp_path, _posts(tmp_path))
    with open(Path(env["X_POST_CLIP_DIR"]) / "clip_06.mp4", "wb") as handle:
        handle.truncate(52428801)
    result = subprocess.run(["bash", str(POST_SH), "clip_06"], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert "too-large" in result.stderr
    assert _calls(tmp_path) == []


def test_missing_clip_id_shows_usage(tmp_path):
    env = _env(tmp_path, _posts(tmp_path))
    result = subprocess.run(["bash", str(POST_SH)], env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert _calls(tmp_path) == []
