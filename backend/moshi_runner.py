"""Runs the Mimi codec and the Moshi LM as a three-stage threaded pipeline.

    PCM frame -> [Mimi encode] -> codes -> [Moshi LM step] -> codes -> [Mimi decode] -> PCM frame
                                                   \\-> text piece

Each stage lives on its own long-lived thread so the stages overlap: on an M1
Pro the LM step takes ~50 ms and Mimi encode/decode ~25 ms each, which only
fits Moshi's 80 ms frame budget when they run concurrently (rustymimi and MLX
both release the GIL while computing).

Sessions share the model. Starting a session sends a reset marker down the
pipeline so every stage clears its streaming state in order; frames that
belong to a closed session are dropped.
"""

import json
import logging
import queue
import threading
from collections.abc import Callable

import mlx.core as mx
import mlx.nn as nn
import numpy as np
import rustymimi
import sentencepiece
from huggingface_hub import hf_hub_download
from moshi_mlx import models, utils

from .audio_utils import FRAME_SIZE

log = logging.getLogger(__name__)

MIMI_FILE = "tokenizer-e351c8d8-checkpoint125.safetensors"
TEXT_TOKENIZER_FILE = "tokenizer_spm_32k_3.model"
# Text tokens that carry no text (padding and end-of-padding).
SILENT_TEXT_TOKENS = (0, 3)
# Warn when this many frames are queued in front of the LM.
BACKLOG_WARNING = 3

_RESET = object()


class Session:
    """One conversation. Callbacks are invoked from the runner's worker threads."""

    def __init__(
        self,
        runner: "MoshiRunner",
        on_audio: Callable[[np.ndarray], None],
        on_text: Callable[[str], None],
        on_error: Callable[[str], None],
    ):
        self._runner = runner
        self.on_audio = on_audio
        self.on_text = on_text
        self.on_error = on_error
        self.closed = False

    def push_frame(self, pcm: np.ndarray) -> None:
        assert pcm.shape == (FRAME_SIZE,), pcm.shape
        if not self.closed:
            self._runner._encode_q.put((self, pcm))

    def close(self) -> None:
        self.closed = True

    def fail(self, message: str) -> None:
        if not self.closed:
            self.closed = True
            self.on_error(message)


class MoshiRunner:
    def __init__(self, hf_repo: str, quantized: int | None, max_steps: int):
        self.max_steps = max_steps

        lm_config = self._load_lm_config(hf_repo)
        model_file = {
            4: "model.q4.safetensors",
            8: "model.q8.safetensors",
            None: "model.safetensors",
        }[quantized]

        log.info("loading text tokenizer")
        self._text_tokenizer = sentencepiece.SentencePieceProcessor(hf_hub_download(hf_repo, TEXT_TOKENIZER_FILE))

        log.info("loading Moshi weights from %s/%s", hf_repo, model_file)
        mx.random.seed(299792458)
        self._model = models.Lm(lm_config)
        self._model.set_dtype(mx.bfloat16)
        if quantized is not None:
            nn.quantize(self._model, bits=quantized, group_size=32 if quantized == 4 else 64)
        self._model.load_weights(hf_hub_download(hf_repo, model_file), strict=True)
        self._model.warmup()
        self._num_codebooks = lm_config.depformer.num_slices

        log.info("loading Mimi codec")
        mimi_file = hf_hub_download(hf_repo, MIMI_FILE)
        # Separate instances: encoder and decoder state advance independently
        # and are driven from different threads.
        self._mimi_encoder = rustymimi.Tokenizer(mimi_file, num_codebooks=self._num_codebooks)
        self._mimi_decoder = rustymimi.Tokenizer(mimi_file, num_codebooks=self._num_codebooks)
        self._gen: models.LmGen | None = None
        self._warming_up = True

        self._encode_q: queue.Queue = queue.Queue()
        self._lm_q: queue.Queue = queue.Queue()
        self._decode_q: queue.Queue = queue.Queue()
        for target, name in (
            (self._encode_loop, "mimi-encode"),
            (self._lm_loop, "moshi-lm"),
            (self._decode_loop, "mimi-decode"),
        ):
            threading.Thread(target=target, name=name, daemon=True).start()

        self._warmup()
        self._warming_up = False
        log.info("model ready")

    @staticmethod
    def _load_lm_config(hf_repo: str) -> models.LmConfig:
        try:
            with open(hf_hub_download(hf_repo, "config.json")) as f:
                return models.LmConfig.from_config_dict(json.load(f))
        except Exception:
            # The original Moshi MLX repos ship without a config.json.
            return models.config_v0_1()

    def new_session(
        self,
        on_audio: Callable[[np.ndarray], None],
        on_text: Callable[[str], None],
        on_error: Callable[[str], None],
    ) -> Session:
        session = Session(self, on_audio, on_text, on_error)
        self._encode_q.put((session, _RESET))
        return session

    def _warmup(self) -> None:
        """Push silence through the whole pipeline so the first real frame is fast."""
        decoded = threading.Event()
        session = self.new_session(
            on_audio=lambda _: decoded.set(),
            on_text=lambda _: None,
            on_error=lambda msg: log.warning("warmup error: %s", msg),
        )
        silence = np.zeros(FRAME_SIZE, dtype=np.float32)
        for _ in range(8):
            session.push_frame(silence)
        if not decoded.wait(timeout=60):
            raise RuntimeError("pipeline warmup timed out")
        session.close()

    def _encode_loop(self) -> None:
        while True:
            session, item = self._encode_q.get()
            if item is _RESET:
                self._mimi_encoder.reset()
                self._lm_q.put((session, _RESET))
                continue
            if session.closed:
                continue
            try:
                codes = self._mimi_encoder.encode_step(item[None, None])
            except Exception as e:
                log.exception("mimi encode failed")
                session.fail(f"audio encoding failed: {e}")
                continue
            self._lm_q.put((session, codes))

    def _lm_loop(self) -> None:
        while True:
            session, item = self._lm_q.get()
            if item is _RESET:
                for cache in self._model.transformer_cache:
                    cache.reset()
                self._gen = models.LmGen(
                    model=self._model,
                    max_steps=self.max_steps,
                    text_sampler=utils.Sampler(),
                    audio_sampler=utils.Sampler(),
                )
                self._decode_q.put((session, _RESET))
                continue
            if session.closed:
                continue
            gen = self._gen
            if gen.step_idx >= self.max_steps:
                session.fail("session length limit reached")
                continue
            backlog = self._lm_q.qsize()
            if backlog >= BACKLOG_WARNING and not self._warming_up:
                log.warning("LM is %d frames behind real time", backlog)
            try:
                # codes: (batch, codebooks, 1) -> (batch, codebooks)
                text_token = gen.step(mx.array(item)[:, : self._num_codebooks, 0])[0].item()
                audio_tokens = gen.last_audio_tokens()
            except Exception as e:
                log.exception("LM step failed")
                session.fail(f"model step failed: {e}")
                continue
            if text_token not in SILENT_TEXT_TOKENS:
                piece = self._text_tokenizer.id_to_piece(text_token).replace("▁", " ")
                session.on_text(piece)
            if audio_tokens is not None:
                self._decode_q.put((session, np.array(audio_tokens).astype(np.uint32)))

    def _decode_loop(self) -> None:
        while True:
            session, item = self._decode_q.get()
            if item is _RESET:
                self._mimi_decoder.reset()
                continue
            if session.closed:
                continue
            try:
                pcm = self._mimi_decoder.decode_step(item[:, :, None])
            except Exception as e:
                log.exception("mimi decode failed")
                session.fail(f"audio decoding failed: {e}")
                continue
            session.on_audio(pcm[0, 0])
