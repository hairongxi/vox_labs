from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if (ROOT / "_vendor").is_dir():
    sys.path.insert(0, str(ROOT / "_vendor"))

from voxhud.config import AppConfig
from voxhud.conversation import ConversationController
from voxhud.events import UiBus, UiEventKind
from voxhud.providers.base import AsrEvent


class FakeAudio:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.epoch = 0
        self.stopped = 0
        self.muted = False
        self.playback_idle = True

    def set_epoch(self, epoch: int) -> None:
        self.epoch = epoch

    def push_tts(self, pcm: bytes, rate: int, epoch: int) -> None:
        if epoch == self.epoch:
            self.chunks.append(pcm)

    def stop_playback(self) -> None:
        self.stopped += 1
        self.chunks.clear()

    def set_muted(self, muted: bool) -> None:
        self.muted = muted

    def stop(self) -> None:
        pass


class FakeAsr:
    def __init__(self) -> None:
        self.events = []

    async def start(self) -> None:
        self.events.append("start")

    def feed(self, pcm: bytes) -> None:
        pass

    async def close(self) -> None:
        pass


class FakeLlm:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, messages, on_delta, cancel):
        self.calls += 1
        text = "你好，我是测试助手。"
        for i in range(0, len(text), 2):
            if cancel.is_set():
                break
            on_delta(text[i:i + 2])
            await asyncio.sleep(0.005)
        return text

    async def close(self) -> None:
        pass


class FakeTts:
    def __init__(self, on_audio) -> None:
        self.on_audio = on_audio
        self.aborted = False
        self.finished = False
        self.texts: list[str] = []

    async def open(self) -> None:
        pass

    async def append(self, text: str) -> None:
        self.texts.append(text)
        self.on_audio(b"\x00\x01" * 240, 24000)
        await asyncio.sleep(0.01)

    async def finish(self) -> None:
        self.finished = True

    async def abort(self) -> None:
        self.aborted = True


class FakeTtsFactory:
    def __init__(self) -> None:
        self.sessions: list[FakeTts] = []

    def __call__(self, on_audio, on_error):
        session = FakeTts(on_audio)
        self.sessions.append(session)
        return session


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_turn_history_and_audio(self) -> None:
        cfg = AppConfig()
        cfg.turn.eot_hold_ms = 80
        cfg.turn.eot_terminal_hold_ms = 40
        bus = UiBus()
        loop = asyncio.get_running_loop()
        ctrl = ConversationController(cfg, bus, loop)
        audio = FakeAudio()
        asr = FakeAsr()
        llm = FakeLlm()
        tts_factory = FakeTtsFactory()
        from voxhud.providers import ProviderBundle
        ctrl.providers = ProviderBundle("test", asr, llm, tts_factory, lambda: asyncio.sleep(0))
        ctrl.audio = audio
        ctrl.manager.start()
        ctrl._poll_task = asyncio.create_task(ctrl._poll_loop())
        try:
            ctrl._on_asr_event(AsrEvent("final", text="你好，请介绍一下你自己。"))
            for _ in range(100):
                if len(ctrl.history) >= 2 and ctrl.manager.state.value == "listening":
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(llm.calls, 1)
            self.assertTrue(audio.chunks)
            self.assertEqual(ctrl.history[0]["role"], "user")
            self.assertEqual(ctrl.history[1]["role"], "assistant")
            self.assertIn("测试助手", ctrl.history[1]["content"])
            self.assertTrue(tts_factory.sessions[-1].finished)
            events = bus.drain(200)
            kinds = {e.kind for e in events}
            self.assertIn(UiEventKind.ASSISTANT_DONE.value, kinds)
        finally:
            await ctrl.shutdown()

    async def test_barge_in_cancels_current_answer(self) -> None:
        cfg = AppConfig()
        cfg.turn.eot_hold_ms = 80
        cfg.turn.eot_terminal_hold_ms = 40
        loop = asyncio.get_running_loop()
        ctrl = ConversationController(cfg, UiBus(), loop)
        audio = FakeAudio()
        asr = FakeAsr()
        llm = FakeLlm()
        tts_factory = FakeTtsFactory()
        from voxhud.providers import ProviderBundle
        ctrl.providers = ProviderBundle("test", asr, llm, tts_factory, lambda: asyncio.sleep(0))
        ctrl.audio = audio
        ctrl.manager.start()
        ctrl._poll_task = asyncio.create_task(ctrl._poll_loop())
        try:
            ctrl._on_asr_event(AsrEvent("final", text="你好，请介绍一下你自己。"))
            for _ in range(100):
                if ctrl.manager.state.value == "speaking":
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(ctrl.manager.state.value, "speaking")
            ctrl.interrupt("test")
            await asyncio.sleep(0.1)
            self.assertEqual(ctrl.manager.state.value, "interrupted")
            self.assertTrue(audio.stopped >= 1)
            self.assertTrue(tts_factory.sessions[-1].aborted)
            # The interrupted partial answer must not be committed to history.
            self.assertEqual(len(ctrl.history), 1)
        finally:
            await ctrl.shutdown()


if __name__ == "__main__":
    unittest.main()
