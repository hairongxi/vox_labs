# 架构

## 目标与约束

VoxHUD 是一个 Windows 优先的端侧全双工语音客户端：

- 低延迟：ASR 流式、LLM 流式、TTS 按句流式；首音目标 1–2 秒（视网络）。
- 可打断：助手播报时用户开口即可取消；取消必须是“代际安全”的，旧回调不能污染新一轮。
- 可替换：ASR/TTS/LLM 与 turn-taking 策略都是显式接口。
- 轻量：单 Python 进程 + Tkinter + PortAudio；无浏览器/Electron。

## 进程与线程模型

```text
Tk 主线程
  ├─ UI 渲染、设置页、托盘菜单回调（经 action queue）
  ├─ UiBus 消费（每 33 ms drain）
  └─ root.after 驱动窗口动画

asyncio 工作线程 (AsyncRuntime)
  ├─ ConversationController
  ├─ TurnManager / EndOfTurnDetector / BargeInPolicy
  ├─ Qwen ASR reader + sender
  ├─ Qwen TTS session reader
  ├─ LLM httpx stream
  └─ 40 ms turn poll

PortAudio 回调线程
  ├─ 从播放队列取 20 ms PCM 写入 outdata
  ├─ AudioProcessor.process(near, far)  ← WebRTC AEC3/NS
  ├─ WebRTC VAD
  └─ 将一帧投递到 asyncio 线程（call_soon_threadsafe）

热键线程（RegisterHotKey + message-only window）
  └─ 只投递 action，不直接碰 Tk

托盘线程（pystray）
  └─ 只投递 action
```

边界原则：

- 音频回调里不做 JSON、base64、网络、文件 I/O。
- 网络回调里不直接调用 Tk；统一走 `UiBus`。
- 热键/托盘线程只写 `queue.Queue`，Tk 每 40 ms 处理。
- 所有 turn 状态变更只发生在 asyncio 线程。

## 音频链路

### 全双工流

`AudioEngine` 打开一条 `sounddevice.Stream(samplerate=16000, blocksize=320, channels=1)`：

- 16 kHz 是 ASR 的目标采样率，也是 WebRTC AEC 的常用处理率。
- 输入输出共用同一个流，避免两个设备时钟漂移。
- TTS 原始 24 kHz 在 `push_tts()` 中线性重采样到 16 kHz。
- `latency='low'` 在本机为 `(0.02, 0.1)`，AEC 的 `stream_delay_ms` 会在启动后按实测延迟设置（约 120–140 ms）。

### AEC

```python
AudioProcessor(
    sample_rate=16000,
    echo_cancellation=True,
    noise_suppression=True,
    auto_gain_control=False,   # 默认关闭，避免放大底噪导致误 VAD
    high_pass_filter=True,
)
clean = processor.process(near, far)
```

`far` 是当前回调写入声卡的 TTS 信号。WebRTC AEC3 内部按 `stream_delay_ms` 对齐远端参考与麦克风回声。AEC 输出再用于：

- WebRTC VAD（barge-in / EOT 的快速信号）
- RMS / speech probability
- 发送给 ASR 的 PCM

> AGC 默认关闭。开启后会把安静房间的底噪放大，WebRTC VAD 更容易误判；如果需要可在设置中打开。

### 播放队列

`push_tts(pcm, sample_rate, epoch)`：

- 校验 `epoch`，丢弃被取消回合的音频。
- 重采样到 16 kHz，应用音量，追加到有界 `deque`（最多 25 秒）。
- `stop_playback()` 清空队列；设备缓冲中最多约 100 ms 无法瞬停，属于可接受延迟。
- `playback_idle` 只表示应用队列为空；上层等待时额外 sleep 120 ms 让设备缓冲排空。

## 对话编排

`ConversationController` 是唯一知道“ASR 文本 → LLM → TTS 音频”的模块：

1. 音频帧到达 → `asr.feed(pcm)` + `TurnManager.on_vad_frame()`。
2. ASR `partial/final/speech_*` → `TurnManager`。
3. EOT 决定提交 → `TurnCallbacks.on_commit(UserTurn)`。
4. `_on_commit`：追加用户历史、设置 epoch、启动 `_run_assistant`。
5. `_run_assistant`：
   - 打开一个 TTS session（与 LLM 同一轮）。
   - LLM 流式 delta → UI 增量 + `SentenceChunker` → 文本队列。
   - TTS writer 逐句 `append()`（Qwen commit 模式）。
   - TTS 音频 delta → epoch 校验 → `AudioEngine.push_tts()`。
   - LLM 完成 → flush chunker → 等待 writer → `tts.finish()` → 等播放排空 → `assistant_finished`。
6. barge-in → `TurnManager` 递增 epoch → `on_cancel` → 取消 task、abort TTS、清空播放、回到聆听。

### 延迟测量

`_commit_ms` 在 EOT 提交时记录；第一块 TTS 音频到达时计算 `eot_to_audio_ms`，通过 `UiEventKind.LATENCY` 显示。它是“提交 → 首音”而不是“用户说完 → 首音”，因为 EOT hold 本身也算交互延迟。

## 为什么用 epoch + turn id

取消存在天然竞态：

- LLM 的 delta 可能已在队列里。
- TTS 的 `response.audio.delta` 可能在 `abort()` 之后才到。
- 旧轮的 `assistant_finished` 可能在新轮开始后到达。

因此：

- `TurnManager._epoch` 在每次 barge-in / session reset 时递增。
- `_run_assistant` 捕获启动时的 epoch，所有音频回调都校验 `epoch == manager.epoch`。
- 每个 assistant 回调带 `turn_id`；TurnManager 只接受当前 `turn_id`。
- UI 的 ASSISTANT_CLEAR 在新轮开始时清空流式文本。

## 配置与 UI

- 配置：`%APPDATA%\VoxHUD\config.json`，`ConfigStore` 做深合并、校验、原子写。
- 密钥：优先 `config.dashscope.api_key`，其次环境变量 `DASHSCOPE_API_KEY`；LLM key 可单独配置或复用。
- UI：`MainWindow` 读 `AppConfig.ui.states`，每个状态有 `label/hint/color/animation`。
- 设置保存后：写盘 → 重新注册热键 → 销毁旧 controller（关闭 ASR/LLM/音频）→ 新 controller 启动。音频/服务配置无需重启应用。

## 失败与恢复

- ASR：`QwenAsrProvider._run()` 指数退避重连；音频在未就绪时进入最多 1 秒的预缓冲。
- ASR 认证错误：设置为 fatal，停止重连，UI 显示错误，等待用户改配置。
- TTS/LLM 错误：本轮失败，UI 提示，3 秒后自动 recover 到 listening（认证错误除外，用户需改配置）。
- 音频设备失败：尝试默认设备；再失败则 UI 错误，设置页可换设备/重连。
- 托盘不可用：自动把 `close_to_tray` 置为 false，避免窗口隐藏后无法找回。

## 已知取舍

- 16 kHz 播放会牺牲一点高频音质，换来 AEC 对齐和更低的 DSP 成本。
- Demo TTS 是音调，不是可懂语音；这是刻意的离线可测试方案。
- 没有唤醒词，启动即监听；隐私敏感用户可静音或退出。
- AEC 无法完全消除外放回声；耳机模式效果最好。
