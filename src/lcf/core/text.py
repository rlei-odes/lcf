"""Wording shared by every surface.

Imports nothing. The check engine, the CLI, the services and the templates all
count things for a reader, and they have to say it the same way, so the rule
lives once here rather than as "(s)" in thirty format strings.
"""


def count(n: int, singular: str, plural: str | None = None) -> str:
    """"1 check", "20 checks".

    "check(s)" is not something anyone says out loud; it reads as a form field
    rather than a sentence. English is regular enough that one helper covers
    almost every case, and `plural` is there for the handful it does not
    (`count(n, "entry", "entries")`).
    """
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def verb(n: int, singular: str = "is", plural: str = "are") -> str:
    """The verb to go with `count`, for sentences that need one to agree."""
    return singular if n == 1 else plural
