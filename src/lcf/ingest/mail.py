"""Email — the messiest input, and the likeliest to arrive.

Two things make mail different from every other format, and both are the reason
this module exists rather than a line in `plain.py`.

**A thread is several statements by several people.** `mail-parser-reply` splits
it on the separator headers mail clients insert — in thirteen languages, German
among them — and strips signatures and legal disclaimers from each part. Every
reply becomes its own unit carrying its own sender, so a candidate pulled out of
a four-deep forward chain can say *who* said it instead of naming a file called
`RE RE FW Reklamation.eml`.

**A sender is evidence about the evidence.** "Die Teile sind innerhalb der
Toleranz" means one thing from the customer's domain and the opposite from a
colleague, so `sender` and `sender_domain` are lifted to columns rather than
buried (EVIDENCE-DESK §4.3).

Boilerplate is where the perceived quality of the whole feature sits: a chunk
that is 90% legal footer poisons every extraction run over it.
"""

import re
from email import message_from_bytes, policy
from email.utils import getaddresses, parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser

from lcf.ingest.parse import Attachment, Image, Parsed, ParseFailed, Unit

# An address inside a separator header, preferring the angle-bracketed form a
# mail client writes: `Von: Peter Meier <p.meier@example.de>`.
_ANGLED = re.compile(r"<([^<>@\s]+@[^<>@\s]+)>")
_BARE = re.compile(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
# What an address may look like once the angle brackets are off: no whitespace,
# no brackets, no separators, exactly one @. Deliberately lenient about the
# domain — `user@intranet` is a real internal sender — and strict about the
# characters that mean a parse went wrong rather than a name being unusual.
_ADDRESS_SHAPE = re.compile(r"^[^\s<>@,;\"]+@[^\s<>@,;\"]+$")
# Dates in the two forms worth parsing from a localised separator line. Anything
# else stays as the raw header text, which is better than a wrong date.
_DMY = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def parse(data: bytes, filename: str = "") -> Parsed:
    try:
        message = message_from_bytes(data, policy=policy.default)
    except Exception as exc:  # noqa: BLE001 — malformed MIME is varied and not ours
        raise ParseFailed(f"that email could not be read: {exc}") from exc

    subject = _header(message, "Subject")
    sender, sender_name = _one_address(_header(message, "From"))
    sent_at = _date(message)
    body, images, attachments = _body(message)

    if not body.strip() and not attachments:
        raise ParseFailed("that email has no text and no attachments")

    units = _units(body, sender=sender, sender_name=sender_name, sent_at=sent_at, subject=subject)

    return Parsed(
        units=units,
        images=images,
        attachments=attachments,
        pages=0,
        sender=sender,
        sender_name=sender_name,
        sent_at=sent_at,
        subject=subject,
        meta={
            "to": _addresses(_header(message, "To")),
            "cc": _addresses(_header(message, "Cc")),
            "message_id": _header(message, "Message-ID"),
        },
    )


def from_text(text: str, sender: str | None = None, subject: str | None = None) -> Parsed:
    """A thread pasted as text rather than uploaded as a file.

    The common case of somebody copying a mail out of Outlook into the paste box,
    which still deserves the thread split and the signature stripping.
    """
    units = _units(text, sender=sender, sender_name=None, sent_at=None, subject=subject)
    if not units:
        raise ParseFailed("there is no text in that")
    return Parsed(units=units, pages=0, sender=sender, subject=subject)


def _units(
    body: str,
    *,
    sender: str | None,
    sender_name: str | None,
    sent_at: object | None,
    subject: str | None,
) -> list[Unit]:
    """One unit per paragraph, attributed to the reply it belongs to."""
    from mailparser_reply import EmailReplyParser

    trail = [subject.strip()] if subject and subject.strip() else []
    try:
        thread = EmailReplyParser(languages=["de", "en"]).read(body)
        replies = list(thread.replies)
    except Exception:  # noqa: BLE001 — never lose a mail to a parser quirk
        replies = []

    if not replies:
        meta = _meta(sender, sender_name, sent_at)
        return [
            Unit(text=block, path=list(trail), kind="message", meta=dict(meta))
            for block in _paragraphs(body)
        ]

    units: list[Unit] = []
    for n, reply in enumerate(replies):
        # The first reply is the message's own; later ones carry their author in
        # the separator header the client wrote above them.
        if n == 0:
            who, name, when = sender, sender_name, sent_at
        else:
            who, name = _one_address(reply.headers or "")
            when = _loose_date(reply.headers or "")
        meta = _meta(who, name, when)
        if n and (reply.headers or "").strip():
            meta["separator"] = " ".join((reply.headers or "").split())[:300]
        for block in _paragraphs(reply.body or ""):
            units.append(Unit(text=block, path=list(trail), kind="message", meta=dict(meta)))
    return units


def _meta(sender: str | None, name: str | None, when: object | None) -> dict:
    meta: dict = {}
    if sender:
        meta["sender"] = sender
        meta["sender_domain"] = domain_of(sender)
    if name:
        meta["sender_name"] = name
    if when is not None:
        meta["sent_at"] = when.isoformat() if hasattr(when, "isoformat") else str(when)
    return meta


def domain_of(address: str | None) -> str | None:
    """The domain half, with any punctuation a malformed header left attached.

    The strip is not decoration: a domain is what tells the customer's material
    from our own, and `nordwerk.de>` matches nothing that `nordwerk.de` matches.
    """
    if not address or "@" not in address:
        return None
    domain = (
        address.rsplit("@", 1)[1].strip().strip("<>\"'\u201c\u201d\u201e\u00ab\u00bb.,;:").lower()
    )
    return domain or None


def _paragraphs(text: str) -> list[str]:
    """Blank-line-separated blocks, with quote markers and wrapping cleaned up.

    `> ` prefixes are stripped: the text is already attributed to the reply it
    came from, and leaving the markers in means every pattern and every quote has
    to cope with them.
    """
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for raw in cleaned.split("\n\n"):
        lines = []
        for line in raw.split("\n"):
            stripped = line.strip()
            while stripped.startswith(">"):
                stripped = stripped[1:].lstrip()
            if stripped:
                lines.append(stripped)
        block = " ".join(lines).strip()
        if block:
            out.append(block)
    return out


def _header(message, name: str) -> str:
    try:
        value = message.get(name)
    except Exception:  # noqa: BLE001 — a malformed header must not lose the mail
        return ""
    return str(value).strip() if value else ""


def _one_address(raw: str) -> tuple[str | None, str | None]:
    """The address and display name out of a header or a separator line."""
    if not raw:
        return None, None

    name = None
    for pair in getaddresses([raw]) if "@" in raw else []:
        display, address = pair
        # Shape-checked, not just `"@" in address`. An unclosed quote in the
        # display name — `From: "A. Schulz <a@b.de>` — makes `getaddresses`
        # hand back the *whole header* as the address, `@` and all. Stored
        # unchecked that becomes a sender nobody can mail and a domain of
        # `b.de>`, which silently matches no filter (EVIDENCE-DESK §4.3).
        if address and _ADDRESS_SHAPE.match(address.strip()):
            return address.strip().lower(), (display.strip() or None)
        name = name or (display.strip() or None)

    match = _ANGLED.search(raw) or _BARE.search(raw)
    if match:
        address = match.group(1).strip().lower()
        before = raw[: match.start()]
        # "Von: Peter Meier <...>" — the name is whatever sits after the label.
        label = before.rsplit(":", 1)[-1].strip(" \t\"'<")
        return address, (label or None)
    return None, name


def _addresses(raw: str) -> list[str]:
    if not raw:
        return []
    return [a.strip().lower() for _, a in getaddresses([raw]) if a and "@" in a]


def _date(message):
    raw = _header(message, "Date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def _loose_date(raw: str):
    """A date out of a localised separator line, or nothing.

    Deliberately only the two unambiguous numeric forms. "Montag, 9. März 2026"
    would need a month-name table per language, and a *wrong* date on a passage
    is worse than no date — the raw separator is kept in `meta` either way, so
    nothing is lost by declining to guess.
    """
    from datetime import date

    match = _DMY.search(raw)
    if match:
        day, month, year = (int(g) for g in match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None
    match = _ISO.search(raw)
    if match:
        year, month, day = (int(g) for g in match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def _body(message) -> tuple[str, list[Image], list[Attachment]]:
    """The readable text, the images, and the files that came along.

    `text/plain` is preferred over `text/html` wherever a mail offers both,
    because the plain part is what the separator patterns and the signature
    patterns were written against.
    """
    plain: list[str] = []
    html: list[str] = []
    images: list[Image] = []
    attachments: list[Attachment] = []

    for part in message.walk() if message.is_multipart() else [message]:
        if part.get_content_maintype() == "multipart":
            continue
        content_type = (part.get_content_type() or "").lower()
        filename = part.get_filename() or ""
        disposition = (part.get("Content-Disposition") or "").lower()
        payload = _payload(part)
        if payload is None:
            continue

        if content_type.startswith("image/"):
            images.append(Image(data=payload, name=filename or "inline", media_type=content_type))
            continue

        is_attachment = "attachment" in disposition or bool(filename)
        if is_attachment:
            attachments.append(
                Attachment(
                    filename=filename or "attachment",
                    media_type=content_type or "application/octet-stream",
                    data=payload,
                )
            )
            continue

        text = _decode(part, payload)
        if content_type == "text/plain":
            plain.append(text)
        elif content_type == "text/html":
            html.append(text)

    body = "\n\n".join(p for p in plain if p.strip())
    if not body.strip() and html:
        body = "\n\n".join(_strip_tags(h) for h in html)
    return body, images, attachments


def _payload(part) -> bytes | None:
    try:
        payload = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001 — a broken part must not lose the others
        return None
    return payload if isinstance(payload, bytes) else None


def _decode(part, payload: bytes) -> str:
    from lcf.ingest.plain import decode

    charset = part.get_content_charset()
    if charset:
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            pass
    return decode(payload)


class _Text(HTMLParser):
    """Tags out, text and block boundaries in.

    Enough to read an HTML-only mail. Not a renderer: `script` and `style` are
    dropped, block elements become blank lines, and everything else becomes its
    text. A real HTML-to-markdown converter would be another dependency to serve
    the minority of mails that have no plain part.
    """

    _BLOCKS = {
        "p",
        "div",
        "br",
        "tr",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "table",
        "blockquote",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self._BLOCKS:
            self.parts.append("\n\n")
        elif tag == "td":
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in self._BLOCKS:
            self.parts.append("\n\n")

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        joined = "".join(self.parts)
        collapsed = re.sub(r"[ \t]+", " ", joined)
        return re.sub(r"\n{3,}", "\n\n", collapsed).strip()


def _strip_tags(html: str) -> str:
    """Text out of an HTML mail, with a fallback that cannot get stuck.

    `HTMLParser` does not raise on an unclosed attribute quote — it waits for a
    closing quote that never comes and swallows the rest of the document into
    one tag, returning nothing at all. One malformed `class="lead>` in a mail
    composed by a newsletter tool would therefore lose the whole message, which
    is worse than any amount of leftover markup. So an empty result is treated
    as a failed parse rather than as an empty mail.
    """
    parser = _Text()
    try:
        parser.feed(unescape(html) if "&" in html else html)
        parser.close()
        text = parser.text()
    except Exception:  # noqa: BLE001 — malformed HTML is the norm in mail
        text = ""
    if text:
        return text
    return _crude_strip(html)


def _crude_strip(html: str) -> str:
    """Every tag replaced by a space, scripts and styles removed first.

    Regex over HTML, knowingly: this runs only where the real parser gave up,
    and the job here is to lose no text rather than to understand structure.
    """
    without = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", html)
    without = re.sub(r"(?s)<[^>]*>", " ", without)
    # An unclosed tag at the end leaves a dangling `<...` the pass above kept.
    without = re.sub(r"(?s)<[^<]*$", " ", without)
    text = unescape(without)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()
