"""Offline demo providers.

Demo mode exists so the complete audio/turn pipeline can be exercised without
credentials. ASR returns scripted transcripts after local VAD detects an
utterance; the LLM streams canned Chinese answers; TTS emits speech-like tones
through the real speaker/AEC path (not intelligible speech).
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from typing import Callable

import numpy as np

from ..audio.dsp import float_to_pcm16, rms
from ..config import DemoConfig
from .base import AsrEvent, ProviderError

SAMPLE_RATE = 16000
TTS_RATE = 24000
FRAME_SAMPLES = 320  # 20 ms
log = logging.getLogger(__name__)


class DemoAsrProvider:
    def __init__(self, config: DemoConfig, on_event: Callable[[AsrEvent], None]) -> None:
        self.config = config
        self.on_event = on_event
        self._turns = list(config.turns) or ["你好，请介绍一下你自己。"]
        self._index = 0
        self._active = False
        self._speech_ms = 0
        self._silence_ms = 0
        self._partial_sent = False
        self._text = ""
        self._energy = deque(maxlen=8)
        try:
            import webrtcvad

            self._vad = webrtcvad.Vad(2)
        except Exception:
            self._vad = None
        self._closed = False

    async def start(self) -> None:
        self.on_event(AsrEvent("ready"))

    def feed(self, pcm16: bytes) -> None:
        if self._closed or not pcm16:
            return
        if len(pcm16) < FRAME_SAMPLES * 2:
            pcm16 = pcm16.ljust(FRAME_SAMPLES * 2, b"\x00")
        elif len(pcm16) > FRAME_SAMPLES * 2:
            pcm16 = pcm16[: FRAME_SAMPLES * 2]

        value = rms(pcm16)
        self._energy.append(value)
        if len(self._energy) < 3:
            return
        energy_vote = sum(self._energy) / len(self._energy) > 0.025
        try:
            vad_vote = bool(self._vad and self._vad.is_speech(pcm16, SAMPLE_RATE))
        except Exception:
            vad_vote = False
        # Requiring both signals prevents AGC-amplified room noise from being
        # interpreted as a user turn in demo mode.
        speech = energy_vote and vad_vote

        if speech:
            self._silence_ms = 0
            self._speech_ms += 20
            if not self._active:
                self._active = True
                self._partial_sent = False
                self._speech_ms = 20
                self._text = self._turns[self._index % len(self._turns)]
                self._index += 1
                log.debug(
                    "demo ASR speech_started rms=%.5f vad=%s energy=%.5f",
                    value, vad_vote, float(sum(self._energy) / len(self._energy)),
                )
                self.on_event(AsrEvent("speech_started"))
            if self._speech_ms >= 320 and not self._partial_sent:
                self._partial_sent = True
                self.on_event(AsrEvent("partial", text=self._text))
        else:
            if self._active:
                self._silence_ms += 20
                if self._silence_ms >= 680:
                    text = self._text
                    self._active = False
                    self._speech_ms = 0
                    self._silence_ms = 0
                    log.debug("demo ASR final text=%r rms=%.5f", text, value)
                    self.on_event(AsrEvent("final", text=text))
                    self.on_event(AsrEvent("speech_stopped"))

    async def close(self) -> None:
        self._closed = True


class DemoLlmProvider:
    def __init__(self, config: DemoConfig) -> None:
        self.config = config

    async def stream(
        self,
        messages: list[dict[str, str]],
        on_delta: Callable[[str], None],
        cancel: asyncio.Event,
    ) -> str:
        user = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                user = message.get("content", "")
                break
        text = _demo_answer(user)
        delay = max(0, self.config.response_delay_ms) / 1000.0
        for i in range(0, len(text), 2):
            if cancel.is_set():
                break
            on_delta(text[i : i + 2])
            await asyncio.sleep(delay)
        return text

    async def close(self) -> None:
        return


class DemoTtsFactory:
    def __init__(self, config: DemoConfig) -> None:
        self.config = config

    def __call__(
        self,
        on_audio: Callable[[bytes, int], None],
        on_error: Callable[[str], None],
    ) -> "ToneTtsSession":
        return ToneTtsSession(self.config, on_audio, on_error)


class ToneTtsSession:
    """Speech-like tone generator; intentionally not intelligible speech."""

    def __init__(
        self,
        config: DemoConfig,
        on_audio: Callable[[bytes, int], None],
        on_error: Callable[[str], None],
    ) -> None:
        self.config = config
        self.on_audio = on_audio
        self.on_error = on_error
        self._tasks: list[asyncio.Task] = []
        self._aborted = False
        self._closed = False

    async def open(self) -> None:
        return

    async def append(self, text: str) -> None:
        if not text.strip() or self._aborted:
            return
        self._tasks.append(asyncio.create_task(self._synth(text)))

    async def finish(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def abort(self) -> None:
        self._aborted = True
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

    async def _synth(self, text: str) -> None:
        try:
            pcm = self._render(text)
            # Feed in 100 ms blocks so barge-in can interrupt mid-answer.
            block = TTS_RATE // 10 * 2
            for i in range(0, len(pcm), block):
                if self._aborted:
                    return
                self.on_audio(pcm[i : i + block], TTS_RATE)
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._aborted:
                self.on_error(f"Demo TTS 错误：{exc}")

    def _render(self, text: str) -> bytes:
        chunks: list[np.ndarray] = []
        sample = 0
        for char in text:
            if char.isspace():
                chunks.append(np.zeros(int(TTS_RATE * 0.04), dtype=np.float32))
                sample += 1
                continue
            if char in "。！？!?，,、；;：:":
                chunks.append(np.zeros(int(TTS_RATE * 0.09), dtype=np.float32))
                continue
            duration = 0.075
            n = int(TTS_RATE * duration)
            t = np.arange(n, dtype=np.float32) / TTS_RATE
            base = 190.0 + (ord(char) * 7 % 170)
            wave = (
                np.sin(2 * math.pi * base * t) * 0.65
                + np.sin(2 * math.pi * base * 1.5 * t) * 0.22
                + np.sin(2 * math.pi * base * 2.0 * t) * 0.13
            )
            env = np.ones(n, dtype=np.float32)
            attack = max(1, int(0.012 * TTS_RATE))
            release = max(1, int(0.03 * TTS_RATE))
            env[:attack] = np.linspace(0, 1, attack, dtype=np.float32)
            env[-release:] = np.linspace(1, 0, release, dtype=np.float32)
            chunks.append((wave * env * self.config.tone_volume).astype(np.float32))
            sample += 1
        if not chunks:
            return b""
        return float_to_pcm16(np.concatenate(chunks))


def _demo_answer(user: str) -> str:
    text = user.strip()
    if any(k in text for k in ("介绍", "你是谁", "名字", "叫什么")):
        return (
            "我是 VoxHUD，一个跑在你桌面上的全双工语音助手。"
            "你可以直接说话，我会在停顿后接话；我说话时你也可以随时打断。"
            "现在的语音是演示音调，配置 Qwen 的语音服务后就会变成真正的人声。"
        )
    if "天气" in text:
        return (
            "我还没有接入天气数据。你可以把天气查询加成一个工具，"
            "或者先问我想了解哪座城市，我可以帮你设计接入方式。"
        )
    if "笑话" in text:
        return (
            "来一个程序员笑话：为什么程序员总分不清万圣节和圣诞节？"
            "因为 Oct 三十一 等于 Dec 二十五。"
        )
    if any(k in text for k in ("能做", "帮我", "功能")):
        return (
            "我可以做三件事：听你说话并把语音转成文字，"
            "调用大模型生成回答，再把回答实时读出来。"
            "你也可以打断我、修改我的状态文案，或者换成自己的语音服务。"
        )
    if not text:
        return "你好，我在。直接说话就可以开始。"
    return (
        f"我听到你说：{text}。"
        "这是演示模式下的本地回复。配置 DashScope 的 ASR、TTS 和 LLM 后，"
        "我就能用 Qwen 真正理解并回答你。"
    )
