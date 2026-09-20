"""S3-compatible object storage (versitygw).

The app creates its own buckets when they are missing, so deployment needs only a
credential with the rights to do so — not a prepared environment.
"""

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from loguru import logger

from lcf.core.config import settings


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


def ensure_buckets(names: list[str] | None = None) -> dict[str, str]:
    """Create any missing buckets. Idempotent — safe to call on every startup."""
    s3 = client()
    existing = {b["Name"] for b in s3.list_buckets().get("Buckets", [])}
    outcome: dict[str, str] = {}

    for name in names or settings().buckets:
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
