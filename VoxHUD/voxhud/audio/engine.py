"""Full-duplex audio engine.

One 16 kHz duplex PortAudio stream is used for both directions. That gives AEC a
sample-aligned far-end reference and avoids input/output clock drift. TTS arrives
at 24 kHz and is resampled into the 16 kHz playback queue; assistant audio is
therefore slightly band-limited (16 kHz) but the echo path is much easier to
cancel than with two independent streams.

The PortAudio callback does only bounded work: pull playback samples, run the
WebRTC audio processor, run WebRTC VAD, and post one frame. Network I/O never
happens on this thread.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np

from ..config import AudioConfig
from .dsp import float_to_pcm16, pcm16_array, resample_pcm16, rms

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BLOCK_SAMPLES = 320  # 20 ms


@dataclass(frozen=True)
class AudioFrameData:
    pcm16: bytes
    timestamp_ms: int
    rms: float
    speech_probability: float
    is_speech: bool
    raw_rms: float
    echo_suppression: float


class AudioEngine:
    def __init__(
        self,
        config: AudioConfig,
        on_frame: Callable[[AudioFrameData], None],
        on_level: Callable[[float, float], None],
        on_error: Callable[[str], None],
    ) -> None:
        self.config = config
        self.on_frame = on_frame
        self.on_level = on_level
        self.on_error = on_error
        self._stream = None
        self._lock = threading.Lock()
        self._playback: deque[np.ndarray] = deque()
        self._buffered_samples = 0
        self._epoch = 0
        self._muted = False
        self._running = False
        self._processor = None
        self._vad = None
        self._last_level_at = 0.0
        self._aec_warned = False
        self._rate = SAMPLE_RATE
        self._block = BLOCK_SAMPLES
        self._debug_frames = 0
        self._debug_raw_sum = 0.0
        self._debug_clean_sum = 0.0

    # ------------------------------------------------------------------ public
    def start(self) -> None:
        if self._running:
            return
        import sounddevice as sd

        self._block = max(160, int(SAMPLE_RATE * self.config.block_ms / 1000))
        self._init_processors()
        device = None
        if self.config.input_device is not None or self.config.output_device is not None:
            device = (self.config.input_device, self.config.output_device)
        try:
            self._stream = self._make_stream(sd, device)
            self._stream.start()
        except Exception as exc:
            log.warning("打开音频流失败（指定设备）：%s，尝试默认设备", exc)
            try:
                self._stream = self._make_stream(sd, None)
                self._stream.start()
            except Exception as exc2:
                raise RuntimeError(f"无法打开音频设备：{exc2}") from exc2
        self._running = True
        try:
            latency = self._stream.latency
            measured = int((latency[0] + latency[1]) * 1000)
            if self._processor is not None and hasattr(self._processor, "stream_delay_ms"):
                self._processor.stream_delay_ms = max(
                    int(self.config.aec_delay_ms), min(500, measured + 20)
                )
            log.info("音频流已启动：latency=%s，AEC delay=%sms", latency, measured)
        except Exception:
            pass

    def stop(self) -> None:
        self._running = False
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        with self._lock:
            self._playback.clear()
            self._buffered_samples = 0

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)

    @property
    def muted(self) -> bool:
        return self._muted

    def set_epoch(self, epoch: int) -> None:
        with self._lock:
            if epoch != self._epoch:
                self._epoch = int(epoch)
                self._playback.clear()
                self._buffered_samples = 0

    def stop_playback(self) -> None:
        with self._lock:
            self._playback.clear()
            self._buffered_samples = 0

    def push_tts(self, pcm16: bytes, sample_rate: int, epoch: int) -> None:
        if not pcm16:
            return
        with self._lock:
            if epoch != self._epoch:
                return
            pcm16 = resample_pcm16(pcm16, sample_rate, SAMPLE_RATE)
            if not pcm16:
                return
            arr = pcm16_array(pcm16).astype(np.float32)
            if self.config.output_volume != 1.0:
                arr = arr * float(self.config.output_volume)
            arr = np.clip(arr, -32768, 32767).astype(np.int16)
            self._playback.append(arr)
            self._buffered_samples += arr.size
            max_samples = SAMPLE_RATE * 25
            while self._buffered_samples > max_samples and self._playback:
                dropped = self._playback.popleft()
                self._buffered_samples -= dropped.size

    @property
    def playback_idle(self) -> bool:
        with self._lock:
            return self._buffered_samples == 0

    @property
    def buffered_ms(self) -> int:
        with self._lock:
            return int(self._buffered_samples * 1000 / SAMPLE_RATE)

    @staticmethod
    def list_devices() -> list[dict]:
        import sounddevice as sd

        out: list[dict] = []
        try:
            for index, device in enumerate(sd.query_devices()):
                out.append({
                    "index": index,
                    "name": device["name"],
                    "inputs": int(device["max_input_channels"]),
                    "outputs": int(device["max_output_channels"]),
                    "default_sr": float(device["default_samplerate"]),
                })
        except Exception as exc:
            log.warning("查询音频设备失败：%s", exc)
        return out

    # ---------------------------------------------------------------- internals
    def _make_stream(self, sd, device):
        return sd.Stream(
            samplerate=SAMPLE_RATE,
            blocksize=self._block,
            dtype="int16",
            channels=1,
            latency=self.config.latency,
            device=device,
            callback=self._callback,
        )

    def _init_processors(self) -> None:
        try:
            from pywebrtc_audio import AudioProcessor

            self._processor = AudioProcessor(
                sample_rate=SAMPLE_RATE,
                echo_cancellation=bool(self.config.aec),
                noise_suppression=bool(self.config.noise_suppression),
                auto_gain_control=bool(self.config.agc),
                high_pass_filter=True,
                stream_delay_ms=int(self.config.aec_delay_ms),
            )
        except Exception as exc:
            self._processor = None
            if self.config.aec and not self._aec_warned:
                self._aec_warned = True
                log.warning("WebRTC AEC 不可用：%s", exc)
                self.on_error("回声消除不可用，建议佩戴耳机或安装 pywebrtc-audio")
        try:
            import webrtcvad

            self._vad = webrtcvad.Vad(int(self.config.vad_mode))
        except Exception as exc:
            self._vad = None
            log.warning("WebRTC VAD 不可用：%s", exc)

    def _pull_playback(self, n: int) -> np.ndarray:
        out = np.zeros(n, dtype=np.int16)
        pos = 0
        with self._lock:
            while pos < n and self._playback:
                chunk = self._playback[0]
                take = min(chunk.size, n - pos)
                out[pos : pos + take] = chunk[:take]
                pos += take
                if take == chunk.size:
                    self._playback.popleft()
                else:
                    self._playback[0] = chunk[take:]
                self._buffered_samples -= take
        return out

    def _callback(self, indata, outdata, frames, time_info, status) -> None:
        if status:
            log.debug("PortAudio status: %s", status)
        try:
            near = np.asarray(indata[:, 0], dtype=np.int16).copy()
            if self.config.mic_gain != 1.0:
                near_f = near.astype(np.float32) * float(self.config.mic_gain)
                near = np.clip(near_f, -32768, 32767).astype(np.int16)
            far = self._pull_playback(frames)
            outdata[:, 0] = far

            raw_rms = rms(near)
            if self._processor is not None:
                try:
                    if self.config.aec:
                        clean = self._processor.process(near, far)
                    else:
                        clean = self._processor.process(near)
                    speech_prob = float(getattr(self._processor, "speech_probability", 0.0))
                except Exception as exc:
                    if not self._aec_warned:
                        self._aec_warned = True
                        log.warning("AEC 处理异常，降级为直通：%s", exc)
                    clean = near
                    speech_prob = 0.0
            else:
                clean = near
                speech_prob = 0.0

            clean = np.asarray(clean, dtype=np.int16)
            if self._muted:
                clean = np.zeros_like(clean)
                speech_prob = 0.0
            clean_bytes = clean.tobytes()
            is_speech = False
            if self._vad is not None and not self._muted:
                try:
                    is_speech = bool(self._vad.is_speech(clean_bytes, SAMPLE_RATE))
                except Exception:
                    is_speech = False
            clean_rms = rms(clean_bytes)
            suppression = 0.0
            if raw_rms > 1e-4:
                suppression = max(0.0, min(1.0, 1.0 - clean_rms / raw_rms))
            self._debug_frames += 1
            self._debug_raw_sum += raw_rms
            self._debug_clean_sum += clean_rms
            if self._debug_frames >= 50:
                raw_avg = self._debug_raw_sum / self._debug_frames
                clean_avg = self._debug_clean_sum / self._debug_frames
                if raw_avg > 0.004:
                    log.debug(
                        "audio raw=%.4f clean=%.4f suppression=%.2f speech_prob=%.2f vad=%s echo=%s",
                        raw_avg, clean_avg,
                        1.0 - clean_avg / max(raw_avg, 1e-6),
                        speech_prob, is_speech, self.config.aec,
                    )
                self._debug_frames = 0
                self._debug_raw_sum = 0.0
                self._debug_clean_sum = 0.0
            frame = AudioFrameData(
                pcm16=clean_bytes,
                timestamp_ms=int(time.monotonic() * 1000),
                rms=clean_rms,
                speech_probability=speech_prob,
                is_speech=is_speech,
                raw_rms=raw_rms,
                echo_suppression=suppression,
            )
            try:
                self.on_frame(frame)
            except Exception:
                log.exception("音频帧回调失败")
            now = time.monotonic()
            if now - self._last_level_at >= 0.05:
                self._last_level_at = now
                try:
                    self.on_level(float(clean_rms), float(rms(far)))
                except Exception:
                    pass
        except Exception:
            log.exception("音频回调异常")
            outdata.fill(0)
