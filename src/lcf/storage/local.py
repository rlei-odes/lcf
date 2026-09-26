"""Object storage on the local disk.

A bucket is a directory and a key is a path inside it, which is enough for the
four operations the application performs. There is no metadata sidecar: the
content type is recoverable from the filename, and nothing reads it back except
the HTTP response that serves the file, which already knows.
"""

from pathlib import Path

from loguru import logger

from lcf.storage import StorageError


class FileStore:
    scheme = "file"

    def __init__(self, root: Path):
        self.root = Path(root)

    def describe(self) -> str:
        return str(self.root)

    def _path(self, bucket: str, key: str) -> Path:
        """Resolve a bucket and key to a file, refusing anything that escapes.

        Keys are built from document ids and timestamps, never from user input,
        so this guards a mistake rather than an attack — but a storage backend
        that can be talked into writing outside its root is not one worth
        shipping either way.
        """
        if not bucket or "/" in bucket or bucket.startswith("."):
            raise StorageError(f"bad bucket name: {bucket!r}")
        base = (self.root / bucket).resolve()
        target = (base / key).resolve()
        if base != target and base not in target.parents:
            raise StorageError(f"key escapes its bucket: {key!r}")
        return target

    def get(self, bucket: str, key: str) -> bytes:
        path = self._path(bucket, key)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise StorageError(f"could not read {bucket}/{key}: {exc}") from exc

    def put(self, bucket: str, key: str, data: bytes, content_type: str) -> str:
        del content_type  # the extension carries it; nothing reads it back
        path = self._path(bucket, key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Written beside the target and moved into place, so a reader never
            # sees a half-written export.
            staging = path.with_name(path.name + ".part")
            staging.write_bytes(data)
            staging.replace(path)
        except OSError as exc:
            raise StorageError(f"could not write {bucket}/{key}: {exc}") from exc
        return f"{self.scheme}://{bucket}/{key}"

    def existing(self) -> set[str]:
        if not self.root.is_dir():
            return set()
        return {p.name for p in self.root.iterdir() if p.is_dir()}

    def ensure(self, names: list[str]) -> dict[str, str]:
        outcome: dict[str, str] = {}
        for name in names:
            path = self._path(name, "")
            if path.is_dir():
                outcome[name] = "present"
                continue
            try:
                path.mkdir(parents=True, exist_ok=True)
                outcome[name] = "created"
                logger.info("created storage directory {}", path)
            except OSError as exc:
                outcome[name] = f"failed: {exc}"
                logger.error("could not create {}: {}", path, exc)
        return outcome
