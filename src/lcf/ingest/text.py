"""Making arbitrary text safe to store, to match and to show.

Material arrives from scanners, Windows mail clients, PDFs produced by a decade
of different tools, and whatever was on somebody's clipboard. Three kinds of
character in it break three different things, and all three break quietly:

- **Unstorable.** A NUL is valid UTF-8 and a valid Python string, and Postgres
  refuses it outright — as it refuses a lone surrogate, which a latin-1 fallback
  or a `\\ud800` escape in model output can both produce. Either one aborts the
  parse of a whole file over one character in it, and the person is told the
  file could not be read with no indication why.
- **Invisible.** A soft hyphen, a zero-width space or a no-break space inside
  `NW-CL-88213` means it does not match `\\bNW-CL-\\d{5}\\b`. PDF extraction
  emits all three routinely. The pattern tier is the one that is supposed to be
  exact and free, so this is the failure that looks most like the tool simply
  not working.
- **Unprintable.** Terminal escapes, bells and vertical tabs reach the model's
  prompt, the passage view and the exported findings, where they are noise at
  best and a rendering bug at worst.

Plus one normalisation. Text composed on macOS, and text out of some PDF
producers, is NFD: `Ü` is two code points that look like one. It displays
identically, deduplicates as a *different* value, and fails a pattern written
against the composed form. Composing to NFC is what makes one identifier one
card.

NFKC is deliberately not used. It rewrites `²` to `2`, `½` to `1/2` and `Nr.`
ligatures to letters — and a measurement is not something to silently rewrite
on the way in.

All of this runs **before** `Parsed.text` is assembled, so chunk offsets are
computed against the cleaned text and the
`source_text[char_from:char_to] == chunk.text` invariant is untouched.
"""

import unicodedata

import regex

# Spaces that are not the space character. Folded to an ordinary space so that
# `\s`, `str.split` and a pattern somebody wrote with the space bar all agree
# about where the words are. The no-break space is the one that matters: German
# typesetting puts it between a number and its unit, which is exactly where a
# measurement question is looking.
_TO_SPACE = (
    "\u00a0"  # no-break space
    "\u1680"  # ogham space mark
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"  # en/em quad … hair
    "\u202f"  # narrow no-break space
    "\u205f"  # medium mathematical space
    "\u3000"  # ideographic space
)

# Breaks that are not the newline character. A form feed is a page break, which
# is a thing a newline already says and nothing downstream knows how to read.
_TO_NEWLINE = (
    "\x0c"  # form feed
    "\u0085"  # next line (C1)
    "\u2028"  # line separator
    "\u2029"  # paragraph separator
)

_TABLE = {ord(c): " " for c in _TO_SPACE} | {ord(c): "\n" for c in _TO_NEWLINE}

# Control, format, surrogate and private-use characters, less the two that are
# load-bearing: tab separates columns in a TSV and newline separates everything.
#
# One class covers all three hazards at once, which is why it is written this
# way rather than as a list: NUL and the C0 range are `Cc`, unpaired surrogates
# are `Cs`, and `Cf` is where the whole invisible family lives — soft hyphen,
# zero-width space, word joiner, byte-order mark and the bidirectional
# overrides that can make rendered text read in an order the stored text does
# not have.
#
# `Cf` also holds the zero-width joiners that Indic scripts and emoji sequences
# are built from. Removing them is the right trade here and would not be in a
# general-purpose text pipeline: this reads quality complaints in European
# languages, where every `Cf` character present is an artefact of the producing
# tool rather than something an author typed.
#
# Unassigned code points (`Cn`) are left alone — a character this Python does
# not know yet is not evidence of anything being wrong.
_STRIP = regex.compile(r"(?![\t\n])[\p{Cc}\p{Cf}\p{Cs}\p{Co}]")

_WHITESPACE = regex.compile(r"\s+")


def clean(text: str) -> str:
    """The text as it should be stored, matched against and shown.

    Idempotent, and never raises: anything it cannot make sense of it removes.
    """
    if not text:
        return ""
    # Line endings first, so the stripping pass never sees a bare \r and the
    # NFC pass never has to consider one.
    out = text.replace("\r\n", "\n").replace("\r", "\n")
    out = out.translate(_TABLE)
    out = _STRIP.sub("", out)
    # Last, and only once the surrogates are gone: normalize() is specified on
    # well-formed text, and composing is the step that makes two spellings of
    # one umlaut into one value.
    return unicodedata.normalize("NFC", out)


def clean_line(text: str) -> str:
    """`clean`, for something that has to fit on one line.

    A filename, a mail subject, a sender, a candidate value, a heading. A
    newline in any of them is a layout bug rather than content — mail headers
    carry folded ones, and a pasted filename can carry anything at all.
    """
    return _WHITESPACE.sub(" ", clean(text)).strip()


def clean_data(value: object) -> object:
    """`clean` over every string in a decoded JSON structure.

    Model output is the other door untrusted text comes through, and it reaches
    the same database columns. A `\\ud800` escape is well-formed JSON, decodes
    to a lone surrogate, and fails on the way *into Postgres* rather than on the
    way out of the model — which puts the error a long way from its cause.

    `clean` and not `clean_line`, because this runs over every call's output and
    one of them drafts markdown prose. Collapsing its newlines would turn a
    guard against bad characters into a corruption of good ones.
    """
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, dict):
        return {clean_line(str(k)): clean_data(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_data(v) for v in value]
    return value
