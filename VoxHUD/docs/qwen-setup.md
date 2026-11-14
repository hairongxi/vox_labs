# Qwen / DashScope 配置

VoxHUD 使用三类 Qwen 能力：

| 能力 | 协议 | 默认模型 |
| --- | --- | --- |
| 实时语音识别 | WebSocket `/api-ws/v1/realtime` | `qwen3-asr-flash-realtime` |
| 实时语音合成 | WebSocket `/api-ws/v1/realtime` | `qwen3-tts-flash-realtime` |
| 对话大模型 | OpenAI-compatible `/chat/completions` | `qwen-plus` |

> 具体模型可用性、配额和计费以阿里云百炼/Model Studio 控制台为准。不同账号、区域可能不同。

## 1. 获取 API Key

在阿里云百炼控制台创建 API Key，确保已开通语音识别/语音合成/大模型服务。

两种配置方式：

1. 打开 VoxHUD 设置 → 服务选 **Qwen（DashScope）** → 填 DashScope Key → 保存。
2. 环境变量：

```powershell
$env:DASHSCOPE_API_KEY = "sk-..."
python run.py
```

配置文件位置：`%APPDATA%\VoxHUD\config.json`。

## 2. 区域与 URL

### 中国（北京）

```json
{
  "asr_url": "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3-asr-flash-realtime",
  "tts_url": "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3-tts-flash-realtime",
  "llm_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"
}
```

### 国际（新加坡）

```json
{
  "asr_url": "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime?model=qwen3-asr-flash-realtime",
  "tts_url": "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime?model=qwen3-tts-flash-realtime",
  "llm_base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
}
```

设置页切换区域会自动更新三个 URL 输入框；也可以填自己的代理/网关地址（保持 `ws/wss`、`http/https` 协议）。

## 3. ASR 事件映射

客户端连接后：

1. 等待 `session.created`。
2. 发送 `session.update`：

```json
{
  "type": "session.update",
  "session": {
    "modalities": ["text"],
    "input_audio_format": "pcm",
    "sample_rate": 16000,
    "input_audio_transcription": {"language": "zh"},
    "turn_detection": {
      "type": "server_vad",
      "threshold": 0.25,
      "silence_duration_ms": 420
    }
  }
}
```

3. 持续发送 16 kHz PCM16 base64：

```json
{"type": "input_audio_buffer.append", "audio": "<base64>"}
```

4. 服务端事件：

| 事件 | VoxHUD 处理 |
| --- | --- |
| `input_audio_buffer.speech_started` | 通知 EOT：用户开始说话，撤回候选 |
| `...transcription.text` | `text + stash` 作为 partial 显示 |
| `...transcription.completed` | final 文本，建立 EOT 候选 |
| `input_audio_buffer.speech_stopped` | EOT：若 final 未到，启动 ASR 超时 |
| `error` | UI 错误；认证类错误停止重连 |
| `session.finished` | 关闭连接 |

认证请求头：

```
Authorization: Bearer <DASHSCOPE_API_KEY>
OpenAI-Beta: realtime=v1
```

## 4. TTS 协议

每轮助手回答创建一个 TTS session：

1. 连接 URL（`model=qwen3-tts-flash-realtime`）。
2. 等待 `session.created`，发送 `session.update`：

```json
{
  "type": "session.update",
  "session": {
    "mode": "commit",
    "voice": "Cherry",
    "language_type": "Chinese",
    "response_format": "pcm",
    "sample_rate": 24000
  }
}
```

3. 每个句子：

```json
{"type": "input_text_buffer.append", "text": "第一句话。"}
{"type": "input_text_buffer.commit"}
```

4. 接收 `response.audio.delta`，base64 PCM16/24 kHz，边收边播。
5. 所有 `response.done` 后发送 `session.finish`，等待 `session.finished` 后关闭。
6. barge-in 时直接 `abort()` 关闭连接（不发送 finish），尽快停止服务端合成。

常用音色：`Cherry`、`Serena`、`Ethan`、`Chelsie` 等；以控制台当前文档为准。

## 5. LLM 协议

默认走 DashScope 的 OpenAI-compatible 接口：

```
POST https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions
Authorization: Bearer <key>
Content-Type: application/json

{
  "model": "qwen-plus",
  "messages": [...],
  "stream": true,
  "temperature": 0.6,
  "max_tokens": 512
}
```

解析 SSE `data:` 行，取 `choices[0].delta.content`。VoxHUD 会在文本流上做句子切分，按句送 TTS。

也支持任何兼容 OpenAI `/chat/completions` 的服务：改 `llm.base_url` 和 `llm.model` 即可。

## 6. 连接自检

```powershell
python scripts\check_qwen.py
```

它会依次：

1. 连接 ASR，等待 `session.updated`；
2. 连接 TTS，合成一句测试文本，统计收到的 PCM 字节；
3. 调用 LLM 流式接口，打印前 120 字。

常见问题：

| 现象 | 排查 |
| --- | --- |
| 401/403 | Key 错误、区域不匹配、未开通服务 |
| ASR 无 final | 检查 `silence_duration_ms`、麦克风电平、防火墙 |
| TTS 无音频 | 模型/音色是否可用；`response_format` 是否为 `pcm` |
| 一直重连 | 网络/代理；看 `%APPDATA%\VoxHUD\logs\voxhud.log` |
| LLM 无响应 | `base_url` 是否包含 `/v1`；模型名是否可用 |

## 7. 安全

- Key 明文保存在本机配置；请限制文件访问权限，不要把 `config.json` 发到公开仓库。
- 日志会记录 URL/模型，但不会记录 API Key。
- 本地只监听 127.0.0.1 的单实例端口；不对外提供 HTTP 服务。
