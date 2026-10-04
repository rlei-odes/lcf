"""PDF, with pypdf.

Pure Python, no model weights, and a page number on every unit — which is the
whole requirement. What it cannot do is read a scan, and the honest response to
one is to say so rather than record a source with no content.
"""

from lcf.ingest.parse import Image, Parsed, ParseFailed, Unit

# A scan is judged per page, not per document, because a one-page cover note is
# legitimately short and a forty-page scan is legitimately empty. A scanned page
# yields nothing, or a handful of characters from a stray text annotation; below
# this average there is nothing to search and saying so beats storing it.
_MIN_CHARS_PER_PAGE = 10

# Images smaller than this are rules, bullets and logo fragments. Judged on
# pixels rather than bytes because a 2 KB PNG can be a perfectly legible crop
# of a measurement, while a 40 KB one can be a letterhead.
MIN_IMAGE_PIXELS = 80 * 80


def parse(data: bytes, filename: str = "") -> Parsed:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(_stream(data))
        pages = list(reader.pages)
    except (PdfReadError, OSError, ValueError) as exc:
        raise ParseFailed(f"that PDF could not be opened: {exc}") from exc

    if reader.is_encrypted:
        # pypdf will have tried the empty password already; reaching here means
        # it is really locked.
        raise ParseFailed("that PDF is password protected. Unlock it and try again.")

    units: list[Unit] = []
    images: list[Image] = []
    characters = 0
    unreadable = 0

    for number, page in enumerate(pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 — one broken page must not lose the rest
            unreadable += 1
            text = ""
        characters += len(text.strip())
        for block in _paragraphs(text):
            units.append(Unit(text=block, page=number, kind="paragraph"))
        images.extend(_images(page, number))

    notes = []
    if unreadable:
        notes.append(f"{unreadable} of {len(pages)} pages could not be read")

    if characters < _MIN_CHARS_PER_PAGE * max(len(pages), 1):
        raise ParseFailed(
            "no text could be read from that PDF — it looks like a scan. Install "
            "the docling extra for OCR, or paste the text in instead."
        )

    return Parsed(units=units, images=images, pages=len(pages), notes=notes)


def _stream(data: bytes):
    import io

    return io.BytesIO(data)


def _paragraphs(text: str) -> list[str]:
    """Blank-line-separated blocks, with hyphenation at line ends repaired.

    PDF text extraction returns hard-wrapped lines. Joining them inside a block
    matters more than it looks: a complaint number broken across a line as
    `NW-CL-\n88213` is invisible to every pattern unless the break is closed up
    first, and a quote sliced out of wrapped text reads as broken to whoever is
    checking it.

    A trailing hyphen is ambiguous, and the two cases pull opposite ways:
    `Eingangs-\nprüfung` wants the hyphen gone, `NW-CL-\n88213` wants it kept.
    What tells them apart is the character the next line starts with — German
    hyphenation breaks before a lowercase continuation, an identifier before a
    digit or a capital. Keeping the hyphen is also the safer error of the two:
    a pattern hunting identifiers is what this whole feature is for.
    """
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n\n"):
        lines = [line.strip() for line in raw.split("\n") if line.strip()]
        if not lines:
            continue
        joined = ""
        for line in lines:
            if not joined:
                joined = line
            elif joined.endswith("-") and line[:1].islower():
                joined = joined[:-1] + line
            elif joined.endswith("-"):
                joined += line
            else:
                joined = f"{joined} {line}"
        if joined.strip():
            out.append(joined.strip())
    return out


def _images(page, number: int) -> list[Image]:
    try:
        found = list(page.images)
    except Exception:  # noqa: BLE001 — an unsupported filter must not lose the page
        return []

    out: list[Image] = []
    for item in found:
        data = getattr(item, "data", None)
        if not data:
            continue
        out.append(
            Image(
                data=data,
                page=number,
                name=getattr(item, "name", "") or "",
                media_type=_media_type(getattr(item, "name", "")),
            )
        )
    return out


def _media_type(name: str) -> str:
    lowered = (name or "").lower()
    if lowered.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lowered.endswith(".tif") or lowered.endswith(".tiff"):
        return "image/tiff"
    return "image/png"
