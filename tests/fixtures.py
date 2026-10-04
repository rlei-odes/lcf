"""Material to parse, built rather than committed.

Every fixture here is generated in Python, which is deliberate. A committed
binary blob is a file nobody can read in a diff and nobody dares change, and
what the parser tests actually need is *known* content — a specific complaint
number on a specific page — so building it is the only way to assert against it.

The PDF is assembled by hand because no dependency here writes one: `pypdf`
reads, `docling` is optional, and adding `reportlab` to the dev set to produce a
two-page test file would be a dependency for one fixture.
"""

from io import BytesIO


def pdf(pages: list[list[str]]) -> bytes:
    """A minimal, valid PDF with one text block per page.

    Assembled object by object with a real cross-reference table, because
    `pypdf` needs the xref offsets to be right — a PDF with plausible objects
    and a wrong xref reads as a corrupt file, which would make the fixture test
    the recovery path instead of the parser.
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    contents: list[int] = []
    for lines in pages:
        drawn = b"\n".join(b"(%s) Tj 0 -16 Td" % _escape(line) for line in lines)
        stream = b"BT /F1 12 Tf 54 720 Td\n" + drawn + b"\nET"
        contents.append(add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)))

    # The page tree has to be referenced by its children, so its object number
    # is reserved before the pages that point at it are written.
    tree = len(objects) + len(pages) + 1
    page_ids = [
        add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>" % (tree, font, content)
        )
        for content in contents
    ]
    kids = b" ".join(b"%d 0 R" % page for page in page_ids)
    pages_obj = add(b"<< /Type /Pages /Count %d /Kids [%s] >>" % (len(page_ids), kids))
    assert pages_obj == tree, "the reserved page-tree object number went stale"
    root = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj)

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"

    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        root,
        xref,
    )
    return bytes(out)


def _escape(text: str) -> bytes:
    """PDF string escaping, and latin-1 because a base-14 font has no umlauts.

    The fixtures avoid umlauts in PDFs for that reason; the mail and paste
    fixtures carry them instead, which is where the parsers that have to cope
    with them actually run.
    """
    escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    return escaped.encode("latin-1", errors="replace")


def docx(blocks: list[tuple[str, str]]) -> bytes:
    """A Word file from (style, text) pairs. `python-docx` is already a dependency."""
    from docx import Document

    document = Document()
    for style, text in blocks:
        if style == "table":
            rows = [row.split("|") for row in text.split("\n")]
            table = document.add_table(rows=len(rows), cols=len(rows[0]))
            for r, row in enumerate(rows):
                for c, cell in enumerate(row):
                    table.cell(r, c).text = cell.strip()
        elif style:
            document.add_paragraph(text, style=style)
        else:
            document.add_paragraph(text)

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def png(width: int = 200, height: int = 160, colour: tuple = (90, 130, 110)) -> bytes:
    """A solid-colour PNG. Size is what the harvester judges, so it is the argument."""
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


# A German reply chain four deep, with the three things that poison a chunk: a
# signature, a legal disclaimer, and quoted text from two earlier messages. The
# separator lines are the forms Outlook and Apple Mail actually write.
THREAD = """From: Anna Schulz <a.schulz@nordwerk.de>
To: Peter Meier <p.meier@wirgmbh.de>
Cc: QS <qs@wirgmbh.de>
Subject: AW: Reklamation NW-CL-88213
Date: Tue, 10 Mar 2026 09:14:00 +0100
Content-Type: text/plain; charset=utf-8

Guten Morgen Herr Meier,

die Teile der Charge LOT-2026-0417 liegen außerhalb der vereinbarten Toleranz.
Der Messwert betrug 12,05 mm.

Mit freundlichen Grüßen
Anna Schulz
Nordwerk GmbH | Tel +49 123 456789

Diese E-Mail enthält vertrauliche Informationen. Sollten Sie nicht der richtige
Adressat sein, löschen Sie diese Nachricht.

-----Ursprüngliche Nachricht-----
Von: Peter Meier <p.meier@wirgmbh.de>
Gesendet: Montag, 9. März 2026 08:12
An: Anna Schulz <a.schulz@nordwerk.de>
Betreff: AW: Reklamation NW-CL-88213

Guten Tag,

wir prüfen das und melden uns bis Mittwoch.

Am 08.03.2026 um 17:40 schrieb Anna Schulz <a.schulz@nordwerk.de>:
> Bitte um Stellungnahme zu unserer Reklamation NW-CL-88213.
"""


# Material with something for each tier: an identifier with a shape, a value
# near a known word, and a date with neither.
NOTES = """# Reklamation NW-CL-88213

Bei der Eingangsprüfung der Charge LOT-2026-0417 wurden Abweichungen festgestellt.
Betroffen ist zusätzlich die Charge LOT-2026-0418.

## 3. Messergebnisse

Der Messwert betrug 12,05 mm bei einem Sollmaß von 12,00 mm. Die Toleranz
beträgt +/- 0,02 mm, die Abweichung liegt somit außerhalb.

## 4. Sonstiges

Die Lieferung erfolgte am 28.02.2026 mit Lieferschein 99812.
"""
