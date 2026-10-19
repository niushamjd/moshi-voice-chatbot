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

### Docker

The image runs the echo backend (MLX needs Apple Silicon, so the model can't run in a container):

```bash
docker build -t moshi:local .
docker run -p 8998:8998 moshi:local    # then open http://localhost:8998
```

CI builds it on every push and PR, smoke-tests the running container with `scripts/smoke_test.py`, and pushes it to `ghcr.io/niushamjd/moshi-voice-chatbot` only from `main` (`latest`, `sha-<short>`) and `v*` tags (semver).

### Kubernetes (kind)

Manifests are in `k8s/` (namespace, Deployment with probes and a locked-down security context, ClusterIP Service). To run them on a local [kind](https://kind.sigs.k8s.io/) cluster:

```bash
./scripts/kind-up.sh                                 # create cluster, build, load, deploy (re-runnable)
kubectl -n moshi port-forward svc/moshi 8998:80      # then open http://localhost:8998
./scripts/kind-down.sh                               # delete the cluster
```

It runs one replica on purpose: each process handles one conversation, and scaling out would need session-sticky routing. CI validates the manifests with kubeconform and runs the same script plus the smoke test in a `k8s-e2e` job.

### CI insights

`tools/ci_insights` is a small stdlib-only CLI that reads this repo's GitHub Actions history (runs, jobs, and the `junit-*` test-report artifacts) and reports build success rate and durations (overall and per job), the most failing tests, flaky tests (passed and failed on the same commit) and the slowest tests:

```bash
GITHUB_TOKEN=... python -m tools.ci_insights --repo niushamjd/moshi-voice-chatbot --limit 50 [--branch main] [--format table|json|markdown]
```

It needs a token (`GITHUB_TOKEN`, `GH_TOKEN`, or `gh auth token`) because GitHub only serves artifact downloads to authenticated requests. Completed runs are cached in `~/.cache/ci-insights/` (`--no-cache` to skip). The `CI insights` workflow runs it every Monday and on demand, and writes the report to the run summary.

Example output (`--top 5`) on this repo, after a demo branch with one always-failing and one random test was pushed four times (it was never merged). The random test shows up as flaky on three commits, where it passed on one Python version and failed on the other:

```
ci-insights: niushamjd/moshi-voice-chatbot (ci.yml, branch: all)

Builds
------
Metric                     Value
Runs analysed              12
Success rate               67%
Conclusions                failure 4, success 8
Duration median            2m 34s
Duration p90               4m 37s
Slowest run                5m 00s on fix/browser-url @ e612791 https://github.com/niushamjd/moshi-voice-chatbot/actions/runs/39287583697
Runs without test reports  0
Test executions            1184 (69 distinct tests)

Median duration per job
-----------------------
Job          Median  Runs
docker       41s     8
k8s-e2e      1m 37s  4
lint         17s     12
test (3.12)  18s     12
test (3.13)  18s     12

Most failing tests
------------------
Test                                              Failures  Runs  Failure rate
tests.test_demo_failures::test_demo_always_fails  8         8     100%
tests.test_demo_failures::test_demo_flaky         3         8     38%

Flaky tests (passed and failed on the same commit)
--------------------------------------------------
Test                                       Commits                    Failures  Runs
tests.test_demo_failures::test_demo_flaky  01f4e2e, 51b4ba1, 9c1193f  3         8

Slowest tests
-------------
Test                                                                           Median  Runs
tests.test_server::test_opus_round_trip_echoes_audio_and_text                  521ms   24
tests.test_server::test_ignores_messages_it_does_not_understand[client-text]   517ms   24
tests.test_server::test_ignores_messages_it_does_not_understand[empty]         517ms   24
tests.test_server::test_ignores_messages_it_does_not_understand[unknown-kind]  517ms   24
tests.test_ci_insights::test_cli_reports_errors_with_exit_code_2               200ms   12
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
