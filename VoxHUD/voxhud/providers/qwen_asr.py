"""Qwen-ASR-Realtime WebSocket adapter.

Protocol reference: DashScope/Model Studio ``/api-ws/v1/realtime`` with
``model=qwen3-asr-flash-realtime``. The session uses server VAD; the final
transcript still goes through VoxHUD's own EndOfTurnDetector, so turn-taking
behaviour is under our control rather than the vendor's.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import queue
import uuid
from collections import deque
from typing import Callable

from ..config import AppConfig
from .base import AsrEvent, ProviderError

log = logging.getLogger(__name__)


class QwenAsrProvider:
    def __init__(
        self,
        config: AppConfig,
        api_key: str,
        on_event: Callable[[AsrEvent], None],
    ) -> None:
        self.config = config
        self.api_key = api_key
        self.on_event = on_event
        self._audio_q: queue.Queue[bytes] = queue.Queue(maxsize=120)
        self._ws = None
        self._ready = asyncio.Event()
        self._session_created = asyncio.Event()
        self._session_updated = asyncio.Event()
        self._finished = asyncio.Event()
        self._closed = False
        self._fatal = False
        self._run_task: asyncio.Task | None = None
        self._sender_task: asyncio.Task | None = None
        self._reader_task: asyncio.Task | None = None
        self._prebuffer: deque[bytes] = deque(maxlen=50)

    # ------------------------------------------------------------------ public
    async def start(self) -> None:
        self._run_task = asyncio.create_task(self._run(), name="qwen-asr")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            log.warning("ASR 连接超时，后台继续重试")
            self.on_event(AsrEvent("error", message="ASR 连接超时，正在后台重试"))

    def feed(self, pcm16: bytes) -> None:
        if not pcm16 or self._closed:
            return
        if not self._ready.is_set():
            # Keep a little audio so a reconnect does not drop the first syllable.
            self._prebuffer.append(bytes(pcm16))
            return
        self._enqueue(pcm16)

    async def close(self) -> None:
        self._closed = True
        self._ready.clear()
        ws = self._ws
        if ws is not None:
            try:
                await asyncio.wait_for(ws.send(json.dumps({
                    "event_id": _event_id(),
                    "type": "session.finish",
                })), timeout=1.5)
                await asyncio.wait_for(self._finished.wait(), timeout=1.5)
            except Exception:
                pass
            try:
                await asyncio.wait_for(ws.close(), timeout=1.5)
            except Exception:
                pass
        for task in (self._sender_task, self._reader_task, self._run_task):
            if task is not None:
                task.cancel()
        self.on_event(AsrEvent("closed"))

    # ------------------------------------------------------------------ loop
    async def _run(self) -> None:
        backoff = 1.0
        while not self._closed and not self._fatal:
            try:
                await self._connect_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("ASR 连接失败：%s", exc)
                self.on_event(AsrEvent("error", message=_friendly_error(exc)))
            finally:
                self._ready.clear()
                self._session_created.clear()
                self._session_updated.clear()
                self._audio_q = queue.Queue(maxsize=120)
                self._ws = None
            if self._closed or self._fatal:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.7, 8.0)

    async def _connect_once(self) -> None:
        import websockets

        url = self.config.dashscope.asr_url
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "OpenAI-Beta": "realtime=v1",
            "user-agent": "VoxHUD/0.1",
        }
        log.info("连接 ASR：%s", url)
        async with websockets.connect(
            url,
            additional_headers=headers,
            max_size=8 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=3,
        ) as ws:
            self._ws = ws
            self._reader_task = asyncio.create_task(self._reader(ws), name="qwen-asr-reader")
            # If the server does not send session.created, still try to configure.
            try:
                await asyncio.wait_for(self._session_created.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                await self._send_session_update(ws)
            try:
                await asyncio.wait_for(self._session_updated.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("ASR 未收到 session.updated，继续发送音频")
            while self._prebuffer:
                self._enqueue(self._prebuffer.popleft())
            self._ready.set()
            self.on_event(AsrEvent("ready"))
            self._sender_task = asyncio.create_task(self._sender(ws), name="qwen-asr-sender")
            if self._reader_task:
                await asyncio.gather(self._reader_task, return_exceptions=True)
            if self._sender_task:
                self._sender_task.cancel()
                await asyncio.gather(self._sender_task, return_exceptions=True)
                self._sender_task = None

    # ----------------------------------------------------------------- reader
    async def _reader(self, ws) -> None:
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    log.debug("ASR 非 JSON 消息：%r", raw[:200])
                    continue
                etype = event.get("type", "")
                if etype == "session.created":
                    self._session_created.set()
                    await self._send_session_update(ws)
                elif etype == "session.updated":
                    self._session_updated.set()
                elif etype == "input_audio_buffer.speech_started":
                    self.on_event(AsrEvent("speech_started", item_id=event.get("item_id", "")))
                elif etype == "input_audio_buffer.speech_stopped":
                    self.on_event(AsrEvent("speech_stopped", item_id=event.get("item_id", "")))
                elif etype == "conversation.item.input_audio_transcription.text":
                    text = f"{event.get('text', '')}{event.get('stash', '')}".strip()
                    if text:
                        self.on_event(AsrEvent(
                            "partial",
                            text=text,
                            item_id=event.get("item_id", ""),
                            language=event.get("language", ""),
                        ))
                elif etype == "conversation.item.input_audio_transcription.completed":
                    text = (event.get("transcript") or "").strip()
                    if text:
                        self.on_event(AsrEvent(
                            "final",
                            text=text,
                            item_id=event.get("item_id", ""),
                            language=event.get("language", ""),
                        ))
                elif etype == "conversation.item.input_audio_transcription.failed":
                    err = event.get("error") or {}
                    self.on_event(AsrEvent(
                        "error",
                        message=err.get("message", "ASR 识别失败"),
                        code=str(err.get("code", "")),
                    ))
                elif etype == "error":
                    err = event.get("error") or {}
                    code = str(err.get("code", ""))
                    message = err.get("message", "ASR 错误")
                    if any(s in code.lower() for s in ("auth", "invalid_api_key", "permission")):
                        self._fatal = True
                    self.on_event(AsrEvent("error", message=message, code=code))
                elif etype == "session.finished":
                    log.info("ASR 会话结束")
                    self._finished.set()
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                log.warning("ASR reader 结束：%s", exc)
                self.on_event(AsrEvent("error", message=_friendly_error(exc)))
        finally:
            self._ready.clear()
            self.on_event(AsrEvent("closed"))
            try:
                await ws.close()
            except Exception:
                pass

    # ----------------------------------------------------------------- sender
    async def _send_session_update(self, ws) -> None:
        cfg = self.config.dashscope
        session: dict = {
            "modalities": ["text"],
            "input_audio_format": "pcm",
            "sample_rate": 16000,
            "turn_detection": {
                "type": "server_vad",
                "threshold": float(cfg.asr_threshold),
                "silence_duration_ms": int(cfg.asr_silence_ms),
            },
        }
        if cfg.language:
            session["input_audio_transcription"] = {"language": cfg.language}
        await ws.send(json.dumps({
            "event_id": _event_id(),
            "type": "session.update",
            "session": session,
        }))

    def _enqueue(self, pcm16: bytes) -> None:
        try:
            self._audio_q.put_nowait(bytes(pcm16))
        except queue.Full:
            try:
                self._audio_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._audio_q.put_nowait(bytes(pcm16))
            except queue.Full:
                pass

    async def _sender(self, ws) -> None:
        while not self._closed:
            try:
                pcm = self._audio_q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.01)
                continue
            if not self._ready.is_set():
                continue
            payload = {
                "event_id": _event_id(),
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            }
            try:
                await ws.send(json.dumps(payload))
            except Exception as exc:
                log.debug("ASR 发送失败：%s", exc)
                return
            # Drain a small burst without yielding too often.
            if self._audio_q.qsize() > 4:
                continue


def _event_id() -> str:
    return "event_" + uuid.uuid4().hex


def _friendly_error(exc: Exception) -> str:
    text = str(exc)
    if "401" in text or "403" in text or "Unauthorized" in text:
        return "ASR 认证失败，请检查 DashScope API Key"
    if "timed out" in text.lower() or "timeout" in text.lower():
        return "ASR 连接超时，请检查网络"
    return f"ASR 连接异常：{text[:160]}"
