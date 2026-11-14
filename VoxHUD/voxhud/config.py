"""Configuration model and persistence.

The config is deliberately plain JSON: users are expected to edit it, and every
field has a safe default so a partial file still boots.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

APP_DIR_NAME = "VoxHUD"
CONFIG_VERSION = 1

CN_ASR_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3-asr-flash-realtime"
CN_TTS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3-tts-flash-realtime"
CN_LLM_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
INTL_ASR_URL = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime?model=qwen3-asr-flash-realtime"
INTL_TTS_URL = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime?model=qwen3-tts-flash-realtime"
INTL_LLM_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

STATE_KEYS: tuple[str, ...] = (
    "idle",
    "listening",
    "thinking",
    "speaking",
    "interrupted",
    "error",
)

DEFAULT_STATE_STYLES: dict[str, dict[str, Any]] = {
    "idle": {
        "label": "待命",
        "hint": "按 Ctrl+Alt+Space 呼出 · 直接说话",
        "color": "#8B8FA3",
        "animation": "breathe",
    },
    "listening": {
        "label": "正在聆听",
        "hint": "说完停顿一下，我会接话；随时可以打断我",
        "color": "#4FD1C5",
        "animation": "wave",
    },
    "thinking": {
        "label": "思考中",
        "hint": "正在组织回答…",
        "color": "#F6AD55",
        "animation": "orbit",
    },
    "speaking": {
        "label": "正在回答",
        "hint": "直接说话即可打断我",
        "color": "#8B7CFF",
        "animation": "wave",
    },
    "interrupted": {
        "label": "已打断",
        "hint": "我在听，请继续",
        "color": "#FC8181",
        "animation": "flash",
    },
    "error": {
        "label": "出错了",
        "hint": "检查设置或网络后重试",
        "color": "#F56565",
        "animation": "flat",
    },
}


@dataclass
class DashScopeConfig:
    api_key: str = ""
    region: str = "cn"  # cn | intl
    asr_url: str = CN_ASR_URL
    asr_model: str = "qwen3-asr-flash-realtime"
    tts_url: str = CN_TTS_URL
    tts_model: str = "qwen3-tts-flash-realtime"
    tts_voice: str = "Cherry"
    tts_language_type: str = "Chinese"
    language: str = "zh"
    asr_silence_ms: int = 420
    asr_threshold: float = 0.25

    def normalized(self) -> "DashScopeConfig":
        if self.region == "intl":
            if "dashscope.aliyuncs.com" in self.asr_url and "dashscope-intl" not in self.asr_url:
                self.asr_url = self.asr_url.replace("dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com")
            if "dashscope.aliyuncs.com" in self.tts_url and "dashscope-intl" not in self.tts_url:
                self.tts_url = self.tts_url.replace("dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com")
        return self


@dataclass
class LLMConfig:
    base_url: str = CN_LLM_URL
    api_key: str = ""
    model: str = "qwen-plus"
    system_prompt: str = (
        "你是 VoxHUD，一个运行在用户 Windows 桌面上的全双工语音助手。"
        "回答要自然、简洁、口语化，通常控制在 1 到 4 句话。"
        "除非用户明确要求，不要使用 Markdown、列表或代码块，因为你的回答会被朗读出来。"
        "如果用户打断你，先听清用户的新问题再回答。"
    )
    temperature: float = 0.6
    max_tokens: int = 512
    history_turns: int = 8
    timeout_s: float = 60.0


@dataclass
class TurnConfig:
    eot_hold_ms: int = 320
    eot_terminal_hold_ms: int = 140
    eot_asr_timeout_ms: int = 1100
    eot_max_utterance_ms: int = 15000
    eot_empty_reset_ms: int = 2200
    barge_in_enabled: bool = True
    barge_in_guard_ms: int = 320
    barge_in_min_speech_ms: int = 140
    barge_in_cooldown_ms: int = 700
    barge_in_sensitivity: float = 0.55
    allow_thinking_interrupt: bool = True
    barge_in_on_asr: bool = False
    asr_barge_in_min_ms: int = 500
    min_utterance_chars: int = 1


@dataclass
class AudioConfig:
    input_device: int | None = None
    output_device: int | None = None
    sample_rate: int = 16000
    block_ms: int = 20
    aec: bool = True
    noise_suppression: bool = True
    agc: bool = False
    aec_delay_ms: int = 120
    latency: str = "low"
    mic_gain: float = 1.0
    output_volume: float = 0.85
    vad_mode: int = 2


@dataclass
class DemoConfig:
    turns: list[str] = field(
        default_factory=lambda: [
            "你好，请介绍一下你自己。",
            "今天天气怎么样？",
            "给我讲一个简短的笑话。",
            "你能帮我做什么？",
        ]
    )
    response_delay_ms: int = 18
    tone_volume: float = 0.12


@dataclass
class UIConfig:
    hotkey: str = "ctrl+alt+space"
    interrupt_hotkey: str = "ctrl+alt+x"
    always_on_top: bool = True
    opacity: float = 0.97
    accent: str = "#7C5CFF"
    show_transcript: bool = True
    show_debug: bool = False
    close_to_tray: bool = True
    start_hidden: bool = False
    window_x: int | None = None
    window_y: int | None = None
    states: dict[str, dict[str, Any]] = field(
        default_factory=lambda: copy.deepcopy(DEFAULT_STATE_STYLES)
    )


@dataclass
class AppConfig:
    version: int = CONFIG_VERSION
    provider_mode: str = "demo"  # demo | qwen
    dashscope: DashScopeConfig = field(default_factory=DashScopeConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    turn: TurnConfig = field(default_factory=TurnConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    demo: DemoConfig = field(default_factory=DemoConfig)

    # ---------------------------------------------------------------- helpers
    def effective_api_key(self) -> str:
        return (
            self.dashscope.api_key.strip()
            or os.environ.get("DASHSCOPE_API_KEY", "").strip()
        )

    def effective_llm_key(self) -> str:
        return (
            self.llm.api_key.strip()
            or self.effective_api_key()
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def state_style(self, state: str) -> dict[str, Any]:
        base = copy.deepcopy(DEFAULT_STATE_STYLES.get(state, DEFAULT_STATE_STYLES["idle"]))
        base.update(self.ui.states.get(state, {}) or {})
        return base

    # ------------------------------------------------------------- validation
    def validate(self) -> list[str]:
        warnings: list[str] = []
        if self.provider_mode not in ("demo", "qwen"):
            warnings.append(f"未知 provider_mode={self.provider_mode!r}，回退到 demo")
            self.provider_mode = "demo"
        if self.ui.hotkey.strip() == "":
            warnings.append("热键为空，使用默认 ctrl+alt+space")
            self.ui.hotkey = "ctrl+alt+space"
        if self.ui.interrupt_hotkey.strip() == "":
            self.ui.interrupt_hotkey = "ctrl+alt+x"
        self.ui.opacity = _clamp(self.ui.opacity, 0.55, 1.0)
        self.audio.mic_gain = _clamp(self.audio.mic_gain, 0.0, 4.0)
        self.audio.output_volume = _clamp(self.audio.output_volume, 0.0, 2.0)
        self.audio.aec_delay_ms = int(_clamp(self.audio.aec_delay_ms, 0, 500))
        self.turn.barge_in_sensitivity = _clamp(self.turn.barge_in_sensitivity, 0.0, 1.0)
        self.turn.eot_hold_ms = int(_clamp(self.turn.eot_hold_ms, 80, 3000))
        self.turn.eot_terminal_hold_ms = int(_clamp(self.turn.eot_terminal_hold_ms, 0, 2000))
        self.turn.eot_asr_timeout_ms = int(_clamp(self.turn.eot_asr_timeout_ms, 200, 5000))
        self.turn.barge_in_guard_ms = int(_clamp(self.turn.barge_in_guard_ms, 0, 2000))
        self.turn.barge_in_min_speech_ms = int(_clamp(self.turn.barge_in_min_speech_ms, 40, 1000))
        self.dashscope.asr_silence_ms = int(_clamp(self.dashscope.asr_silence_ms, 100, 3000))
        self.dashscope.asr_threshold = _clamp(self.dashscope.asr_threshold, 0.0, 1.0)
        self.audio.vad_mode = int(_clamp(self.audio.vad_mode, 0, 3))
        self.audio.block_ms = int(_clamp(self.audio.block_ms, 10, 30))
        for key in STATE_KEYS:
            style = self.ui.states.setdefault(key, copy.deepcopy(DEFAULT_STATE_STYLES[key]))
            default = DEFAULT_STATE_STYLES[key]
            for f in ("label", "hint", "color", "animation"):
                style[f] = str(style.get(f, default[f]))
        if not self.dashscope.asr_url.startswith(("ws://", "wss://")):
            warnings.append("ASR URL 必须是 ws:// 或 wss://，已恢复默认")
            self.dashscope.asr_url = CN_ASR_URL
        if not self.dashscope.tts_url.startswith(("ws://", "wss://")):
            warnings.append("TTS URL 必须是 ws:// 或 wss://，已恢复默认")
            self.dashscope.tts_url = CN_TTS_URL
        if not self.llm.base_url.startswith(("http://", "https://")):
            warnings.append("LLM base URL 必须是 http:// 或 https://，已恢复默认")
            self.llm.base_url = CN_LLM_URL
        self.dashscope.normalized()
        if self.provider_mode == "qwen" and not self.effective_api_key():
            warnings.append("Qwen 模式尚未配置 DashScope API Key")
        return warnings


def _clamp(value: float, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


def _deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def config_from_dict(data: dict[str, Any]) -> AppConfig:
    from dataclasses import fields

    base = AppConfig().to_dict()
    merged = _deep_merge(base, data or {})
    try:
        kwargs: dict[str, Any] = {}
        for f in fields(AppConfig):
            val = merged.get(f.name)
            if f.name == "dashscope":
                kwargs[f.name] = DashScopeConfig(**_only_fields(DashScopeConfig, val))
            elif f.name == "llm":
                kwargs[f.name] = LLMConfig(**_only_fields(LLMConfig, val))
            elif f.name == "turn":
                kwargs[f.name] = TurnConfig(**_only_fields(TurnConfig, val))
            elif f.name == "audio":
                kwargs[f.name] = AudioConfig(**_only_fields(AudioConfig, val))
            elif f.name == "ui":
                kwargs[f.name] = UIConfig(**_only_fields(UIConfig, val))
            elif f.name == "demo":
                kwargs[f.name] = DemoConfig(**_only_fields(DemoConfig, val))
            else:
                kwargs[f.name] = val
        return AppConfig(**kwargs)
    except Exception:
        log.exception("配置解析失败，使用默认配置")
        return AppConfig()


def _only_fields(cls: type, data: Any) -> dict[str, Any]:
    from dataclasses import fields

    if not isinstance(data, dict):
        return {}
    allowed = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in allowed}


def default_config_path() -> Path:
    override = os.environ.get("VOXHUD_CONFIG")
    if override:
        return Path(override).expanduser()
    return data_dir() / "config.json"


def data_dir() -> Path:
    override = os.environ.get("VOXHUD_DATA_DIR")
    if override:
        candidates = [Path(override).expanduser()]
    else:
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / ".config"
        candidates = [base / APP_DIR_NAME, Path.cwd() / ".voxhud"]
    last_error: Exception | None = None
    for p in candidates:
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError as exc:
            last_error = exc
    raise RuntimeError(f"无法创建数据目录：{last_error}")


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_config_path()
        self.config = AppConfig()
        self.warnings: list[str] = []

    def load(self) -> AppConfig:
        if not self.path.exists():
            self.config = AppConfig()
            self.config.validate()
            try:
                self.save(self.config)
            except OSError as exc:
                log.warning("首次写入配置失败（将使用内存配置）：%s", exc)
                self.warnings.append(f"无法写入配置文件：{exc}")
            return self.config
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("读取配置失败：%s，将备份并使用默认配置", self.path)
            try:
                self.path.replace(self.path.with_suffix(f".broken-{int(time.time())}.json"))
            except OSError:
                pass
            self.config = AppConfig()
            self.config.validate()
            return self.config
        self.config = config_from_dict(raw)
        self.warnings = self.config.validate()
        for warning in self.warnings:
            log.warning("配置：%s", warning)
        return self.config

    def save(self, config: AppConfig | None = None) -> None:
        cfg = config or self.config
        cfg.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        payload = json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)
        log.info("配置已保存：%s", self.path)

    def redacted(self) -> dict[str, Any]:
        data = self.config.to_dict()

        def scrub(d: dict[str, Any]) -> None:
            for key, value in d.items():
                if isinstance(value, dict):
                    scrub(value)
                elif "key" in key.lower() or "token" in key.lower():
                    d[key] = "***" if value else ""

        scrub(data)
        return data
