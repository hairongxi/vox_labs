# Spec: VoxHUD — 豆包式全双工桌面语音助手

## Objective

在 Windows 桌面提供一个**尽可能轻量**、可由全局热键呼出的全双工语音交互小窗。用户直接说话即可开始一轮对话；助手思考和播报时用户仍可随时插话（barge-in）；每轮结束由可插拔的 turn detector 判定。ASR / TTS / LLM 均可替换，默认提供 Qwen（DashScope）适配器，并内置完全离线的 Demo 模式用于首次启动和开发调试。

成功标准：

1. 双击/脚本启动后出现 400×640 左右的深色无边框小窗，3 秒内进入聆听状态。
2. 默认 `Ctrl+Alt+Space` 全局热键显示/隐藏；`Esc` 隐藏；托盘菜单可显示、设置、退出。
3. Demo 模式下，说话 → 本地 VAD → 脚本 ASR → 流式 LLM → 音调 TTS 全链路可运行，无需 API Key。
4. 配置 Qwen 后，麦克风音频以 PCM16/16kHz 流式送入 Qwen-ASR-Realtime；LLM 流式文本按句送入 Qwen-TTS-Realtime；24kHz PCM 边收边播。
5. 助手播报时用户持续说话 ≥ 可配置阈值，触发 barge-in：停止播放、取消 LLM/TTS、立即回到聆听，且不把被打断的半句写入历史。
6. 用户停顿后，EndOfTurnDetector 在可配置 hold 时间后提交本轮；提交前用户继续说话会撤回候选。
7. 所有输出状态（idle/listening/thinking/speaking/interrupted/error）的文案、提示、颜色、动画可配置，并即时生效。
8. `python -m unittest discover -s tests` 全部通过；网络适配器用本地假 WebSocket 服务验证协议。
9. 代码模块边界清晰：turn manager、end-of-turn、barge-in、ASR/TTS/LLM provider 均为显式接口，后续可替换。

## Tech Stack

- Python 3.10+（本机验证 3.12），Tkinter（标准库）原生小窗。
- `sounddevice` + PortAudio：全双工音频流，16kHz。
- `pywebrtc-audio`（WebRTC AEC3/NS/AGC）+ `webrtcvad-wheels`：回声消除与语音活动检测。
- `websockets`：Qwen ASR/TTS Realtime WebSocket。
- `httpx`：Qwen OpenAI-compatible 流式 LLM。
- `keyboard`：Windows 全局热键；`pystray` + `Pillow`：托盘。
- 标准库 `asyncio`、`tkinter`、`unittest`；无前端构建步骤、无浏览器/Electron 依赖。

## Commands

```powershell
# 开发运行（本仓库已带 _vendor）
python run.py

# 安装依赖（普通 Windows 环境）
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
powershell -ExecutionPolicy Bypass -File scripts/run.ps1

# 测试
python -m unittest discover -s tests -v

# 可选打包（需先安装 pyinstaller）
powershell -ExecutionPolicy Bypass -File scripts/build.ps1
```

## Project Structure

```
run.py                     # 入口（自动加入 _vendor）
voxhud/
  app.py                   # 组装、托盘、热键、生命周期
  config.py                # 配置模型、默认值、校验、持久化
  events.py                # UI 事件与线程安全总线
  conversation.py          # 对话编排：provider + turn manager + 音频
  logging_setup.py
  audio/
    engine.py              # sounddevice 全双工、AEC、VAD、播放队列
    dsp.py                 # PCM/重采样/RMS
  turns/
    interfaces.py          # TurnState、EndOfTurnDetector、BargeInPolicy 协议
    eot.py                 # HybridEndOfTurnDetector
    barge_in.py            # SpeechBargeInPolicy
    manager.py             # TurnManager 状态机
  providers/
    base.py                # AsrProvider / LlmProvider / TtsSession 接口
    qwen_asr.py
    qwen_tts.py
    qwen_llm.py
    demo.py                # DemoAsr / DemoLlm / ToneTts
  ui/
    window.py              # 主窗口、设置页、状态动画
    widgets.py             # 圆角/滚动容器/输入控件
    theme.py               # 颜色与字体
  platform/
    win32.py               # DPI、圆角、单实例、开机启动
tests/                     # unittest；含假 WebSocket 协议测试
docs/                      # 架构、turn management、配置说明
scripts/                   # setup/run/build/install-shortcut
```

## Code Style

- 类型注解 + `from __future__ import annotations`；公共数据结构用 `@dataclass(frozen=True)`。
- 网络/音频/UI 分层；跨线程只通过 `queue.Queue`、`loop.call_soon_threadsafe` 或线程安全方法。
- 不把 API Key 写入日志；配置保存时脱敏日志。
- 示例：

```python
@dataclass(frozen=True)
class VadFrame:
    timestamp_ms: int
    rms: float
    speech_probability: float
    is_speech: bool

class EndOfTurnDetector(Protocol):
    def on_vad(self, frame: VadFrame) -> None: ...
    def on_asr_final(self, text: str) -> None: ...
    def poll(self, now_ms: int) -> EndOfTurnDecision: ...
```

## Testing Strategy

- 框架：标准库 `unittest`，不引入 pytest。
- 单元：EOT 候选/撤回/标点 hold、barge-in guard/最短语音/冷却、TurnManager 状态迁移与 epoch 取消、句子切分、配置合并。
- 协议：本地 `websockets` 假服务验证 ASR session.update/append/事件映射，TTS session.update/append+commit/audio.delta/finish。
- 集成：假 ASR/LLM/TTS + 假音频，验证一次完整轮次和一次 barge-in 取消。
- 手动：启动真实 Tk 窗口、Demo 对话、热键显隐、托盘、AEC 回声路径、Qwen 真机连接（需用户 Key）。

## Boundaries

- Always: 提交前跑单元测试；新增 provider 必须实现 `base.py` 接口；对用户可见错误做中文提示；日志脱敏。
- Ask first: 引入新的重型运行时（Electron/浏览器/深度学习框架）；改变配置格式破坏兼容；删除失败测试。
- Never: 把 API Key 提交到仓库；在测试中打开真实音频设备或访问真实 DashScope；把 WebSocket 事件处理写成 UI 线程阻塞。

## Assumptions

1. 主要目标 Windows 10/11 x64；其他平台不作为首版验收目标。
2. 用户允许应用启动后持续监听；提供静音与隐藏行为配置。默认不改变系统默认音频设备。
3. Qwen 使用 DashScope 中国大陆默认域名；国际站通过配置切换。
4. Demo 模式 TTS 使用本地合成音调而非真实语音，仅用于链路验证；真实语音需配置 Qwen。
5. 首版不做唤醒词、不做本地离线 LLM、不做多会话管理。
6. “输出状态定制”指固定状态机的展示文案/颜色/动画可配置，而非任意新增状态。

## Success Criteria

见 Objective 1–9。验收时以 `python -m unittest discover -s tests -v`、Demo 端到端运行截图、Qwen 配置连接测试（有 Key 时）为准。

## Open Questions

- 是否需要开机自启？首版提供可选脚本与设置开关，默认关闭。
- 是否要打包为单文件 exe？首版提供 PyInstaller 脚本，构建产物视环境而定。

## Implementation Status (v0.1.0)

已完成：

- Tkinter 无边框 HUD、状态球/波形动画、对话记录、首音延迟、静音/打断/重试。
- 原生 `RegisterHotKey` 全局热键、单实例唤起、托盘（失败时自动降级为关闭即退出）。
- 16 kHz 全双工音频、WebRTC AEC3/NS/VAD、24 kHz TTS → 16 kHz 播放。
- `TurnManager` + `HybridEndOfTurnDetector` + `SpeechBargeInPolicy`，含 epoch/turn-id 取消。
- Qwen ASR/TTS WebSocket 适配器、OpenAI-compatible 流式 LLM、Demo 模式。
- 设置页：服务、ASR/TTS/LLM、turn 参数、音频设备、热键、透明度、六状态外观。
- 41 个单元/协议/集成测试通过（`python -m unittest discover -s tests -v`）。
- PyInstaller `--onedir` 构建验证，产物约 80 MB（`dist\VoxHUD\VoxHUD.exe`）。

已验证：

- Demo 模式完整轮次（文字输入提交 → LLM 流式 → TTS 音调 → 播放 → LISTENING）。
- barge-in 取消与 stale 回调隔离（集成测试）。
- 本地假 WebSocket 服务验证 Qwen ASR/TTS 协议。
- 热键隐藏/显示（在另一个前台窗口存在时用合成按键验证；当前 DSH 沙箱会吞掉部分合成按键，真实物理按键不受此限制）。
- 单实例第二次启动唤起已有窗口。

未验证/限制：

- 真实 DashScope Key 下的端到端音质、配额和网络延迟（本环境无 Key）。
- 当前沙箱中 pystray 因 `ChangeWindowMessageFilterEx` 权限失败；真实桌面应正常，失败时已有降级。
- Demo TTS 是音调，不是可懂语音。
- 真实外放 AEC 效果依赖设备/房间；建议耳机。
