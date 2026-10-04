"""Images out of the pile, reduced to the ones worth looking at.

Extraction is the easy part — `pypdf` and `python-docx` both hand them over.
What makes the tray usable is everything after:

**Deduplication by content hash.** A letterhead on twenty pages is one asset,
not twenty. Without this the first real PDF fills the tray with the customer's
logo and the defect photograph is on screen three.

**Discarding furniture.** Rules, bullets, icons and logo fragments are images in
the file's eyes. Judged on pixel area rather than byte size, because a 2 KB PNG
can be a perfectly legible crop of a measurement while a 40 KB one is a
letterhead. The count is reported rather than hidden, so a person who expected
six photographs and sees two knows why.

**A thumbnail.** The card shows a 320 px WebP and the original is kept for the
model call and the download.
"""

import hashlib
from dataclasses import dataclass, field

# Below this, an image is furniture. 80×80 is small enough to keep a thumbnail
# of a dimension callout and large enough to drop every bullet and rule.
MIN_PIXELS = 80 * 80
# A 1×400 strip has plenty of pixels and is a horizontal rule. Anything this
# lopsided is a divider or a sidebar, never a photograph.
MAX_ASPECT = 12
THUMBNAIL = 320


@dataclass
class Prepared:
    """One image, ready to be stored."""

    sha256: str
    data: bytes
    media_type: str
    thumbnail: bytes
    width: int
    height: int
    page: int | None = None
    name: str = ""

    @property
    def size_bytes(self) -> int:
        return len(self.data)


@dataclass
class Harvest:
    """What came out of one source's images."""

    images: list[Prepared] = field(default_factory=list)
    duplicates: int = 0
    too_small: int = 0
    unreadable: int = 0

    @property
    def dropped(self) -> int:
        return self.duplicates + self.too_small + self.unreadable

    def note(self) -> str:
        """One line for the gather panel, or nothing if there is nothing to say."""
        parts = []
        if self.duplicates:
            parts.append(f"{self.duplicates} repeated")
        if self.too_small:
            parts.append(f"{self.too_small} too small to be evidence")
        if self.unreadable:
            parts.append(f"{self.unreadable} unreadable")
        return ", ".join(parts)


def harvest(images, seen: set[str] | None = None) -> Harvest:
    """Prepare a source's images, dropping duplicates and furniture.

    `seen` carries hashes already held by the case, so the logo in the second
    file is recognised as the logo from the first. Passed in rather than kept
    here because this module stays pure — the caller owns what the case knows.
    """
    known = set(seen or ())
    out = Harvest()

    for found in images:
        data = getattr(found, "data", None)
        if not data:
            out.unreadable += 1
            continue

        digest = hashlib.sha256(data).hexdigest()
        if digest in known:
            out.duplicates += 1
            continue

        prepared = prepare(
            data,
            page=getattr(found, "page", None),
            name=getattr(found, "name", "") or "",
            media_type=getattr(found, "media_type", "") or "",
            digest=digest,
        )
        if prepared is None:
            out.too_small += 1
            known.add(digest)  # a logo dropped once need not be re-examined
            continue

        known.add(digest)
        out.images.append(prepared)

    return out


def prepare(
    data: bytes,
    page: int | None = None,
    name: str = "",
    media_type: str = "",
    digest: str | None = None,
) -> Prepared | None:
    """Normalise one image and build its thumbnail, or None if it is furniture."""
    import io

    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as opened:
            opened.load()
            width, height = opened.size
            if not _worth_keeping(width, height):
                return None
            # EXIF orientation applied here rather than relied on downstream: a
            # phone photograph of a defect arrives rotated, and a sideways
            # thumbnail is read as a broken thumbnail.
            upright = ImageOps.exif_transpose(opened) or opened
            thumbnail = _thumbnail(upright)
            resolved = media_type or _media_type(opened.format)
            width, height = upright.size
    except (UnidentifiedImageError, OSError, ValueError):
        return None

    return Prepared(
        sha256=digest or hashlib.sha256(data).hexdigest(),
        data=data,
        media_type=resolved,
        thumbnail=thumbnail,
        width=width,
        height=height,
        page=page,
        name=name,
    )


def _worth_keeping(width: int, height: int) -> bool:
    if width * height < MIN_PIXELS:
        return False
    short, long = min(width, height), max(width, height)
    return short > 0 and long / short <= MAX_ASPECT


def _thumbnail(image) -> bytes:
    import io

    copy = image.convert("RGB")
    copy.thumbnail((THUMBNAIL, THUMBNAIL))
    buffer = io.BytesIO()
    # WebP for the card: a quarter of the bytes of an equivalent PNG, and every
    # browser this runs in has supported it for years.
    copy.save(buffer, format="WEBP", quality=82, method=4)
    return buffer.getvalue()


def _media_type(fmt: str | None) -> str:
    return {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
        "GIF": "image/gif",
        "TIFF": "image/tiff",
        "BMP": "image/bmp",
    }.get((fmt or "").upper(), "image/png")


def as_data_url(data: bytes, media_type: str = "image/png") -> str:
    """An image as a data URL, which is how a vision call carries one.

    OpenAI-compatible endpoints take `image_url` with either a URL or an inline
    data URL. Inline is the only option here: the LLM host has no route to this
    application's storage, and presigning for it would mean the model host
    reaching back into the network (ARCHITECTURE §1).
    """
    import base64

    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def for_call(data: bytes, media_type: str = "image/png", longest: int = 1024) -> tuple[bytes, str]:
    """Shrink an image to something worth sending to a vision model.

    A 12-megapixel photograph is tens of thousands of tokens and no more
    legible to the model than a 1024 px one. Resizing before the call is what
    keeps the per-call image budget meaningful.
    """
    import io

    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as opened:
            opened.load()
            upright = ImageOps.exif_transpose(opened) or opened
            if max(upright.size) <= longest and media_type in ("image/jpeg", "image/png"):
                return data, media_type
            copy = upright.convert("RGB")
            copy.thumbnail((longest, longest))
            buffer = io.BytesIO()
            copy.save(buffer, format="JPEG", quality=85)
            return buffer.getvalue(), "image/jpeg"
    except (UnidentifiedImageError, OSError, ValueError):
        return data, media_type or "image/png"
