"""Connectivity smoke test for DashScope ASR / TTS / LLM.

Usage:
    python scripts/check_qwen.py [--config path\to\config.json]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / "_vendor").is_dir():
    sys.path.insert(0, str(ROOT / "_vendor"))

from voxhud.config import ConfigStore
from voxhud.providers.base import AsrEvent
from voxhud.providers.qwen_asr import QwenAsrProvider
from voxhud.providers.qwen_llm import QwenLlmProvider
from voxhud.providers.qwen_tts import QwenTtsSession


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    store = ConfigStore(Path(args.config) if args.config else None)
    cfg = store.load()
    cfg.provider_mode = "qwen"
    key = cfg.effective_api_key()
    if not key:
        print("未配置 DashScope API Key。请先在 VoxHUD 设置中填写，或设置 DASHSCOPE_API_KEY。")
        return 2

    print("1) ASR …")
    events: list[AsrEvent] = []
    asr = QwenAsrProvider(cfg, key, events.append)
    try:
        await asr.start()
        if not any(e.kind == "ready" for e in events):
            print("   ASR 未就绪：", [e.message for e in events if e.kind == "error"])
            return 3
        print("   ASR 连接成功")
    finally:
        await asr.close()

    print("2) TTS …")
    audio_bytes = 0

    def on_audio(pcm: bytes, rate: int) -> None:
        nonlocal audio_bytes
        audio_bytes += len(pcm)

    errors: list[str] = []
    tts = QwenTtsSession(cfg, key, on_audio, errors.append)
    try:
        await asyncio.wait_for(tts.open(), timeout=15)
        await tts.append("你好，这是 VoxHUD 的语音连接测试。")
        await asyncio.wait_for(tts.finish(), timeout=30)
        print(f"   TTS 收到音频 {audio_bytes} bytes，错误={errors or '无'}")
        if audio_bytes == 0:
            return 4
    finally:
        await tts.abort()

    print("3) LLM …")
    llm = QwenLlmProvider(cfg.llm, cfg.effective_llm_key())
    text: list[str] = []
    try:
        await llm.stream(
            [
                {"role": "system", "content": "只回答一句话。"},
                {"role": "user", "content": "用一句话向我打个招呼。"},
            ],
            text.append,
            asyncio.Event(),
        )
        print("   LLM:", "".join(text)[:120] or "（空响应）")
    finally:
        await llm.close()

    print("全部连接成功。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
