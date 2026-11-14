# 配置说明

配置路径：`%APPDATA%\VoxHUD\config.json`。首次启动会自动生成。也可以通过：

```powershell
python run.py --config D:\voxhud\config.json
python run.py --data-dir D:\voxhud-data
```

`--data-dir` 会影响配置和日志的默认位置；`--config` 只覆盖配置文件路径。

## 顶层字段

```json
{
  "version": 1,
  "provider_mode": "demo",
  "dashscope": {},
  "llm": {},
  "turn": {},
  "audio": {},
  "ui": {},
  "demo": {}
}
```

`provider_mode`：

- `demo`：离线演示，不需要 Key。
- `qwen`：DashScope ASR/TTS/LLM。

## dashscope

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `api_key` | `""` | DashScope Key；也可用 `DASHSCOPE_API_KEY` |
| `region` | `cn` | `cn` / `intl` |
| `asr_url` | 北京 realtime URL | 必须是 `ws://`/`wss://` |
| `asr_model` | `qwen3-asr-flash-realtime` | 仅记录/展示；URL 中的 model 是实际值 |
| `tts_url` | 北京 realtime URL | 同上 |
| `tts_model` | `qwen3-tts-flash-realtime` | 仅记录/展示 |
| `tts_voice` | `Cherry` | Qwen TTS 音色 |
| `tts_language_type` | `Chinese` | TTS 语言类型，可设 `Auto` |
| `language` | `zh` | ASR 语言提示 |
| `asr_silence_ms` | `420` | 服务端 VAD 静音断句 |
| `asr_threshold` | `0.25` | 服务端 VAD 阈值 |

## llm

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `base_url` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI-compatible 根 |
| `api_key` | `""` | 留空复用 DashScope Key |
| `model` | `qwen-plus` | 任意兼容模型 |
| `system_prompt` | 内置中文提示 | 要求口语化、短回答、不用 Markdown |
| `temperature` | `0.6` | |
| `max_tokens` | `512` | |
| `history_turns` | `8` | 保留最近 N 轮（2N 条消息） |
| `timeout_s` | `60` | HTTP 超时 |

## turn

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `eot_hold_ms` | `320` | ASR final 后的候选 hold |
| `eot_terminal_hold_ms` | `140` | 句末标点时缩短的 hold |
| `eot_asr_timeout_ms` | `1100` | speech_stopped 后等 final 的时间 |
| `eot_max_utterance_ms` | `15000` | 单轮最长，超过强制提交 |
| `eot_empty_reset_ms` | `2200` | 空轮清理 |
| `barge_in_enabled` | `true` | 总开关 |
| `barge_in_guard_ms` | `320` | assistant 开始后的保护期 |
| `barge_in_min_speech_ms` | `140` | 触发打断的最短持续语音 |
| `barge_in_cooldown_ms` | `700` | 打断冷却 |
| `barge_in_sensitivity` | `0.55` | 0–1，越高越灵敏 |
| `allow_thinking_interrupt` | `true` | 思考阶段也允许打断 |
| `barge_in_on_asr` | `false` | 允许 ASR final 触发打断（需 VAD 佐证） |
| `asr_barge_in_min_ms` | `500` | assistant 开始后多久才接受 ASR final 打断 |
| `min_utterance_chars` | `1` | 最短提交字符数 |

## audio

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `input_device` | `null` | sounddevice 设备索引；null=默认 |
| `output_device` | `null` | 同上 |
| `sample_rate` | `16000` | 当前实现固定 16k 全双工 |
| `block_ms` | `20` | 20 ms 帧 |
| `aec` | `true` | WebRTC AEC3 |
| `noise_suppression` | `true` | WebRTC NS |
| `agc` | `false` | 默认关闭，避免放大底噪 |
| `aec_delay_ms` | `120` | AEC 延迟提示；启动后会按实测 latency 调整 |
| `latency` | `low` | PortAudio 延迟策略 |
| `mic_gain` | `1.0` | 输入增益 |
| `output_volume` | `0.85` | TTS 输出音量 |
| `vad_mode` | `2` | webrtcvad 0–3 |

## ui

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `hotkey` | `ctrl+alt+space` | 原生 `RegisterHotKey` 语法 |
| `interrupt_hotkey` | `ctrl+alt+x` | 打断 |
| `always_on_top` | `true` | 置顶 |
| `opacity` | `0.97` | 0.55–1 |
| `accent` | `#7C5CFF` | 强调色 |
| `show_transcript` | `true` | 是否显示对话记录 |
| `show_debug` | `false` | 预留 |
| `close_to_tray` | `true` | 关闭时隐藏到托盘 |
| `start_hidden` | `false` | 启动即隐藏 |
| `window_x/window_y` | `null` | 保存的窗口位置 |
| `states` | 内置 | 六个状态的外观 |

### 输出状态定制

```json
"states": {
  "idle":        {"label": "待命",     "hint": "按 Ctrl+Alt+Space 呼出 · 直接说话", "color": "#8B8FA3", "animation": "breathe"},
  "listening":   {"label": "正在聆听", "hint": "说完停顿一下，我会接话；随时可以打断我", "color": "#4FD1C5", "animation": "wave"},
  "thinking":    {"label": "思考中",   "hint": "正在组织回答…", "color": "#F6AD55", "animation": "orbit"},
  "speaking":    {"label": "正在回答", "hint": "直接说话即可打断我", "color": "#8B7CFF", "animation": "wave"},
  "interrupted": {"label": "已打断",   "hint": "我在听，请继续", "color": "#FC8181", "animation": "flash"},
  "error":       {"label": "出错了",   "hint": "检查设置或网络后重试", "color": "#F56565", "animation": "flat"}
}
```

动画：`breathe` / `wave` / `orbit` / `flash` / `flat`。

设置页里修改后会立即应用到当前状态；保存后写入配置。

## demo

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `turns` | 4 条中文示例 | Demo ASR 依次返回的文本 |
| `response_delay_ms` | `18` | 假 LLM 每 2 字延迟 |
| `tone_volume` | `0.12` | 音调 TTS 音量 |

## 热键语法

支持：

- 修饰键：`ctrl` / `control`、`alt`、`shift`、`win` / `windows`
- 主键：`a-z`、`0-9`、`space`、`tab`、`enter`、`esc`、`backspace`、`delete`、`insert`、`home`、`end`、`pageup`、`pagedown`、方向键、`f1`–`f24`

示例：`ctrl+alt+space`、`ctrl+shift+v`、`win+alt+q`、`f9`。

如果热键被其他程序占用，启动时会有 toast 提示；换一个组合即可。

## 配置文件示例

见仓库根目录 `config.example.json`（已清空密钥，可直接复制为 `config.json`）。
