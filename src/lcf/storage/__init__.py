"""Where the bytes go.

Two backends behind four operations. The local one is the default because a
single installation does not need an object store to keep a few .docx files, and
requiring one was the largest piece of setup that bought the deployer nothing.
S3 is what you move to when more than one process has to read the same bytes.

A stored artefact is addressed by `<scheme>://<bucket>/<key>`, and `fetch` reads
the scheme rather than assuming the current backend. That is what lets an
installation switch from S3 to disk, or back, without orphaning what it already
wrote: old rows keep pointing at the backend that holds them.
"""

from functools import lru_cache
from typing import Protocol

from lcf.core.config import settings


class StorageError(Exception):
    """The backend could not be reached, or refused."""


class Backend(Protocol):
    scheme: str

    def describe(self) -> str:
        """Where this is, for the admin page to print."""

    def get(self, bucket: str, key: str) -> bytes: ...

    def put(self, bucket: str, key: str, data: bytes, content_type: str) -> str:
        """Store the bytes and return the URI they can be read back by."""

    def existing(self) -> set[str]:
        """Buckets that are there now."""

    def ensure(self, names: list[str]) -> dict[str, str]:
        """Create any that are missing. Idempotent."""


@lru_cache
def store() -> Backend:
    """The backend this installation writes with.

    An S3 endpoint means somebody configured one on purpose; an empty one is not
    a missing setting, it is the ordinary case.
    """
    s = settings()
    if s.s3_endpoint.strip():
        from lcf.storage.s3 import S3Store

        return S3Store()

    from lcf.storage.local import FileStore

    return FileStore(s.storage_path)


def _backend_for(scheme: str) -> Backend:
    current = store()
    if scheme == current.scheme:
        return current
    if scheme == "s3":
        from lcf.storage.s3 import S3Store

        return S3Store()
    if scheme == "file":
        from lcf.storage.local import FileStore

        return FileStore(settings().storage_path)
    raise StorageError(f"no backend for {scheme}://")


def fetch(uri: str) -> bytes:
    """Read back something `put` returned, whichever backend wrote it."""
    scheme, _, rest = uri.partition("://")
    if not rest:
        raise StorageError(f"not a storage URI: {uri!r}")
    bucket, _, key = rest.partition("/")
    if not key:
        raise StorageError(f"no key in {uri!r}")
    return _backend_for(scheme).get(bucket, key)


def put(bucket: str, key: str, data: bytes, content_type: str) -> str:
    return store().put(bucket, key, data, content_type)


def ensure_buckets(names: list[str] | None = None) -> dict[str, str]:
    return store().ensure(names or settings().buckets)


def existing_buckets() -> set[str]:
    return store().existing()
