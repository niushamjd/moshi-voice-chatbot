"""WebSocket server: streams browser audio through Moshi and back.

Run from the repository root:

    python -m backend.server                 # real model (Apple Silicon only)
    python -m backend.server --backend echo  # echoes audio back, runs anywhere

then open http://localhost:8998. Defaults can also come from the environment:
MOSHI_HOST, MOSHI_PORT and MOSHI_BACKEND.
"""

import argparse
import asyncio
import logging
import os
from pathlib import Path

import aiohttp
import numpy as np
import sphn
from aiohttp import web

from .audio_utils import SAMPLE_RATE, MessageKind, PcmFramer, decode_message, encode_message

log = logging.getLogger("moshi-server")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
FRAME_RATE = 12.5


class ChatServer:
    def __init__(self, runner):
        self.runner = runner
        # One model, one conversation at a time.
        self.busy = False

    async def handle_chat(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        peer = request.remote

        if self.busy:
            log.info("rejecting %s: a session is already active", peer)
            await ws.send_bytes(encode_message(MessageKind.ERROR, b"server busy: another session is active"))
            await ws.close()
            return ws

        self.busy = True
        log.info("session started for %s", peer)
        loop = asyncio.get_running_loop()
        outbox: asyncio.Queue = asyncio.Queue()

        def post(kind: MessageKind, payload) -> None:
            loop.call_soon_threadsafe(outbox.put_nowait, (kind, payload))

        session = self.runner.new_session(
            on_audio=lambda pcm: post(MessageKind.AUDIO, pcm),
            on_text=lambda text: post(MessageKind.TEXT, text),
            on_error=lambda message: post(MessageKind.ERROR, message),
        )
        opus_reader = sphn.OpusStreamReader(SAMPLE_RATE)
        opus_writer = sphn.OpusStreamWriter(SAMPLE_RATE)
        framer = PcmFramer()

        async def send_loop() -> None:
            while True:
                kind, payload = await outbox.get()
                if kind == MessageKind.AUDIO:
                    pages = opus_writer.append_pcm(payload)
                    if len(pages) > 0:
                        await ws.send_bytes(encode_message(kind, pages))
                elif kind == MessageKind.TEXT:
                    await ws.send_bytes(encode_message(kind, payload.encode()))
                elif kind == MessageKind.ERROR:
                    log.warning("session error for %s: %s", peer, payload)
                    await ws.send_bytes(encode_message(kind, payload.encode()))
                    await ws.close()
                    return

        await ws.send_bytes(encode_message(MessageKind.HANDSHAKE))
        sender = asyncio.create_task(send_loop())
        try:
            async for message in ws:
                if message.type != aiohttp.WSMsgType.BINARY:
                    log.warning("ignoring non-binary message from %s", peer)
                    continue
                try:
                    kind, payload = decode_message(message.data)
                except ValueError:
                    continue
                if kind != MessageKind.AUDIO:
                    log.warning("ignoring message kind %d from %s", kind, peer)
                    continue
                try:
                    pcm = opus_reader.append_bytes(payload)
                except ValueError as e:
                    # A corrupt Ogg stream can't be resynced, so end the session.
                    log.warning("invalid audio stream from %s: %s", peer, e)
                    await ws.send_bytes(encode_message(MessageKind.ERROR, f"invalid audio stream: {e}".encode()))
                    await ws.close()
                    break
                for frame in framer.push(np.asarray(pcm)):
                    session.push_frame(frame)
        finally:
            session.close()
            sender.cancel()
            self.busy = False
            log.info("session ended for %s", peer)
        return ws


async def handle_index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(FRONTEND_DIR / "index.html")


CHAT_KEY = web.AppKey("chat", ChatServer)
BACKEND_KEY = web.AppKey("backend", str)


async def handle_healthz(request: web.Request) -> web.Response:
    # Always 200, even mid-conversation: a busy server is healthy, and a probe
    # that failed during calls would get the pod restarted mid-call.
    return web.json_response({"status": "ok", "backend": request.app[BACKEND_KEY], "busy": request.app[CHAT_KEY].busy})


def build_app(runner, backend: str = "moshi") -> web.Application:
    app = web.Application()
    app[CHAT_KEY] = ChatServer(runner)
    app[BACKEND_KEY] = backend
    app.router.add_get("/api/chat", app[CHAT_KEY].handle_chat)
    app.router.add_get("/healthz", handle_healthz)
    app.router.add_get("/", handle_index)
    app.router.add_static("/", FRONTEND_DIR)
    return app


def make_runner(backend: str, hf_repo: str, quantized: str, max_minutes: float):
    max_steps = int(max_minutes * 60 * FRAME_RATE)
    if backend == "echo":
        from .echo_runner import EchoRunner

        return EchoRunner(max_steps=max_steps)
    # Imported lazily: it needs MLX, which only exists on Apple Silicon.
    from .moshi_runner import MoshiRunner

    return MoshiRunner(
        hf_repo=hf_repo,
        quantized=None if quantized == "bf16" else int(quantized),
        max_steps=max_steps,
    )


def browser_url(host: str, port: int) -> str:
    # A bind-all address isn't a secure context, so browsers hide WebCodecs on
    # it; point people at localhost instead.
    if host in ("0.0.0.0", "::", ""):
        host = "localhost"
    return f"http://{host}:{port}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=os.environ.get("MOSHI_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MOSHI_PORT", "8998")))
    parser.add_argument(
        "--backend",
        choices=["moshi", "echo"],
        default=os.environ.get("MOSHI_BACKEND", "moshi"),
        help="moshi runs the model; echo sends audio straight back (no MLX needed)",
    )
    parser.add_argument(
        "--hf-repo", default="kyutai/moshika-mlx-q4", help="Hugging Face repo holding the Moshi MLX weights"
    )
    parser.add_argument(
        "-q", "--quantized", choices=["4", "8", "bf16"], default="4", help="weight precision; must match the repo"
    )
    parser.add_argument("--max-minutes", type=float, default=10, help="maximum length of one conversation")
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    runner = make_runner(args.backend, args.hf_repo, args.quantized, args.max_minutes)
    app = build_app(runner, backend=args.backend)
    log.info("open %s in your browser (backend: %s)", browser_url(args.host, args.port), args.backend)
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
