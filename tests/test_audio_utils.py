import numpy as np
import pytest

from backend.audio_utils import FRAME_SIZE, MessageKind, PcmFramer, decode_message, encode_message


def test_message_kinds_match_kyutai_protocol():
    assert [int(k) for k in MessageKind] == [0, 1, 2, 5]


def test_encode_decode_round_trip():
    assert decode_message(encode_message(MessageKind.TEXT, b"hi")) == (MessageKind.TEXT, b"hi")


def test_encode_without_payload_is_one_byte():
    assert encode_message(MessageKind.HANDSHAKE) == b"\x00"


def test_decode_empty_message_raises():
    with pytest.raises(ValueError):
        decode_message(b"")


def test_framer_emits_nothing_until_a_full_frame():
    framer = PcmFramer()
    assert framer.push(np.ones(FRAME_SIZE - 1, dtype=np.float32)) == []
    frames = framer.push(np.ones(1, dtype=np.float32))
    assert len(frames) == 1 and frames[0].shape == (FRAME_SIZE,)


def test_framer_splits_and_keeps_remainder_in_order():
    framer = PcmFramer()
    pcm = np.arange(FRAME_SIZE * 2 + 10, dtype=np.float32)
    frames = framer.push(pcm)
    assert len(frames) == 2
    rest = framer.push(np.arange(FRAME_SIZE - 10, dtype=np.float32))
    np.testing.assert_array_equal(np.concatenate(frames), pcm[: FRAME_SIZE * 2])
    np.testing.assert_array_equal(rest[0][:10], pcm[FRAME_SIZE * 2 :])


def test_framer_ignores_empty_input():
    assert PcmFramer().push(np.zeros(0, dtype=np.float32)) == []


def test_framer_converts_to_float32():
    frames = PcmFramer(frame_size=4).push(np.ones(4, dtype=np.float64))
    assert frames[0].dtype == np.float32
