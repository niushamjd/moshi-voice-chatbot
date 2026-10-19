"""A stand-in for MoshiRunner that sends every frame straight back.

It has the same interface as `moshi_runner.MoshiRunner` but needs no model and
no MLX, so the server, the wire protocol and the browser client can run and be
tested anywhere (CI, Docker, Kubernetes). Each session also sends one text
piece, so the transcript path is exercised too.
"""

import logging
from collections.abc import Callable

import numpy as np

from .audio_utils import FRAME_SIZE

log = logging.getLogger(__name__)

ECHO_TEXT = " (echo)"


class EchoSession:
    def __init__(
        self,
        runner: "EchoRunner",
        on_audio: Callable[[np.ndarray], None],
        on_text: Callable[[str], None],
        on_error: Callable[[str], None],
    ):
        self._runner = runner
        self.on_audio = on_audio
        self.on_text = on_text
        self.on_error = on_error
        self.closed = False
        self.steps = 0

    def push_frame(self, pcm: np.ndarray) -> None:
        assert pcm.shape == (FRAME_SIZE,), pcm.shape
        if self.closed:
            return
        if self.steps >= self._runner.max_steps:
            self.fail("session length limit reached")
            return
        if self.steps == 0:
            self.on_text(ECHO_TEXT)
        self.steps += 1
        self.on_audio(pcm.copy())

    def close(self) -> None:
        self.closed = True

    def fail(self, message: str) -> None:
        if not self.closed:
            self.closed = True
            self.on_error(message)


class EchoRunner:
    def __init__(self, max_steps: int):
        self.max_steps = max_steps
        log.info("echo backend ready (no model loaded)")

    def new_session(
        self,
        on_audio: Callable[[np.ndarray], None],
        on_text: Callable[[str], None],
        on_error: Callable[[str], None],
    ) -> EchoSession:
        return EchoSession(self, on_audio, on_text, on_error)
