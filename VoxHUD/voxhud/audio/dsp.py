"""Tiny PCM helpers. Avoid scipy to keep the runtime light."""

from __future__ import annotations

import numpy as np


def pcm16_to_float(pcm: bytes | bytearray | np.ndarray) -> np.ndarray:
    if isinstance(pcm, np.ndarray):
        if pcm.dtype == np.int16:
            return (pcm.astype(np.float32) / 32768.0).clip(-1.0, 1.0)
        return pcm.astype(np.float32)
    if not pcm:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(bytes(pcm), dtype="<i2").astype(np.float32) / 32768.0


def float_to_pcm16(samples: np.ndarray) -> bytes:
    arr = np.asarray(samples, dtype=np.float32)
    arr = np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=-1.0)
    return (np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def pcm16_array(pcm: bytes) -> np.ndarray:
    if not pcm:
        return np.zeros(0, dtype=np.int16)
    return np.frombuffer(bytes(pcm), dtype="<i2").copy()


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear resampling; good enough for speech at 16–48 kHz."""
    if not pcm or src_rate == dst_rate:
        return pcm
    src = pcm16_array(pcm)
    if src.size == 0:
        return b""
    n_dst = max(1, int(round(src.size * dst_rate / src_rate)))
    x_old = np.arange(src.size, dtype=np.float64)
    x_new = np.linspace(0.0, src.size - 1, n_dst, dtype=np.float64)
    out = np.interp(x_new, x_old, src.astype(np.float64))
    return np.clip(out, -32768, 32767).astype("<i2").tobytes()


def rms(pcm: bytes | np.ndarray) -> float:
    arr = pcm16_to_float(pcm)
    if arr.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(arr)) + 1e-12))


def rms_db(pcm: bytes | np.ndarray) -> float:
    value = max(rms(pcm), 1e-7)
    return 20.0 * float(np.log10(value))


def scale_pcm16(pcm: bytes, gain: float) -> bytes:
    if not pcm or gain == 1.0:
        return pcm
    arr = pcm16_to_float(pcm) * float(gain)
    return float_to_pcm16(arr)
