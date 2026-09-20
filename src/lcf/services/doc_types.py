"""Publishing and reading document types.

A published version is immutable. Republishing the same version number is refused
rather than quietly overwritten — documents pin versions, and mutating one under
them would invalidate work already done (DESIGN decision 1).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lcf.models.tables import DocType, DocTypeVersion
from lcf.spec.linter import validate
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


async def list_types(session: AsyncSession) -> list[DocType]:
    return list(await session.scalars(select(DocType).order_by(DocType.key)))
