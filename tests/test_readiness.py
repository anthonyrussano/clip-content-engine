"""Regression checks for fetch isolation, invalid inputs and real rendering."""
import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

import ce_api
import ce_cli
import engine
import llm_ranking


@pytest.mark.parametrize('segments', [
    [{'start': -1, 'duration': 1, 'text': 'bad'}],
    [{'start': 0, 'duration': float('nan'), 'text': 'bad'}],
    [{'start': 0, 'duration': 1, 'text': None}],
    [{'start': 2, 'duration': 1, 'text': 'a'}, {'start': 1, 'duration': 1, 'text': 'b'}],
    [None],
])
def test_invalid_transcripts_fail_before_ranking(segments):
    with pytest.raises(ValueError):
        ce_api.score_clips(segments)


@pytest.mark.parametrize('kwargs', [
    {'stride': 0}, {'stride': float('nan')}, {'max_duration': float('inf')},
    {'count': 0}, {'count': True},
])
def test_invalid_scoring_options(kwargs):
    with pytest.raises(ValueError):
        engine.score_moments([], **kwargs)


def test_srt_rounding_carries_to_next_minute():
    assert engine._fmt_srt_time(59.9996) == '00:01:00,000'
    assert engine._fmt_srt_time(3599.9996) == '01:00:00,000'


def test_fetch_failure_cannot_reuse_existing_captions(tmp_path, monkeypatch):
    monkeypatch.setenv('CONTENT_ENGINE_HOME', str(tmp_path))
    monkeypatch.setattr(engine, '_ytdlp', lambda: 'yt-dlp')
    work = tmp_path / 'reuse'
    work.mkdir()
    (work / 'transcript.en.vtt').write_text('WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nOld video\n')
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if '--dump-single-json' in args:
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                'id': 'gmEj_z17wVo', 'title': 'Current video', 'channel': 'Test',
                'duration_string': '1:00', 'subtitles': {'en-US': []},
            }), stderr='')
        return SimpleNamespace(returncode=1, stdout='', stderr='network failed')

    monkeypatch.setattr(engine.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='network failed'):
        engine.fetch_transcript('https://youtu.be/gmEj_z17wVo', out_dir='reuse')
    assert '--no-playlist' in calls[0] and '--no-playlist' in calls[1]
    assert calls[1][calls[1].index('--sub-lang') + 1] == 'en-US'
    assert not (work / 'transcript.json').exists()


def test_cli_timeout_is_clean_error(monkeypatch, capsys):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('yt-dlp', 120)
    monkeypatch.setattr(ce_api, 'fetch_summary', timeout)
    assert ce_cli.main(['fetch', 'https://youtu.be/gmEj_z17wVo']) == 1
    assert 'timed out' in capsys.readouterr().err


def test_cli_render_failure_sets_exit_status(monkeypatch, capsys, tmp_path):
    clips = tmp_path / 'clips.json'
    clips.write_text('[{"start": 0, "end": 1}]')
    monkeypatch.setattr(engine, 'cut_clips', lambda *a, **k: [{'output': None, 'error': 'encode failed'}])
    assert ce_cli.main(['cut', 'source.mp4', '--clips-file', str(clips)]) == 1
    assert json.loads(capsys.readouterr().out)[0]['error'] == 'encode failed'


@pytest.mark.parametrize('response', ['redirect', [], {'scores': [None]}])
def test_llm_http_failures_fall_back_without_following_redirects(response):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.path)
            self.rfile.read(int(self.headers['Content-Length']))
            if response == 'redirect':
                self.send_response(307)
                self.send_header('Location', '/leak')
            else:
                self.send_response(200)
            self.end_headers()
            if response != 'redirect':
                self.wfile.write(json.dumps({'choices': [{'message': {
                    'content': json.dumps(response),
                }}]}).encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        segments = [{'start': i, 'duration': 5, 'text': 'A complete thought because context matters.'}
                    for i in range(0, 120, 5)]
        clips, state = llm_ranking.rank_moments(
            segments, model='test', base_url=f'http://127.0.0.1:{server.server_port}/v1',
        )
        assert clips and state['mode'] == 'heuristic_fallback'
        # A redirect fails at once; a malformed answer is retried once.
        assert requests == ['/v1/chat/completions'] * (1 if response == 'redirect' else 2)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_fetch_success_prefers_manual_english_and_returns_current_text(tmp_path, monkeypatch):
    monkeypatch.setenv('CONTENT_ENGINE_HOME', str(tmp_path))
    monkeypatch.setattr(engine, '_ytdlp', lambda: 'yt-dlp')

    def run(args, **kwargs):
        if '--dump-single-json' in args:
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                'id': 'gmEj_z17wVo', 'title': 'Title\nwith newline', 'channel': 'Test',
                'duration_string': '1:00', 'subtitles': {'en-US': [{}]},
                'automatic_captions': {'en': [{}]},
            }), stderr='')
        assert args[args.index('--sub-lang') + 1] == 'en-US'
        target = args[args.index('-o') + 1]
        Path(target + '.en-US.vtt').write_text(
            'WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nCurrent caption\n')
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(engine.subprocess, 'run', run)
    result = engine.fetch_transcript('https://youtu.be/gmEj_z17wVo', out_dir='fresh')
    assert result['title'] == 'Title\nwith newline'
    assert json.loads(Path(result['transcript_path']).read_text())[0]['text'] == 'Current caption'


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='ffmpeg/ffprobe required')
def test_real_render_relative_source_portrait_and_landscape(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('CONTENT_ENGINE_HOME', str(tmp_path / 'output'))
    subprocess.run([
        'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=160x240:r=12',
        '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '1', '-c:v', 'libx264',
        '-pix_fmt', 'yuv420p', '-c:a', 'aac', 'source.mp4',
    ], check=True, capture_output=True)
    segments = [{'start': 0, 'duration': 1, 'text': 'Captions stay inside the frame.'}]
    results = engine.cut_clips('source.mp4', [
        {'label': 'portrait', 'start': 0, 'end': 1, 'orientation': 'v'},
        {'label': 'landscape', 'start': 0, 'end': 1, 'orientation': 'h'},
    ], segments=segments)
    for result, size in zip(results, [(1080, 1920), (1920, 1080)]):
        assert result['error'] is None, result['error']
        probe = subprocess.run([
            'ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', result['output'],
        ], check=True, capture_output=True, text=True)
        info = json.loads(probe.stdout)
        video = next(s for s in info['streams'] if s['codec_type'] == 'video')
        assert (video['width'], video['height']) == size
        assert video['pix_fmt'] == 'yuv420p'
        assert any(s['codec_type'] == 'audio' for s in info['streams'])
        assert abs(float(info['format']['duration']) - 1) < .2
        assert result['subs'] == 1
    with pytest.raises(ValueError, match='exceeds source duration'):
        engine.cut_clips('source.mp4', [{'start': 4, 'end': 5}])
    with pytest.raises(ValueError, match='unique'):
        engine.cut_clips('source.mp4', [{'start': 0, 'end': 1}] * 2)
