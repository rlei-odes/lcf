"""Holding model output to the words it was given.

The same mechanism serves two jobs that look unrelated. A judged check may only
call a point established if it can quote the text that establishes it; intake may
only file a fragment under a section if the fragment is really in what the author
pasted. Both are the same question — *are these the author's words, or the
model's?* — and both answers have to be verifiable without a person re-reading
the source.

So it lives in one place, and anything that asks a model for a quotation checks
it here before acting on it.
"""


def quoted_from(quote: str, source: str) -> bool:
    """Is this quote actually in the source?

    Compared on collapsed whitespace and casing, because a model reflows text it
    quotes. A long quote is checked by its opening words: models truncate and
    elide, and we are guarding against fabrication, not sloppy transcription.
    """
    haystack = " ".join(source.split()).casefold()
    needle = " ".join(quote.split()).casefold()
    if not needle:
        return False
    if needle in haystack:
        return True
    words = needle.split()
    return len(words) > 8 and " ".join(words[:8]) in haystack
