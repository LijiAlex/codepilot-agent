# src/codepilot/safety/sanitize.py
"""Remove content a human reviewer would not see but a model would read.

Pattern from claude-code-action's sanitizer: HTML comments, invisible and
direction-override characters, image alt text, link titles, hidden attributes.
"""
from __future__ import annotations

import html
import re

_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.S)          # an unclosed comment hides the rest on GitHub
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\u00ad\U000e0000-\U000e007f]")
_WRAPPER = re.compile(r"<(/?)(untrusted_\w+)", re.I)     # the text must not close (or fake) the tag it is wrapped in
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
    text = _INVISIBLE.sub("", text)                        # again: entities like &#x200b; decode to invisibles
    text = _WRAPPER.sub(r"‹\1\2", text)
    if len(text) > max_chars:
        cut = len(text) - max_chars
        text = text[:max_chars] + f"\n[truncated {cut} characters]"
    return text
