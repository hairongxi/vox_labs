"""Hybrid end-of-turn detector.

The detector fuses three signals:

1. local WebRTC VAD frames (fast, always available),
2. server VAD ``speech_started`` / ``speech_stopped`` from Qwen ASR,
3. ASR partial/final text (the final result already includes the server's
   silence decision).

A final transcript creates a *candidate* commit. The candidate is withdrawn if
speech resumes before the hold expires. Terminal punctuation shortens the hold;
trailing fillers or commas lengthen it. This is intentionally small and
replaceable: a semantic classifier can implement the same protocol later.
"""

from __future__ import annotations

import re
from typing import Any

from ..config import TurnConfig
from .interfaces import EndOfTurnDecision, VadFrame

_TERMINAL = "。！？!?…"
_CLAUSE = "，,、；;：:"
_FILLERS = {
    "呃",
    "额",
    "嗯",
    "啊",
    "哦",
    "唔",
    "那个",
    "然后",
    "就是",
    "um",
    "uh",
    "er",
    "and",
    "so",
}
_TRAILING_FILLERS = ("那个", "然后", "就是", "呃", "嗯", "啊", "um", "uh")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


class HybridEndOfTurnDetector:
    def __init__(self, config: TurnConfig) -> None:
        self.config = config
        self.reset()

    # ------------------------------------------------------------------ reset
    def reset(self) -> None:
        self._utterance_started_ms: int | None = None
        self._last_activity_ms: int | None = None
        self._speech_active = False
        self._last_vad_ms: int | None = None
        self._partial = ""
        self._final = ""
        self._final_at_ms: int | None = None
        self._candidate_at_ms: int | None = None
        self._candidate_reason = ""
        self._candidate_confidence = 0.0
        self._candidate_hold_ms = 0
        self._committed = False
        self._last_vad_speech = False

    # ---------------------------------------------------------------- inputs
    def on_vad(self, frame: VadFrame) -> None:
        self._last_vad_ms = frame.timestamp_ms
        if frame.is_speech:
            if not self._last_vad_speech and self._utterance_started_ms is None:
                self._utterance_started_ms = frame.timestamp_ms
            self._last_vad_speech = True
            self._speech_active = True
            self._last_activity_ms = frame.timestamp_ms
            # Speech resumed: a pending candidate is no longer valid.
            if self._candidate_at_ms is not None:
                self._cancel_candidate()
            # If a final already arrived and the user starts a new phrase, a new
            # ASR item will follow; do not commit the old final.
            if self._final and self._final_at_ms is not None:
                if frame.timestamp_ms - self._final_at_ms > 120:
                    self._final = ""
                    self._final_at_ms = None
        else:
            if self._last_vad_speech:
                self._last_vad_speech = False
            self._speech_active = False

    def on_asr_partial(self, text: str, now_ms: int) -> None:
        text = _clean(text)
        if not text:
            return
        if self._utterance_started_ms is None:
            self._utterance_started_ms = now_ms
        self._partial = text
        self._last_activity_ms = now_ms
        # A new partial after the final (not a duplicate arriving immediately)
        # means the user resumed; withdraw the final candidate and wait for the
        # next completed item.
        if (
            self._final
            and self._final_at_ms is not None
            and now_ms - self._final_at_ms > 150
            and text != self._final
        ):
            self._final = ""
            self._final_at_ms = None
            self._cancel_candidate()
            return
        # A newer partial means the user is still talking; withdraw a candidate
        # only if it was based on an earlier partial (never withdraw a final
        # candidate merely because of a late duplicate partial).
        if self._candidate_at_ms is not None and self._candidate_reason == "asr_timeout":
            self._cancel_candidate()

    def on_asr_final(self, text: str, now_ms: int) -> None:
        text = _clean(text)
        if not text:
            self._last_activity_ms = now_ms
            return
        if self._utterance_started_ms is None:
            self._utterance_started_ms = now_ms
        self._final = text
        self._final_at_ms = now_ms
        self._partial = text
        self._last_activity_ms = now_ms
        hold = self._hold_for(text)
        confidence = self._confidence_for(text)
        self._schedule(now_ms + hold, "asr_final", confidence, hold)

    def on_server_speech_started(self, now_ms: int) -> None:
        self._speech_active = True
        self._last_activity_ms = now_ms
        self._last_vad_speech = True
        if self._utterance_started_ms is None:
            self._utterance_started_ms = now_ms
        if self._candidate_at_ms is not None:
            self._cancel_candidate()
        # A new server-side utterance after a final is a new turn fragment.
        if self._final and self._final_at_ms is not None and now_ms - self._final_at_ms > 120:
            self._final = ""
            self._final_at_ms = None

    def on_server_speech_stopped(self, now_ms: int) -> None:
        self._speech_active = False
        self._last_activity_ms = now_ms
        if self._final:
            # A final should be on its way; keep/extend its candidate.
            if self._candidate_at_ms is None:
                hold = self._hold_for(self._final)
                self._schedule(now_ms + hold, "asr_final", self._confidence_for(self._final), hold)
        else:
            timeout = self.config.eot_asr_timeout_ms
            self._schedule(
                now_ms + timeout,
                "asr_timeout",
                0.35,
                timeout,
            )

    # ----------------------------------------------------------------- poll
    def poll(self, now_ms: int) -> EndOfTurnDecision:
        if self._committed:
            return EndOfTurnDecision.wait()

        # Hard stop for monologues; a max-length utterance should still be
        # answered rather than listened to forever.
        if (
            self._utterance_started_ms is not None
            and now_ms - self._utterance_started_ms >= self.config.eot_max_utterance_ms
        ):
            text = _clean(self._final or self._partial)
            if text:
                self._committed = True
                return EndOfTurnDecision(
                    commit=True,
                    reason="max_utterance",
                    confidence=0.3,
                    text=text,
                    hold_ms=now_ms - (self._utterance_started_ms or now_ms),
                )
            self.reset()
            return EndOfTurnDecision.wait()

        if self._candidate_at_ms is None:
            # No candidate: clear a long-idle empty utterance.
            if (
                self._last_activity_ms is not None
                and now_ms - self._last_activity_ms >= self.config.eot_empty_reset_ms
                and not (self._final or self._partial)
            ):
                self.reset()
            return EndOfTurnDecision.wait()

        if now_ms < self._candidate_at_ms:
            return EndOfTurnDecision.wait()

        text = _clean(self._final or self._partial)
        if not text:
            if (
                self._last_activity_ms is not None
                and now_ms - self._last_activity_ms >= self.config.eot_empty_reset_ms
            ):
                self.reset()
            return EndOfTurnDecision.wait()

        if self._is_filler(text):
            # Do not answer an isolated "嗯". Give the user room to continue.
            self._candidate_at_ms = now_ms + 650
            self._candidate_reason = "filler_wait"
            self._candidate_confidence = 0.2
            return EndOfTurnDecision.wait()

        self._committed = True
        return EndOfTurnDecision(
            commit=True,
            reason=self._candidate_reason or "asr_final",
            confidence=self._candidate_confidence,
            text=text,
            hold_ms=self._candidate_hold_ms,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "utterance_started_ms": self._utterance_started_ms,
            "last_activity_ms": self._last_activity_ms,
            "speech_active": self._speech_active,
            "partial": self._partial,
            "final": self._final,
            "candidate_at_ms": self._candidate_at_ms,
            "candidate_reason": self._candidate_reason,
            "candidate_confidence": self._candidate_confidence,
        }

    # -------------------------------------------------------------- helpers
    def _schedule(self, at_ms: int, reason: str, confidence: float, hold_ms: int) -> None:
        self._candidate_at_ms = at_ms
        self._candidate_reason = reason
        self._candidate_confidence = confidence
        self._candidate_hold_ms = max(0, hold_ms)

    def _cancel_candidate(self) -> None:
        self._candidate_at_ms = None
        self._candidate_reason = ""
        self._candidate_confidence = 0.0
        self._candidate_hold_ms = 0

    def _hold_for(self, text: str) -> int:
        hold = max(0, int(self.config.eot_hold_ms))
        if text.endswith(tuple(_TERMINAL)):
            hold = min(hold, max(0, int(self.config.eot_terminal_hold_ms)))
        elif text.endswith(tuple(_CLAUSE)):
            hold += 260
        if len(re.sub(r"[\W_]+", "", text)) <= 4:
            hold += 160
        if text.endswith(_TRAILING_FILLERS):
            hold += 300
        return hold

    def _confidence_for(self, text: str) -> float:
        if text.endswith(tuple(_TERMINAL)):
            return 0.92
        if text.endswith(tuple(_CLAUSE)):
            return 0.62
        if len(re.sub(r"[\W_]+", "", text)) <= 4:
            return 0.6
        return 0.78

    @staticmethod
    def _is_filler(text: str) -> bool:
        normalized = re.sub(r"[\W_]+", "", text.lower())
        return normalized in _FILLERS
