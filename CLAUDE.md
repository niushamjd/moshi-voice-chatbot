# CLAUDE.md

Context for Claude Code working in this repo. The task list is in `PLAN.md`.

## What this repo is
Real-time, full-duplex voice chat with Kyutai's Moshi, running locally on Apple Silicon via MLX.
- `backend/server.py`: aiohttp server. It runs the WebSocket at `/api/chat`, serves `/healthz` and the static frontend, and handles one conversation at a time. `build_app(runner)` is the app factory.
- `backend/moshi_runner.py`: the real model, a three-thread Mimi/LM/Mimi pipeline. **It needs MLX, so it only runs on Apple Silicon.**
- `backend/echo_runner.py`: a stand-in with the same interface that sends audio straight back. It runs anywhere, and CI, Docker and Kubernetes all use it.
- `backend/audio_utils.py`: the wire protocol (1-byte kind + payload) and PCM framing.
- `frontend/`: vanilla JS with no build step.
- `scripts/smoke_test.py`: a real-time end-to-end check against a running server, which exits non-zero on failure.

## Hard constraints
- **Never put `moshi_mlx`, `mlx` or `rustymimi` in CI, the Docker image, or Kubernetes.** Those use `--backend echo` (`MOSHI_BACKEND=echo`).
- Don't change `moshi_runner.py` behaviour or the wire protocol. The frontend must keep working with Kyutai's `moshi_mlx.local_web`.
- `requirements.txt` holds the Mac/model deps. `requirements-dev.txt` holds the cross-platform deps for the server, tests and lint.
- Python 3.12 is the target. CI also tests 3.13.

## Commands (run before every commit)
```bash
pip install -r requirements-dev.txt
ruff check . && ruff format --check .
pytest
python -m backend.server --backend echo   # manual run on :8998
```

## Conventions
- One branch per PLAN.md step, with small commits and Conventional Commit messages (`feat:`, `ci:`, `docs:`, `chore:`, `test:`).
- Each new piece of behaviour gets a test. Network calls in tests are mocked; there is no live GitHub API in tests.
- Keep secrets out of the repo. CI uses `GITHUB_TOKEN`, and the tool reads `GITHUB_TOKEN`/`GH_TOKEN` or falls back to `gh auth token`.
- Pin GitHub Actions to their current major versions. Workflows must pass `actionlint`.
- When you finish a step, tick it in `PLAN.md` and add a line to its "Notes" about anything that went wrong and how you fixed it. Those notes become interview material.
