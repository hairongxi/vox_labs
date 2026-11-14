"""Qwen-TTS-Realtime WebSocket adapter (commit mode).

One ``QwenTtsSession`` corresponds to one assistant turn. Text is appended in
sentence-sized chunks and committed immediately so audio can start before the
LLM has finished. ``abort`` closes the socket without a polite finish, which is
the fastest way to stop server-side synthesis on barge-in.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from typing import Callable

from ..config import AppConfig
from .base import ProviderError

log = logging.getLogger(__name__)

TTS_SAMPLE_RATE = 24000


class QwenTtsSession:
    def __init__(
        self,
        config: AppConfig,
        api_key: str,
        on_audio: Callable[[bytes, int], None],
        on_error: Callable[[str], None],
    ) -> None:
        self.config = config
        self.api_key = api_key
        self.on_audio = on_audio
        self.on_error = on_error
        self._ws = None
        self._reader_task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._created = asyncio.Event()
        self._updated = asyncio.Event()
        self._finished = asyncio.Event()
        self._pending = 0
        self._pending_done = asyncio.Event()
        self._pending_done.set()
        self._aborted = False
        self._closed = False
        self._committed = 0

    async def open(self) -> None:
        import websockets

        url = self.config.dashscope.tts_url
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "user-agent": "VoxHUD/0.1",
        }
        log.info("连接 TTS：%s", url)
        self._ws = await websockets.connect(
            url,
            additional_headers=headers,
            max_size=16 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=3,
        )
        self._reader_task = asyncio.create_task(self._reader(self._ws), name="qwen-tts-reader")
        try:
            await asyncio.wait_for(self._created.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            log.warning("TTS 未收到 session.created，继续配置")
        await self._send_session_update()
        try:
            await asyncio.wait_for(self._updated.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            log.warning("TTS 未收到 session.updated，继续尝试合成")
        self._ready.set()

    async def append(self, text: str) -> None:
        if self._aborted or self._closed:
            return
        text = text.strip()
        if not text:
            return
        if not self._ready.is_set() or self._ws is None:
            raise ProviderError("TTS 尚未连接")
        await self._send({
            "type": "input_text_buffer.append",
            "text": text,
        })
        self._pending += 1
        self._pending_done.clear()
        self._committed += 1
        await self._send({"type": "input_text_buffer.commit"})

    async def finish(self) -> None:
        if self._aborted or self._closed:
            return
        if self._pending:
            try:
                await asyncio.wait_for(self._pending_done.wait(), timeout=8.0)
            except asyncio.TimeoutError:
                log.warning("TTS 等待 response.done 超时（pending=%s）", self._pending)
        try:
            await self._send({"type": "session.finish"})
            await asyncio.wait_for(self._finished.wait(), timeout=3.0)
        except Exception:
            pass
        await self._close_socket()

    async def abort(self) -> None:
        self._aborted = True
        await self._close_socket()

    # ------------------------------------------------------------------ internals
    async def _send(self, event: dict) -> None:
        if self._ws is None:
            return
        event = dict(event)
        event["event_id"] = "event_" + uuid.uuid4().hex
        await self._ws.send(json.dumps(event))

    async def _send_session_update(self) -> None:
        cfg = self.config.dashscope
        await self._send({
            "type": "session.update",
            "session": {
                "mode": "commit",
                "voice": cfg.tts_voice,
                "language_type": cfg.tts_language_type,
                "response_format": "pcm",
                "sample_rate": TTS_SAMPLE_RATE,
            },
        })

    async def _reader(self, ws) -> None:
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                etype = event.get("type", "")
                if etype == "session.created":
                    self._created.set()
                elif etype == "session.updated":
                    self._updated.set()
                elif etype == "response.audio.delta":
                    if self._aborted:
                        continue
                    delta = event.get("delta") or ""
                    if not delta:
                        continue
                    try:
                        pcm = base64.b64decode(delta)
                    except Exception:
                        continue
                    self.on_audio(pcm, TTS_SAMPLE_RATE)
                elif etype == "response.done":
                    self._pending = max(0, self._pending - 1)
                    if self._pending == 0:
                        self._pending_done.set()
                elif etype == "input_text_buffer.committed":
                    pass
                elif etype == "error":
                    err = event.get("error") or {}
                    message = err.get("message", "TTS 错误")
                    log.warning("TTS 错误：%s", message)
                    self.on_error(message)
                elif etype == "session.finished":
                    self._finished.set()
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed and not self._aborted:
                log.warning("TTS reader 结束：%s", exc)
                self.on_error(_friendly_error(exc))
        finally:
            self._ready.clear()
            self._finished.set()
            self._pending_done.set()
            try:
                await ws.close()
            except Exception:
                pass

    async def _close_socket(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader_task:
            self._reader_task.cancel()
        ws = self._ws
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), timeout=1.5)
            except Exception:
                pass
        self._ws = None


def _friendly_error(exc: Exception) -> str:
    text = str(exc)
    if "401" in text or "403" in text:
        return "TTS 认证失败，请检查 DashScope API Key"
    return f"TTS 连接异常：{text[:160]}"
