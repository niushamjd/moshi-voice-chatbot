# Echo-backend image: the server, the protocol and the browser client, without
# the model. MLX only runs on Apple Silicon, so the real Moshi stays on the Mac.

# The deps are installed in a build stage and copied over, for two reasons:
# - sphn only ships Linux wheels for x86_64. On arm64 (e.g. building on an M1
#   Mac) pip compiles it from source, which needs a C toolchain and CMake
#   (maturin fetches Rust itself). None of that should be in the final image.
# - sphn's extension module is ~80 MB unstripped, even in the PyPI wheel.
#   Stripping it is most of the difference between ~280 MB and ~230 MB.
FROM python:3.12-slim AS deps
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential cmake pkg-config \
    && rm -rf /var/lib/apt/lists/*
# The Opus copy bundled with sphn declares an old cmake_minimum_required, which
# CMake 4 rejects unless told to assume a newer policy level.
ENV CMAKE_POLICY_VERSION_MINIMUM=3.5
COPY requirements-server.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements-server.txt \
    && find /install -name "*.so" -exec strip --strip-unneeded {} +

FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/niushamjd/moshi-voice-chatbot" \
      org.opencontainers.image.title="moshi-voice-chatbot" \
      org.opencontainers.image.description="Full-duplex voice chat server (echo backend) with the Moshi web client"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=deps /install /usr/local

COPY backend/ backend/
COPY frontend/ frontend/

# Numeric UID so Kubernetes can verify runAsNonRoot.
RUN useradd --system --uid 10001 --no-create-home moshi
USER 10001

ENV MOSHI_BACKEND=echo MOSHI_HOST=0.0.0.0 MOSHI_PORT=8998
EXPOSE 8998

# The slim image has no curl, so probe with the stdlib.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['MOSHI_PORT']}/healthz\", timeout=2)"]

CMD ["python", "-m", "backend.server"]
