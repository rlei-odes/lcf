"""What a file turns into, and who turns it.

Several backends, dispatched on media type and extension, each a module exposing
one `parse(data, filename) -> Parsed`. This is the only place that knows which —
everything downstream takes a `Parsed`.

The important shape here is `Unit`: a parser does not return a wall of text, it
returns the smallest pieces it can still vouch for — a paragraph, a table row,
one message of a mail thread — each carrying where it came from. The chunker
packs those into chunks (`chunk.py`); nothing downstream has to guess where a
page ended or who wrote a reply.

`Parsed.text` is the concatenation of those units and is stored verbatim on the
source row, because chunk offsets index into it and because *show me what you
actually read* is the one debugging surface a parser needs.
"""

from dataclasses import dataclass, field

from lcf.ingest.text import clean, clean_line


class ParseFailed(Exception):
    """The bytes could not be turned into text worth keeping.

    Raised rather than returned empty: a source recorded as parsed with no
    content is indistinguishable from one that genuinely says nothing, and the
    person who dropped a scanned PDF in needs to be told it was a scan.
    """


@dataclass
class Unit:
    """The smallest piece of a source a parser will vouch for."""

    text: str
    # 1-based, as a reader counts them. None where the format has no pages.
    page: int | None = None
    # The heading trail above this unit: ["3. Messergebnisse", "Tabelle 2"].
    # What tells a model, and a person, what a bare row of numbers is about.
    path: list[str] = field(default_factory=list)
    # paragraph | heading | table_row | list_item | message | caption
    kind: str = "paragraph"
    # Whatever the unit knew about itself. For one message of a mail thread:
    # its own sender and date, which is the whole reason mail is parsed per
    # reply rather than as one body (EVIDENCE-DESK §4.3).
    meta: dict = field(default_factory=dict)

    @property
    def heading_trail(self) -> str:
        return " › ".join(self.path)


@dataclass
class Image:
    """An image found inside a source, still as bytes."""

    data: bytes
    # Where it was found, for the card that will show it.
    page: int | None = None
    name: str = ""
    media_type: str = "image/png"


@dataclass
class Attachment:
    """A file that arrived inside another file.

    Returned rather than parsed in place, because an attachment is a source in
    its own right: the measurement PDF that came with the complaint deserves its
    own page count, its own chunks and its own row, with the mail as its parent.
    """

    filename: str
    media_type: str
    data: bytes


@dataclass
class Parsed:
    """Everything one file yielded."""

    units: list[Unit]
    images: list[Image] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    pages: int = 0
    # Lifted from the format when it carries them. Mail fills every field here;
    # nothing else fills any of them.
    sender: str | None = None
    sender_name: str | None = None
    sent_at: object | None = None  # datetime, kept loose so this module stays pure
    subject: str | None = None
    meta: dict = field(default_factory=dict)
    # Parsers that silently dropped something say how much, so the gather panel
    # can report it rather than leaving a person to wonder.
    notes: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        """The units joined, which is what chunk offsets index into."""
        return "\n\n".join(u.text for u in self.units)


def _extension(filename: str) -> str:
    _, _, ext = filename.rpartition(".")
    return ext.lower() if ext and ext != filename else ""


# Media types browsers actually send, mapped to the extension we trust more.
# A .eml dropped from a mail client arrives as `application/octet-stream` about
# as often as `message/rfc822`, so the extension decides and the media type is
# only a fallback for a paste with no filename at all.
TEXTUAL = {"txt", "md", "markdown", "csv", "tsv", "log", "json", "yaml", "yml"}
IMAGES = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff"}


def is_image(media_type: str, filename: str) -> bool:
    return _extension(filename) in IMAGES or media_type.startswith("image/")


def parse(data: bytes, filename: str, media_type: str = "", parser: str = "auto") -> Parsed:
    """Turn one file into units.

    `parser` selects the backend for PDFs: `auto` prefers docling when the extra
    is installed, `builtin` never reaches for it, `docling` insists on it. Every
    other format has exactly one implementation, so the setting does not apply
    and is not consulted.

    Every backend's output passes through `sanitise` here rather than in each
    backend. A parser's job is to find the text; making it storable and matchable
    is one job with one answer, and leaving it to the backends means the next one
    added forgets it (`text.py`).
    """
    parsed = sanitise(_dispatch(data, filename, media_type, parser))
    if not parsed.units and not parsed.attachments:
        # A backend that returned units made entirely of control characters has
        # found nothing, and saying so is the contract every other empty parse
        # already honours.
        raise ParseFailed(
            f"{filename or 'that file'} held no readable text — only formatting characters."
        )
    return parsed


def sanitise(parsed: Parsed) -> Parsed:
    """Clean every piece of text a parser produced, in place.

    Runs before `Parsed.text` is ever read, so chunk offsets are computed against
    the cleaned text and the offset invariant holds. Units left empty by the
    cleaning are dropped: a unit that was nothing but control characters is not a
    paragraph, and keeping it would put an empty passage in front of a person.
    """
    units: list[Unit] = []
    for unit in parsed.units:
        unit.text = clean(unit.text).strip()
        if not unit.text:
            continue
        unit.path = [p for p in (clean_line(p) for p in unit.path) if p]
        unit.meta = {k: _clean_value(v) for k, v in unit.meta.items()}
        units.append(unit)
    parsed.units = units

    parsed.sender = clean_line(parsed.sender or "") or None
    parsed.sender_name = clean_line(parsed.sender_name or "") or None
    parsed.subject = clean_line(parsed.subject or "") or None
    parsed.meta = {k: _clean_value(v) for k, v in parsed.meta.items()}
    parsed.notes = [clean_line(n) for n in parsed.notes]

    for image in parsed.images:
        image.name = clean_line(image.name)
    for attachment in parsed.attachments:
        attachment.filename = clean_line(attachment.filename)
    return parsed


def _clean_value(value: object) -> object:
    """Strings inside parser metadata, which lands in a JSON column."""
    if isinstance(value, str):
        return clean_line(value)
    if isinstance(value, list):
        return [_clean_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _clean_value(v) for k, v in value.items()}
    return value


def _dispatch(data: bytes, filename: str, media_type: str, parser: str) -> Parsed:
    ext = _extension(filename)

    if ext == "pdf" or media_type == "application/pdf":
        from lcf.ingest import pdf

        if parser in ("auto", "docling"):
            from lcf.ingest import docling_backend

            if docling_backend.available():
                return docling_backend.parse_pdf(data, filename)
            if parser == "docling":
                raise ParseFailed(
                    "LCF_INGEST_PARSER is set to 'docling' but the extra is not "
                    "installed. Install lcf[docling], or set it to 'builtin'."
                )
        return pdf.parse(data, filename)

    if ext in ("docx", "docm") or media_type.endswith("wordprocessingml.document"):
        from lcf.ingest import office

        return office.parse(data, filename)

    if ext == "eml" or media_type in ("message/rfc822", "text/rfc822"):
        from lcf.ingest import mail

        return mail.parse(data, filename)

    if ext in TEXTUAL or media_type.startswith("text/") or not ext:
        from lcf.ingest import plain

        return plain.parse(data, filename)

    raise ParseFailed(
        f"{filename or 'that file'} is a .{ext} and nothing here reads one. "
        "PDF, Word, email, images and plain text are what the desk takes."
    )
