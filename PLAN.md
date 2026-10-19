# PLAN: CI/CD pipeline, container, Kubernetes, CI insights tool, docs

Goal: show the daily work of a developer-productivity/platform team on a small scale. Code gets built, tested and shipped automatically, the pipeline gets measured, and all of it gets documented.

Read `CLAUDE.md` first. Do the steps in order, one branch and one PR per step. Every step must leave `ruff check .`, `ruff format --check .` and `pytest` green.

---

## Step 1: CI (done, branch `ci/pipeline`)
- [x] Echo backend, `build_app()`, `/healthz`, `MOSHI_*` env vars
- [x] 28 pytest tests (unit + WebSocket integration), ruff config in `pyproject.toml`
- [x] `.github/workflows/ci.yml`: lint job + test matrix (3.12, 3.13), JUnit report uploaded as artifact `junit-py<ver>`

Notes:
- The original `ci/pipeline` work only existed as a git bundle from another session that never reached this machine, so Step 1 was rebuilt from its summary. Lesson: a step isn't done until it's on a remote branch.
- `venv/` (640 files), `.DS_Store`, an empty `.env` and a gitlink to a nested repo had been committed before `.gitignore` covered them; untracked with `git rm --cached`.
- Garbage audio bytes put `sphn.OpusStreamReader` into a permanent error state and raised an uncaught `ValueError` in the handler. The server now catches it, sends an `ERROR` message and ends the session (no wire-protocol change).
- `server.py` imported `moshi_runner` (and so MLX) at module load, which would break on Linux. The import is now lazy and only happens for `--backend moshi`.
- Checked that the tests bite: making the echo return silence fails 2 tests; skipping the busy-flag reset fails 1.

If `ci/pipeline` isn't merged into `main` yet, merge it first.

---

## Step 2: Docker image, built and pushed by the pipeline (done, branch `feat/docker`)
- [x] `requirements-server.txt`; `requirements-dev.txt` includes it
- [x] `Dockerfile` (two-stage, non-root UID 10001, stdlib `HEALTHCHECK`, OCI labels), `.dockerignore`
- [x] `docker` job: build with `load: true` → run → `/healthz` → `smoke_test.py --seconds 3` → push to ghcr only on `main`/`v*`
- [x] Local: `docker build` + `docker run` works, `/healthz` answers, container reports `healthy`, smoke test passes (arm64 native and amd64 emulated)
- [ ] Browser UI echoes a real voice through the container (manual check with a mic)
- [ ] On GitHub: a PR builds and smoke-tests without pushing; a push to `main` publishes the package (needs the branch pushed)

Branch: `feat/docker`

**Files**
1. `requirements-server.txt` with only what the running server needs (`aiohttp`, `sphn`, `numpy`). Change `requirements-dev.txt` to start with `-r requirements-server.txt` and add only the test/lint tools.
2. `Dockerfile`:
   - Base `python:3.12-slim`. Use a multi-stage build if it clearly shrinks the image, otherwise keep it single-stage.
   - Install `requirements-server.txt` only. Copy `backend/` and `frontend/` only.
   - Run as a non-root user.
   - `ENV MOSHI_BACKEND=echo MOSHI_HOST=0.0.0.0 MOSHI_PORT=8998`, `EXPOSE 8998`.
   - `HEALTHCHECK` hits `/healthz` using Python's `urllib` (no curl in a slim image).
   - `CMD ["python", "-m", "backend.server"]`.
   - OCI labels (`org.opencontainers.image.source` = the GitHub repo URL, so ghcr links the package to the repo).
3. `.dockerignore` excludes `.git`, `venv`, `tests`, caches, `reports`, `*.wav`.
4. A new job `docker` in `ci.yml`:
   - `needs: [lint, test]`
   - Uses `docker/setup-buildx-action`, `docker/metadata-action`, `docker/login-action`, `docker/build-push-action`, with GHA layer cache (`cache-from/to: type=gha`).
   - Image `ghcr.io/niushamjd/moshi-voice-chatbot` (must be lowercase).
   - Tags: `sha-<short>`, `latest` on `main`, and semver on `v*` tags. Add `tags: ['v*']` to the push trigger.
   - **Builds on every push and PR, but pushes only on `main` and `v*` tags.** Never push from PRs.
   - `permissions: packages: write` on this job only.
   - **Container smoke test before pushing:** build with `load: true`, `docker run -d -p 8998:8998`, wait for `/healthz`, then run `python scripts/smoke_test.py --seconds 3` against it (it sends silence when no `--wav` is given; that's fine). Fail the job if it fails.

**Done when**
- `docker build -t moshi:local . && docker run -p 8998:8998 moshi:local` works, `/healthz` answers, and the browser UI connects and echoes your voice.
- A push to `main` makes a package appear under the repo's Packages on GitHub. A PR builds and smoke-tests without pushing.
- The image is under ~250 MB. Record the actual size in Notes.

Notes:
- Image size: **196 MB on linux/amd64** (what CI publishes), **217 MB on linux/arm64** (local M1 build). The first single-stage attempt was 284 MB on amd64 and 316 MB on arm64.
- `sphn` only publishes Linux wheels for x86_64, so on an M1 `pip install` fell back to building from source and failed with `linker 'cc' not found`. Fix: a build stage with `build-essential`/`cmake`. maturin downloads Rust by itself.
- Next, the Opus copy bundled with `sphn` (via `audiopus_sys`) failed under CMake 4 with "Compatibility with CMake < 3.5 has been removed". Fix: `CMAKE_POLICY_VERSION_MINIMUM=3.5` in the build stage only.
- Size: `sphn`'s extension `.so` is ~80 MB unstripped, both when compiled locally and in the official PyPI wheel. The build stage installs into `/install`, runs `strip --strip-unneeded` on every `.so` and the runtime stage copies `/install`, which brings `sphn` down to ~3.5 MB. So multi-stage clearly pays here: it keeps the toolchain out and lets us strip.
- The CI push step re-runs `build-push-action` with `push: true`. It hits the GHA layer cache from the smoke-test build, so it doesn't rebuild, and it only runs after the smoke test passes. A `load: true` image can't be pushed by buildx directly.
- No Python 3.13 locally; the 3.13 leg is only checked in CI.

---

## Step 3: Kubernetes (kind) with manifests in the repo
Branch: `feat/k8s`

**Files: `k8s/`**
- `namespace.yaml`: namespace `moshi`.
- `deployment.yaml`:
  - `replicas: 1`, and add a comment explaining why: one conversation per process, and scaling out needs session-sticky routing, which is out of scope.
  - Env `MOSHI_BACKEND=echo`.
  - `readinessProbe` and `livenessProbe` on `GET /healthz`. **The probe must not fail while a call is active**: `/healthz` already returns 200 whether busy or not, so keep it that way and add a test asserting it.
  - Resource requests/limits.
  - `securityContext`: `runAsNonRoot`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true` (mount an `emptyDir` at `/tmp` if anything needs it), drop all capabilities.
- `service.yaml`: ClusterIP, port 80 → 8998.
- `kustomization.yaml`: lists the above and uses an `images:` override so CI or the script can set the tag.

**`scripts/kind-up.sh`** (idempotent, `set -euo pipefail`):
1. Create the kind cluster `moshi` if it doesn't exist.
2. `docker build`, then `kind load docker-image`.
3. `kubectl apply -k k8s/`, then `kubectl rollout status` with a timeout.
4. Print the port-forward command: `kubectl -n moshi port-forward svc/moshi 8998:80`.

Also add `scripts/kind-down.sh`.

**CI**
- In the `lint` job, validate the manifests with `kubeconform` (strict; `kustomize build k8s | kubeconform -strict -summary`).
- New job `k8s-e2e` (`needs: docker`, runs on PRs and `main`):
  - `helm/kind-action` to create the cluster.
  - Build the image, `kind load`, `kubectl apply -k`, wait for the rollout.
  - Port-forward in the background, then run `scripts/smoke_test.py` through it.
  - On failure, dump `kubectl describe` and the pod logs.

**Done when** `./scripts/kind-up.sh` on the Mac gives a working echo UI through port-forward, and the `k8s-e2e` job is green.

Notes:

---

## Step 4: `ci-insights`, a CLI that reports on the pipeline itself
Branch: `feat/ci-insights`

A small internal developer tool that answers two questions: which tests fail most often, and how long builds take.

**Location**: `tools/ci_insights/` (package) with `python -m tools.ci_insights` as the entry point. Use the stdlib (`urllib`, `zipfile`, `xml.etree`, `argparse`, `statistics`) and add no new runtime deps unless clearly justified.

**Data**: GitHub REST API.
- Workflow runs: `GET /repos/{owner}/{repo}/actions/workflows/ci.yml/runs` (paginate; filter by `--branch`).
- Jobs per run: `.../runs/{id}/jobs` for per-job durations.
- Artifacts per run: `.../runs/{id}/artifacts`. Download the `junit-*` zips and parse the `testcase` elements, which carry `failure`/`error`/`skipped` and `time`.
- Auth: `GITHUB_TOKEN` or `GH_TOKEN`, else `gh auth token`. A clear error message if none is found.
- Cache downloaded artifacts and API pages under `~/.cache/ci-insights/` (runs are immutable once completed). `--no-cache` flag.
- Handle rate limits by reading `X-RateLimit-Remaining` and printing a helpful message.

**CLI**
```
python -m tools.ci_insights --repo niushamjd/moshi-voice-chatbot [--limit 50] [--branch main] [--format table|json|markdown]
```

**Report contents**
- Builds: number of runs, success rate, median and p90 total duration, median duration per job (lint / test-3.12 / test-3.13 / docker / k8s-e2e), and the slowest run with its link.
- Tests: the top N by failure count and failure rate.
- **Flaky tests**: tests that both failed and passed on the same commit SHA (re-runs or matrix entries).
- Slowest tests by median time.

**Tests**: put recorded API responses and a small JUnit zip in `tests/fixtures/ci_insights/`, mock the HTTP layer, and keep the network out. Cover pagination, the flaky detection logic, percentile maths, and missing artifacts (runs that failed before uploading).

**Wire it into CI**: a new workflow `.github/workflows/ci-insights.yml` that runs on a weekly `schedule` plus `workflow_dispatch`, and writes `--format markdown` to `$GITHUB_STEP_SUMMARY`.

**Optional stretch (only if time allows)**: a `--format prometheus` option that writes textfile-exporter metrics, plus `observability/` with a docker-compose for Prometheus and Grafana and one dashboard JSON (build duration over time, failure rate, top failing tests).

**Done when** running it against the real repo prints a sensible report. To give it real data, deliberately push a branch with one failing test and one flaky (random) test, then delete them afterwards. Paste an example report into the README.

Notes:

---

## Step 5: Documentation
Branch: `docs/platform`

- **README**: a short "Platform / CI-CD" section near the top with the badges (CI, image), a one-paragraph overview, and links to the docs below. Keep the existing model docs.
- **`docs/architecture.md`**: two Mermaid diagrams (GitHub renders them):
  1. Delivery pipeline: push/PR → lint → test matrix → docker build + container smoke test → push to ghcr (main/tags only) → kind e2e; plus the weekly ci-insights run reading the runs and artifacts.
  2. Runtime: browser ↔ WebSocket ↔ server ↔ runner (moshi on the Mac, echo in container/k8s), with the k8s Service/Deployment/probes.
- **`docs/adding-a-test.md`**: where each kind of test lives (unit vs WebSocket integration), the fixtures available (`make_client`, `send_audio`, `collect`), a worked example of adding a test for a new message kind, how to run a single test, what CI runs, and how to read a CI failure, including downloading the JUnit artifact.
- **`docs/decisions.md`**: short ADRs:
  1. The echo backend for CI/k8s, because MLX only runs on Apple Silicon.
  2. `replicas: 1` and why.
  3. Push only from main/tags.
  4. Stdlib-only ci-insights.
- **`docs/runbook.md`**: what to do when CI is red, when the image push fails (permissions, package visibility), and when the kind rollout hangs.

**Done when** a new reader can go from clone to a green local `pytest`, a local kind deploy, and an understanding of the pipeline using only the docs.

Notes:

---

## Out of scope
- Running the real Moshi model in CI or Kubernetes. A GPU/CUDA variant using Kyutai's PyTorch `moshi` package could be mentioned in `decisions.md` as future work.
- Deploying to a cloud cluster (EKS etc.). kind is enough to show the manifests are real.
