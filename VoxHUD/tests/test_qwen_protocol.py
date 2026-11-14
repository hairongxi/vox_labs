from __future__ import annotations

import asyncio
import base64
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_vendor"))

import websockets

from voxhud.config import AppConfig
from voxhud.providers.base import AsrEvent
from voxhud.providers.qwen_asr import QwenAsrProvider
from voxhud.providers.qwen_tts import QwenTtsSession


class QwenProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_asr_session_flow(self) -> None:
        received: list[dict] = []
        events: list[AsrEvent] = []
        done = asyncio.Event()

        async def handler(ws):
            await ws.send(json.dumps({"type": "session.created", "session": {"id": "s1"}}))
            async for raw in ws:
                msg = json.loads(raw)
                received.append(msg)
                if msg["type"] == "session.update":
                    await ws.send(json.dumps({"type": "session.updated", "session": {"id": "s1"}}))
                elif msg["type"] == "input_audio_buffer.append":
                    await ws.send(json.dumps({
                        "type": "input_audio_buffer.speech_started",
                        "item_id": "item1",
                    }))
                    await ws.send(json.dumps({
                        "type": "conversation.item.input_audio_transcription.text",
                        "item_id": "item1",
                        "text": "你好",
                        "stash": "世界",
                    }))
                    await ws.send(json.dumps({
                        "type": "conversation.item.input_audio_transcription.completed",
                        "item_id": "item1",
                        "transcript": "你好世界",
                    }))
                    await ws.send(json.dumps({
                        "type": "input_audio_buffer.speech_stopped",
                        "item_id": "item1",
                    }))
                    done.set()
                elif msg["type"] == "session.finish":
                    await ws.send(json.dumps({"type": "session.finished"}))
                    break

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = AppConfig()
            cfg.dashscope.asr_url = f"ws://127.0.0.1:{port}/api-ws/v1/realtime?model=test"
            prov = QwenAsrProvider(cfg, "test-key", events.append)
            await prov.start()
            prov.feed(b"\x00\x01" * 320)
            await asyncio.wait_for(done.wait(), 5)
            await asyncio.sleep(0.1)
            await prov.close()

        kinds = [e.kind for e in events]
        self.assertIn("ready", kinds)
        self.assertIn("partial", kinds)
        self.assertIn("final", kinds)
        final = next(e for e in events if e.kind == "final")
        self.assertEqual(final.text, "你好世界")
        update = next(m for m in received if m["type"] == "session.update")
        self.assertEqual(update["session"]["sample_rate"], 16000)
        self.assertEqual(update["session"]["turn_detection"]["type"], "server_vad")
        append = next(m for m in received if m["type"] == "input_audio_buffer.append")
        self.assertEqual(len(base64.b64decode(append["audio"])), 640)

    async def test_tts_commit_flow(self) -> None:
        received: list[dict] = []
        audio: list[bytes] = []
        done = asyncio.Event()

        async def handler(ws):
            await ws.send(json.dumps({"type": "session.created", "session": {"id": "t1"}}))
            async for raw in ws:
                msg = json.loads(raw)
                received.append(msg)
                if msg["type"] == "session.update":
                    await ws.send(json.dumps({"type": "session.updated", "session": {"id": "t1"}}))
                elif msg["type"] == "input_text_buffer.commit":
                    await ws.send(json.dumps({"type": "response.created", "response": {"id": "r1"}}))
                    await ws.send(json.dumps({
                        "type": "response.audio.delta",
                        "delta": base64.b64encode(b"\x00\x01" * 100).decode(),
                    }))
                    await ws.send(json.dumps({"type": "response.audio.done"}))
                    await ws.send(json.dumps({"type": "response.done"}))
                    done.set()
                elif msg["type"] == "session.finish":
                    await ws.send(json.dumps({"type": "session.finished"}))
                    break

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = AppConfig()
            cfg.dashscope.tts_url = f"ws://127.0.0.1:{port}/api-ws/v1/realtime?model=test"
            errors: list[str] = []
            sess = QwenTtsSession(cfg, "test-key", lambda pcm, rate: audio.append(pcm), errors.append)
            await sess.open()
            await sess.append("你好。")
            await asyncio.wait_for(done.wait(), 5)
            await sess.finish()

        self.assertEqual(audio, [b"\x00\x01" * 100])
        self.assertEqual(errors, [])
        update = next(m for m in received if m["type"] == "session.update")
        self.assertEqual(update["session"]["mode"], "commit")
        self.assertEqual(update["session"]["sample_rate"], 24000)
        self.assertTrue(any(m["type"] == "input_text_buffer.append" for m in received))
        self.assertTrue(any(m["type"] == "session.finish" for m in received))


if __name__ == "__main__":
    unittest.main()
