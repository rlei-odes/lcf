"""S3-compatible object storage (versitygw, MinIO, Ceph, AWS).

The app creates its own buckets when they are missing, so deployment needs only a
credential with the rights to do so — not a prepared environment.
"""

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from loguru import logger

from lcf.core.config import settings
from lcf.storage import StorageError


def client():
    s = settings()
    return boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint,
        aws_access_key_id=s.s3_access_key,
        aws_secret_access_key=s.s3_secret_key,
        region_name=s.s3_region,
        # versitygw serves path-style addressing; virtual-host style would resolve
        # bucket names as subdomains and fail on a bare IP endpoint.
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


class S3Store:
    scheme = "s3"

    def describe(self) -> str:
        return settings().s3_endpoint or "s3"

    def get(self, bucket: str, key: str) -> bytes:
        try:
            return client().get_object(Bucket=bucket, Key=key)["Body"].read()
        except Exception as exc:
            raise StorageError(f"could not read s3://{bucket}/{key}: {exc}") from exc

    def put(self, bucket: str, key: str, data: bytes, content_type: str) -> str:
        try:
            client().put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
        except Exception as exc:
            raise StorageError(f"could not write s3://{bucket}/{key}: {exc}") from exc
        return f"{self.scheme}://{bucket}/{key}"

    def existing(self) -> set[str]:
        try:
            return {b["Name"] for b in client().list_buckets().get("Buckets", [])}
        except Exception as exc:
            raise StorageError(f"could not list buckets: {exc}") from exc

    def ensure(self, names: list[str]) -> dict[str, str]:
        s3 = client()
        existing = self.existing()
        outcome: dict[str, str] = {}

        for name in names:
            if name in existing:
                outcome[name] = "present"
                continue
            try:
                s3.create_bucket(Bucket=name)
                outcome[name] = "created"
                logger.info("created bucket {}", name)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                    outcome[name] = "present"
                else:
                    outcome[name] = f"failed: {code or exc}"
                    logger.error("could not create bucket {}: {}", name, exc)
        return outcome


def ensure_buckets(names: list[str] | None = None) -> dict[str, str]:
    """Kept for callers that want S3 specifically. Most want `storage.ensure_buckets`."""
    return S3Store().ensure(names or settings().buckets)
