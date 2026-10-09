"""WebSocket server: streams browser audio through Moshi and back.

Run from the repository root:

    python -m backend.server

then open http://localhost:8998.
"""

import argparse
import asyncio
import logging
from pathlib import Path

import aiohttp
import numpy as np
import sphn
from aiohttp import web

from .audio_utils import SAMPLE_RATE, MessageKind, PcmFramer, decode_message, encode_message
from .moshi_runner import MoshiRunner

log = logging.getLogger("moshi-server")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
FRAME_RATE = 12.5


class ChatServer:
    def __init__(self, runner: MoshiRunner):
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
                pcm = opus_reader.append_bytes(payload)
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8998)
    parser.add_argument("--hf-repo", default="kyutai/moshika-mlx-q4",
                        help="Hugging Face repo holding the Moshi MLX weights")
    parser.add_argument("-q", "--quantized", choices=["4", "8", "bf16"], default="4",
                        help="weight precision; must match the repo")
    parser.add_argument("--max-minutes", type=float, default=10,
                        help="maximum length of one conversation")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    runner = MoshiRunner(
        hf_repo=args.hf_repo,
        quantized=None if args.quantized == "bf16" else int(args.quantized),
        max_steps=int(args.max_minutes * 60 * FRAME_RATE),
    )

    app = web.Application()
    app.router.add_get("/api/chat", ChatServer(runner).handle_chat)
    app.router.add_get("/", handle_index)
    app.router.add_static("/", FRONTEND_DIR)
    log.info("open http://%s:%d in your browser", args.host, args.port)
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
