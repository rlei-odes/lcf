"""Word documents, with python-docx.

Already a dependency, because it underlies the docx renderer. Here it is read
rather than written, and the two things worth getting right are real heading
styles — which give a trustworthy heading trail, unlike a PDF where headings are
guessed from text — and table cells, which is where measurement data lives.

A .docx has no pages. Pagination happens at render time in Word and is not in
the file, so `page` stays None and the heading trail does the locating instead.
"""

from lcf.ingest.parse import Image, Parsed, ParseFailed, Unit


def parse(data: bytes, filename: str = "") -> Parsed:
    import io

    from docx import Document
    from docx.opc.exceptions import PackageNotFoundError

    try:
        document = Document(io.BytesIO(data))
    except (PackageNotFoundError, KeyError, ValueError) as exc:
        raise ParseFailed(f"that Word file could not be opened: {exc}") from exc

    units: list[Unit] = []
    trail: list[str] = []

    for block in _body(document):
        if block[0] == "paragraph":
            paragraph = block[1]
            text = (paragraph.text or "").strip()
            if not text:
                continue
            level = _heading_level(paragraph)
            if level:
                trail = trail[: level - 1] + [text]
                units.append(Unit(text=text, path=list(trail[:-1]), kind="heading"))
            else:
                units.append(Unit(text=text, path=list(trail), kind="paragraph"))
        else:
            units.extend(_table_units(block[1], trail))

    if not units:
        raise ParseFailed("that Word file has no text in it")

    return Parsed(units=units, images=_images(document), pages=0)


def _body(document):
    """Paragraphs and tables in the order they appear in the file.

    `document.paragraphs` and `document.tables` are two separate lists, so using
    them would put every table after every paragraph — and a measurement table
    would lose the heading that says what it measures. The body element's own
    children are the only place the real order exists.
    """
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            yield ("paragraph", Paragraph(child, document))
        elif tag == "tbl":
            yield ("table", Table(child, document))


def _heading_level(paragraph) -> int:
    """The outline level from the paragraph's style, 0 for body text.

    Reads the style name rather than `outline_lvl`, because that is what authors
    actually set and what survives a document being passed between Word
    installations in different languages — hence `Überschrift` beside `Heading`.
    """
    name = (getattr(paragraph.style, "name", "") or "").strip()
    lowered = name.lower()
    for prefix in ("heading", "überschrift", "uberschrift", "titre", "titolo"):
        if lowered.startswith(prefix):
            tail = lowered[len(prefix) :].strip()
            if tail.isdigit():
                return min(int(tail), 6)
            return 1
    if lowered in ("title", "titel"):
        return 1
    return 0


def _table_units(table, trail: list[str]) -> list[Unit]:
    """One unit per row, cells paired with their header.

    The same reasoning as a CSV row: `12,05` means nothing without `Messwert`.
    The first row is taken as the header when every cell in it is non-empty,
    which is true of real tables and false of a table being used for layout.
    """
    rows = [[(cell.text or "").strip() for cell in row.cells] for row in table.rows]
    if not rows:
        return []

    header = rows[0] if all(cell for cell in rows[0]) and len(rows) > 1 else []
    out: list[Unit] = []
    path = list(trail)

    if header:
        out.append(Unit(text=" | ".join(header), path=path, kind="heading"))

    for n, row in enumerate(rows[1:] if header else rows, start=1):
        if header:
            pairs = [
                f"{header[i] if i < len(header) else f'col{i + 1}'}: {cell}"
                for i, cell in enumerate(row)
                if cell
            ]
            text = " · ".join(pairs)
        else:
            text = " | ".join(cell for cell in row if cell)
        if text:
            out.append(Unit(text=text, path=path, kind="table_row", meta={"row": n}))
    return out


def _images(document) -> list[Image]:
    out: list[Image] = []
    for part in document.part.package.parts:
        content_type = getattr(part, "content_type", "") or ""
        if not content_type.startswith("image/"):
            continue
        blob = getattr(part, "blob", None)
        if not blob:
            continue
        out.append(
            Image(
                data=blob,
                page=None,
                name=str(getattr(part, "partname", "")).rsplit("/", 1)[-1],
                media_type=content_type,
            )
        )
    return out
