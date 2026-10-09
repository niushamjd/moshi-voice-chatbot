"""End-to-end check of a running Moshi server over the WebSocket protocol.

Streams a WAV file (or silence) to the server in real time, records what comes
back, and reports handshake time, time to first audio, whether the server keeps
up with real time, and Moshi's transcript.

    python scripts/smoke_test.py --wav question.wav --out reply.wav
"""

import argparse
import asyncio
import time

import aiohttp
import numpy as np
import sphn

SAMPLE_RATE = 24000
CHUNK = 480  # 20 ms, the same packet size the browser client sends


async def run(args) -> int:
    if args.wav:
        pcm, _ = sphn.read(args.wav, sample_rate=SAMPLE_RATE)
        pcm = pcm[0]
    else:
        pcm = np.zeros(0, dtype=np.float32)
    # Pad with silence so Moshi has time to answer.
    total = max(len(pcm), int(args.seconds * SAMPLE_RATE))
    pcm = np.concatenate([pcm, np.zeros(total - len(pcm), dtype=np.float32)]).astype(np.float32)

    writer = sphn.OpusStreamWriter(SAMPLE_RATE)
    reader = sphn.OpusStreamReader(SAMPLE_RATE)
    received: list[np.ndarray] = []
    text: list[str] = []
    first_audio_at = None
    errors: list[str] = []

    async with aiohttp.ClientSession() as http:
        t0 = time.monotonic()
        async with http.ws_connect(args.url) as ws:
            msg = await ws.receive_bytes(timeout=30)
            if msg[0] != 0:
                print(f"FAIL: expected handshake, got kind {msg[0]}: {msg[1:]!r}")
                return 1
            handshake_s = time.monotonic() - t0
            start = time.monotonic()

            async def send() -> None:
                for i in range(0, len(pcm), CHUNK):
                    # Pace to the wall clock like a live microphone.
                    await asyncio.sleep(max(0.0, start + i / SAMPLE_RATE - time.monotonic()))
                    pages = writer.append_pcm(pcm[i : i + CHUNK])
                    if len(pages) > 0:
                        await ws.send_bytes(b"\x01" + pages)
                await asyncio.sleep(1.0)
                await ws.close()

            async def recv() -> None:
                nonlocal first_audio_at
                async for m in ws:
                    if m.type != aiohttp.WSMsgType.BINARY:
                        continue
                    kind, payload = m.data[0], m.data[1:]
                    if kind == 1:
                        out = reader.append_bytes(payload)
                        if out.size:
                            if first_audio_at is None:
                                first_audio_at = time.monotonic() - start
                            received.append(out)
                    elif kind == 2:
                        text.append(payload.decode())
                    elif kind == 5:
                        errors.append(payload.decode())

            await asyncio.gather(send(), recv())

    sent_s = len(pcm) / SAMPLE_RATE
    out = np.concatenate(received) if received else np.zeros(0, dtype=np.float32)
    got_s = len(out) / SAMPLE_RATE
    print(f"handshake:        {handshake_s * 1000:.0f} ms")
    print(f"first audio back: {first_audio_at * 1000:.0f} ms" if first_audio_at else "first audio back: never")
    print(f"audio sent:       {sent_s:.1f} s")
    print(f"audio received:   {got_s:.1f} s ({got_s / sent_s:.0%} of real time)")
    print(f"peak level out:   {np.abs(out).max() if out.size else 0:.3f}")
    print(f"transcript:       {''.join(text).strip()!r}")
    if errors:
        print(f"server errors:    {errors}")
    if args.out and out.size:
        sphn.write_wav(args.out, out[None], SAMPLE_RATE)
        print(f"wrote {args.out}")

    # Moshi lags its input by a couple of frames, so allow some shortfall.
    ok = not errors and got_s >= sent_s * 0.9
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="ws://localhost:8998/api/chat")
    parser.add_argument("--wav", help="audio to send; silence if omitted")
    parser.add_argument("--seconds", type=float, default=10, help="minimum length to stream")
    parser.add_argument("--out", help="write Moshi's reply to this WAV file")
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
