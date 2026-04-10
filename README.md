# Moshi Voice Chatbot

Real-time full-duplex voice AI chatbot built with [Kyutai's Moshi](https://github.com/kyutai-labs/moshi) and Apple Silicon MLX.

## Demo
> Talk naturally — Moshi listens and responds simultaneously with ~200ms latency.

## Stack
- `moshi_mlx` — Apple Silicon optimised inference
- Kyutai Mimi codec — 24kHz neural audio
- Vanilla JS + Web Audio API — zero framework frontend
- WebSocket — real-time audio streaming

## Run locally
Requires macOS with Apple Silicon and Python 3.12.

```bash
conda create -n moshi python=3.12 -y
conda activate moshi
pip install moshi_mlx
python -m moshi_mlx.local_web -q 4
```

Open `frontend/index.html` in your browser.

## Architecture
Based on Kyutai's RQ-Transformer with:
- Helium 7B Temporal Transformer for language
- Depth Transformer for audio codebook prediction  
- Mimi neural codec for audio encoding/decoding
- Inner Monologue for improved response quality