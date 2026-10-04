"""Text that is already text.

A paste, a .txt, a .md, a CSV. The only real work is splitting into units and
keeping markdown headings as the heading trail, so a pasted report with `##`
headings chunks as well as a Word file with real ones.
"""

import csv
import io

from lcf.ingest.parse import Parsed, ParseFailed, Unit

# A markdown heading, ATX style. Setext headings (underlined with === or ---)
# are not recognised, because a line of dashes in pasted plain text is far more
# often a separator somebody typed than a heading they meant.
_MAX_HEADING_LEVEL = 6


def decode(data: bytes) -> str:
    """Make text out of bytes without ever failing on an encoding.

    Material arrives from Windows mail clients and German Office installs, so
    cp1252 is a real possibility and latin-1 is the backstop that cannot fail.
    Losing a character is better than refusing the file.
    """
    for encoding in ("utf-8", "utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def parse(data: bytes, filename: str = "") -> Parsed:
    return from_text(decode(data), filename)


def from_text(text: str, filename: str = "") -> Parsed:
    """Split text into units, honouring markdown headings and CSV rows."""
    body = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        raise ParseFailed("there is no text in that")

    if filename.lower().endswith((".csv", ".tsv")):
        units = _csv_units(body, "\t" if filename.lower().endswith(".tsv") else ",")
        if units:
            return Parsed(units=units, pages=1)

    units: list[Unit] = []
    trail: list[str] = []
    for block in _blocks(body):
        level = _heading_level(block)
        if level:
            title = block.lstrip("#").strip()
            trail = trail[: level - 1] + [title]
            units.append(Unit(text=block.strip(), path=list(trail[:-1]), kind="heading"))
            continue
        units.append(Unit(text=block.strip(), path=list(trail), kind="paragraph"))

    return Parsed(units=units, pages=1)


def _blocks(body: str) -> list[str]:
    """Blank-line-separated blocks, with headings split out even when crowded.

    A pasted report routinely has a heading on the line directly above its
    paragraph with no blank line between them. Treating that as one block would
    bury the heading in the body text and lose the trail, so a heading line
    always ends the block before it and forms one of its own.
    """
    out: list[str] = []
    current: list[str] = []

    def flush() -> None:
        joined = "\n".join(current).strip()
        if joined:
            out.append(joined)
        current.clear()

    for line in body.split("\n"):
        if not line.strip():
            flush()
            continue
        if _heading_level(line):
            flush()
            out.append(line.strip())
            continue
        current.append(line)
    flush()
    return out


def _heading_level(line: str) -> int:
    stripped = line.strip()
    if not stripped.startswith("#"):
        return 0
    level = len(stripped) - len(stripped.lstrip("#"))
    if level > _MAX_HEADING_LEVEL or not stripped[level:].strip():
        return 0
    return level


def _rows(body: str, delimiter: str) -> list[list[str]]:
    """CSV rows, with quoting switched off when the quoting is broken.

    One unmatched `"` opening a field makes `csv.reader` read to end of file
    looking for its partner, and a measurement table comes back as a single cell
    containing the rest of the document. Nothing raises; the rows are simply
    gone, along with the per-row provenance that makes a table worth parsing as
    a table at all.

    An odd number of quote characters is exactly the condition — a correctly
    quoted file has them in pairs, and a doubled `""` inside a field counts two.
    When it holds, `"` is treated as an ordinary character, which is what it
    evidently is in that file.
    """
    if body.count('"') % 2:
        return list(csv.reader(io.StringIO(body), delimiter=delimiter, quoting=csv.QUOTE_NONE))
    return list(csv.reader(io.StringIO(body), delimiter=delimiter))


def _csv_units(body: str, delimiter: str) -> list[Unit]:
    """One unit per row, with the header woven in.

    A row of bare values is useless to a model and to a reader — `12,05` means
    nothing without `Messwert`. Pairing each cell with its column turns a
    spreadsheet row into a sentence a question can be asked of.
    """
    try:
        rows = _rows(body, delimiter)
    except csv.Error:
        return []
    if len(rows) < 2:
        return []

    header = [cell.strip() for cell in rows[0]]
    if not any(header):
        return []

    units = [Unit(text=delimiter.join(header), kind="heading")]
    for n, row in enumerate(rows[1:], start=1):
        pairs = [
            f"{header[i] if i < len(header) and header[i] else f'col{i + 1}'}: {cell.strip()}"
            for i, cell in enumerate(row)
            if cell.strip()
        ]
        if pairs:
            units.append(Unit(text=" · ".join(pairs), kind="table_row", meta={"row": n}))
    return units
