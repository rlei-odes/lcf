"""The LLM provider.

One structured-output entry point, `complete_json`, used by every typed call.

The `response_format` form below is the one verified against the deployment
(ARCHITECTURE §5.2). `guided_json` — the form most vLLM documentation shows — is
*silently ignored* by this build: it returns a 200 with prose. That is why the
result is validated locally even though decoding is supposed to be constrained:
an endpoint that drops a constraint does not tell you it dropped it.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from loguru import logger
from openai import AsyncOpenAI

from lcf.core.config import settings
from lcf.ingest.text import clean_data

PROMPTS = Path(__file__).parent / "prompts"


class LLMUnavailable(Exception):
    """The endpoint could not be reached or refused the request."""


class LLMMalformed(Exception):
    """The endpoint answered, but not with content matching the schema."""


class _Padding(Exception):
    """Internal: generation degenerated into whitespace and will not terminate."""

    def __init__(self, message: str, partial: str = ""):
        super().__init__(message)
        self.partial = partial


# Real formatted JSON never has this much consecutive whitespace. Anything beyond
# it is the padding pathology, not indentation.
_WHITESPACE_RUN = 80


def _closed_at_root(partial: str, schema: dict[str, Any], optional: tuple[str, ...]) -> str | None:
    """A padded generation finished off, where finishing it off loses nothing.

    The padding starts where the grammar's next obligation is a property name,
    and the commonest such place is the boundary between two top-level fields:
    the object is written, the last field the model had anything to say about is
    closed, and the newlines stand where the next field name would go. Supplying
    the `}` costs nothing and saves the whole second call.

    That is only true at *that* boundary, which is why every other one is
    refused. If an array or a nested object is still open, the padding began in
    the middle of a list the model was still adding to, and closing it would drop
    entries nobody ever saw — a draft silently short of two gaps is worse than a
    retry, because gap detection is the product. So the sole open structure has
    to be the root, and `optional` has to cover every required field that did not
    arrive: the caller names what it can do without, and anything else missing
    means this was a truncation rather than a tail.
    """
    depth: list[str] = []
    in_string = escaped = False
    for char in partial:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth.append("}" if char == "{" else "]")
        elif char in "}]" and depth:
            depth.pop()

    if in_string or depth != ["}"]:
        return None

    text = partial.rstrip().rstrip(",") + "}"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    wanted = [k for k in schema.get("required", []) if k not in optional]
    return text if all(k in parsed for k in wanted) else None


@dataclass
class Completion:
    data: dict[str, Any]
    raw: str
    prompt: str
    model: str
    duration_ms: int
    attempts: int
    # Counted from the stream rather than asked for: the response is abandoned
    # the moment its JSON object closes, so the usage chunk a server sends at
    # the end never arrives. Chunks are one token each on every server this runs
    # against, which is why the figure is reported as approximate.
    chunks: int = 0
    chars: int = 0
    # The generation padded at a field boundary and was closed off rather than
    # retried. Worth reporting: it saved a call, and it means a field named in
    # the caller's `optional` may be absent (`_closed_at_root`).
    salvaged: bool = False

    @property
    def per_second(self) -> float:
        return round(self.chunks / (self.duration_ms / 1000), 1) if self.duration_ms else 0.0


@dataclass
class CallRecord:
    """What one model call cost, for whoever is keeping the log."""

    purpose: str
    model: str
    ok: bool
    duration_ms: int
    attempts: int
    chunks: int
    chars: int
    detail: str = ""
    salvaged: bool = False
    # What was sent and what came back, for whoever has to answer "why did it
    # write that" (DESIGN §5.8). Handed over on every call; whether any of it is
    # kept is the log's decision, not this module's.
    prompt: str = ""
    response: str = ""

    @property
    def per_second(self) -> float:
        return round(self.chunks / (self.duration_ms / 1000), 1) if self.duration_ms else 0.0


# Installed by `lcf.services.events`. A plain hook, because `engine/` calls
# straight into here and must never reach a database: the provider hands over a
# record and stays ignorant of what happens to it (ARCHITECTURE §3).
_observer: Callable[[CallRecord], Awaitable[None]] | None = None


def observe(fn: Callable[[CallRecord], Awaitable[None]] | None) -> None:
    global _observer
    _observer = fn


async def _emit(record: CallRecord) -> None:
    """Never let bookkeeping break the work it is describing."""
    if _observer is None:
        return
    try:
        await _observer(record)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not record llm call: {}", exc)


@lru_cache
def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8").strip()


@lru_cache
def client() -> AsyncOpenAI:
    s = settings()
    return AsyncOpenAI(
        base_url=s.llm_base_url, api_key=s.llm_api_key or "none", timeout=s.llm_timeout_s
    )


async def complete_json(
    system: str,
    user: str | list[dict[str, Any]],
    schema: dict[str, Any],
    schema_name: str = "response",
    purpose: str = "",
    optional: tuple[str, ...] = (),
) -> Completion:
    """Ask for one JSON object matching `schema`, and insist on getting one.

    `purpose` names the call in the event log. It defaults to the schema name,
    which is already a decent description of what was asked for.

    `user` is normally a string. It may be the OpenAI content-part list, which is
    how an image-bearing call is expressed; `complete_json_with_images` builds
    that form and everything else here is shared.

    `optional` names the required fields this call can do without, which is what
    lets a generation that padded at a field boundary be closed off instead of
    retried (`_closed_at_root`). It is passed per call rather than read off the
    schema because the two say different things to different readers: the schema
    tells the *server* what to constrain the tokens to, and moving a field out of
    its `required` list changes where the model stalls — measured, and for the
    worse. This tells *us* what we are willing to accept short.
    """
    import time

    s = settings()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    started = time.monotonic()
    last_error = ""
    chunks = chars = 0
    label = purpose or schema_name

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    salvaged = False
    for attempt in (1, 2):
        raw = ""
        problem: str | None = None
        try:
            raw, streamed = await _stream_one_object(messages, schema, schema_name)
            chunks += streamed
            chars += len(raw)
        except _Padding as exc:
            raw = exc.partial
            chars += len(raw)
            closed = _closed_at_root(raw, schema, optional)
            if closed is None:
                problem = f"degenerate output: {exc}"
            else:
                raw = closed
                salvaged = True
                logger.info("attempt {} padded at a field boundary; closed it", attempt)
        except Exception as exc:  # network, timeout, refusal
            await _emit(
                CallRecord(
                    label,
                    s.llm_model,
                    False,
                    elapsed(),
                    attempt,
                    chunks,
                    chars,
                    f"{type(exc).__name__}: {exc}",
                    prompt=f"{system}\n\n---\n\n{_readable(user)}",
                )
            )
            raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc

        try:
            # `clean_data` because a model's reply is untrusted text like any
            # other: `\ud800` is well-formed JSON, decodes to a lone surrogate,
            # and fails on the way into Postgres rather than here, which puts the
            # error a long way from its cause (`ingest/text.py`).
            data = clean_data(json.loads(raw)) if problem is None else None
        except json.JSONDecodeError as exc:
            problem = f"not JSON: {exc}"

        if problem is not None:
            last_error = problem
            logger.warning("attempt {} unusable ({}): {!r}", attempt, problem, raw[:160])
            if attempt == 1:
                # Feed back a *collapsed* copy: the padding is the problem, and
                # resending thousands of newlines would spend the budget twice.
                messages.append({"role": "assistant", "content": " ".join(raw.split())[:2000]})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "That reply was unusable. Return one complete JSON object "
                            "matching this schema, compact, with no blank lines and no "
                            f"text around it:\n{json.dumps(schema)}"
                        ),
                    }
                )
                continue
            await _emit(
                CallRecord(
                    label,
                    s.llm_model,
                    False,
                    elapsed(),
                    attempt,
                    chunks,
                    chars,
                    last_error,
                    prompt=f"{system}\n\n---\n\n{_readable(user)}",
                    # The unusable output itself. On a padded generation this is
                    # the only way to see where it stalled, which is the whole
                    # question when one of these turns up in the log.
                    response=raw,
                )
            )
            raise LLMMalformed(last_error)

        await _emit(
            CallRecord(
                label,
                s.llm_model,
                True,
                elapsed(),
                attempt,
                chunks,
                chars,
                salvaged=salvaged,
                prompt=f"{system}\n\n---\n\n{_readable(user)}",
                response=raw,
            )
        )
        return Completion(
            data=data,
            raw=raw,
            prompt=f"{system}\n\n---\n\n{_readable(user)}",
            model=s.llm_model,
            duration_ms=elapsed(),
            attempts=attempt,
            chunks=chunks,
            chars=chars,
            salvaged=salvaged,
        )

    raise LLMMalformed(last_error)


def _readable(user: str | list[dict[str, Any]]) -> str:
    """The user message as something worth storing on the call record.

    A content-part list holds base64 image data. Storing that would put
    megabytes of it on the row and make the prompt view unopenable, so an image
    is recorded as the fact that it was sent and its size.
    """
    if isinstance(user, str):
        return user
    parts: list[str] = []
    for part in user:
        if part.get("type") == "text":
            parts.append(str(part.get("text") or ""))
        else:
            url = str((part.get("image_url") or {}).get("url") or "")
            parts.append(f"[image attached, {len(url)} characters of data URL]")
    return "\n\n".join(parts)


async def complete_json_with_images(
    system: str,
    user: str,
    images: list[tuple[bytes, str]],
    schema: dict[str, Any],
    schema_name: str = "response",
    purpose: str = "",
) -> Completion:
    """`complete_json`, with images attached to the user message.

    The only call that sends image content. Everything that makes the text path
    survive this deployment — the streaming guards, the whitespace abort, the
    single retry on unusable output — is unchanged and shared; the difference is
    entirely in how the user message is built.

    Images travel as inline data URLs rather than as links. The LLM host has no
    route into this application's storage and must not be given one
    (ARCHITECTURE §1), so a presigned URL would either fail or be a hole in the
    thing the whole design is built on.
    """
    from lcf.ingest.images import as_data_url

    parts: list[dict[str, Any]] = [{"type": "text", "text": user}]
    for data, media_type in images:
        parts.append({"type": "image_url", "image_url": {"url": as_data_url(data, media_type)}})
    return await complete_json(system, parts, schema, schema_name=schema_name, purpose=purpose)


async def _stream_one_object(
    messages: list[dict[str, Any]], schema: dict[str, Any], schema_name: str
) -> tuple[str, int]:
    """Stream the response and stop the moment the JSON object closes.

    A JSON grammar allows unlimited whitespace between tokens, and this model uses
    it: generations were observed emitting thousands of newlines mid-object and
    running to the token ceiling — minutes of waiting for an object that was
    otherwise finished in seconds.

    Two guards, both needing the stream rather than a finished response:

    - Stop as soon as brace depth returns to zero: the object is complete, and
      anything after it is padding. Quotes and escapes are tracked too, because a
      brace inside a string is not structure.
    - Abort on a long run of whitespace outside a string. That is the pathology
      starting, and the object will never close. Aborting turns a minute of dead
      generation into an immediate retry, which is what recovers it in practice.
    """
    s = settings()
    stream = await client().chat.completions.create(
        model=s.llm_model,
        temperature=s.llm_temperature,
        max_tokens=s.llm_max_tokens,
        messages=messages,
        response_format={
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema, "strict": True},
        },
        stream=True,
    )

    out: list[str] = []
    depth = 0
    started = False
    in_string = False
    escaped = False
    run = 0
    # One chunk is one token on every server this runs against. Counted here
    # because the stream is abandoned as soon as the object closes, so the usage
    # totals a server reports at the end never arrive.
    chunks = 0

    try:
        async for chunk in stream:
            if not chunk.choices:
                continue
            chunks += 1
            piece = chunk.choices[0].delta.content or ""
            for char in piece:
                out.append(char)
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                    continue

                if char.isspace():
                    run += 1
                    if run > _WHITESPACE_RUN:
                        raise _Padding(f"{run} whitespace characters mid-object", "".join(out))
                    continue
                run = 0

                if char == '"':
                    in_string = True
                elif char == "{":
                    depth += 1
                    started = True
                elif char == "}":
                    depth -= 1
                    if started and depth == 0:
                        return "".join(out), chunks
    finally:
        await stream.close()

    return "".join(out), chunks


async def reachable() -> bool:
    try:
        await client().models.list()
        return True
    except Exception:
        return False
