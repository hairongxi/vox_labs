from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from voxhud.config import AppConfig, ConfigStore, config_from_dict


class ConfigTests(unittest.TestCase):
    def test_defaults_validate(self) -> None:
        cfg = AppConfig()
        self.assertEqual(cfg.validate(), [])
        self.assertEqual(cfg.provider_mode, "demo")
        self.assertIn("idle", cfg.ui.states)

    def test_partial_config_merges_defaults(self) -> None:
        cfg = config_from_dict({"provider_mode": "qwen", "ui": {"hotkey": "ctrl+alt+v"}})
        self.assertEqual(cfg.provider_mode, "qwen")
        self.assertEqual(cfg.ui.hotkey, "ctrl+alt+v")
        self.assertEqual(cfg.audio.sample_rate, 16000)

    def test_unknown_fields_ignored(self) -> None:
        cfg = config_from_dict({"ui": {"nope": 1}, "unknown": 2})
        self.assertEqual(cfg.provider_mode, "demo")

    def test_validation_clamps(self) -> None:
        cfg = AppConfig()
        cfg.ui.opacity = 5
        cfg.turn.barge_in_sensitivity = -1
        cfg.audio.mic_gain = 99
        cfg.validate()
        self.assertEqual(cfg.ui.opacity, 1.0)
        self.assertEqual(cfg.turn.barge_in_sensitivity, 0.0)
        self.assertEqual(cfg.audio.mic_gain, 4.0)

    def test_store_roundtrip(self) -> None:
        tmp = Path.cwd() / "_test_tmp"
        tmp.mkdir(exist_ok=True)
        path = tmp / f"config-{uuid.uuid4().hex}.json"
        store = ConfigStore(path)
        cfg = store.load()
        cfg.ui.accent = "#123456"
        store.save(cfg)
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["ui"]["accent"], "#123456")
        again = ConfigStore(path).load()
        self.assertEqual(again.ui.accent, "#123456")
        path.unlink(missing_ok=True)

    def test_redacted_secrets(self) -> None:
        store = ConfigStore(Path.cwd() / "_test_tmp" / "does-not-exist.json")
        store.config.dashscope.api_key = "sk-secret"
        data = store.redacted()
        self.assertEqual(data["dashscope"]["api_key"], "***")

    def test_state_style_custom(self) -> None:
        cfg = AppConfig()
        cfg.ui.states["listening"]["label"] = "我在听"
        self.assertEqual(cfg.state_style("listening")["label"], "我在听")
        self.assertIn("hint", cfg.state_style("listening"))


if __name__ == "__main__":
    unittest.main()
