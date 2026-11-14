from __future__ import annotations

import unittest

from voxhud.config import TurnConfig
from voxhud.turns.barge_in import SpeechBargeInPolicy
from voxhud.turns.interfaces import BargeInContext, TurnState, VadFrame


def speak(t: int, rms: float = 0.05) -> VadFrame:
    return VadFrame(t, rms, 0.9, True)


def silence(t: int) -> VadFrame:
    return VadFrame(t, 0.001, 0.02, False)


def ctx(t: int, state: TurnState = TurnState.SPEAKING, since: int = 0) -> BargeInContext:
    return BargeInContext(
        now_ms=t,
        state=state,
        state_since_ms=since,
        speaking_since_ms=since,
        assistant_active=True,
    )


class BargeInTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = TurnConfig(
            barge_in_enabled=True,
            barge_in_guard_ms=200,
            barge_in_min_speech_ms=100,
            barge_in_sensitivity=0.5,
            barge_in_cooldown_ms=500,
        )
        self.policy = SpeechBargeInPolicy(self.cfg)

    def test_guard_blocks_early_speech(self) -> None:
        for t in range(0, 200, 20):
            decision = self.policy.on_frame(speak(t), ctx(t))
            self.assertFalse(decision.trigger)

    def test_sustained_speech_triggers_after_min_duration(self) -> None:
        triggered = None
        for t in range(200, 500, 20):
            decision = self.policy.on_frame(speak(t), ctx(t))
            if decision.trigger:
                triggered = decision
                break
        self.assertIsNotNone(triggered)
        self.assertEqual(triggered.reason, "user_speech")
        self.assertGreaterEqual(triggered.speech_ms, 100)

    def test_short_noise_does_not_trigger(self) -> None:
        self.assertFalse(self.policy.on_frame(speak(300), ctx(300)).trigger)
        self.assertFalse(self.policy.on_frame(silence(320), ctx(320)).trigger)
        self.assertFalse(self.policy.on_frame(speak(500), ctx(500)).trigger)
        self.assertFalse(self.policy.on_frame(speak(520), ctx(520)).trigger)
        self.assertFalse(self.policy.on_frame(speak(540), ctx(540)).trigger)

    def test_quiet_speech_does_not_trigger(self) -> None:
        for t in range(300, 600, 20):
            self.assertFalse(self.policy.on_frame(speak(t, rms=0.001), ctx(t)).trigger)

    def test_listening_state_never_triggers(self) -> None:
        for t in range(300, 600, 20):
            decision = self.policy.on_frame(speak(t), ctx(t, TurnState.LISTENING))
            self.assertFalse(decision.trigger)

    def test_thinking_interrupt_can_be_disabled(self) -> None:
        self.cfg.allow_thinking_interrupt = False
        for t in range(300, 600, 20):
            decision = self.policy.on_frame(speak(t), ctx(t, TurnState.THINKING, 0))
            self.assertFalse(decision.trigger)

    def test_cooldown_blocks_repeat(self) -> None:
        for t in range(300, 500, 20):
            decision = self.policy.on_frame(speak(t), ctx(t))
            if decision.trigger:
                break
        self.assertTrue(decision.trigger)
        self.policy.reset()
        early = BargeInContext(520, TurnState.SPEAKING, 0, 0, True, last_barge_in_ms=480)
        self.assertFalse(self.policy.on_frame(speak(540), early).trigger)


if __name__ == "__main__":
    unittest.main()
