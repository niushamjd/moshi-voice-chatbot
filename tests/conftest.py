"""Shared fixtures: an echo-backed server and helpers to talk to it over the WebSocket."""

from dataclasses import dataclass, field

import aiohttp
import numpy as np
import pytest
import sphn

from backend.audio_utils import SAMPLE_RATE, MessageKind, encode_message
from backend.echo_runner import EchoRunner
from backend.server import build_app

CHUNK = 480  # 20 ms, the packet size the browser client sends


def tone(seconds: float, freq: float = 440.0, amplitude: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


@dataclass
class Received:
    kinds: list[int] = field(default_factory=list)
    audio: list[np.ndarray] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    closed: bool = False

    @property
    def pcm(self) -> np.ndarray:
        return np.concatenate(self.audio) if self.audio else np.zeros(0, dtype=np.float32)


@pytest.fixture
def make_client(aiohttp_client):
    """Build an app around an EchoRunner and return a test client for it."""

    async def make(max_steps: int = 10_000):
        runner = EchoRunner(max_steps=max_steps)
        return await aiohttp_client(build_app(runner, backend="echo"))

    return make


@pytest.fixture
def send_audio():
    """Opus-encode PCM in 20 ms chunks and send it as AUDIO messages."""

    async def send(ws, pcm: np.ndarray) -> None:
        writer = sphn.OpusStreamWriter(SAMPLE_RATE)
        for i in range(0, len(pcm), CHUNK):
            pages = writer.append_pcm(pcm[i : i + CHUNK])
            if len(pages) > 0:
                await ws.send_bytes(encode_message(MessageKind.AUDIO, pages))

    return send


@pytest.fixture
def collect():
    """Read messages until the socket closes or goes quiet for `idle` seconds."""

    async def collect(ws, idle: float = 0.5) -> Received:
        reader = sphn.OpusStreamReader(SAMPLE_RATE)
        got = Received()
        while True:
            try:
                msg = await ws.receive(timeout=idle)
            except TimeoutError:
                return got
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                got.closed = True
                return got
            kind, payload = msg.data[0], msg.data[1:]
            got.kinds.append(kind)
            if kind == MessageKind.AUDIO:
                pcm = reader.append_bytes(payload)
                if pcm.size:
                    got.audio.append(pcm)
            elif kind == MessageKind.TEXT:
                got.text.append(payload.decode())
            elif kind == MessageKind.ERROR:
                got.errors.append(payload.decode())

    return collect
