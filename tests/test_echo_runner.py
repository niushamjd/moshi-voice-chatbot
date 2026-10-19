import numpy as np
import pytest

from backend.audio_utils import FRAME_SIZE
from backend.echo_runner import ECHO_TEXT, EchoRunner


def start(max_steps: int = 100):
    out = {"audio": [], "text": [], "errors": []}
    session = EchoRunner(max_steps=max_steps).new_session(
        on_audio=out["audio"].append, on_text=out["text"].append, on_error=out["errors"].append
    )
    return session, out


def frame(value: float = 0.5) -> np.ndarray:
    return np.full(FRAME_SIZE, value, dtype=np.float32)


def test_echoes_each_frame_unchanged():
    session, out = start()
    session.push_frame(frame(0.1))
    session.push_frame(frame(0.2))
    assert len(out["audio"]) == 2
    np.testing.assert_array_equal(out["audio"][1], frame(0.2))


def test_sends_text_once_per_session():
    session, out = start()
    for _ in range(5):
        session.push_frame(frame())
    assert out["text"] == [ECHO_TEXT]


def test_closed_session_drops_frames():
    session, out = start()
    session.close()
    session.push_frame(frame())
    assert out["audio"] == [] and out["errors"] == []


def test_length_limit_fails_session_once():
    session, out = start(max_steps=2)
    for _ in range(5):
        session.push_frame(frame())
    assert len(out["audio"]) == 2
    assert out["errors"] == ["session length limit reached"]


def test_rejects_wrong_frame_size():
    session, _ = start()
    with pytest.raises(AssertionError):
        session.push_frame(np.zeros(10, dtype=np.float32))
