"""Offline coverage for CLI, LLM fallback and agent-neutral pipeline."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import ce_api
import ce_cli
import engine
import llm_ranking


@pytest.fixture
def segments():
    return [
        {
            "start": float(i), "duration": 5.0,
            "text": f"In 1947 John Smith showed evidence number {i} because it mattered."
        }
        for i in range(0, 600, 5)
    ]


def test_standalone_directory_is_not_hermes(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("CONTENT_ENGINE_HOME", str(tmp_path / "engine-home"))
    assert engine._plugin_work_dir() == (tmp_path / "engine-home").resolve()
    assert Path(engine._resolve_out_dir("clips")).is_relative_to(engine._plugin_work_dir())
    with pytest.raises(ValueError):
        engine._resolve_out_dir("../../outside")


def test_llm_rank_heuristic_is_offline(segments, monkeypatch):
    def must_not_call(*args, **kwargs):
        raise AssertionError("LLM was called without a model")
    monkeypatch.setattr(llm_ranking, "_scores", must_not_call)
    clips, state = llm_ranking.rank_moments(segments, count=3)
    assert state == {"mode": "heuristic"}
    assert 0 < len(clips) <= 3


def test_llm_selects_non_overlapping_clips(segments, monkeypatch):
    def fake_scores(pool, model, url, api_key, allow_remote, timeout):
        return {
            c["id"]: {"hook": 5, "completeness": 4, "standalone": 4,
                      "visual": 3, "reason": "Concise self-contained explanation"}
            for c in pool
        }
    monkeypatch.setattr(llm_ranking, "_scores", fake_scores)
    clips, state = llm_ranking.rank_moments(segments, count=4, model="test-model")
    assert state["mode"] == "llm"
    assert state["visual_evidence"] == "transcript_only"
    assert clips
    assert all(4 <= c["scores"]["hook"] <= 5 for c in clips)
    assert all(c["scoring_source"] == "llm" for c in clips)
    for i, a in enumerate(clips):
        for b in clips[i + 1:]:
            assert a["end"] <= b["start"] or b["end"] <= a["start"]


def test_invalid_llm_output_falls_back_and_required_fails(segments, monkeypatch):
    def bad(*args, **kwargs):
        raise ValueError("bad model JSON")
    monkeypatch.setattr(llm_ranking, "_scores", bad)
    clips, state = llm_ranking.rank_moments(segments, model="test-model")
    assert clips
    assert state["mode"] == "heuristic_fallback"
    assert "bad model JSON" in state["warning"]
    with pytest.raises(RuntimeError, match="LLM scoring failed"):
        llm_ranking.rank_moments(segments, model="test-model", required=True)


def test_remote_llm_requires_explicit_opt_in():
    assert llm_ranking._endpoint("http://127.0.0.1:1234/v1", False).endswith("/chat/completions")
    with pytest.raises(ValueError):
        llm_ranking._endpoint("https://models.example/v1", False)
    with pytest.raises(ValueError):
        llm_ranking._endpoint("http://models.example/v1", True)
    assert llm_ranking._endpoint("https://models.example/v1", True).endswith("/chat/completions")


def test_load_segments_checks_shape(tmp_path, segments):
    p = tmp_path / "transcript.json"
    p.write_text(json.dumps(segments), encoding="utf-8")
    assert ce_api.load_segments(str(p)) == segments
    p.write_text('{"segments": "bad"}', encoding="utf-8")
    with pytest.raises(ValueError):
        ce_api.load_segments(str(p))


def test_run_pipeline_works_without_hermes(monkeypatch, segments):
    monkeypatch.setattr(engine, "fetch_transcript", lambda *a, **kw: {
        "video_id": "aircAruvnKk", "title": "Demo", "channel": "Demo channel",
        "duration": "10:00", "segment_count": len(segments),
        "transcript_path": "/tmp/transcript.json", "transcript_txt": "/tmp/transcript.txt",
        "work_dir": "/tmp", "segments": segments,
    })
    result = ce_api.run_pipeline("https://youtu.be/aircAruvnKk", count=2)
    assert result["video"]["title"] == "Demo"
    assert result["scoring"]["mode"] == "heuristic"
    assert len(result["clips"]) == 2
    assert set(result["copy"]) == {c["id"] for c in result["clips"]}


def test_cli_copy_smoke(capsys):
    code = ce_cli.main(["copy", "--text",
                        "In 1947 John Smith uncovered a remarkable discovery.",
                        "--url", "https://youtu.be/aircAruvnKk"])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert "https://youtu.be/aircAruvnKk" in result["x"]
    assert "Link in bio." in result["tiktok"]


def test_cli_score_offline(monkeypatch, segments, tmp_path, capsys):
    p = tmp_path / "transcript.json"
    p.write_text(json.dumps(segments), encoding="utf-8")
    assert ce_cli.main(["score", str(p), "--count", "2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["clips"]) == 2
    assert out["scoring"]["mode"] == "heuristic"


def test_mcp_exposes_server_instance():
    from mcp_server import mcp
    assert mcp is not None
