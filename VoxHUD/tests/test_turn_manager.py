from __future__ import annotations

import unittest

from voxhud.config import TurnConfig
from voxhud.turns.barge_in import SpeechBargeInPolicy
from voxhud.turns.eot import HybridEndOfTurnDetector
from voxhud.turns.interfaces import (
    BargeInDecision,
    TurnCallbacks,
    TurnState,
    VadFrame,
)
from voxhud.turns.manager import TurnManager, now_ms


class FakePolicy:
    def reset(self) -> None:
        pass

    def on_frame(self, frame, context):
        return BargeInDecision(trigger=True, reason="fake", confidence=1.0, speech_ms=200)


class Callbacks:
    def __init__(self) -> None:
        self.states: list[tuple[TurnState, str]] = []
        self.partials: list[str] = []
        self.finals: list[str] = []
        self.commits = []
        self.barge_ins = []
        self.cancels: list[str] = []

    def callbacks(self) -> TurnCallbacks:
        return TurnCallbacks(
            on_state=lambda s, d: self.states.append((s, d)),
            on_user_partial=lambda t: self.partials.append(t),
            on_user_final=lambda t: self.finals.append(t),
            on_commit=lambda turn: self.commits.append(turn),
            on_barge_in=lambda reason, decision: self.barge_ins.append((reason, decision)),
            on_cancel=lambda reason: self.cancels.append(reason),
        )


class TurnManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = TurnConfig(eot_hold_ms=100, eot_terminal_hold_ms=50)
        self.cb = Callbacks()
        self.detector = HybridEndOfTurnDetector(self.cfg)
        self.manager = TurnManager(
            self.detector,
            FakePolicy(),
            self.cb.callbacks(),
            barge_in_on_asr=True,
            asr_barge_in_min_ms=0,
        )
        self.manager.start()

    def test_commit_then_speaking_then_listening(self) -> None:
        self.manager.on_asr_final("你好，请介绍一下你自己。", 100)
        self.manager.poll(200)
        self.assertEqual(len(self.cb.commits), 1)
        turn = self.cb.commits[0]
        self.assertEqual(turn.text, "你好，请介绍一下你自己。")
        self.assertEqual(self.manager.state, TurnState.THINKING)
        self.manager.on_assistant_started(turn.turn_id, 300)
        self.assertEqual(self.manager.state, TurnState.SPEAKING)
        self.manager.on_assistant_finished(turn.turn_id, 400)
        self.assertEqual(self.manager.state, TurnState.LISTENING)

    def test_barge_in_cancels_and_ignores_stale_finish(self) -> None:
        self.manager.on_asr_final("你好，请介绍一下你自己。", 100)
        self.manager.poll(200)
        turn = self.cb.commits[0]
        self.manager.on_assistant_started(turn.turn_id, 300)
        frame = VadFrame(now_ms(), 0.05, 0.9, True)
        self.manager.on_vad_frame(frame)
        self.assertEqual(self.manager.state, TurnState.INTERRUPTED)
        self.assertIn("fake", self.cb.cancels)
        self.assertEqual(self.manager.epoch, 1)
        self.manager.on_assistant_finished(turn.turn_id, now_ms() + 10)
        self.assertEqual(self.manager.state, TurnState.INTERRUPTED)

    def test_stale_assistant_start_is_ignored(self) -> None:
        self.manager.on_asr_final("你好，请介绍一下你自己。", 100)
        self.manager.poll(200)
        self.manager.on_assistant_started(999, 300)
        self.assertEqual(self.manager.state, TurnState.THINKING)

    def test_interrupt_while_listening_resets(self) -> None:
        self.manager.interrupt("manual")
        self.assertEqual(self.manager.state, TurnState.LISTENING)
        self.assertFalse(self.cb.commits)

    def test_force_commit(self) -> None:
        self.manager.force_commit("文字输入测试")
        self.assertEqual(len(self.cb.commits), 1)
        self.assertEqual(self.cb.commits[0].text, "文字输入测试")
        self.assertEqual(self.manager.state, TurnState.THINKING)

    def test_asr_final_during_speaking_can_barge_in(self) -> None:
        t0 = now_ms()
        self.manager.on_asr_final("第一轮，请介绍一下你自己。", t0 + 100)
        self.manager.poll(t0 + 200)
        turn = self.cb.commits[0]
        self.manager.on_assistant_started(turn.turn_id, t0 + 300)
        self.manager.on_server_speech_started(t0 + 1200)
        self.manager.on_asr_final("等一下，换一个问题。", t0 + 1300)
        self.assertEqual(self.manager.state, TurnState.INTERRUPTED)
        self.assertIn("asr_final", self.cb.cancels)
        self.assertIn("等一下，换一个问题。", self.cb.finals)


if __name__ == "__main__":
    unittest.main()
