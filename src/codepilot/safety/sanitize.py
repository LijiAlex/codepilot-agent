# src/codepilot/safety/sanitize.py
"""Remove content a human reviewer would not see but a model would read.

Pattern from claude-code-action's sanitizer: HTML comments, invisible and
direction-override characters, image alt text, link titles, hidden attributes.
"""
from __future__ import annotations

import html
import re

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤﻿­]")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_TITLE = re.compile(r"(\[[^\]]*\]\()([^\s)]+)\s+\"[^\"]*\"\)")
_ATTRS = re.compile(r"\s(?:data-[\w-]+|title|aria-[\w-]+|alt)\s*=\s*(\"[^\"]*\"|'[^']*'|\S+)", re.I)


def sanitize_untrusted(text: str, max_chars: int = 20000) -> str:
    text = text or ""
    text = _COMMENT.sub("", text)
    text = _INVISIBLE.sub("", text)
    text = _IMAGE.sub("[image]", text)
    text = _LINK_TITLE.sub(r"\1\2)", text)
    text = _ATTRS.sub("", text)
    text = html.unescape(text)
    if len(text) > max_chars:
        cut = len(text) - max_chars
        text = text[:max_chars] + f"\n[truncated {cut} characters]"
    return text
