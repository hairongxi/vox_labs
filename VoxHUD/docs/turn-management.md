# Turn Management

VoxHUD 的 turn management 参考 FireRedChat 的关注点拆分：**end-of-turn** 与 **barge-in** 是两个独立策略，对话状态机只做仲裁和取消。

## 三个接口

### EndOfTurnDetector

```python
class EndOfTurnDetector(Protocol):
    def reset(self) -> None: ...
    def on_vad(self, frame: VadFrame) -> None: ...
    def on_asr_partial(self, text: str, now_ms: int) -> None: ...
    def on_asr_final(self, text: str, now_ms: int) -> None: ...
    def on_server_speech_started(self, now_ms: int) -> None: ...
    def on_server_speech_stopped(self, now_ms: int) -> None: ...
    def poll(self, now_ms: int) -> EndOfTurnDecision: ...
    def snapshot(self) -> dict[str, Any]: ...
```

默认实现 `HybridEndOfTurnDetector` 融合：

1. 本地 WebRTC VAD（20 ms 帧）；
2. Qwen ASR 的 `speech_started/speech_stopped`；
3. ASR partial/final 文本。

候选提交规则：

| 信号 | 行为 |
| --- | --- |
| ASR final 到达 | 建立候选，hold = `eot_hold_ms` |
| final 以 `。！？!?…` 结尾 | 缩短到 `eot_terminal_hold_ms` |
| final 以逗号/顿号/分号结尾 | hold +260 ms |
| final 很短（≤4 个有效字符） | hold +160 ms |
| final 以“那个/然后/就是/嗯…”结尾 | hold +300 ms |
| 用户重新说话（VAD 或 server speech_started） | **撤回候选** |
| `speech_stopped` 后 `eot_asr_timeout_ms` 仍无 final | 用 partial 作为候选（低置信度） |
| 单轮超过 `eot_max_utterance_ms` | 强制提交，避免无限独白 |
| 只有“嗯/呃/um”等语气词 | 不提交，延长 650 ms 等用户继续 |

接口设计上，`poll()` 返回纯数据 `EndOfTurnDecision`，不直接触发回答；由 `TurnManager` 决定是否提交、是否重置。这样语义 turn detector 可以无缝替换。

### BargeInPolicy

```python
class BargeInPolicy(Protocol):
    def reset(self) -> None: ...
    def on_frame(self, frame: VadFrame, context: BargeInContext) -> BargeInDecision: ...
```

默认 `SpeechBargeInPolicy`：

- 只在 `THINKING` / `SPEAKING` 状态评估。
- 从 assistant 状态开始计 guard（默认 320 ms），避免 AEC 未收敛时自打断。
- 要求 WebRTC VAD 为 speech **且** clean RMS 超过阈值；阈值随 `barge_in_sensitivity` 降低。
- 持续语音 ≥ `barge_in_min_speech_ms`（默认 140 ms）才触发；120 ms 以上的静音间隙会重置连续段。
- 触发后 `barge_in_cooldown_ms` 内不重复触发。

可配置项：

```json
"turn": {
  "barge_in_enabled": true,
  "barge_in_guard_ms": 320,
  "barge_in_min_speech_ms": 140,
  "barge_in_cooldown_ms": 700,
  "barge_in_sensitivity": 0.55,
  "allow_thinking_interrupt": true,
  "barge_in_on_asr": false
}
```

`barge_in_on_asr` 默认关闭。打开后，ASR final 也可触发打断，但必须同时满足“最近 650 ms 有 VAD speech”的佐证，避免残余回声被 ASR 转写后自打断。

### TurnManager

```python
class TurnManager:
    def start() -> None
    def on_vad_frame(frame: VadFrame) -> None
    def on_asr_partial(text: str, at_ms: int | None = None) -> None
    def on_asr_final(text: str, at_ms: int | None = None) -> None
    def on_server_speech_started/stopped(...) -> None
    def on_assistant_started(turn_id: int, at_ms: int | None = None) -> None
    def on_assistant_finished(turn_id: int, at_ms: int | None = None) -> None
    def on_assistant_failed(turn_id: int, message: str) -> None
    def interrupt(reason: str = "manual") -> None
    def poll(at_ms: int | None = None) -> None
    def force_commit(text: str, reason: str = "text_input") -> None
```

状态机：

```text
IDLE
  └─ start() → LISTENING
LISTENING
  ├─ EOT commit → THINKING
  └─ barge-in / interrupt → INTERRUPTED → LISTENING
THINKING
  ├─ first TTS audio → SPEAKING
  ├─ barge-in → INTERRUPTED
  └─ answer failed → ERROR
SPEAKING
  ├─ assistant finished → LISTENING
  └─ barge-in → INTERRUPTED
INTERRUPTED
  └─ 750 ms 后 → LISTENING（或期间新 final 提交 → THINKING）
ERROR
  └─ recover() → LISTENING
```

关键实现点：

- **epoch**：`_do_barge_in()` 递增 `_epoch`；`ConversationController` 将它传给音频队列，旧音频直接丢弃。
- **stale 回调**：`on_assistant_started/finished` 校验 `turn_id`；旧轮回调不会把状态从 LISTENING 改回 SPEAKING。
- **提交后忽略窗口**：提交后 900 ms 内的 trailing ASR final 被忽略，避免刚提交的那句话又触发一次 barge-in。
- **force_commit**：文字测试框绕过 ASR/EOT，直接进入同一套 commit → LLM/TTS 流程。

## 一次正常轮次

```text
用户说话
  → AudioEngine AEC/VAD frames
  → ASR partial (UI 实时显示)
  → ASR speech_stopped + final
  → EOT 候选 (hold 140–580 ms)
  → TurnManager.commit
  → controller: 追加用户历史, epoch++, 启动 assistant task
  → LLM delta → chunker → TTS append/commit
  → TTS audio.delta → 播放
  → response.done / finish → 等播放排空
  → TurnManager.assistant_finished → LISTENING
```

## 一次 barge-in

```text
助手正在 SPEAKING
  → 用户持续说话（clean VAD + RMS）≥ 140 ms
  → BargeInPolicy trigger
  → TurnManager._do_barge_in:
       epoch++
       state=INTERRUPTED
       callbacks.on_cancel(reason)
  → controller._cancel_assistant:
       cancel event.set()
       audio.stop_playback()
       audio.set_epoch(manager.epoch)
       tts.abort()
       assistant_task.cancel()
  → 旧 LLM/TTS 回调因 epoch/turn_id 不匹配被丢弃
  → 用户这句话继续被 ASR 识别
  → EOT 再次提交 → 新一轮
```

## 与 FireRedChat 的对应关系

| FireRedChat 关注点 | VoxHUD 对应 |
| --- | --- |
| turn detector / end-of-turn | `EndOfTurnDetector` + `HybridEndOfTurnDetector` |
| barge-in detector | `BargeInPolicy` + `SpeechBargeInPolicy` |
| dialogue state / cancellation | `TurnManager` + epoch |
| cascaded ASR → LLM → TTS | `ConversationController` + providers |
| 半双工/全双工切换 | 音频始终全双工；策略层控制是否打断 |

## 调参建议

- **总是误打断**：提高 `barge_in_guard_ms`、提高 `barge_in_min_speech_ms`、降低 `barge_in_sensitivity`、使用耳机、确认 AEC 开启。
- **打断不灵敏**：降低 guard / min speech，提高 sensitivity；检查麦克风增益和 Windows 输入设备。
- **回复太早**：提高 `eot_hold_ms`、`eot_terminal_hold_ms`；Qwen 侧也可提高 `asr_silence_ms`。
- **回复太慢**：降低 `eot_hold_ms` 和 `asr_silence_ms`；确认 TTS 首包正常，观察右下角首音延迟。

## 后续可扩展

- 语义 turn classifier（用小模型判断“说完/未说完”），保持 `EndOfTurnDetector` 接口。
- 声学 barge-in 模型（关键词/声纹/能量），保持 `BargeInPolicy` 接口。
- 支持“等待中”状态：EOT 候选后 UI 显示“我在听…”，而不是直接提交。
- 多轮话题栈与打断后的上下文修复。
