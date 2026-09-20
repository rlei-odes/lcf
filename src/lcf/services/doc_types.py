"""Publishing and reading document types.

A published version is immutable. Republishing the same version number is refused
rather than quietly overwritten — documents pin versions, and mutating one under
them would invalidate work already done (DESIGN decision 1).
"""

from dataclasses import dataclass

from pydantic import ValidationError
from ruamel.yaml.error import MarkedYAMLError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lcf.models.tables import DocType, DocTypeVersion, Document
from lcf.spec import loader
from lcf.spec.linter import lint, validate
from lcf.spec.models import DocTypeSpec


class VersionExists(Exception):
    pass


class NotFound(Exception):
    pass


async def publish(session: AsyncSession, spec: DocTypeSpec) -> DocTypeVersion:
    validate(spec)

    doc_type = await session.scalar(select(DocType).where(DocType.key == spec.id))
    if doc_type is None:
        doc_type = DocType(key=spec.id, title=spec.title)
        session.add(doc_type)
        await session.flush()

    clash = await session.scalar(
        select(DocTypeVersion).where(
            DocTypeVersion.doc_type_id == doc_type.id, DocTypeVersion.version == spec.version
        )
    )
    if clash is not None:
        raise VersionExists(
            f"{spec.id} v{spec.version} is already published; bump the version to change it"
        )

    version = DocTypeVersion(doc_type_id=doc_type.id, version=spec.version, spec=spec.to_dict())
    session.add(version)
    await session.flush()
    return version


async def get_version(
    session: AsyncSession, key: str, version: int | None = None
) -> DocTypeVersion:
    doc_type = await session.scalar(select(DocType).where(DocType.key == key))
    if doc_type is None:
        raise NotFound(f"no document type {key!r}")

    query = select(DocTypeVersion).where(DocTypeVersion.doc_type_id == doc_type.id)
    if version is not None:
        query = query.where(DocTypeVersion.version == version)
    query = query.order_by(DocTypeVersion.version.desc()).limit(1)

    found = await session.scalar(query)
    if found is None:
        raise NotFound(f"no version {version} of {key!r}")
    return found


def spec_of(version: DocTypeVersion) -> DocTypeSpec:
    return DocTypeSpec.model_validate(version.spec)


@dataclass
class Review:
    """What the editor learns about a draft without publishing it.

    Three failure modes, one shape. The rule builder does not care whether the
    YAML would not parse, the shape did not match the model, or the references did
    not resolve — they care what is wrong and where.
    """

    spec: DocTypeSpec | None
    errors: list[str]

    @property
    def ok(self) -> bool:
        return self.spec is not None and not self.errors


def review(text: str) -> Review:
    """Parse and lint a draft spec. Never raises — errors are the answer."""
    try:
        spec = loader.parse(text)
    except ValidationError as exc:
        return Review(None, [_pydantic_error(e) for e in exc.errors()])
    except MarkedYAMLError as exc:
        line = getattr(exc.problem_mark, "line", None)
        where = f"line {line + 1}" if line is not None else "YAML"
        return Review(None, [f"{where}: {exc.problem or exc}"])
    except Exception as exc:  # ruamel raises a family of these
        return Review(None, [f"YAML: {exc}"])

    errors = [str(e) for e in lint(spec)]
    return Review(spec if not errors else None, errors)


def _pydantic_error(error: dict) -> str:
    """One Pydantic error as a line a person can act on."""
    location = ".".join(str(part) for part in error.get("loc", ()) if part != "__root__")
    message = error.get("msg", "invalid")
    # Pydantic prefixes validator failures with 'Value error, '; the rule
    # builder wrote the rule, not the validator.
    message = message.removeprefix("Value error, ")
    return f"{location or 'spec'}: {message}"


async def list_types(session: AsyncSession) -> list[DocType]:
    return list(await session.scalars(select(DocType).order_by(DocType.key)))


async def get_type(session: AsyncSession, key: str) -> DocType:
    doc_type = await session.scalar(select(DocType).where(DocType.key == key))
    if doc_type is None:
        raise NotFound(f"no document type {key!r}")
    return doc_type


async def versions_of(session: AsyncSession, key: str) -> list[DocTypeVersion]:
    """Every published version, newest first."""
    doc_type = await get_type(session, key)
    return list(
        await session.scalars(
            select(DocTypeVersion)
            .where(DocTypeVersion.doc_type_id == doc_type.id)
            .order_by(DocTypeVersion.version.desc())
        )
    )


async def usage(session: AsyncSession, key: str) -> dict[int, int]:
    """How many documents pin each version.

    Shown before editing, because "nobody has used this yet" and "eleven reports
    depend on this wording" are different situations for a rule builder, and the
    spec itself cannot tell them apart.
    """
    doc_type = await get_type(session, key)
    rows = await session.execute(
        select(DocTypeVersion.version, func.count(Document.id))
        .join(Document, Document.doc_type_version_id == DocTypeVersion.id, isouter=True)
        .where(DocTypeVersion.doc_type_id == doc_type.id)
        .group_by(DocTypeVersion.version)
    )
    return {version: count for version, count in rows.all()}


# The starting point for a new type: the smallest spec that lints, publishes, and
# can actually be filled in. A rule builder learns the shape faster by changing
# something that works than by reading a schema.
SKELETON = """\
id: my-document-type
version: 1
title: My Document Type
description: What this document is for, in one line.
language: en

# Optional. Layered under the system default, above nothing — how the writing
# should read across the whole type. Never instructions about *what* to write.
style: >
  Write plainly, in the past tense, for a reader outside the team.

sections:
  - key: summary
    title: Summary
    description: What this section has to establish.
    guidance: >
      Shown to the author and to the assistant. Say what a good one contains.
    required: true
    depends_on: []

    questions:
      - key: what_happened
        prompt: "What happened?"
        type: text
        required: true
        hint: "A few sentences is enough."

    blocks:
      - key: text
        kind: prose
        label: Summary
        hint: "One paragraph."

    # The checks are the instructions: each of these is shown to the assistant as
    # a target *and* used to grade the result. There is no separate prompt.
    requirements:
      - { id: summary_present, kind: present, block: text, severity: blocker }
      - id: summary_length
        kind: length
        block: text
        min_words: 40
        severity: warning

quality_criteria: []
"""
