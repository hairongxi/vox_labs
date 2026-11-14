"""Streaming text utilities: sentence chunking and TTS sanitising."""

from __future__ import annotations

import re

_TERMINAL = "。！？!?…\n"
_CLAUSE = "，,、；;：:"
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_CODE = re.compile(r"```.*?```", re.S)
_MD_INLINE = re.compile(r"`([^`]*)`")
_MD_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~)")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_MD_BULLET = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+", re.M)
_SPACES = re.compile(r"[ \t]+")


class SentenceChunker:
    """Turn a token stream into speakable chunks without waiting for a full stop."""

    def __init__(self, min_clause_chars: int = 10, max_chars: int = 48) -> None:
        self.min_clause_chars = min_clause_chars
        self.max_chars = max_chars
        self._buf = ""

    def feed(self, text: str) -> list[str]:
        if not text:
            return []
        self._buf += text
        out: list[str] = []
        while True:
            boundary = self._find_boundary()
            if boundary <= 0:
                break
            chunk = self._buf[:boundary].strip()
            self._buf = self._buf[boundary:]
            if chunk:
                out.append(chunk)
        return out

    def flush(self) -> list[str]:
        chunk = self._buf.strip()
        self._buf = ""
        return [chunk] if chunk else []

    def _find_boundary(self) -> int:
        buf = self._buf
        # Emit the *earliest* complete sentence in the buffer, so a burst of
        # text does not turn into one giant utterance.
        terminals = [buf.find(ch) for ch in _TERMINAL if buf.find(ch) >= 0]
        if terminals:
            return min(terminals) + 1
        # Speak a clause early if it is already reasonably long.
        clauses = [buf.find(ch) for ch in _CLAUSE if buf.find(ch) >= 0]
        clauses = [i for i in clauses if i + 1 >= self.min_clause_chars]
        if clauses:
            return min(clauses) + 1
        if len(buf) >= self.max_chars:
            return len(buf)
        return -1


def sanitize_for_tts(text: str) -> str:
    """Remove markup that should not be read aloud."""
    if not text:
        return ""
    text = _MD_CODE.sub(" ", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_INLINE.sub(r"\1", text)
    text = _MD_HEADING.sub("", text)
    text = _MD_BULLET.sub("", text)
    text = _MD_EMPHASIS.sub("", text)
    text = text.replace("|", "，")
    text = _SPACES.sub(" ", text)
    return text.strip()
