"""WebSocket integration tests against the real aiohttp app with the echo backend."""

import numpy as np
import pytest

from backend.audio_utils import SAMPLE_RATE, MessageKind, encode_message
from backend.echo_runner import ECHO_TEXT, EchoRunner
from backend.server import browser_url, make_runner, parse_args

from .conftest import tone


async def test_healthz_reports_ok(make_client):
    client = await make_client()
    resp = await client.get("/healthz")
    assert resp.status == 200
    assert await resp.json() == {"status": "ok", "backend": "echo", "busy": False}


async def test_healthz_stays_200_during_a_session(make_client):
    # Kubernetes probes hit this mid-call; failing it would restart the pod.
    client = await make_client()
    async with client.ws_connect("/api/chat") as ws:
        await ws.receive_bytes()
        resp = await client.get("/healthz")
        assert resp.status == 200
        assert (await resp.json())["busy"] is True


async def test_serves_frontend(make_client):
    client = await make_client()
    index = await client.get("/")
    assert index.status == 200 and "<html" in (await index.text()).lower()
    assert (await client.get("/app.js")).status == 200


async def test_handshake_is_first_message(make_client):
    client = await make_client()
    async with client.ws_connect("/api/chat") as ws:
        assert await ws.receive_bytes() == encode_message(MessageKind.HANDSHAKE)


async def test_opus_round_trip_echoes_audio_and_text(make_client, send_audio, collect):
    client = await make_client()
    sent = tone(1.0)
    async with client.ws_connect("/api/chat") as ws:
        await ws.receive_bytes()
        await send_audio(ws, sent)
        got = await collect(ws)
    assert got.errors == []
    assert "".join(got.text) == ECHO_TEXT
    # Opus and 80 ms framing hold back a little audio at the end.
    assert len(got.pcm) >= 0.7 * len(sent)
    rms = float(np.sqrt(np.mean(got.pcm**2)))
    assert 0.1 < rms < 0.4  # a 0.3-amplitude sine has RMS ~0.21


async def test_second_client_gets_busy_error(make_client, collect):
    client = await make_client()
    async with client.ws_connect("/api/chat") as first:
        await first.receive_bytes()
        async with client.ws_connect("/api/chat") as second:
            got = await collect(second)
    assert got.errors == ["server busy: another session is active"]
    assert MessageKind.HANDSHAKE not in got.kinds
    assert got.closed


async def test_busy_flag_resets_after_disconnect(make_client):
    client = await make_client()
    async with client.ws_connect("/api/chat") as ws:
        await ws.receive_bytes()
    async with client.ws_connect("/api/chat") as ws:
        assert await ws.receive_bytes() == encode_message(MessageKind.HANDSHAKE)
    assert (await (await client.get("/healthz")).json())["busy"] is False


async def test_length_limit_sends_error_and_closes(make_client, send_audio, collect):
    client = await make_client(max_steps=3)
    async with client.ws_connect("/api/chat") as ws:
        await ws.receive_bytes()
        await send_audio(ws, tone(1.0))
        got = await collect(ws)
    assert got.errors == ["session length limit reached"]
    assert got.closed


@pytest.mark.parametrize(
    "message",
    [b"", encode_message(MessageKind.TEXT, b"hello"), bytes([42]) + b"junk"],
    ids=["empty", "client-text", "unknown-kind"],
)
async def test_ignores_messages_it_does_not_understand(make_client, send_audio, collect, message):
    client = await make_client()
    async with client.ws_connect("/api/chat") as ws:
        await ws.receive_bytes()
        await ws.send_bytes(message)
        await ws.send_str("not binary")
        await send_audio(ws, tone(0.5))
        got = await collect(ws)
    assert got.errors == []
    assert got.audio, "session should keep working after a bad message"


async def test_corrupt_audio_stream_sends_error(make_client, collect):
    client = await make_client()
    async with client.ws_connect("/api/chat") as ws:
        await ws.receive_bytes()
        await ws.send_bytes(encode_message(MessageKind.AUDIO, b"not an ogg stream" * 30))
        got = await collect(ws)
    assert len(got.errors) == 1 and got.errors[0].startswith("invalid audio stream")
    assert got.closed


def test_env_vars_set_defaults(monkeypatch):
    monkeypatch.setenv("MOSHI_HOST", "0.0.0.0")
    monkeypatch.setenv("MOSHI_PORT", "9000")
    monkeypatch.setenv("MOSHI_BACKEND", "echo")
    args = parse_args([])
    assert (args.host, args.port, args.backend) == ("0.0.0.0", 9000, "echo")


def test_flags_override_env_vars(monkeypatch):
    monkeypatch.setenv("MOSHI_BACKEND", "echo")
    assert parse_args(["--backend", "moshi"]).backend == "moshi"


def test_make_runner_echo_needs_no_mlx():
    runner = make_runner("echo", "unused", "4", max_minutes=1)
    assert isinstance(runner, EchoRunner)
    assert runner.max_steps == 60 * 12.5
    assert SAMPLE_RATE == 24000


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_browser_url_avoids_bind_all_address(host):
    # WebCodecs only exists in secure contexts, and http://0.0.0.0 isn't one.
    assert browser_url(host, 8998) == "http://localhost:8998"


def test_browser_url_keeps_a_real_host():
    assert browser_url("192.168.1.5", 9000) == "http://192.168.1.5:9000"
