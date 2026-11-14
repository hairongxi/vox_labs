"""Conversation orchestration.

Owns the provider bundle, the turn state machine, history and cancellation.
This module is the only place that ties ASR text, LLM deltas and TTS audio
together; providers and TurnManager remain independently testable.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .audio.engine import AudioEngine, AudioFrameData
from .config import AppConfig
from .events import UiBus, UiEventKind
from .providers import AsrEvent, ProviderBundle, ProviderError, build_providers
from .text import SentenceChunker, sanitize_for_tts
from .turns.barge_in import SpeechBargeInPolicy
from .turns.eot import HybridEndOfTurnDetector
from .turns.interfaces import (
    BargeInDecision,
    TurnCallbacks,
    TurnState,
    UserTurn,
    VadFrame,
)
from .turns.manager import TurnManager, now_ms

log = logging.getLogger(__name__)


class ConversationController:
    def __init__(self, config: AppConfig, bus: UiBus, loop: asyncio.AbstractEventLoop) -> None:
        self.config = config
        self.bus = bus
        self.loop = loop
        self.audio: AudioEngine | None = None
        self.providers: ProviderBundle | None = None
        self.detector = HybridEndOfTurnDetector(config.turn)
        self.barge_policy = SpeechBargeInPolicy(config.turn)
        self.manager = TurnManager(
            self.detector,
            self.barge_policy,
            TurnCallbacks(
                on_state=self._on_state,
                on_user_partial=self._on_user_partial,
                on_user_final=self._on_user_final,
                on_commit=self._on_commit,
                on_barge_in=self._on_barge_in,
                on_cancel=self._on_cancel,
                on_debug=self._on_debug,
            ),
            min_utterance_chars=config.turn.min_utterance_chars,
            barge_in_on_asr=config.turn.barge_in_on_asr,
            asr_barge_in_min_ms=config.turn.asr_barge_in_min_ms,
        )
        self.history: list[dict[str, str]] = []
        self._assistant_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._asr_start_task: asyncio.Task | None = None
        self._cancel_event = asyncio.Event()
        self._tts = None
        self._assistant_text = ""
        self._commit_ms: int | None = None
        self._speaking_turn_id: int | None = None
        self._epoch = 0
        self._shutdown = False
        self._muted = False
        self._last_closed_notice = 0.0
        self._asr_resume_at = 0.0

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self.bus.emit(UiEventKind.CONNECTION, status="connecting", message="正在连接语音服务…")
        self.bus.emit(UiEventKind.CONFIG, config=self.config.to_dict(), mode=self.config.provider_mode)
        try:
            self.providers = build_providers(self.config, self._on_asr_event)
        except ProviderError as exc:
            log.warning("Provider 初始化失败：%s", exc)
            self.bus.emit(UiEventKind.ERROR, message=str(exc))
            self.manager.set_error(str(exc))
            self.bus.emit(UiEventKind.CONNECTION, status="error", message=str(exc))
            return

        self.audio = AudioEngine(
            self.config.audio,
            on_frame=self._audio_frame_from_thread,
            on_level=self._on_level,
            on_error=self._on_audio_error,
        )
        try:
            self.audio.start()
        except Exception as exc:
            log.exception("音频启动失败")
            self.bus.emit(UiEventKind.ERROR, message=f"音频启动失败：{exc}")
            self.manager.set_error(f"音频启动失败：{exc}")
            return

        self._asr_start_task = asyncio.create_task(
            self.providers.asr.start(), name="asr-start"
        )
        self.manager.start()
        self._poll_task = asyncio.create_task(self._poll_loop(), name="turn-poll")
        self.bus.emit(UiEventKind.CONNECTION, status="ready", message="语音服务就绪")

    async def shutdown(self) -> None:
        self._shutdown = True
        self._cancel_assistant("shutdown")
        if self.audio is not None:
            self.audio.stop()
        for task in (self._assistant_task, self._poll_task, self._asr_start_task):
            if task is not None and not task.done():
                task.cancel()
        if self._tts is not None:
            try:
                await self._tts.abort()
            except Exception:
                pass
        if self.providers is not None:
            try:
                await self.providers.close()
            except Exception:
                log.exception("关闭 providers 失败")

    # ---------------------------------------------------------------- audio path
    def _audio_frame_from_thread(self, frame: AudioFrameData) -> None:
        if self._shutdown:
            return
        try:
            self.loop.call_soon_threadsafe(self._handle_audio_frame, frame)
        except RuntimeError:
            pass

    def _handle_audio_frame(self, frame: AudioFrameData) -> None:
        if self._shutdown or self.providers is None:
            return
        if self._muted:
            return
        # The demo ASR has no acoustic echo canceller of its own. Pause its
        # scripted recognizer while the demo voice is playing so it cannot
        # transcribe its own tones; real ASR keeps listening for barge-in.
        now = time.monotonic()
        if self.config.provider_mode == "demo":
            if self.manager.state in (TurnState.THINKING, TurnState.SPEAKING):
                return
            if now < self._asr_resume_at:
                return
        self.providers.asr.feed(frame.pcm16)
        self.manager.on_vad_frame(VadFrame(
            timestamp_ms=frame.timestamp_ms,
            rms=frame.rms,
            speech_probability=frame.speech_probability,
            is_speech=frame.is_speech,
            raw_rms=frame.raw_rms,
            echo_suppression=frame.echo_suppression,
        ))

    def _on_level(self, mic: float, out: float) -> None:
        self.bus.emit(UiEventKind.LEVEL, mic=round(mic, 5), out=round(out, 5))

    def _on_audio_error(self, message: str) -> None:
        self.bus.emit(UiEventKind.TOAST, message=message, level="warn")

    # ---------------------------------------------------------------- ASR events
    def _on_asr_event(self, event: AsrEvent) -> None:
        if self._shutdown:
            return
        if event.kind == "ready":
            self.bus.emit(UiEventKind.CONNECTION, status="connected", message="ASR 已连接")
            self.manager.recover()
        elif event.kind == "speech_started":
            self.manager.on_server_speech_started()
        elif event.kind == "speech_stopped":
            self.manager.on_server_speech_stopped()
        elif event.kind == "partial":
            self.manager.on_asr_partial(event.text)
        elif event.kind == "final":
            self.manager.on_asr_final(event.text)
        elif event.kind == "error":
            log.warning("ASR 错误：%s (%s)", event.message, event.code)
            self.bus.emit(UiEventKind.ERROR, message=event.message, source="asr")
            if any(s in event.code.lower() for s in ("auth", "invalid_api_key", "permission")):
                self.manager.set_error(event.message)
        elif event.kind == "closed":
            now = time.monotonic()
            if now - self._last_closed_notice > 4.0:
                self._last_closed_notice = now
                self.bus.emit(UiEventKind.CONNECTION, status="reconnecting", message="ASR 断开，正在重连…")

    # ------------------------------------------------------------- turn callbacks
    def _on_state(self, state: TurnState, detail: str) -> None:
        if state == TurnState.LISTENING and detail == "assistant_done":
            # Let the last device buffer / room reverb decay before demo ASR
            # starts listening again.
            self._asr_resume_at = time.monotonic() + 0.7
        self.bus.emit(UiEventKind.STATE, state=state.value, detail=detail)

    def _on_user_partial(self, text: str) -> None:
        self.bus.emit(UiEventKind.PARTIAL, text=text)

    def _on_user_final(self, text: str) -> None:
        self.bus.emit(UiEventKind.FINAL, text=text)

    def _on_barge_in(self, reason: str, decision: BargeInDecision) -> None:
        self.bus.emit(
            UiEventKind.TOAST,
            message="已打断，我在听",
            level="info",
            reason=reason,
        )
        self.bus.emit(UiEventKind.DEBUG, event="barge_in", data={
            "reason": reason,
            "confidence": decision.confidence,
            "speech_ms": decision.speech_ms,
        })

    def _on_cancel(self, reason: str) -> None:
        self._cancel_assistant(reason)

    def _on_debug(self, name: str, data: dict[str, Any]) -> None:
        self.bus.emit(UiEventKind.DEBUG, event=name, data=data)

    def _on_commit(self, turn: UserTurn) -> None:
        if self._shutdown:
            return
        self.history.append({"role": "user", "content": turn.text})
        self._trim_history()
        self._assistant_text = ""
        self._speaking_turn_id = None
        self._commit_ms = int(time.monotonic() * 1000)
        self._epoch = self.manager.epoch
        if self.audio is not None:
            self.audio.set_epoch(self._epoch)
        self._cancel_event = asyncio.Event()
        if self._assistant_task is not None and not self._assistant_task.done():
            self._assistant_task.cancel()
        self._assistant_task = asyncio.create_task(
            self._run_assistant(turn, self._epoch, self._cancel_event),
            name=f"assistant-{turn.turn_id}",
        )
        self.bus.emit(UiEventKind.ASSISTANT_CLEAR)

    # ------------------------------------------------------------- assistant
    async def _run_assistant(self, turn: UserTurn, epoch: int, cancel: asyncio.Event) -> None:
        tts = None
        writer: asyncio.Task | None = None
        finished_tts = False
        try:
            if self.providers is None:
                raise ProviderError("Provider 尚未初始化")
            tts = self.providers.tts_factory(
                lambda pcm, rate: self._on_tts_audio(turn, epoch, cancel, pcm, rate),
                lambda message: self._on_tts_error(turn, message),
            )
            self._tts = tts
            await tts.open()
            if cancel.is_set():
                return

            chunk_q: asyncio.Queue[str | None] = asyncio.Queue()

            async def tts_writer() -> None:
                while True:
                    chunk = await chunk_q.get()
                    if chunk is None:
                        return
                    if cancel.is_set():
                        return
                    clean = sanitize_for_tts(chunk)
                    if clean:
                        await tts.append(clean)

            writer = asyncio.create_task(tts_writer(), name="tts-writer")
            chunker = SentenceChunker()

            def on_delta(delta: str) -> None:
                if cancel.is_set() or not delta:
                    return
                self._assistant_text += delta
                self.bus.emit(UiEventKind.ASSISTANT_DELTA, text=delta)
                for chunk in chunker.feed(delta):
                    chunk_q.put_nowait(chunk)

            messages = self._build_messages()
            await self.providers.llm.stream(messages, on_delta, cancel)
            if cancel.is_set():
                return
            for chunk in chunker.flush():
                chunk_q.put_nowait(chunk)
            chunk_q.put_nowait(None)
            if writer is not None:
                await writer
                writer = None
            if cancel.is_set():
                return
            await tts.finish()
            finished_tts = True
            await self._wait_audio_idle(cancel)
            if cancel.is_set() or epoch != self.manager.epoch:
                return
            text = self._assistant_text.strip()
            if text:
                self.history.append({"role": "assistant", "content": text})
                self._trim_history()
                self.bus.emit(UiEventKind.ASSISTANT_DONE, text=text)
            self.manager.on_assistant_finished(turn.turn_id)
        except asyncio.CancelledError:
            raise
        except ProviderError as exc:
            if not cancel.is_set():
                log.warning("回答失败：%s", exc)
                self.bus.emit(UiEventKind.ERROR, message=str(exc), source="assistant")
                self.manager.on_assistant_failed(turn.turn_id, str(exc))
                asyncio.create_task(self._recover_later())
        except Exception as exc:
            if not cancel.is_set():
                log.exception("回答异常")
                message = f"回答失败：{exc}"
                self.bus.emit(UiEventKind.ERROR, message=message, source="assistant")
                self.manager.on_assistant_failed(turn.turn_id, message)
                asyncio.create_task(self._recover_later())
        finally:
            if writer is not None and not writer.done():
                writer.cancel()
            if tts is not None:
                if not finished_tts:
                    try:
                        await tts.abort()
                    except Exception:
                        pass
                elif not cancel.is_set():
                    pass
            self._tts = None

    def _on_tts_audio(
        self,
        turn: UserTurn,
        epoch: int,
        cancel: asyncio.Event,
        pcm: bytes,
        sample_rate: int,
    ) -> None:
        if cancel.is_set() or epoch != self.manager.epoch or self.audio is None:
            return
        if self._speaking_turn_id != turn.turn_id:
            # First chunk of this turn.
            self._speaking_turn_id = turn.turn_id
            self.audio.set_epoch(epoch)
            self.manager.on_assistant_started(turn.turn_id)
            if self._commit_ms is not None:
                latency = int(time.monotonic() * 1000) - self._commit_ms
                self.bus.emit(UiEventKind.LATENCY, eot_to_audio_ms=latency)
        self.audio.push_tts(pcm, sample_rate, epoch)

    def _on_tts_error(self, turn: UserTurn, message: str) -> None:
        log.warning("TTS 错误：%s", message)
        self.bus.emit(UiEventKind.TOAST, message=message, level="warn")

    async def _wait_audio_idle(self, cancel: asyncio.Event) -> None:
        if self.audio is None:
            return
        deadline = time.monotonic() + 20.0
        while not cancel.is_set() and time.monotonic() < deadline:
            if self.audio.playback_idle:
                break
            await asyncio.sleep(0.05)
        # Let the device buffer (usually <= 120 ms) drain.
        try:
            await asyncio.wait_for(self._sleep_unless_cancel(cancel, 0.12), timeout=0.3)
        except asyncio.TimeoutError:
            pass

    @staticmethod
    async def _sleep_unless_cancel(cancel: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(cancel.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _recover_later(self, delay: float = 3.0) -> None:
        await asyncio.sleep(delay)
        if self._shutdown:
            return
        if self.manager.state == TurnState.ERROR:
            self.manager.recover()

    # --------------------------------------------------------------- public API
    def interrupt(self, reason: str = "manual") -> None:
        self.manager.interrupt(reason)

    def submit_text(self, text: str) -> None:
        self.manager.force_commit(text)

    def clear_conversation(self) -> None:
        self.manager.interrupt("clear")
        self.history.clear()
        self._assistant_text = ""
        self.manager.reset_session()
        self.bus.emit(UiEventKind.CLEAR)

    def set_muted(self, muted: bool) -> bool:
        self._muted = bool(muted)
        if self.audio is not None:
            self.audio.set_muted(self._muted)
        self.manager.set_muted(self._muted)
        self.bus.emit(UiEventKind.MUTE, muted=self._muted)
        return self._muted

    @property
    def muted(self) -> bool:
        return self._muted

    def _cancel_assistant(self, reason: str) -> None:
        self._cancel_event.set()
        if self.audio is not None:
            self.audio.stop_playback()
            self.audio.set_epoch(self.manager.epoch)
        tts = self._tts
        if tts is not None:
            asyncio.create_task(self._abort_tts(tts))
        task = self._assistant_task
        if task is not None and not task.done():
            task.cancel()
        self._assistant_task = None
        log.debug("取消回答：%s", reason)

    @staticmethod
    async def _abort_tts(tts) -> None:
        try:
            await tts.abort()
        except Exception:
            pass

    # ---------------------------------------------------------------- internals
    async def _poll_loop(self) -> None:
        try:
            while not self._shutdown:
                self.manager.poll(now_ms())
                await asyncio.sleep(0.04)
        except asyncio.CancelledError:
            return

    def _build_messages(self) -> list[dict[str, str]]:
        limit = max(2, int(self.config.llm.history_turns) * 2)
        history = self.history[-limit:]
        return [{"role": "system", "content": self.config.llm.system_prompt}, *history]

    def _trim_history(self) -> None:
        limit = max(2, int(self.config.llm.history_turns) * 2)
        if len(self.history) > limit:
            self.history = self.history[-limit:]
