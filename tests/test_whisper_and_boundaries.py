"""Regression checks for Whisper transcription, sentence boundaries and portrait cropping."""
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import ce_api
import ce_cli
import engine
import transcribe

needs_ffmpeg = pytest.mark.skipif(
    not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='ffmpeg/ffprobe required')


def _words(text, start=0.0, step=0.3):
    out = []
    for i, w in enumerate(text.split()):
        out.append({'start': start + i * step, 'end': start + i * step + 0.25, 'word': ' ' + w})
    return out


def test_words_to_segments_never_cross_sentence_ends():
    words = _words('This is one. Here is a much longer second sentence that keeps going on and on '
                   'until it must wrap somewhere sensible. Done!')
    segments = transcribe.words_to_segments(words)
    joined = ' '.join(s['text'] for s in segments)
    assert joined.startswith('This is one. Here is')
    assert segments[0]['text'] == 'This is one.'
    assert all(len(s['text']) <= 60 for s in segments)
    assert segments[-1]['text'].endswith('Done!')
    engine.validate_segments(segments)


def test_words_to_segments_splits_on_long_pauses():
    words = _words('before the pause') + _words('after it', start=10)
    assert [s['text'] for s in transcribe.words_to_segments(words)] == ['before the pause', 'after it']


def test_zero_duration_segments_are_accepted_and_unsorted_files_are_sorted(tmp_path):
    engine.validate_segments([{'start': 1, 'duration': 0, 'text': 'blip'}])
    path = tmp_path / 't.json'
    path.write_text(json.dumps([{'start': 2, 'duration': 1, 'text': 'b'},
                                {'start': 1, 'duration': 1, 'text': 'a'}]))
    assert [s['text'] for s in ce_api.load_segments(str(path))] == ['a', 'b']


def _sentences(n=40, each=5.0):
    segs = []
    for i in range(n):
        # Each sentence spans two cues; only the second ends with punctuation.
        segs.append({'start': i * each, 'duration': each / 2, 'text': f'sentence {i} starts here and'})
        segs.append({'start': i * each + each / 2, 'duration': each / 2, 'text': 'then it ends.'})
    return segs


def test_candidates_start_and_end_on_sentence_boundaries():
    segments = _sentences()
    cands = engine._make_candidates(segments, 45, 60, 60)
    assert cands
    for c in cands:
        assert c['clean_start'] and c['clean_end'], c
        assert c['text'].startswith('sentence') and c['text'].endswith('.')
        assert c['first'].endswith('ends.')
    clips = engine.score_moments(segments, count=2)
    assert all(c['text'].endswith('.') for c in clips)


def test_unpunctuated_transcripts_keep_original_windows():
    segments = [{'start': i * 2.0, 'duration': 2.0, 'text': f'word {i} and more'} for i in range(100)]
    cands = engine._make_candidates(segments, 45, 60, 60)
    assert cands and all('clean_start' not in c for c in cands)


def test_auto_transcriber_falls_back_to_whisper(monkeypatch):
    def no_captions(url, out_dir=None):
        raise RuntimeError('No transcript found. HTTP Error 429')

    calls = []
    monkeypatch.setattr(engine, 'fetch_transcript', no_captions)
    monkeypatch.setattr(transcribe, 'transcribe_url',
                        lambda url, out_dir=None, model=None: calls.append(model) or {'segments': []})
    result = ce_api.get_transcript('https://youtu.be/gmEj_z17wVo', transcriber='auto', whisper_model='base.en')
    assert calls == ['base.en'] and '429' in result['caption_error']
    with pytest.raises(RuntimeError):
        ce_api.get_transcript('https://youtu.be/gmEj_z17wVo', transcriber='captions')
    with pytest.raises(ValueError):
        ce_api.get_transcript('https://youtu.be/gmEj_z17wVo', transcriber='bogus')


def test_transcribe_rejects_bad_options(tmp_path):
    media = tmp_path / 'a.wav'
    media.write_bytes(b'')
    pytest.importorskip('faster_whisper')
    for kwargs in ({'model': '../x'}, {'language': 'english'}, {'model': 'small.en', 'language': 'de'}):
        with pytest.raises(ValueError):
            transcribe.transcribe_file(str(media), **kwargs)


def test_download_media_moves_only_the_produced_file(tmp_path, monkeypatch):
    monkeypatch.setenv('CONTENT_ENGINE_HOME', str(tmp_path))
    monkeypatch.setattr(engine, '_ytdlp', lambda: 'yt-dlp')
    work = tmp_path / 'dl'
    work.mkdir()
    (work / 'audio.webm').write_text('stale')

    def run(args, **kwargs):
        target = args[args.index('-o') + 1].replace('%(ext)s', 'm4a')
        Path(target).with_suffix('.webm').write_text('intermediate audio')
        Path(target).write_text('new audio')
        info = {'id': 'gmEj_z17wVo', 'title': 'T', 'channel': 'C', 'duration_string': '1:00',
                'filepath': target}
        return SimpleNamespace(returncode=0, stdout=json.dumps(info) + '\n', stderr='')

    monkeypatch.setattr(transcribe.subprocess, 'run', run)
    result = transcribe.download_media('https://youtu.be/gmEj_z17wVo', out_dir='dl', audio_only=True)
    assert Path(result['path']).read_text() == 'new audio'
    assert sorted(p.name for p in work.iterdir()) == ['audio.m4a']


@pytest.mark.parametrize('returncode', [0, 1])
def test_empty_replay_download_preserves_existing_source(tmp_path, monkeypatch, returncode):
    monkeypatch.setenv('CONTENT_ENGINE_HOME', str(tmp_path))
    monkeypatch.setattr(engine, '_ytdlp', lambda: 'yt-dlp')
    work = tmp_path / 'replay'
    work.mkdir()
    source = work / 'source.mp4'
    source.write_bytes(b'previous download')

    def run(args, **kwargs):
        stage = Path(args[args.index('-o') + 1]).parent
        (stage / 'source.mp4').touch()
        return SimpleNamespace(returncode=returncode, stdout='',
                               stderr='ERROR: The downloaded file is empty' if returncode else '')

    monkeypatch.setattr(transcribe.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='listed DASH video and audio tracks'):
        transcribe.download_media('https://www.youtube.com/live/gmEj_z17wVo', out_dir='replay')
    assert source.read_bytes() == b'previous download'
    assert list(work.iterdir()) == [source]


def test_fetch_removes_captions_from_an_earlier_fetch(tmp_path, monkeypatch):
    monkeypatch.setenv('CONTENT_ENGINE_HOME', str(tmp_path))
    monkeypatch.setattr(engine, '_ytdlp', lambda: 'yt-dlp')
    (tmp_path / 'same').mkdir()
    (tmp_path / 'same' / 'transcript.en.vtt').write_text('old')

    def run(args, **kwargs):
        if '--dump-single-json' in args:
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                {'id': 'gmEj_z17wVo', 'automatic_captions': {'en-orig': [{}]}}), stderr='')
        Path(args[args.index('-o') + 1] + '.en-orig.vtt').write_text(
            'WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nNew caption.\n')
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(engine.subprocess, 'run', run)
    result = engine.fetch_transcript('https://youtu.be/gmEj_z17wVo', out_dir='same')
    assert sorted(p.name for p in (tmp_path / 'same').glob('*.vtt')) == ['transcript.en-orig.vtt']
    assert result['transcript_source'] == 'captions:en-orig'


@needs_ffmpeg
def test_symlink_output_is_rejected_before_any_clip_is_written(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('CONTENT_ENGINE_HOME', str(tmp_path / 'output'))
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=64x64:r=5',
                    '-t', '2', '-pix_fmt', 'yuv420p', 'source.mp4'], check=True)
    out = tmp_path / 'output' / 'clips'
    out.mkdir(parents=True)
    os.symlink(tmp_path / 'elsewhere.mp4', out / 'second_h.mp4')
    with pytest.raises(ValueError, match='symlink'):
        engine.cut_clips('source.mp4', [{'label': 'first', 'start': 0, 'end': 1},
                                        {'label': 'second', 'start': 0, 'end': 1}], out_dir='clips')
    assert not (out / 'first_h.mp4').exists()
    with pytest.raises(ValueError, match='crop_x'):
        engine.cut_clips('source.mp4', [{'start': 0, 'end': 1, 'orientation': 'v', 'crop_x': 2}])


@needs_ffmpeg
def test_auto_crop_follows_motion_on_the_right(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('CONTENT_ENGINE_HOME', str(tmp_path / 'output'))
    # A white box moving up and down near the right edge of a static 16:9 frame.
    subprocess.run([
        'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=gray:s=320x180:r=10:d=3',
        '-f', 'lavfi', '-i', 'color=c=white:s=30x50:r=10:d=3',
        '-filter_complex', "[0][1]overlay=x=250:y='mod(t*60,120)':eval=frame",
        '-pix_fmt', 'yuv420p', 'source.mp4'], check=True)
    source = engine._probe_video(str(tmp_path / 'source.mp4'))
    crop = engine._auto_crop_x(str(tmp_path / 'source.mp4'), 0, 3, source)
    assert 0.75 < crop <= 0.95, crop
    [result] = engine.cut_clips('source.mp4', [{'label': 'v', 'start': 0, 'end': 2, 'orientation': 'v'}])
    assert result['error'] is None and result['crop_x'] == round(crop, 3)
    [manual] = engine.cut_clips('source.mp4', [{'label': 'm', 'start': 0, 'end': 2,
                                                'orientation': 'v', 'crop_x': 0}])
    assert manual['error'] is None and manual['crop_x'] == 0


def test_cli_exposes_download_transcribe_and_transcriber_options():
    parser = ce_cli.build_parser()
    args = parser.parse_args(['run', 'https://youtu.be/gmEj_z17wVo', '--transcriber', 'whisper',
                              '--whisper-model', 'base.en'])
    assert (args.transcriber, args.whisper_model) == ('whisper', 'base.en')
    assert parser.parse_args(['download', 'https://youtu.be/x', '--audio-only']).audio_only
    assert parser.parse_args(['transcribe', 'a.mp4']).whisper_model == transcribe.DEFAULT_WHISPER_MODEL


def test_rolling_captions_with_untagged_single_word_are_not_doubled(tmp_path):
    vtt = tmp_path / 'c.vtt'
    vtt.write_text(
        'WEBVTT\n\n'
        '00:00:08.000 --> 00:00:09.590 align:start position:0%\n'
        'I am going to send the Spirit to\n'
        'lead,<00:00:08.300><c> guide</c><00:00:08.600><c> you</c><00:00:09.100><c> into</c><00:00:09.300><c> all</c>\n\n'
        '00:00:09.590 --> 00:00:09.600 align:start position:0%\n'
        'lead, guide you into all\n \n\n'
        '00:00:09.600 --> 00:00:11.290 align:start position:0%\n'
        'lead, guide you into all\n'
        'truth.\n\n'
        '00:00:11.290 --> 00:00:11.300 align:start position:0%\n'
        'truth.\n \n\n'
        '00:00:11.300 --> 00:00:12.790 align:start position:0%\n'
        'truth.\n'
        'I<00:00:11.400><c> will</c><00:00:11.600><c> stay.</c>\n')
    texts = [s['text'] for s in engine._parse_vtt(str(vtt))]
    assert texts == ['lead, guide you into all', 'truth.', 'I will stay.']


def test_numpy_timestamps_from_whisper_produce_valid_json_segments():
    np = pytest.importorskip('numpy')
    words = [{'start': np.float64(0.5), 'end': np.float64(0.9), 'word': ' Hello.'}]
    [segment] = transcribe.words_to_segments(words)
    assert type(segment['start']) is float and type(segment['duration']) is float
    json.dumps(segment)
    engine.validate_segments([{'start': np.float64(1.0), 'duration': np.float32(0.5), 'text': 'x'}])
    with pytest.raises(ValueError):
        engine.validate_segments([{'start': True, 'duration': 1, 'text': 'x'}])


def test_word_pieces_without_leading_space_are_joined():
    words = [{'start': 0, 'end': .2, 'word': ' a'}, {'start': .2, 'end': .4, 'word': ' spirit'},
             {'start': .4, 'end': .6, 'word': '-led'}, {'start': .6, 'end': .8, 'word': ' life.'}]
    assert transcribe.words_to_segments(words)[0]['text'] == 'a spirit-led life.'
    words[2]['word'] = ' -led'
    assert transcribe.words_to_segments(words)[0]['text'] == 'a spirit-led life.'


def test_cues_do_not_split_inside_a_word():
    words = _words('aaaa bbbb cccc dddd eeee ffff gggg spirit')
    words.append({'start': 10, 'end': 10.2, 'word': '-led'})
    words.append({'start': 10.2, 'end': 10.4, 'word': ' parenting.'})
    texts = [s['text'] for s in transcribe.words_to_segments(words, max_chars=30)]
    assert not any(t.startswith('-') for t in texts) and 'spirit-led' in ' '.join(texts)


@needs_ffmpeg
def test_auto_crop_switches_position_at_camera_cuts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('CONTENT_ENGINE_HOME', str(tmp_path / 'output'))
    # Shot 1: motion on the right of a gray frame. Shot 2 (hard cut): motion on the left of a black frame.
    subprocess.run([
        'ffmpeg', '-v', 'error',
        '-f', 'lavfi', '-i', 'color=c=gray:s=320x180:r=12:d=3',
        '-f', 'lavfi', '-i', 'color=c=black:s=320x180:r=12:d=3',
        '-f', 'lavfi', '-i', 'color=c=white:s=30x50:r=12:d=6',
        '-filter_complex',
        "[0][2]overlay=x=260:y='mod(t*60,120)':eval=frame:shortest=1[a];"
        "[1][2]overlay=x=30:y='mod(t*60,120)':eval=frame:shortest=1[b];[a][b]concat=n=2:v=1[v]",
        '-map', '[v]', '-pix_fmt', 'yuv420p', 'source.mp4'], check=True)
    source = engine._probe_video(str(tmp_path / 'source.mp4'))
    shots = engine._auto_crop_shots(str(tmp_path / 'source.mp4'), 0, 6, source)
    assert len(shots) == 2, shots
    assert shots[0]['t'] == 0 and shots[0]['x'] > 0.75
    assert 2.5 <= shots[1]['t'] <= 3.5 and shots[1]['x'] < 0.25
    [result] = engine.cut_clips('source.mp4', [{'label': 'cut', 'start': 0, 'end': 6, 'orientation': 'v'}])
    assert result['error'] is None
    assert [(c['t'], c['x'], c['mode']) for c in result['crop_shots']] == \
        [(sh['t'], sh['x'], sh['mode']) for sh in shots]



def _frame_brightness_columns(path, at):
    """Mean brightness per column (32 columns) of one frame of a rendered clip."""
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(at), '-i', path, '-frames:v', '1',
                          '-vf', 'scale=32:56,format=gray', '-f', 'rawvideo', '-'],
                         check=True, capture_output=True).stdout
    return [sum(raw[y * 32 + x] for y in range(56)) / 56 for x in range(32)]


@needs_ffmpeg
def test_virtual_camera_follows_a_subject_across_the_frame(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('CONTENT_ENGINE_HOME', str(tmp_path / 'output'))
    # A box that bobs up and down while walking from the left edge to the right edge over 10 s.
    subprocess.run([
        'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=gray:s=320x180:r=12:d=10',
        '-f', 'lavfi', '-i', 'color=c=white:s=24x40:r=12:d=10',
        '-filter_complex', "[0][1]overlay=x='20+t*27':y='40+mod(t*80,60)':eval=frame",
        '-pix_fmt', 'yuv420p', 'source.mp4'], check=True)
    [result] = engine.cut_clips('source.mp4', [{'label': 'walk', 'start': 0, 'end': 10,
                                                'orientation': 'v', 'framing': 'track'}])
    assert result['error'] is None
    assert result['crop_shots'][0]['moves'] >= 2
    # Motion covering the whole width looks like full-frame content: auto shows all of it.
    [auto] = engine.cut_clips('source.mp4', [{'label': 'auto', 'start': 0, 'end': 10, 'orientation': 'v'}])
    assert auto['crop_shots'][0]['mode'] == 'fit'
    with pytest.raises(ValueError, match='framing'):
        engine.cut_clips('source.mp4', [{'start': 0, 'end': 1, 'orientation': 'v', 'framing': 'zoom'}])
    for at in (1, 9.5):  # the subject stays inside the portrait frame at both ends
        cols = _frame_brightness_columns(result['output'], at)
        assert max(cols) > min(cols) + 20, (at, cols)


@needs_ffmpeg
def test_still_slides_are_fitted_not_cropped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('CONTENT_ENGINE_HOME', str(tmp_path / 'output'))
    # A static "slide": white bars at the far left and far right edges.
    subprocess.run([
        'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=320x180:r=10:d=3',
        '-vf', 'drawbox=x=0:y=60:w=20:h=60:c=white:t=fill,drawbox=x=300:y=60:w=20:h=60:c=white:t=fill',
        '-pix_fmt', 'yuv420p', 'slide.mp4'], check=True)
    source = engine._probe_video(str(tmp_path / 'slide.mp4'))
    shots = engine._auto_crop_shots(str(tmp_path / 'slide.mp4'), 0, 3, source)
    assert [s['mode'] for s in shots] == ['fit']
    [result] = engine.cut_clips('slide.mp4', [{'label': 's', 'start': 0, 'end': 3, 'orientation': 'v'}])
    assert result['error'] is None and result['crop_shots'][0]['mode'] == 'fit'
    cols = _frame_brightness_columns(result['output'], 1)
    # Both edge bars survive: bright columns near both sides of the frame.
    # (The fitted slide fills about a third of the height, so column means are diluted;
    # a centre crop would leave both edges at 0.)
    assert max(cols[:8]) > 15 and max(cols[-8:]) > 15, cols
