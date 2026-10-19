# Moshi Voice Chatbot

Real-time, full-duplex voice chat with [Kyutai's Moshi](https://github.com/kyutai-labs/moshi) running locally on Apple Silicon via MLX. You talk, Moshi listens and answers at the same time, and audio streams both ways over a WebSocket.

## How it works

```
Browser                                              Python server (backend/)
mic → AudioWorklet (24 kHz PCM)                      
    → WebCodecs Opus encoder → Ogg pages ──WS──▶  Ogg/Opus decode → 80 ms frames
                                                       → Mimi encode → Moshi LM step → Mimi decode
speakers ← AudioWorklet jitter buffer                  ↓ text tokens
    ← WebCodecs Opus decoder ← Ogg pages ◀──WS──  Ogg/Opus encode
```

- **`backend/server.py`**: aiohttp WebSocket server, which also serves the frontend. It handles one conversation at a time; a second client gets a "server busy" error.
- **`backend/moshi_runner.py`**: runs Mimi encode, the Moshi LM step and Mimi decode on three separate threads, so the stages overlap. On an M1 Pro each stage alone takes ~25 / ~50 / ~25 ms. Run in sequence that's ~90 ms, over Moshi's 80 ms frame budget. Pipelined, it averages ~65 ms per frame. Model state resets at the start of each conversation.
- **`backend/audio_utils.py`**: the wire protocol and PCM framing.
- **`frontend/`**: vanilla JS, no build step. `ogg.js` packs and unpacks Ogg pages, `audio-worklets.js` handles mic capture and playback, and `app.js` handles the protocol and UI.

### Wire protocol

This is the protocol Kyutai's own Moshi servers use, so the frontend also works with `moshi_mlx.local_web`. Every binary WebSocket message starts with a one-byte kind:

| Byte | Direction | Payload |
|---|---|---|
| `0x00` | server → client | handshake, sent when the model is ready |
| `0x01` | both | chunk of an Ogg/Opus stream, 24 kHz mono |
| `0x02` | server → client | UTF-8 text piece of Moshi's speech |
| `0x05` | server → client | UTF-8 error message |

## Run locally

Requires macOS on Apple Silicon, Python 3.12, and a recent Chrome, Edge, Firefox or Safari (for WebCodecs audio).

```bash
conda create -n moshi python=3.12 -y
conda activate moshi
pip install -r requirements.txt
python scripts/download_models.py     # ~4 GB, kyutai/moshika-mlx-q4
python -m backend.server
```

Open http://localhost:8998 and click **Connect**. Use headphones: Moshi is full-duplex, so on speakers it can hear itself.

Server options: `--port`, `--hf-repo` (e.g. `kyutai/moshiko-mlx-q4` for the male voice), `-q 4|8|bf16` (must match the repo), and `--max-minutes` (conversation length limit, default 10).

To run the same frontend against Kyutai's reference server instead:

```bash
python -m moshi_mlx.local_web -q 4 --hf-repo kyutai/moshika-mlx-q4 --static frontend
```

## Testing

Without a Mac or the model, run the server with the echo backend, which sends your audio straight back over the same protocol:

```bash
pip install -r requirements-dev.txt
python -m backend.server --backend echo   # or MOSHI_BACKEND=echo; also MOSHI_HOST, MOSHI_PORT
pytest                                    # unit + WebSocket integration tests
ruff check . && ruff format --check .
```

`GET /healthz` returns 200 with `{"status", "backend", "busy"}`, including during a conversation.

`scripts/smoke_test.py` streams a WAV file to a running server in real time and checks what comes back:

```bash
say -o q.aiff "Hi Moshi, can you tell me about Paris?" && afconvert -f WAVE -d LEI16@24000 q.aiff q.wav
python scripts/smoke_test.py --wav q.wav --seconds 15 --out reply.wav
```

It reports time to first audio, whether the server kept up with real time, and Moshi's transcript.

## Model

Moshi is Kyutai's speech-text foundation model: a 7B temporal transformer (Helium) plus a depth transformer that predicts Mimi audio codebooks, with an "inner monologue" text stream (the transcript shown in the UI). Mimi is Kyutai's streaming neural audio codec: 24 kHz audio at 12.5 frames per second. Weights come from Hugging Face under Kyutai's licenses.
