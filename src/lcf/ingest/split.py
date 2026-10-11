"""Cutting long text into parts a model reads well, at natural boundaries."""

import re

# In order of preference: where a cut is least likely to separate things that
# belong together.
_BREAKS = (
    re.compile(r"\n[ \t]*\n\s*"),  # a blank line: between paragraphs
    re.compile(r"\n\s*"),  # a line break: between list items, mail lines
    re.compile(r"(?<=[.!?…])[\"'”)\]]?\s+"),  # after a sentence
    re.compile(r"\s+"),  # between words
)


def split_text(text: str, size: int, tolerance: int = 15) -> list[str]:
    """`text` in parts of about `size` characters.

    Each cut falls within ± `tolerance` percent of `size`, at the most natural
    boundary the window offers and, among those, the one nearest `size`. Text
    shorter than the upper bound is returned whole. Only a window with no
    whitespace at all is cut mid-word.
    """
    size = max(1, size)
    spread = int(size * max(0, tolerance) / 100)
    parts: list[str] = []
    start = 0
    while len(text) - start > size + spread:
        target = start + size
        lo, hi = target - spread, target + spread
        cut = target
        for pattern in _BREAKS:
            ends = [
                m.end()
                for m in pattern.finditer(text, lo, hi)
                if lo < m.end() <= hi and not _introduces(text[start : m.start()])
            ]
            if ends:
                cut = min(ends, key=lambda e: abs(e - target))
                break
        part = text[start:cut].strip()
        if part:
            parts.append(part)
        start = cut
    rest = text[start:].strip()
    if rest:
        parts.append(rest)
    return parts


def _introduces(before: str) -> bool:
    """Does the text before a break end on a line that leads into what follows —
    a heading, or a line ending in a colon? Cutting there orphans it."""
    last = before.rstrip().rsplit("\n", 1)[-1].strip()
    return last.startswith("#") or last.endswith(":")
