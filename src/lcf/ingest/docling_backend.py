"""The optional layout-aware parser.

`docling` is the better PDF parser — a layout model, real table structure,
reading order, OCR for scans, and provenance down to a bounding box. It is not a
dependency because it brings `torch` and downloads model weights from
HuggingFace the first time it runs, and an installation on a private network
with no egress must still be able to read a PDF (EVIDENCE-DESK §4.2).

So it lives behind `available()`, and every import of it is inside a function.
Importing `docling` at module scope would cost seconds of startup on every
installation that has it and an ImportError on every one that does not.
"""

from functools import lru_cache

from lcf.ingest.parse import Image, Parsed, ParseFailed, Unit


@lru_cache(maxsize=1)
def available() -> bool:
    """Is the extra installed?

    Cached: this is asked once per uploaded file, and the answer cannot change
    without the process restarting.
    """
    try:
        import docling.document_converter  # noqa: F401
    except Exception:  # noqa: BLE001 — a half-installed extra must read as absent
        return False
    return True


def parse_pdf(data: bytes, filename: str = "") -> Parsed:
    """Parse with docling, falling back to the built-in parser on failure.

    The fallback matters more than it looks. docling's first run downloads
    weights, and a download that fails behind a proxy would otherwise turn a
    working feature into a broken one on the installation least able to debug
    it. A worse parse is the right answer there; no parse is not.
    """
    import io

    from lcf.ingest import pdf

    try:
        from docling.datamodel.base_models import InputFormat
        from docling.document_converter import DocumentConverter

        stream = _stream(filename or "upload.pdf", io.BytesIO(data))
        result = DocumentConverter(allowed_formats=[InputFormat.PDF]).convert(stream)
        document = result.document
    except ParseFailed:
        raise
    except Exception as exc:  # noqa: BLE001 — torch, model loading, OCR, any of it
        from loguru import logger

        logger.warning("docling could not parse {}: {}; falling back", filename, exc)
        return pdf.parse(data, filename)

    units, pages = _units(document)
    if not units:
        return pdf.parse(data, filename)

    return Parsed(
        units=units,
        images=_images(document),
        pages=pages,
        notes=["parsed with docling"],
    )


def _stream(name: str, buffer):
    from docling.datamodel.base_models import DocumentStream

    return DocumentStream(name=name, stream=buffer)


def _units(document) -> tuple[list[Unit], int]:
    """Walk the document's own items, keeping the heading trail and the page.

    docling's `iterate_items` yields in reading order with a level per item,
    which is what makes the trail trustworthy here in a way it cannot be in the
    built-in PDF parser, where a heading is guessed from text.
    """
    units: list[Unit] = []
    trail: list[str] = []
    pages = 0

    for item, _level in document.iterate_items():
        text = (getattr(item, "text", "") or "").strip()
        label = str(getattr(item, "label", "") or "").lower()
        page = _page(item)
        if page:
            pages = max(pages, page)

        if label in ("table", "picture") and not text:
            table = _table(item, document, trail, page)
            units.extend(table)
            continue
        if not text:
            continue

        if "header" in label or "title" in label or "section" in label:
            depth = max(_heading_depth(item), 1)
            trail = trail[: depth - 1] + [text]
            units.append(Unit(text=text, page=page, path=list(trail[:-1]), kind="heading"))
            continue

        if "list" in label:
            kind = "list_item"
        elif "caption" in label:
            kind = "caption"
        else:
            kind = "paragraph"
        units.append(Unit(text=text, page=page, path=list(trail), kind=kind))

    return units, pages or getattr(getattr(document, "pages", None), "__len__", lambda: 0)()


def _table(item, document, trail: list[str], page: int | None) -> list[Unit]:
    """A docling table as one unit per row, cells paired with their header."""
    try:
        frame = item.export_to_dataframe(doc=document)
    except Exception:  # noqa: BLE001 — not every table exports cleanly
        try:
            text = item.export_to_markdown(doc=document)
        except Exception:  # noqa: BLE001
            return []
        if not text.strip():
            return []
        return [Unit(text=text.strip(), page=page, path=list(trail), kind="table_row")]

    out: list[Unit] = []
    columns = [str(c) for c in frame.columns]
    for n, row in enumerate(frame.itertuples(index=False), start=1):
        pairs = [
            f"{columns[i]}: {value}"
            for i, value in enumerate(row)
            if str(value).strip() and str(value).strip().lower() != "nan"
        ]
        if pairs:
            out.append(
                Unit(
                    text=" · ".join(pairs),
                    page=page,
                    path=list(trail),
                    kind="table_row",
                    meta={"row": n},
                )
            )
    return out


def _page(item) -> int | None:
    prov = getattr(item, "prov", None) or []
    for entry in prov:
        number = getattr(entry, "page_no", None)
        if number:
            return int(number)
    return None


def _heading_depth(item) -> int:
    for attribute in ("level", "heading_level"):
        value = getattr(item, attribute, None)
        if isinstance(value, int) and value > 0:
            return min(value, 6)
    return 1


def _images(document) -> list[Image]:
    out: list[Image] = []
    for picture in getattr(document, "pictures", None) or []:
        data = _png(picture, document)
        if data:
            out.append(Image(data=data, page=_page(picture), media_type="image/png"))
    return out


def _png(picture, document) -> bytes | None:
    import io

    try:
        image = picture.get_image(document)
    except Exception:  # noqa: BLE001 — a picture without stored bytes
        return None
    if image is None:
        return None
    buffer = io.BytesIO()
    try:
        image.save(buffer, format="PNG")
    except Exception:  # noqa: BLE001
        return None
    return buffer.getvalue()
