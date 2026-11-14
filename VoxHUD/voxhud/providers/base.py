"""Provider contracts shared by demo and Qwen implementations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, Protocol


class ProviderError(RuntimeError):
    """A user-presentable provider failure."""


@dataclass(frozen=True)
class AsrEvent:
    kind: str
    # ready | speech_started | speech_stopped | partial | final | error | closed
    text: str = ""
    item_id: str = ""
    message: str = ""
    code: str = ""
    language: str = ""


class AsrProvider(Protocol):
    async def start(self) -> None: ...

    def feed(self, pcm16: bytes) -> None: ...

    async def close(self) -> None: ...


class LlmProvider(Protocol):
    async def stream(
        self,
        messages: list[dict[str, str]],
        on_delta: Callable[[str], None],
        cancel: asyncio.Event,
    ) -> str: ...

    async def close(self) -> None: ...


class TtsSession(Protocol):
    async def open(self) -> None: ...

    async def append(self, text: str) -> None: ...

    async def finish(self) -> None: ...

    async def abort(self) -> None: ...
