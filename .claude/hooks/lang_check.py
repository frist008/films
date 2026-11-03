"""Everything the owner reads is Russian (owner, 2026-10-28: «опять на англ заговорил? исправляй баг» — the notes between
tool calls went out in English). Used by russian-guard.sh (PreToolUse, every tool) and plan_guard.py (Stop).
Tests: tests/test_russian_guard.py."""
from __future__ import annotations

import json
import os
import re

# code, links and paths are not prose: names stay as they are («`_shift_seasons`», «films/match.py»)
_NOT_PROSE = re.compile(r"```.*?```|`[^`]*`|https?://\S+|\[[^\]]*\]\([^)]*\)|[\w./\\-]+\.(?:py|sh|js|md|json|toml|db|log)\b"
                        r"|\b[\w-]*_[\w-]*\b|\b[a-z]+[A-Z]\w*\b", re.S)


def not_russian(text: str) -> bool:
    """Prose mostly in Latin letters: at least 20 Latin letters and fewer Cyrillic ones than half of them."""
    prose = _NOT_PROSE.sub(" ", text or "")
    lat = len(re.findall(r"[A-Za-z]", prose))
    cyr = len(re.findall(r"[А-Яа-яЁёІіЇїЄєҐґ]", prose))
    return lat >= 20 and cyr * 2 < lat


def last_text(transcript: str, tail: int = 400_000) -> str:
    """The last assistant text block of the transcript (read from its end: the file grows to megabytes)."""
    try:
        with open(transcript, "rb") as f:
            f.seek(max(0, os.path.getsize(transcript) - tail))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") != "assistant":
            continue
        texts = [b.get("text", "") for b in (d.get("message") or {}).get("content") or []
                 if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()]
        if texts:
            return texts[-1]
    return ""
