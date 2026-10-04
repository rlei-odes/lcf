"""Finding the chunks worth reading, and the matches that need no reading.

Three things live here, in increasing cost:

1. **`matches`** — run a pattern over a chunk. No model, and the result is a
   character offset rather than a claim, so there is nothing to verify.
2. **`keyword_hits`** — which chunks mention a term, stemmed for the chunk's own
   language, so `Toleranz` finds `Toleranzen`.
3. **`rank`** — BM25 over the chunks, to decide which ones a model is worth
   spending on.

Ranking uses `bm25s`, which needs only `numpy` and carries bundled stopword
lists for fourteen languages plus a hook for a Snowball stemmer. That is the
language-aware behaviour the desk needs, and none of it is the BM25 formula —
which is the half that would have been easy to write.
"""

from dataclasses import dataclass
from functools import lru_cache

import regex

from lcf.ingest.language import stemmer_name, stopword_set

# A window of context around a match, so a person reading a card can tell
# whether the hit means what the question asked.
WINDOW = 160


class PatternTooSlow(Exception):
    """The pattern did not finish inside its timeout.

    Its own exception because the honest report is *this pattern is too slow on
    this material* against the question, not a missing candidate and not a
    crashed run. A model-written regex over megabytes of someone else's text is
    where catastrophic backtracking actually happens.
    """


class PatternInvalid(Exception):
    """The pattern does not compile, or would match everywhere."""


@dataclass
class Match:
    """One pattern hit, located."""

    value: str
    quote: str
    char_from: int
    char_to: int


@dataclass
class Hit:
    """One chunk a term was found in."""

    index: int
    terms: list[str]
    count: int


@dataclass
class Ranked:
    index: int
    score: float
    # 1-based, as a person counts: "ranked 2 of 48".
    rank: int
    # True when the ranking could not tell these chunks apart — no query term
    # survived tokenisation into any of them. The order is then document order
    # and the caller has to say so rather than present it as a ranking.
    arbitrary: bool = False


def compile_pattern(pattern: str):
    """Compile, refusing the two shapes that cannot usefully be run.

    An empty-matching pattern is the dangerous one: it hits at every position in
    every chunk, so it produces a candidate per character and looks like the
    engine has lost its mind rather than like a bad pattern.
    """
    if not (pattern or "").strip():
        raise PatternInvalid("there is no pattern there")
    try:
        compiled = regex.compile(pattern)
    except regex.error as exc:
        raise PatternInvalid(f"that is not a valid pattern: {exc}") from exc
    if compiled.search("") is not None:
        raise PatternInvalid("that pattern matches an empty string, so it would match everywhere")
    return compiled


def matches(pattern: str, text: str, timeout: float = 2.0, limit: int = 200) -> list[Match]:
    """Every hit of `pattern` in `text`, with its offsets and a readable window.

    `regex` rather than `re` for one reason: it takes a `timeout`, and `re` does
    not. Without it a pattern with nested quantifiers hangs the worker, and the
    person who pasted two example numbers has no way to know why.

    Where the pattern has exactly one capturing group, that group is the value
    and the whole match is the context — which is how `Charge:\\s*(\\S+)` is
    meant to be read. More than one group, and the whole match is the value,
    because guessing which group was meant would be worse than taking all of it.
    """
    compiled = compile_pattern(pattern)
    group = 1 if compiled.groups == 1 else 0

    try:
        found = list(compiled.finditer(text, timeout=timeout))
    except TimeoutError as exc:
        raise PatternTooSlow(f"`{pattern}` did not finish within {timeout:g}s") from exc
    except regex.error as exc:  # a pattern that only fails on this input
        raise PatternInvalid(str(exc)) from exc

    out: list[Match] = []
    for hit in found[:limit]:
        value = (hit.group(group) or "").strip()
        if not value:
            continue
        out.append(
            Match(
                value=value,
                quote=window(text, hit.start(), hit.end()),
                char_from=hit.start(group),
                char_to=hit.end(group),
            )
        )
    return out


def window(text: str, start: int, end: int, size: int = WINDOW) -> str:
    """The match plus context, snapped outward to word boundaries.

    Snapped rather than cut, because a quote beginning mid-word reads as a bug
    in the extraction to whoever is checking the finding.
    """
    left = max(0, start - size)
    right = min(len(text), end + size)
    if left:
        space = text.find(" ", left, start)
        left = space + 1 if space != -1 else left
    if right < len(text):
        space = text.rfind(" ", end, right)
        right = space if space != -1 else right
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(text) else ""
    return f"{prefix}{' '.join(text[left:right].split())}{suffix}"


def fullmatch_all(pattern: str, examples: list[str]) -> list[str]:
    """Which of these examples the pattern does **not** match end to end.

    The test that decides whether a proposed pattern may be saved at all. A
    pattern that does not match what it was built from is not a candidate for
    anything, and `fullmatch` rather than `search` is the strict reading: a
    pattern matching only the `88213` of `NW-CL-88213` would pass a search and
    then find the wrong thing in every document.
    """
    compiled = compile_pattern(pattern)
    failures: list[str] = []
    for example in examples:
        value = (example or "").strip()
        if not value:
            continue
        try:
            if compiled.fullmatch(value, timeout=1.0) is None:
                failures.append(value)
        except TimeoutError as exc:
            raise PatternTooSlow(f"`{pattern}` did not finish on {value!r}") from exc
    return failures


def keyword_hits(keywords: list[str], texts: list[str], language: str = "en") -> list[Hit]:
    """Which chunks mention any of these terms.

    Word-bounded and stemmed. Bounded because a keyword is a word, not a
    substring — a list that matched `cal` inside `calibration` would narrow
    nothing. Stemmed because German compounds and plurals are the normal case,
    and asking a person to tick `Toleranz`, `Toleranzen` and `Toleranzgrenze`
    separately is asking them to do the stemmer's job.
    """
    terms = [k.strip() for k in keywords if k and k.strip()]
    if not terms:
        return []

    stems = {_stem_one(term, language): term for term in terms}
    out: list[Hit] = []
    for index, text in enumerate(texts):
        tokens = _stems_of(text, language)
        found: dict[str, int] = {}
        for token in tokens:
            original = stems.get(token)
            if original:
                found[original] = found.get(original, 0) + 1
        # A multi-word term is matched as a phrase on the raw text instead, since
        # tokenising both sides would lose the adjacency that makes it a phrase.
        for term in terms:
            if " " in term and term.casefold() in text.casefold():
                found[term] = found.get(term, 0) + text.casefold().count(term.casefold())
        if found:
            out.append(Hit(index=index, terms=sorted(found), count=sum(found.values())))
    return out


def rank(query: str, texts: list[str], language: str = "en", top_k: int = 6) -> list[Ranked]:
    """The chunks most worth asking a model about, best first.

    Three outcomes, and the third is the one that matters:

    1. Some chunks score above zero — those, best first.
    2. None do, but words are shared — `_fallback` counts the overlap.
    3. Nothing is shared at all. Then the chunks are returned in **document
       order**, marked `arbitrary`, because this is not a failure: it is the
       ordinary case of an English question asked of German material, which is
       exactly what the `ask` tier exists for. Reading the first few beats
       reading none, and the caller is obliged to say the order means nothing.
    """
    if not texts or top_k <= 0:
        return []
    query_text = " ".join((query or "").split())
    if not query_text:
        return []

    import bm25s

    stemmer = _stemmer(language)
    stopwords = stopword_set(language)

    out: list[Ranked] = []
    try:
        corpus = bm25s.tokenize(texts, stopwords=stopwords, stemmer=stemmer, show_progress=False)
        asked = bm25s.tokenize(
            [query_text], stopwords=stopwords, stemmer=stemmer, show_progress=False
        )
        retriever = bm25s.BM25()
        retriever.index(corpus, show_progress=False)
        indices, scores = retriever.retrieve(asked, k=min(top_k, len(texts)), show_progress=False)
    except Exception:  # noqa: BLE001 — an empty vocabulary, a corpus of stopwords
        indices, scores = [[]], [[]]

    for position, (index, score) in enumerate(zip(indices[0], scores[0], strict=False), start=1):
        value = float(score)
        if value <= 0:
            continue
        out.append(Ranked(index=int(index), score=round(value, 3), rank=position))

    if out:
        return out
    return _fallback(query_text, texts, top_k) or _in_order(texts, top_k)


def _in_order(texts: list[str], top_k: int) -> list[Ranked]:
    """Document order, when nothing can be ranked. Marked as the guess it is."""
    return [
        Ranked(index=index, score=0.0, rank=index + 1, arbitrary=True)
        for index in range(min(top_k, len(texts)))
    ]


def _fallback(query: str, texts: list[str], top_k: int) -> list[Ranked]:
    """Overlap counting, for when tokenisation leaves nothing to index.

    Reached by a query made entirely of stopwords and by a corpus of pure
    numbers — a measurement table indexes to almost nothing. Returning the
    chunks with the most shared words beats returning none, and it is still
    deterministic.
    """
    wanted = {w.casefold() for w in regex.findall(r"\w+", query) if len(w) > 2}
    if not wanted:
        return []
    scored: list[tuple[int, int]] = []
    for index, text in enumerate(texts):
        words = {w.casefold() for w in regex.findall(r"\w+", text)}
        overlap = len(wanted & words)
        if overlap:
            scored.append((index, overlap))
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return [
        Ranked(index=index, score=float(count), rank=position)
        for position, (index, count) in enumerate(scored[:top_k], start=1)
    ]


@lru_cache(maxsize=16)
def _stemmer(language: str):
    """The Snowball stemmer for a language, or None.

    Cached because building one is not free and a run tokenises every chunk of
    every source with it.
    """
    name = stemmer_name(language)
    if not name:
        return None
    try:
        import Stemmer

        return Stemmer.Stemmer(name)
    except Exception:  # noqa: BLE001 — an algorithm this build lacks
        return None


def _stems_of(text: str, language: str) -> list[str]:
    words = [w.casefold() for w in regex.findall(r"[\w\-/]+", text or "")]
    stemmer = _stemmer(language)
    if stemmer is None:
        return words
    return list(stemmer.stemWords(words))


def _stem_one(term: str, language: str) -> str:
    stems = _stems_of(term, language)
    return stems[0] if stems else term.casefold()
