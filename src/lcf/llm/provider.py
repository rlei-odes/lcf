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
    user: str,
    schema: dict[str, Any],
    schema_name: str = "response",
    purpose: str = "",
) -> Completion:
    """Ask for one JSON object matching `schema`, and insist on getting one.

    `purpose` names the call in the event log. It defaults to the schema name,
    which is already a decent description of what was asked for.
    """
    import time

    s = settings()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    started = time.monotonic()
    last_error = ""
    chunks = chars = 0
    label = purpose or schema_name

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    for attempt in (1, 2):
        raw = ""
        problem: str | None = None
        try:
            raw, streamed = await _stream_one_object(messages, schema, schema_name)
            chunks += streamed
            chars += len(raw)
        except _Padding as exc:
            problem = f"degenerate output: {exc}"
            raw = exc.partial
            chars += len(raw)
        except Exception as exc:  # network, timeout, refusal
            await _emit(
                CallRecord(label, s.llm_model, False, elapsed(), attempt, chunks, chars,
                           f"{type(exc).__name__}: {exc}")
            )
            raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc

        try:
            data = json.loads(raw) if problem is None else None
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
                CallRecord(label, s.llm_model, False, elapsed(), attempt, chunks, chars, last_error)
            )
            raise LLMMalformed(last_error)

        await _emit(CallRecord(label, s.llm_model, True, elapsed(), attempt, chunks, chars))
        return Completion(
            data=data,
            raw=raw,
            prompt=f"{system}\n\n---\n\n{user}",
            model=s.llm_model,
            duration_ms=elapsed(),
            attempts=attempt,
            chunks=chunks,
            chars=chars,
        )

    raise LLMMalformed(last_error)


async def _stream_one_object(
    messages: list[dict[str, str]], schema: dict[str, Any], schema_name: str
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
