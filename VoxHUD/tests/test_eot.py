from __future__ import annotations

import unittest

from voxhud.config import TurnConfig
from voxhud.turns.eot import HybridEndOfTurnDetector
from voxhud.turns.interfaces import VadFrame


def frame(t: int, speech: bool = False, rms: float = 0.02) -> VadFrame:
    return VadFrame(
        timestamp_ms=t,
        rms=rms if speech else 0.001,
        speech_probability=0.9 if speech else 0.05,
        is_speech=speech,
    )


class EndOfTurnTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = TurnConfig(
            eot_hold_ms=300,
            eot_terminal_hold_ms=100,
            eot_asr_timeout_ms=900,
            eot_max_utterance_ms=10000,
            eot_empty_reset_ms=1500,
        )
        self.det = HybridEndOfTurnDetector(self.cfg)

    def test_terminal_final_commits_after_short_hold(self) -> None:
        self.det.on_vad(frame(0, True))
        self.det.on_asr_partial("你好", 200)
        self.det.on_asr_final("你好。", 1000)
        self.assertFalse(self.det.poll(1200).commit)
        decision = self.det.poll(1300)
        self.assertTrue(decision.commit)
        self.assertEqual(decision.text, "你好。")
        self.assertEqual(decision.reason, "asr_final")
        self.assertGreaterEqual(decision.confidence, 0.9)

    def test_resumed_speech_withdraws_candidate(self) -> None:
        self.det.on_vad(frame(0, True))
        self.det.on_asr_final("我想问", 500)
        self.assertFalse(self.det.poll(650).commit)
        self.det.on_vad(frame(700, True))
        self.assertFalse(self.det.poll(900).commit)
        self.assertEqual(self.det.snapshot()["candidate_at_ms"], None)

    def test_filler_is_not_committed(self) -> None:
        self.det.on_asr_final("嗯", 100)
        self.assertFalse(self.det.poll(500).commit)
        self.assertFalse(self.det.poll(900).commit)
        self.det.on_asr_final("嗯，我想问一下天气。", 1000)
        self.assertTrue(self.det.poll(1400).commit)

    def test_question_mark_short_hold(self) -> None:
        self.det.on_asr_final("今天天气怎么样？", 0)
        self.assertFalse(self.det.poll(90).commit)
        self.assertTrue(self.det.poll(110).commit)

    def test_comma_extends_hold(self) -> None:
        self.det.on_asr_final("我想问一下，", 0)
        self.assertFalse(self.det.poll(300).commit)
        self.assertTrue(self.det.poll(600).commit)

    def test_partial_timeout_does_not_commit_without_text(self) -> None:
        self.det.on_server_speech_stopped(0)
        self.assertFalse(self.det.poll(1000).commit)
        # Empty utterance is eventually discarded.
        self.det.poll(4000)
        self.assertEqual(self.det.snapshot()["final"], "")

    def test_max_utterance_forces_commit(self) -> None:
        self.det.on_vad(frame(0, True))
        self.det.on_asr_partial("这是一段很长的独白", 10)
        decision = self.det.poll(10001)
        self.assertTrue(decision.commit)
        self.assertEqual(decision.reason, "max_utterance")

    def test_new_partial_after_final_withdraws_candidate(self) -> None:
        self.det.on_asr_final("第一句话。", 100)
        self.assertFalse(self.det.poll(150).commit)
        self.det.on_asr_partial("第一句话。还有", 400)
        self.assertFalse(self.det.poll(500).commit)
        self.assertEqual(self.det.snapshot()["final"], "")

    def test_reset_clears_state(self) -> None:
        self.det.on_asr_final("你好。", 0)
        self.det.reset()
        snap = self.det.snapshot()
        self.assertEqual(snap["final"], "")
        self.assertIsNone(snap["candidate_at_ms"])


if __name__ == "__main__":
    unittest.main()
