from __future__ import annotations

import unittest

from voxhud.text import SentenceChunker, sanitize_for_tts


class TextTests(unittest.TestCase):
    def test_chunker_splits_sentences(self) -> None:
        chunker = SentenceChunker()
        out = chunker.feed("你好。今天天气怎么样？")
        self.assertEqual(out, ["你好。", "今天天气怎么样？"])
        self.assertEqual(chunker.flush(), [])

    def test_chunker_streams_across_tokens(self) -> None:
        chunker = SentenceChunker()
        out = []
        for token in ["你", "好", "。", "我", "很", "好", "。"]:
            out.extend(chunker.feed(token))
        self.assertEqual(out, ["你好。", "我很好。"])
        self.assertEqual(chunker.flush(), [])

    def test_chunker_flushes_remainder(self) -> None:
        chunker = SentenceChunker()
        self.assertEqual(chunker.feed("没有标点的一句话"), [])
        self.assertEqual(chunker.flush(), ["没有标点的一句话"])

    def test_sanitize_markdown(self) -> None:
        text = "**你好**，[点这里](https://example.com)。`code`"
        self.assertEqual(sanitize_for_tts(text), "你好，点这里。code")

    def test_sanitize_code_block(self) -> None:
        text = "前文\n```python\nprint('x')\n```\n后文"
        cleaned = sanitize_for_tts(text)
        self.assertNotIn("print", cleaned)
        self.assertIn("前文", cleaned)
        self.assertIn("后文", cleaned)


if __name__ == "__main__":
    unittest.main()
