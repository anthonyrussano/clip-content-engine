"""Coverage and complete model inputs for long recordings."""
import json

import pytest

import ce_api
import ce_cli
import engine
import llm_ranking


def test_long_form_preset_and_explicit_overrides():
    parser = ce_cli.build_parser()
    args = parser.parse_args(["score", "transcript.json", "--preset", "long-form"])
    options = ce_cli._score_kwargs(args)
    assert {k: options[k] for k in ("count", "min_duration", "max_duration", "stride")} == {
        "count": 12, "min_duration": 30, "max_duration": 90, "stride": 30,
    }
    args = parser.parse_args(["score", "transcript.json", "--preset", "long-form",
                              "--count", "20", "--stride", "15"])
    assert ce_cli._score_kwargs(args)["count"] == 20
    assert ce_cli._score_kwargs(args)["stride"] == 15
    standard = ce_cli._score_kwargs(parser.parse_args(["score", "transcript.json"]))
    assert [standard[k] for k in ("count", "min_duration", "max_duration", "stride")] == [6, 45, 60, 60]


def test_capped_pool_cannot_be_consumed_by_one_keyword_rich_section(monkeypatch):
    candidates = [{"id": f"C{i}", "start": i * 60, "end": i * 60 + 30,
                   "duration": 30, "text": "high" if i < 15 else "low",
                   "first": "", "last": ""} for i in range(60)]
    monkeypatch.setattr(engine, "_make_candidates", lambda *args: candidates)
    monkeypatch.setattr(engine, "_score_candidate", lambda text, *args: {
        "total": 20 if text == "high" else 5,
    })
    stats = {}
    pool = llm_ranking._candidates([], 30, 90, 30, 12, 4, stats)
    assert len(pool) == 4
    assert {int(c["start"] // 900) for c in pool} == {0, 1, 2, 3}
    assert stats["generated_candidate_count"] == 60
    assert stats["distinct_candidate_count"] == 60
    assert stats["pool_limited"] is True
    assert stats["pool_sampling"] == "timeline_bands"


def test_model_receives_the_payoff_beyond_the_old_text_limit(monkeypatch):
    text = "A story unfolds. " * 160 + "The payoff must stay visible."
    seen = []

    def chat(system, user, *args):
        seen.extend(json.loads(user)["candidates"])
        return {"scores": [{"id": "C01", "hook": 4, "completeness": 4,
                            "standalone": 4, "visual": 3}]}

    monkeypatch.setattr(llm_ranking, "_chat", chat)
    llm_ranking._scores([{"id": "C01", "start": 0, "end": 90, "text": text}],
                       "local", "http://127.0.0.1:8080/v1", None, False, 10)
    assert seen[0]["transcript"] == text


def test_large_inputs_split_batches_without_truncation():
    pool = [{"id": str(i), "text": "x" * 7000} for i in range(4)]
    batches = list(llm_ranking._score_batches(pool, 12))
    assert [len(b) for b in batches] == [3, 1]
    assert [c for batch in batches for c in batch] == pool
    with pytest.raises(ValueError, match="reduce max_duration"):
        list(llm_ranking._score_batches([{"text": "x" * 24001}], 12))


def test_selection_reports_shortfalls_instead_of_padding():
    segments = [{"start": i, "duration": 5, "text": "One useful sentence."}
                for i in range(0, 40, 5)]
    clips, scoring = ce_api.score_clips(segments, count=12, min_duration=30,
                                       max_duration=90, stride=30)
    assert len(clips) == 1
    assert scoring["selection"]["requested_count"] == 12
    assert scoring["selection"]["returned_count"] == 1


def test_empty_model_pool_reports_zero_coverage_without_a_request(monkeypatch):
    def unexpected_request(*args):
        raise AssertionError("An empty transcript must not reach the model")

    monkeypatch.setattr(llm_ranking, "_chat", unexpected_request)
    clips, scoring = ce_api.score_clips([], count=12, llm_model="local", llm_required=True)
    assert clips == []
    assert scoring["generated_candidate_count"] == scoring["distinct_candidate_count"] == 0
    assert scoring["candidate_count"] == scoring["selection"]["returned_count"] == 0
