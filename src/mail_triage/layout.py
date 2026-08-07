"""Terminal column arithmetic, shared by every table this tool prints.

``len()`` is the wrong measure for anything destined for a terminal: it
counts every character as one column, but emoji and East Asian wide and
fullwidth characters occupy two. Sender names and subject lines routinely
contain emoji, so padding computed with ``len()`` misaligns every row after
the first one that contains one.

The rule that governs all of it: **measure and pad first, colour afterwards.**
An ANSI escape sequence occupies no columns but is several characters long,
so styling before padding throws the arithmetic out by the length of the
escape — invisibly, and only for the coloured rows.
"""

from __future__ import annotations

import unicodedata

# Codepoints at or above this are treated as double width. This is where the
# emoji blocks begin (Miscellaneous Symbols and Pictographs onwards); below
# it, the East Asian width table is authoritative.
_WIDE_EMOJI_THRESHOLD = 0x1F300

_ELLIPSIS = "…"


def char_width(char: str) -> int:
    """Terminal columns a single character occupies."""
    if ord(char) >= _WIDE_EMOJI_THRESHOLD:
        return 2
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return 2
    return 1


def display_width(text: str) -> int:
    """Terminal columns ``text`` occupies, accounting for wide characters."""
    return sum(char_width(char) for char in text)


def clip(text: str, width: int) -> str:
    """Collapse newlines and truncate to ``width`` display columns.

    A truncated string ends in an ellipsis, which itself costs a column, so
    the kept text is measured against ``width - 1``.
    """
    text = text.replace("\n", " ").strip()
    if display_width(text) <= width:
        return text
    kept: list[str] = []
    total = 0
    for char in text:
        step = char_width(char)
        if total + step > width - 1:
            break
        kept.append(char)
        total += step
    return "".join(kept) + _ELLIPSIS


def pad(text: str, width: int, right: bool = False) -> str:
    """Pad to ``width`` display columns, left-aligned unless ``right``.

    Apply after clipping and before colouring.
    """
    padding = " " * max(width - display_width(text), 0)
    return padding + text if right else text + padding


def cell(text: str, width: int, right: bool = False) -> str:
    """Clip *and* pad ``text`` to exactly ``width`` display columns."""
    return pad(clip(text, width), width, right=right)
