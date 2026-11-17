"""Face-aware portrait framing, with the detector and download stubbed (no network)."""
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

import engine
import faces

needs_ffmpeg = pytest.mark.skipif(
    not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='ffmpeg/ffprobe required')


def _walking_box(tmp_path):
    # Motion across the whole width: without faces, auto framing fits this shot.
    subprocess.run([
        'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=gray:s=320x180:r=12:d=6',
        '-f', 'lavfi', '-i', 'color=c=white:s=24x40:r=12:d=6',
        '-filter_complex', "[0][1]overlay=x='20+t*45':y='40+mod(t*80,60)':eval=frame",
        '-pix_fmt', 'yuv420p', 'source.mp4'], check=True)
    return str(tmp_path / 'source.mp4')


@needs_ffmpeg
def test_detected_faces_are_followed_instead_of_fitting_wide_motion(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('CONTENT_ENGINE_HOME', str(tmp_path / 'output'))
    path = _walking_box(tmp_path)
    source = engine._probe_video(path)
    assert engine._auto_crop_shots(path, 0, 6, source, faces=False)[0]['mode'] == 'fit'
    # A face that walks from 0.3 to 0.8 of the width.
    votes = [(i / 3, 0.3 + 0.5 * i / 18) for i in range(18)]
    monkeypatch.setattr(faces, 'try_face_votes', lambda *a: (votes, None))
    [shot] = engine._auto_crop_shots(path, 0, 6, source)
    assert (shot['mode'], shot['subject']) == ('crop', 'face')
    assert len(shot['path']) >= 3 and shot['path'][-1][1] > 0.6
    [result] = engine.cut_clips(path, [{'label': 'f', 'start': 0, 'end': 6, 'orientation': 'v'}])
    assert result['error'] is None and result['crop_shots'][0]['subject'] == 'face'
    [off] = engine.cut_clips(path, [{'label': 'o', 'start': 0, 'end': 6, 'orientation': 'v'}], faces='off')
    assert off['crop_shots'][0]['subject'] == 'none'


@needs_ffmpeg
def test_occasional_face_on_a_still_slide_keeps_it_fitted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('CONTENT_ENGINE_HOME', str(tmp_path / 'output'))
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=320x180:r=10:d=4',
                    '-pix_fmt', 'yuv420p', 'slide.mp4'], check=True)
    source = engine._probe_video(str(tmp_path / 'slide.mp4'))
    monkeypatch.setattr(faces, 'try_face_votes', lambda *a: ([(t / 3, 0.2) for t in range(4)], None))
    assert engine._auto_crop_shots(str(tmp_path / 'slide.mp4'), 0, 4, source)[0]['mode'] == 'fit'


@needs_ffmpeg
def test_detector_failure_falls_back_to_motion_with_a_warning(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('CONTENT_ENGINE_HOME', str(tmp_path / 'output'))
    path = _walking_box(tmp_path)
    monkeypatch.setenv('CONTENT_ENGINE_FACES', 'on')
    monkeypatch.setattr(faces, 'available', lambda: True)
    monkeypatch.setattr(faces, 'face_votes', lambda *a, **k: (_ for _ in ()).throw(OSError('offline')))
    [result] = engine.cut_clips(path, [{'label': 'w', 'start': 0, 'end': 6, 'orientation': 'v'}])
    assert result['error'] is None and 'offline' in result['warning']
    with pytest.raises(ValueError, match='faces'):
        engine.cut_clips(path, [{'start': 0, 'end': 1, 'orientation': 'v'}], faces='maybe')


def test_faces_can_be_disabled_by_environment(monkeypatch):
    monkeypatch.setenv('CONTENT_ENGINE_FACES', 'off')
    assert faces.available() is False
    assert faces.try_face_votes('x', 0, 1, {}) == (None, None)


class _Response:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        return self.data[:n]


def test_model_download_is_verified_and_cached(tmp_path, monkeypatch):
    monkeypatch.setenv('CONTENT_ENGINE_HOME', str(tmp_path))
    good = b'model bytes'
    monkeypatch.setattr(faces, '_model_spec', lambda: ('c' * 40, 'yunet.onnx', hashlib.sha256(good).hexdigest()))
    calls = []

    class Opener:
        def __init__(self, data):
            self.data = data

        def open(self, url, timeout):
            calls.append(url)
            assert url.startswith('https://media.githubusercontent.com/media/opencv/opencv_zoo/')
            return _Response(self.data)

    monkeypatch.setattr(faces.urllib.request, 'build_opener', lambda *h: Opener(b'tampered'))
    with pytest.raises(ValueError, match='checksum'):
        faces.model_path()
    assert not (tmp_path / 'models' / 'yunet.onnx').exists()
    monkeypatch.setattr(faces.urllib.request, 'build_opener', lambda *h: Opener(good))
    path = faces.model_path()
    assert Path(path).read_bytes() == good
    faces.model_path()  # cached: no second download
    assert len(calls) == 2


def test_model_redirects_must_stay_on_github():
    handler = faces._GitHubOnly()
    with pytest.raises(ValueError, match='outside GitHub'):
        handler.redirect_request(None, None, 302, '', {}, 'https://example.com/model.onnx')
