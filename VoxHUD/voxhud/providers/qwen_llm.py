"""OpenAI-compatible streaming chat completions (Qwen via DashScope)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

import httpx

from ..config import LLMConfig
from .base import ProviderError

log = logging.getLogger(__name__)


class QwenLlmProvider:
    def __init__(self, config: LLMConfig, api_key: str) -> None:
        self.config = config
        self.api_key = api_key
        self._client: httpx.AsyncClient | None = None

    def _url(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.timeout_s, connect=10.0),
                follow_redirects=True,
            )
        return self._client

    async def stream(
        self,
        messages: list[dict[str, str]],
        on_delta: Callable[[str], None],
        cancel: asyncio.Event,
    ) -> str:
        if not self.api_key:
            raise ProviderError("未配置 LLM API Key（可复用 DashScope Key）")
        payload = {
            "model": self.config.model,
            "messages": messages,
            "stream": True,
            "temperature": float(self.config.temperature),
            "max_tokens": int(self.config.max_tokens),
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        full: list[str] = []
        client = self._client_or_create()
        try:
            async with client.stream("POST", self._url(), headers=headers, json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", errors="replace")
                    raise ProviderError(_http_error(resp.status_code, body))
                async for line in resp.aiter_lines():
                    if cancel.is_set():
                        break
                    if not line:
                        continue
                    if line.startswith("data:"):
                        data = line[5:].strip()
                    else:
                        data = line.strip()
                    if not data:
                        continue
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")
                    if isinstance(content, list):
                        content = "".join(
                            part.get("text", "") for part in content if isinstance(part, dict)
                        )
                    if content:
                        full.append(str(content))
                        on_delta(str(content))
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError("LLM 请求超时，请检查网络") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ProviderError(f"LLM 请求失败：{exc}") from exc
        return "".join(full)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _http_error(status: int, body: str) -> str:
    try:
        obj = json.loads(body)
        message = obj.get("error", {}).get("message") or obj.get("message") or body
    except Exception:
        message = body
    message = str(message)[:240]
    if status in (401, 403):
        return f"LLM 认证失败（{status}），请检查 API Key"
    return f"LLM 请求失败（HTTP {status}）：{message}"
