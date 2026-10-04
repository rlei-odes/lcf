"""Which language a source is in.

Detected per source, because a case routinely holds a German complaint, an
English measurement report from the same customer's UK plant, and an internal
note in whichever language the engineer types. One language per case would be
wrong and one per installation wronger.

`py3langid` is the choice for the same reason the built-in parser is: its model
is a file inside the package, so there is no download on first use and nothing
to pre-fetch on an installation with no egress.
"""

from functools import lru_cache

# What the ranker has a stopword list and a stemmer for. A source detected as
# something else is indexed with the English stopword list and no stemmer, which
# is a worse index rather than a broken one.
SUPPORTED = {
    "en": "english",
    "de": "german",
    "nl": "dutch",
    "fr": "french",
    "es": "spanish",
    "pt": "portuguese",
    "it": "italian",
    "ru": "russian",
    "sv": "swedish",
    "no": "norwegian",
    "da": "danish",
    "tr": "turkish",
    "zh": "chinese",
    "ko": "korean",
}

# Below this many characters, detection is a coin toss dressed as a measurement.
# A two-line note gets the fallback and the person can override it.
MIN_CHARS = 60

FALLBACK = "en"


@lru_cache(maxsize=1)
def _identifier():
    from py3langid.langid import MODEL_FILE, LanguageIdentifier

    # `from_model_file` resolves the name against the package directory, which is
    # where the bundled model lives; `from_modelpath` would resolve it against
    # the working directory and fail wherever the server happens to be started.
    #
    # `norm_probs=True` turns the raw log-probability into a number that can be
    # shown to a person as a confidence. It costs a little and is the only reason
    # the figure is worth printing at all.
    return LanguageIdentifier.from_model_file(MODEL_FILE, norm_probs=True)


def detect(text: str) -> tuple[str, float]:
    """The language of this text, and how sure that is.

    Returns the fallback with a confidence of 0 rather than raising, because a
    source that cannot be classified still has to be indexed — and a wrong
    stemmer costs a rank position, not a missing answer.
    """
    body = " ".join((text or "").split())
    if len(body) < MIN_CHARS:
        return FALLBACK, 0.0
    try:
        code, confidence = _identifier().classify(body[:20000])
    except Exception:  # noqa: BLE001 — never lose a source to the detector
        return FALLBACK, 0.0
    return str(code), round(float(confidence), 3)


def stopword_set(code: str) -> str:
    """The name `bm25s` knows this language's bundled stopword list by."""
    return SUPPORTED.get((code or "").lower(), "english")


def stemmer_name(code: str) -> str | None:
    """The Snowball stemmer for this language, where one exists.

    Chinese and Korean have stopword lists in `bm25s` but no Snowball stemmer,
    so they index unstemmed — which is correct for them anyway.
    """
    name = SUPPORTED.get((code or "").lower())
    return name if name not in (None, "chinese", "korean") else None
