"""Prepare editable selections and a local, self-contained clip review gallery."""
from __future__ import annotations

import html
import json
import math
import os
from pathlib import Path
import subprocess
from urllib.parse import quote

import engine


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _bounds(row):
    if not isinstance(row, dict):
        raise ValueError("Clip entries must be objects")
    start, end = row.get("start"), row.get("end")
    if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (start, end))
            or not 0 <= start < end):
        raise ValueError("Selections need finite 0 <= start < end")
    return start, end


def _output(folder, name):
    path = folder / name
    if path.is_symlink() or not path.resolve().is_relative_to(folder):
        raise ValueError("Review outputs must not be symlinks or escape the work directory")
    return path


def prepare(scored_file, out_dir=None):
    """Write selections once, protecting subsequent editorial changes."""
    data = _read(scored_file)
    clips = data.get("clips") if isinstance(data, dict) else None
    if not isinstance(clips, list) or not clips:
        raise ValueError("Scored file must contain a nonempty clips array")
    selections = []
    for i, clip in enumerate(sorted(clips, key=lambda c: _bounds(c)[0]), 1):
        start, end = _bounds(clip)
        label = f"clip_{i:02d}"
        selections.append({"label": label, "title": label, "start": start, "end": end,
                           "orientation": "v", "candidate_id": clip.get("id", "")})
    folder = Path(engine._resolve_out_dir(out_dir))
    path = _output(folder, "selections.json")
    try:
        with path.open("x", encoding="utf-8") as f:
            json.dump(selections, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except FileExistsError:
        raise ValueError("selections.json already exists; preserve your edits or choose another out-dir") from None
    return {"selections_path": str(path), "clip_count": len(selections)}


def _run(args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Review media check failed: " + result.stderr[-400:])


def gallery(renders_file, clips_file, out_dir=None, verify=False):
    """Build posters, four-frame contact sheets and native video players for any batch size."""
    renders, selections = _read(renders_file), _read(clips_file)
    if not isinstance(renders, list) or not renders or not isinstance(selections, list):
        raise ValueError("Render results and selections must be nonempty arrays")
    base = engine._plugin_work_dir()
    plan, labels = [], set()
    for row in renders:
        _bounds(row)
        if not isinstance(row.get("label"), str):
            raise ValueError("Render labels must be strings")
        label = engine._safe_label(row.get("label"))
        if label in labels or row.get("error") or not row.get("output"):
            raise ValueError("Every render needs a unique label and a successful output")
        labels.add(label)
        path = Path(row["output"]).expanduser().resolve(strict=True)
        if not path.is_relative_to(base):
            raise ValueError("Gallery media must be inside CONTENT_ENGINE_HOME")
        plan.append((row, path))
    by_label = {}
    for selection in selections:
        _bounds(selection)
        if not isinstance(selection.get("label"), str):
            raise ValueError("Selection labels must be strings")
        label = engine._safe_label(selection.get("label"))
        _bounds(selection)
        if label in by_label:
            raise ValueError("Selection labels must be unique")
        by_label[label] = selection
    if set(by_label) != labels:
        raise ValueError("Selection labels must match the rendered batch")
    for row, _ in plan:
        if any(abs(a - b) > 0.001 for a, b in zip(_bounds(row), _bounds(by_label[row["label"]]))):
            raise ValueError("Selection bounds changed; render the edited selections again")
    folder = Path(engine._resolve_out_dir(out_dir))
    assets = Path(engine._resolve_out_dir(str(folder / "review")))
    page, report = _output(folder, "gallery.html"), _output(folder, "review-report.json")
    ffmpeg = engine._ffmpeg()
    cards, checks = [], []
    for row, path in plan:
        label = row["label"]
        selection = by_label[label]
        probe = engine._probe_video(str(path))
        duration = probe["duration"]
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Rendered media needs a finite positive duration")
        poster, sheet = _output(assets, label + ".jpg"), _output(assets, label + "-review.jpg")
        scale = "scale=270:480:force_original_aspect_ratio=decrease,pad=270:480:(ow-iw)/2:(oh-ih)/2"
        _run([ffmpeg, "-v", "error", "-y", "-ss", str(duration * 0.4), "-i", str(path),
              "-vf", scale, "-frames:v", "1", str(poster)])
        _run([ffmpeg, "-v", "error", "-y", "-threads", "2", "-i", str(path),
              "-vf", f"fps={4 / duration},{scale},tile=4x1", "-frames:v", "1", str(sheet)])
        if verify:
            _run([ffmpeg, "-v", "error", "-threads", "2", "-i", str(path), "-f", "null", "-"])
        title = html.escape(str(selection.get("title") or label), quote=True)
        media_url = quote(Path(os.path.relpath(path, folder)).as_posix(), safe="/")
        image_url = quote(poster.relative_to(folder).as_posix(), safe="/")
        start, end = _bounds(selection)
        stamp = lambda t: f"{int(t) // 60:02}:{t % 60:05.2f}"
        cards.append(f'<article><video controls preload="none" playsinline poster="{image_url}" aria-label="{title}">'
                     f'<source src="{media_url}" type="video/mp4"></video><div><h2>{title}</h2>'
                     f'<p>{duration:.0f} seconds · Source {stamp(start)}–{stamp(end)}</p>'
                     f'<a href="{media_url}">Open video</a></div></article>')
        checks.append({"label": label, "duration": round(duration, 2), "width": probe["width"],
                       "height": probe["height"], "full_decode": verify, "review_sheet": str(sheet),
                       "framing_subjects": [s.get("subject") for s in row.get("crop_shots", [])]})
    style = ("body{margin:0;background:#12161c;color:#edf0f4;font:16px system-ui,sans-serif}"
             "main{max-width:1140px;margin:auto;padding:36px 24px}.grid{display:grid;"
             "grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:24px}"
             "article{background:#202731;border:1px solid #36404f;border-radius:12px;overflow:hidden}"
             "video{display:block;width:100%;aspect-ratio:9/16;max-height:560px;background:black}"
             "article div{padding:18px}h2{font-size:19px}p{color:#bbc4d0}a{color:#99c7ff}"
             "a:focus-visible,video:focus-visible{outline:3px solid #99c7ff;outline-offset:3px}")
    page.write_text('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<title>Clip review</title><style>' + style + '</style></head><body><main>'
                    f'<h1>Clip review</h1><p>{len(plan)} clips · Review previews before publishing.</p>'
                    '<section class="grid">' + ''.join(cards) + '</section></main></body></html>', encoding="utf-8")
    report.write_text(json.dumps({"clips": checks, "review_frames_per_clip": 4,
                                  "visual_review_completed": False}, indent=2) + "\n", encoding="utf-8")
    return {"gallery_path": str(page), "report_path": str(report), "clip_count": len(plan)}
