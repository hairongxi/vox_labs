"""Energy + WebRTC-VAD barge-in policy.

Runs on AEC-cleaned microphone audio, so the assistant's own voice is normally
removed before this policy sees it. The policy deliberately requires *sustained*
speech (not a click or a cough) and has a guard interval after playback starts,
which prevents the most common false interruption: AEC not yet converged.
"""

from __future__ import annotations

from ..config import TurnConfig
from .interfaces import BargeInContext, BargeInDecision, TurnState, VadFrame


class SpeechBargeInPolicy:
    def __init__(self, config: TurnConfig) -> None:
        self.config = config
        self.reset()

    def reset(self) -> None:
        self._run_started_ms: int | None = None
        self._last_speech_ms: int | None = None
        self._armed = False

    def on_frame(self, frame: VadFrame, context: BargeInContext) -> BargeInDecision:
        cfg = self.config
        if not cfg.barge_in_enabled:
            self.reset()
            return BargeInDecision.no()

        if context.state not in (TurnState.THINKING, TurnState.SPEAKING):
            self.reset()
            return BargeInDecision.no()

        if context.state == TurnState.THINKING and not cfg.allow_thinking_interrupt:
            self.reset()
            return BargeInDecision.no()

        now = context.now_ms
        if (
            context.last_barge_in_ms is not None
            and now - context.last_barge_in_ms < cfg.barge_in_cooldown_ms
        ):
            return BargeInDecision.no()

        # Guard relative to when the assistant state began, not when the user
        # happened to start speaking.
        anchor = context.speaking_since_ms
        if context.state == TurnState.THINKING or anchor is None:
            anchor = context.state_since_ms
        if anchor is not None and now - anchor < cfg.barge_in_guard_ms:
            self._run_started_ms = None
            self._last_speech_ms = None
            return BargeInDecision.no()

        if not self._armed:
            # A short period of non-speech before the first trigger prevents a
            # single loud frame from firing.
            self._armed = True

        sensitivity = max(0.0, min(1.0, cfg.barge_in_sensitivity))
        min_prob = 0.78 - 0.38 * sensitivity
        rms_threshold = 0.034 - 0.026 * sensitivity

        speech = bool(frame.is_speech) or frame.speech_probability >= min_prob
        strong_enough = frame.rms >= rms_threshold

        if speech and strong_enough:
            # A long enough silence means this is a new run, not a continuation.
            if self._last_speech_ms is not None and now - self._last_speech_ms > 120:
                self._run_started_ms = None
            self._last_speech_ms = now
            if self._run_started_ms is None:
                self._run_started_ms = now
            duration = now - self._run_started_ms
            if duration >= cfg.barge_in_min_speech_ms:
                self._run_started_ms = None
                return BargeInDecision(
                    trigger=True,
                    reason="user_speech",
                    confidence=min(1.0, 0.55 + duration / 500.0),
                    speech_ms=duration,
                )
        else:
            if self._last_speech_ms is None or now - self._last_speech_ms > 240:
                self._run_started_ms = None

        return BargeInDecision.no()
