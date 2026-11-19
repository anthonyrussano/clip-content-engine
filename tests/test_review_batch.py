"""A reusable review workflow must protect editorial edits and produce playable local artifacts."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

import ce_cli
import review_batch


def write(path, data):
    path.write_text(json.dumps(data))
    return str(path)


def test_prepare_orders_clips_and_preserves_editorial_edits(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CONTENT_ENGINE_HOME", str(tmp_path))
    scored = write(tmp_path / "scored.json", {"clips": [
        {"id": "C2", "start": 50, "end": 80}, {"id": "C1", "start": 0, "end": 30},
    ]})
    assert ce_cli.main(["prepare", scored, "--out-dir", "run"]) == 0
    result = json.loads(capsys.readouterr().out)
    path = Path(result["selections_path"])
    selections = json.loads(path.read_text())
    assert [c["candidate_id"] for c in selections] == ["C1", "C2"]
    assert all(c["orientation"] == "v" for c in selections)
    path.write_text("edited by a person")
    with pytest.raises(ValueError, match="preserve your edits"):
        review_batch.prepare(scored, "run")
    assert path.read_text() == "edited by a person"


@pytest.mark.parametrize("clip", [None, {"start": True, "end": 4},
                                  {"start": 2, "end": 1}, {"start": 0, "end": float("nan")}])
def test_prepare_rejects_invalid_ranges_before_writing(tmp_path, monkeypatch, clip):
    monkeypatch.setenv("CONTENT_ENGINE_HOME", str(tmp_path))
    scored = write(tmp_path / "scored.json", {"clips": [clip]})
    with pytest.raises(ValueError):
        review_batch.prepare(scored, "run")
    assert not (tmp_path / "run/selections.json").exists()


def test_gallery_refuses_external_media_and_stale_selection_bounds(tmp_path, monkeypatch):
    base = tmp_path / "workspace"
    base.mkdir()
    monkeypatch.setenv("CONTENT_ENGINE_HOME", str(base))
    media = tmp_path / "outside.mp4"
    media.touch()
    selections = write(base / "selections.json", [{"label": "one", "start": 0, "end": 1}])
    row = {"label": "one", "start": 0, "end": 1, "output": str(media), "error": None}
    renders = write(base / "renders.json", [row])
    with pytest.raises(ValueError, match="inside CONTENT_ENGINE_HOME"):
        review_batch.gallery(renders, selections, "review")
    media = base / "clip.mp4"
    media.touch()
    row.update(output=str(media), end=2)
    write(base / "renders.json", [row])
    with pytest.raises(ValueError, match="render the edited selections again"):
        review_batch.gallery(renders, selections, "review")
    assert not (base / "review/gallery.html").exists()


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="media tools required")
def test_gallery_supports_other_counts_verifies_media_and_escapes_titles(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CONTENT_ENGINE_HOME", str(tmp_path))
    media = tmp_path / "clip #1.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x96:r=5:d=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(media)], check=True)
    rows = [{"label": label, "start": i, "end": i + 1, "output": str(media), "error": None}
            for i, label in enumerate(("one", "two"))]
    selected = [{"label": row["label"], "start": row["start"], "end": row["end"],
                 "title": '<script>alert("no")</script>'} for row in rows]
    renders, selections = write(tmp_path / "renders.json", rows), write(tmp_path / "selections.json", selected)
    assert ce_cli.main(["gallery", "--renders-file", renders, "--clips-file", selections,
                        "--out-dir", "review", "--verify"]) == 0
    result = json.loads(capsys.readouterr().out)
    page = Path(result["gallery_path"]).read_text()
    assert result["clip_count"] == 2 and page.count("<video ") == 2
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "clip%20%231.mp4" in page
    report = json.loads(Path(result["report_path"]).read_text())
    assert all(c["full_decode"] and Path(c["review_sheet"]).is_file() for c in report["clips"])
    assert report["visual_review_completed"] is False
