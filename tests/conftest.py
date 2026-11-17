import pytest


@pytest.fixture(autouse=True)
def _no_face_downloads(monkeypatch):
    """Keep tests offline: face detection is off unless a test turns it on."""
    monkeypatch.setenv('CONTENT_ENGINE_FACES', 'off')
