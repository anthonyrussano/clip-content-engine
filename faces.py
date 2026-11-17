"""Optional face detection for portrait framing.

Install with ``pip install '.[faces]'`` (opencv-python-headless). The YuNet
face detector model (MIT license, OpenCV Zoo) is downloaded once from a
pinned commit, verified against its published SHA-256, and cached in the
work dir. Detection runs locally on small decoded frames.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:
    from . import engine
except ImportError:  # standalone module install
    import engine

# OpenCV major version -> (opencv_zoo commit, file name, SHA-256 from its Git LFS pointer).
# OpenCV 5's ONNX Runtime engine needs the dynamic-shape 2026 export; OpenCV 4 uses 2023mar.
MODELS = {
    5: ("26cc381e4d2594bb9f47a26eb8fd96c94a13660d", "face_detection_yunet_2026may.onnx",
        "ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0"),
    4: ("f12e12798e8314f7c074a6656816c048dcc95b7a", "face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
}
_URL = "https://media.githubusercontent.com/media/opencv/opencv_zoo/{commit}/models/face_detection_yunet/{name}"
_ALLOWED_HOSTS = {"media.githubusercontent.com", "objects.githubusercontent.com",
                  "github-cloud.githubusercontent.com"}
_MAX_MODEL_BYTES = 2 * 1024 * 1024
FRAME_WIDTH = 640           # detection resolution; YuNet finds faces from about 10 px wide
SCORE_THRESHOLD = 0.7


def available() -> bool:
    """True when OpenCV with the YuNet detector is installed and not disabled."""
    if os.environ.get("CONTENT_ENGINE_FACES", "").lower() in ("0", "off", "false", "no"):
        return False
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return False
    return hasattr(cv2, "FaceDetectorYN")


def _model_spec() -> Tuple[str, str, str]:
    import cv2

    major = int(cv2.__version__.split(".")[0])
    return MODELS[5] if major >= 5 else MODELS[4]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class _GitHubOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme != "https" or urlparse(newurl).hostname not in _ALLOWED_HOSTS:
            raise ValueError("Model download redirected outside GitHub")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def model_path(timeout: float = 60) -> str:
    """Return the cached, verified YuNet model, downloading it on first use."""
    commit, name, digest = _model_spec()
    folder = engine._plugin_work_dir() / "models"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / name
    if dest.is_file() and not dest.is_symlink() and _sha256(dest) == digest:
        return str(dest)
    url = _URL.format(commit=commit, name=name)
    with urllib.request.build_opener(_GitHubOnly()).open(url, timeout=timeout) as response:
        data = response.read(_MAX_MODEL_BYTES + 1)
    if len(data) > _MAX_MODEL_BYTES:
        raise ValueError("Face model download is unexpectedly large")
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Face model checksum mismatch; refusing to use it")
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".download-")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    return str(dest)


def face_votes(video_path: str, start: float, dur: float, source: Dict[str, Any],
               fps: int = 3) -> List[Tuple[float, float]]:
    """(time from clip start, horizontal centre 0-1) of the largest face per sampled frame."""
    import cv2
    import numpy as np

    w, h = source.get("width") or 0, source.get("height") or 0
    if w <= 0 or h <= 0:
        return []
    width = FRAME_WIDTH
    height = max(2, int(round(width * h / w / 2)) * 2)
    detector = cv2.FaceDetectorYN.create(model_path(), "", (width, height), SCORE_THRESHOLD, 0.3, 50)
    r = subprocess.run(
        [engine._ffmpeg(), "-v", "error", "-nostdin", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
         "-i", video_path, "-an", "-vf", f"fps={fps},scale={width}:{height}",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        capture_output=True, timeout=max(60, dur * 4),
    )
    if r.returncode != 0:
        return []
    size = width * height * 3
    votes: List[Tuple[float, float]] = []
    for i, k in enumerate(range(0, len(r.stdout) - size + 1, size)):
        frame = np.frombuffer(r.stdout, dtype=np.uint8, count=size, offset=k).reshape(height, width, 3)
        _, faces = detector.detect(frame)
        if faces is None or not len(faces):
            continue
        # The largest face is usually the speaker; audience heads are smaller.
        x, _, fw, _ = max(faces, key=lambda f: float(f[2]) * float(f[3]))[:4]
        votes.append((round(i / fps, 3), float((x + fw / 2) / width)))
    return votes


def try_face_votes(video_path: str, start: float, dur: float,
                   source: Dict[str, Any]) -> Tuple[Optional[List[Tuple[float, float]]], Optional[str]]:
    """Face votes, or (None, reason) when detection is unavailable or fails."""
    if not available():
        return None, None
    try:
        return face_votes(video_path, start, dur, source), None
    except Exception as exc:  # detection is an optional aid; framing falls back to motion
        return None, f"face detection unavailable: {exc}"[:300]
