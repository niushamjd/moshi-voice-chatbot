"""Wire protocol and PCM helpers shared by the server and the model runner.

The WebSocket protocol is the one used by Kyutai's own Moshi servers, so the
browser client works against both this backend and `moshi_mlx.local_web`.
Every binary message starts with a one-byte kind:

    0x00 HANDSHAKE  server -> client, sent once the model is ready
    0x01 AUDIO      both directions, payload is a chunk of an Ogg/Opus stream
    0x02 TEXT       server -> client, payload is a UTF-8 text piece
    0x05 ERROR      server -> client, payload is a UTF-8 error message
"""

from enum import IntEnum

import numpy as np

SAMPLE_RATE = 24000
# Mimi consumes and produces 80 ms frames (12.5 Hz) at 24 kHz.
FRAME_SIZE = 1920


class MessageKind(IntEnum):
    HANDSHAKE = 0
    AUDIO = 1
    TEXT = 2
    ERROR = 5


def encode_message(kind: MessageKind, payload: bytes = b"") -> bytes:
    return bytes([kind]) + payload


def decode_message(message: bytes) -> tuple[int, bytes]:
    if not message:
        raise ValueError("empty message")
    return message[0], message[1:]


class PcmFramer:
    """Re-chunks a stream of float32 PCM into fixed FRAME_SIZE frames."""

    def __init__(self, frame_size: int = FRAME_SIZE):
        self.frame_size = frame_size
        self._pending = np.zeros(0, dtype=np.float32)

    def push(self, pcm: np.ndarray) -> list[np.ndarray]:
        if pcm.size == 0:
            return []
        self._pending = np.concatenate((self._pending, pcm.astype(np.float32)))
        n_frames = self._pending.size // self.frame_size
        frames = [
            self._pending[i * self.frame_size : (i + 1) * self.frame_size]
            for i in range(n_frames)
        ]
        self._pending = self._pending[n_frames * self.frame_size :]
        return frames
