"""Provider adapters.

The conversation controller knows only the protocols in :mod:`base`. Swapping
Qwen for another vendor means adding one module and one branch in
``build_providers``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from ..config import AppConfig
from .base import (
    AsrEvent,
    AsrProvider,
    LlmProvider,
    ProviderError,
    TtsSession,
)
from .demo import DemoAsrProvider, DemoLlmProvider, DemoTtsFactory
from .qwen_asr import QwenAsrProvider
from .qwen_llm import QwenLlmProvider
from .qwen_tts import QwenTtsSession


@dataclass
class ProviderBundle:
    mode: str
    asr: AsrProvider
    llm: LlmProvider
    tts_factory: Callable[
        [Callable[[bytes, int], None], Callable[[str], None]], TtsSession
    ]
    close: Callable[[], Awaitable[None]]


def build_providers(config: AppConfig, on_asr_event: Callable[[AsrEvent], None]) -> ProviderBundle:
    if config.provider_mode == "qwen":
        key = config.effective_api_key()
        if not key:
            raise ProviderError("Qwen 模式需要 DashScope API Key，请在设置中填写")
        asr = QwenAsrProvider(config, key, on_asr_event)
        llm = QwenLlmProvider(config.llm, config.effective_llm_key())
        tts_factory = lambda on_audio, on_error: QwenTtsSession(  # noqa: E731
            config, key, on_audio, on_error
        )

        async def close() -> None:
            await asr.close()
            await llm.close()

        return ProviderBundle("qwen", asr, llm, tts_factory, close)

    asr = DemoAsrProvider(config.demo, on_asr_event)
    llm = DemoLlmProvider(config.demo)
    tts_factory = DemoTtsFactory(config.demo)

    async def close_demo() -> None:
        await asr.close()
        await llm.close()

    return ProviderBundle("demo", asr, llm, tts_factory, close_demo)


__all__ = [
    "AsrEvent",
    "AsrProvider",
    "LlmProvider",
    "ProviderBundle",
    "ProviderError",
    "TtsSession",
    "build_providers",
]
