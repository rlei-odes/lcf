"""Units into chunks.

Structural first, budgeted second. A chunk boundary falls where the document
already had one — a heading, a page, a new message in a thread — and only then
is the character budget allowed to force one. The alternative, slicing every
1800 characters, puts the heading that says what a table measures in a different
chunk from the table.

Three invariants, all tested:

- **Nothing is lost.** Every unit's text appears in some chunk.
- **Offsets are true.** `source_text[chunk.char_from:chunk.char_to]` is exactly
  the chunk's own text, so a quote can be located in the document a person can
  open and read.
- **The budget is respected.** A unit too long for one chunk is split on
  sentence boundaries rather than being handed over whole, because the budget
  exists to keep a call affordable and a 9000-character paragraph out of a PDF
  would otherwise defeat it.
"""

import re
from dataclasses import dataclass, field

from lcf.ingest.parse import Parsed, Unit

# The separator `Parsed.text` joins units with. Offsets are computed against the
# same join, so the two can never disagree about where a unit starts.
JOIN = "\n\n"

# Where an oversized unit may be cut, in order of preference. Sentence ends
# first, then any line break, then a space — a cut mid-word would corrupt the
# one thing a pattern tier is looking for.
_SENTENCE = re.compile(r"(?<=[.!?:;])\s+")


@dataclass
class Piece:
    """A unit, or part of one too long to fit a chunk whole."""

    text: str
    char_from: int
    char_to: int
    unit: Unit

    @property
    def page(self) -> int | None:
        return self.unit.page

    @property
    def kind(self) -> str:
        return self.unit.kind

    @property
    def path(self) -> list[str]:
        return self.unit.path

    @property
    def meta(self) -> dict:
        return self.unit.meta


@dataclass
class Chunk:
    seq: int
    text: str
    char_from: int
    char_to: int
    page_from: int | None = None
    page_to: int | None = None
    path: list[str] = field(default_factory=list)
    kind: str = "text"
    meta: dict = field(default_factory=dict)

    @property
    def heading_trail(self) -> str:
        return " › ".join(self.path)

    @property
    def where(self) -> str:
        """Where this came from, for a card with no room to explain."""
        bits: list[str] = []
        if self.page_from:
            bits.append(
                f"p. {self.page_from}"
                if self.page_to in (None, self.page_from)
                else f"pp. {self.page_from}–{self.page_to}"
            )
        if self.meta.get("sender"):
            bits.append(str(self.meta["sender"]))
        if self.path:
            bits.append(self.heading_trail)
        return " · ".join(bits)


def chunk_source(parsed: Parsed, budget: int = 1800) -> list[Chunk]:
    """Pack a parsed source's units into chunks with provenance.

    `budget` is in characters, knowingly: nothing in the application counts
    tokens yet, so the bound is set low enough that the approximation cannot
    matter. When a tokenizer lands it replaces this argument.
    """
    budget = max(budget, 200)
    source_text = parsed.text
    pieces = [p for unit_pieces in _pieces(parsed.units, budget) for p in unit_pieces]
    if not pieces:
        return []

    chunks: list[Chunk] = []
    current: list[Piece] = []

    def flush() -> None:
        if current:
            chunks.append(_assemble(len(chunks), current, source_text))
            current.clear()

    for piece in pieces:
        if current and _breaks(current, piece, budget):
            carry = _overlap(current, piece, budget)
            flush()
            current.extend(carry)
        current.append(piece)

    flush()
    return chunks


def _pieces(units: list[Unit], budget: int) -> list[list[Piece]]:
    """Every unit with its offsets, oversized ones split on sentence boundaries."""
    out: list[list[Piece]] = []
    cursor = 0
    for n, unit in enumerate(units):
        if n:
            cursor += len(JOIN)
        out.append(_split(unit, cursor, budget))
        cursor += len(unit.text)
    return out


def _split(unit: Unit, start: int, budget: int) -> list[Piece]:
    """One unit as one piece, or several if it is too long to fit a chunk."""
    if len(unit.text) <= budget:
        return [Piece(unit.text, start, start + len(unit.text), unit)]

    pieces: list[Piece] = []
    offset = 0
    for fragment in _fragments(unit.text, budget):
        # `find` from the running offset rather than a running sum, so the
        # whitespace the split consumed is accounted for exactly and the offsets
        # stay true against the original text.
        at = unit.text.find(fragment, offset)
        if at < 0:  # pragma: no cover — _fragments only ever returns substrings
            at = offset
        pieces.append(Piece(fragment, start + at, start + at + len(fragment), unit))
        offset = at + len(fragment)
    return pieces


def _fragments(text: str, budget: int) -> list[str]:
    """Cut text into pieces no longer than the budget, preferring sentence ends."""
    out: list[str] = []
    for sentence in _SENTENCE.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if out and len(out[-1]) + 1 + len(sentence) <= budget:
            out[-1] = f"{out[-1]} {sentence}"
        elif len(sentence) <= budget:
            out.append(sentence)
        else:
            out.extend(_hard_cut(sentence, budget))
    return out or [text[:budget]]


def _hard_cut(text: str, budget: int) -> list[str]:
    """A sentence longer than a whole chunk: cut it at the last space that fits.

    Reached by a table flattened into one line and by PDFs whose extraction
    produced no sentence punctuation at all. Cutting on a space rather than a
    character count is what keeps an identifier from being sliced in half.
    """
    out: list[str] = []
    rest = text
    while len(rest) > budget:
        window = rest[:budget]
        cut = window.rfind(" ")
        if cut < budget // 2:  # no usable space: take the whole window
            cut = budget
        out.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
    if rest:
        out.append(rest)
    return [piece for piece in out if piece]


def _breaks(current: list[Piece], piece: Piece, budget: int) -> bool:
    """Should a chunk end before this piece?

    Structural reasons first and absolutely: a heading opens a new chunk because
    it describes what follows, a new page or a new author is a new place, and a
    change of heading trail means the subject changed. Only then does length
    decide.
    """
    previous = current[-1]
    if piece.kind == "heading":
        return True
    if previous.page is not None and piece.page is not None and piece.page != previous.page:
        return True
    if piece.kind == "message" and previous.kind == "message":
        if piece.meta.get("sender") != previous.meta.get("sender"):
            return True
        if piece.meta.get("separator") != previous.meta.get("separator"):
            return True
    if piece.path != previous.path:
        return True
    return piece.char_to - current[0].char_from > budget


def _overlap(current: list[Piece], upcoming: Piece, budget: int) -> list[Piece]:
    """The one piece to repeat at the start of the next chunk, if any.

    A fact sitting across a boundary would otherwise be in neither chunk whole.
    Repeating the last piece is cheap because deduplication collapses the
    duplicate candidates it causes — and it is skipped wherever the boundary is a
    real change of subject, since the preceding paragraph is then about something
    else and would only be noise.
    """
    last = current[-1]
    if upcoming.kind == "heading" or last.kind == "heading":
        return []
    if last.path != upcoming.path:
        return []
    if last.page is not None and upcoming.page is not None and last.page != upcoming.page:
        return []
    if last.kind == "message" and last.meta.get("sender") != upcoming.meta.get("sender"):
        return []
    # An overlap that leaves no room for the piece it is meant to give context
    # to would turn one oversized chunk into two.
    if len(last.text) + len(JOIN) + len(upcoming.text) > budget:
        return []
    return [last]


def _assemble(seq: int, pieces: list[Piece], source_text: str) -> Chunk:
    pages = [p.page for p in pieces if p.page is not None]
    # The first piece's trail, not the longest: a chunk belongs where it starts,
    # and a trailing table row carrying a deeper trail should not relabel it.
    path = list(pieces[0].path)
    if pieces[0].kind == "heading":
        path = path + [pieces[0].text]

    meta: dict = {}
    for piece in pieces:
        for key in ("sender", "sender_name", "sender_domain", "sent_at", "separator"):
            if key in piece.meta and key not in meta:
                meta[key] = piece.meta[key]

    kinds = {p.kind for p in pieces}
    kind = "table" if kinds == {"table_row"} else ("message" if "message" in kinds else "text")

    # Sliced out of the source rather than rebuilt by joining the pieces. The
    # pieces of a chunk are always a contiguous run — the only piece ever carried
    # over from the previous chunk is the one immediately before this run — so
    # the slice is both exactly the chunk's text and verbatim source, and the
    # offset invariant holds by construction rather than by arithmetic.
    return Chunk(
        seq=seq,
        text=source_text[pieces[0].char_from : pieces[-1].char_to],
        char_from=pieces[0].char_from,
        char_to=pieces[-1].char_to,
        page_from=min(pages) if pages else None,
        page_to=max(pages) if pages else None,
        path=path,
        kind=kind,
        meta=meta,
    )
