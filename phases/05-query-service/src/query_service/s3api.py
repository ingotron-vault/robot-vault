import logging
import os
import re
from typing import Optional, List, Dict, Any
from urllib.parse import quote, urlencode

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from . import download_tickets
from . import settings

router = APIRouter(prefix="/api/v1/s3", tags=["s3"])
logger = logging.getLogger(__name__)

S3_ENDPOINT = os.environ.get("S3_ENDPOINT")
S3_PUBLIC_ENDPOINT = os.environ.get("S3_PUBLIC_ENDPOINT")  # optional public URL for clients
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
S3_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
S3_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
S3_SECURE = os.environ.get("S3_SECURE", "false").lower() == "true"
S3_PARQUET_BUCKET = os.environ.get("S3_PARQUET_BUCKET", os.environ.get("S3_BUCKET", ""))
S3_PREFIX_SCOPE = os.environ.get("S3_PREFIX_SCOPE", "")
if S3_PREFIX_SCOPE and not S3_PREFIX_SCOPE.endswith("/"):
    S3_PREFIX_SCOPE = S3_PREFIX_SCOPE + "/"

ALLOWED_PREFIXES = (
    "processed/parquet/",
    # add more allowed prefixes later (e.g., "raw/")
)

_MAX_PRESIGN_SECONDS = 3600
_DECIMAL_INTEGER = re.compile(r"^[0-9]+$")


def _s3_client(endpoint_url: Optional[str] = None, download_config: Optional[BotoConfig] = None):
    if not (S3_ENDPOINT and S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY):
        raise HTTPException(status_code=500, detail="s3_not_configured")
    # SigV4 must be explicit: without it boto3 presigns with SigV2
    # (development/objectstore/CONTRACT.md section 3).
    cfg = BotoConfig(
        region_name=S3_REGION,
        signature_version="s3v4",
        # Path style always (development/objectstore/CONTRACT.md section 3).
        s3={"addressing_style": "path"},
    )
    if download_config is not None:
        cfg = cfg.merge(download_config)
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url or S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY_ID,
        aws_secret_access_key=S3_SECRET_ACCESS_KEY,
        use_ssl=S3_SECURE,
        config=cfg,
    )


def _resolve_bucket(bucket: Optional[str]) -> str:
    """Allowlist the caller-supplied `bucket` against the one configured bucket.

    Without this check, `/list`, `/presign` and `/download` would accept an
    arbitrary `bucket` query/body param and use it verbatim, falling back to
    `S3_PARQUET_BUCKET` only when the caller omitted it. The key itself is
    checked against `ALLOWED_PREFIXES` (`processed/parquet/...`), but
    that check says nothing about *which bucket* -- a caller (anyone with the
    viewer role once the router's OIDC gate is on; anyone at all when it is
    not) could point this service's live lake credentials at any other
    bucket the same credentials happen to reach, as long as the requested key
    started with an allowed prefix.

    Omitting `bucket` resolves to `S3_PARQUET_BUCKET` -- every real caller
    (the SPA's `/api/v1/s3/list` and `/presign` calls; this file's own
    request bodies) always omits it. A
    `bucket` that names a *different* bucket is rejected with
    400, matching this module's existing error style (`invalid_prefix`,
    `missing_key`) rather than a generic 500.
    """
    if not bucket:
        return S3_PARQUET_BUCKET
    if bucket != S3_PARQUET_BUCKET:
        raise HTTPException(status_code=400, detail="invalid_bucket")
    return bucket


def _resolve_expiry(value: Any) -> int:
    """Return a bounded integer expiry instead of leaking conversion errors.

    S3 presigned URLs are bearer credentials. Keeping their lifetime at one
    hour or less limits exposure, and rejecting floats/bools avoids surprising
    JSON coercions such as ``true -> 1`` or ``1.9 -> 1``.
    """
    if isinstance(value, bool):
        raise HTTPException(status_code=400, detail="invalid_expires")
    if isinstance(value, int):
        expires = value
    elif isinstance(value, str) and _DECIMAL_INTEGER.fullmatch(value.strip()):
        expires = int(value.strip())
    else:
        raise HTTPException(status_code=400, detail="invalid_expires")
    if not 1 <= expires <= _MAX_PRESIGN_SECONDS:
        raise HTTPException(status_code=400, detail="invalid_expires")
    return expires


def _download_disposition(key: str) -> str:
    """Build a bounded, injection-safe RFC 6266 attachment header."""
    name = key.rsplit("/", 1)[-1] or "download"
    # Bound header size by bytes, preserving valid UTF-8 at the cut point.
    name = name.encode("utf-8")[:200].decode("utf-8", errors="ignore") or "download"
    fallback = "".join(
        char if 0x20 <= ord(char) < 0x7F and char not in {'"', "\\"} else "_"
        for char in name
    )
    encoded = quote(name, safe="")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


def _scope_and_validate_key(key: str) -> str:
    """Apply S3_PREFIX_SCOPE and enforce the ALLOWED_PREFIXES allowlist.

    Shared by /presign and /download so the proxy can never reach an object
    the presigned path would have refused.
    """
    scoped_key = f"{S3_PREFIX_SCOPE}{key}" if S3_PREFIX_SCOPE and not key.startswith(S3_PREFIX_SCOPE) else key
    valid = any(
        (scoped_key if S3_PREFIX_SCOPE else key).startswith(ap if not S3_PREFIX_SCOPE else S3_PREFIX_SCOPE + ap)
        for ap in ALLOWED_PREFIXES
    )
    if not valid:
        raise HTTPException(status_code=400, detail="invalid_key_prefix")
    return scoped_key


try:
    # The audit logger lives in `auth`, which imports nothing from here. The
    # guard is there only so a partial install still loads this router.
    from .auth import audit as _audit  # type: ignore
except Exception:  # pragma: no cover
    async def _audit(*args, **kwargs):
        return None


@router.get("/list")
def s3_list(prefix: str = Query("") , max_keys: int = Query(100, ge=1, le=1000), continuation_token: Optional[str] = None, bucket: Optional[str] = None, request: Request = None):
    scoped_prefix = f"{S3_PREFIX_SCOPE}{prefix}" if S3_PREFIX_SCOPE else prefix
    if not any(scoped_prefix.startswith(ap if not S3_PREFIX_SCOPE else S3_PREFIX_SCOPE + ap) for ap in ALLOWED_PREFIXES):
        raise HTTPException(status_code=400, detail="invalid_prefix")
    bucket_name = _resolve_bucket(bucket)
    if not bucket_name:
        raise HTTPException(status_code=500, detail="bucket_not_configured")
    client = _s3_client()
    kwargs: Dict[str, Any] = {
        "Bucket": bucket_name,
        "Prefix": scoped_prefix,
        "MaxKeys": max_keys,
    }
    if continuation_token:
        kwargs["ContinuationToken"] = continuation_token
    try:
        resp = client.list_objects_v2(**kwargs)
    except ClientError as e:
        code = (e.response or {}).get("Error", {}).get("Code", "")
        if code == "NoSuchBucket":
            raise HTTPException(status_code=404, detail="no_such_bucket")
        raise HTTPException(status_code=500, detail=f"s3_error:{code}")
    items: List[Dict[str, Any]] = []
    for c in resp.get("Contents", []) or []:
        items.append({
            "key": c.get("Key"),
            "size": int(c.get("Size") or 0),
            "last_modified": settings.iso_utc(c.get("LastModified")),
        })
    try:
        # fire-and-forget audit
        _audit(request, "s3_list", {"prefix": prefix, "returned": len(items)})
    except Exception as exc:
        logger.warning(
            "Could not emit S3 list audit event (%s)",
            type(exc).__name__,
        )
    return {
        "items": items,
        "is_truncated": bool(resp.get("IsTruncated")),
        "next_continuation_token": resp.get("NextContinuationToken"),
    }


@router.post("/presign")
def s3_presign(body: Dict[str, Any], request: Request = None):
    key = str(body.get("key") or "")
    requested_bucket = body.get("bucket")
    expires = _resolve_expiry(body.get("expires", 3600))
    if not key:
        raise HTTPException(status_code=400, detail="missing_key")
    scoped_key = _scope_and_validate_key(key)
    bucket = _resolve_bucket(str(requested_bucket) if requested_bucket else None)
    if not bucket:
        raise HTTPException(status_code=500, detail="bucket_not_configured")
    # SigV4 covers the request host. When a public object-store endpoint is
    # configured, sign for that endpoint from the outset; rewriting the host
    # after signing produces a URL S3 must reject.
    client = _s3_client(S3_PUBLIC_ENDPOINT) if S3_PUBLIC_ENDPOINT else _s3_client()
    try:
        url = client.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": bucket, "Key": scoped_key},
            ExpiresIn=expires,
        )
        # The object store's host port is not published, so a presigned URL pointing at
        # the object store is only reachable by a client that can route to it.
        # Unless an operator has declared a genuinely reachable public
        # endpoint, hand back this service's own download proxy instead --
        # query is the one service that stays published, so the browser can
        # always reach it.
        if not S3_PUBLIC_ENDPOINT:
            url = "/api/v1/s3/download?" + urlencode({"key": key, "bucket": bucket})
        try:
            _audit(request, "s3_presign", {"key": key, "expires": expires})
        except Exception as exc:
            logger.warning(
                "Could not emit S3 presign audit event (%s)",
                type(exc).__name__,
            )
        return {"url": url}
    except ClientError as e:
        code = (e.response or {}).get("Error", {}).get("Code", "")
        if code == "NoSuchBucket":
            raise HTTPException(status_code=404, detail="no_such_bucket")
        raise HTTPException(status_code=500, detail=f"s3_error:{code}")


@router.get("/download")
def s3_download(key: str = Query(...), bucket: str = Query(default="")):
    """Stream an allowlisted object through this service.

    The object store's host port is not published, so a presigned URL is not
    reachable from a browser on the host. The query service is the one
    component that stays published, so it proxies the bytes. The key goes
    through the same scope+allowlist check as /presign, so this exposes
    nothing the presigned path did not already expose.
    """
    bucket = _resolve_bucket(bucket)
    if not bucket:
        raise HTTPException(status_code=500, detail="bucket_not_configured")
    scoped_key = _scope_and_validate_key(key)
    try:
        obj = _s3_client().get_object(Bucket=bucket, Key=scoped_key)
    except ClientError as e:
        code = (e.response or {}).get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            raise HTTPException(status_code=404, detail="no_such_key")
        if code == "NoSuchBucket":
            raise HTTPException(status_code=404, detail="no_such_bucket")
        raise HTTPException(status_code=500, detail=f"s3_error:{code}")

    headers = {"Content-Disposition": _download_disposition(scoped_key)}
    if obj.get("ContentLength") is not None:
        headers["Content-Length"] = str(obj["ContentLength"])
    return StreamingResponse(
        obj["Body"].iter_chunks(chunk_size=1024 * 1024),
        media_type=obj.get("ContentType") or "application/octet-stream",
        headers=headers,
    )


@router.post("/download/ticket")
def s3_download_ticket(key: str = Query(..., max_length=2048)):
    # Mounted behind the same viewer guard as /download. Never issue a
    # capability for an arbitrary bucket, export key, or unallowlisted prefix.
    _scope_and_validate_key(key)
    return download_tickets.issue(lambda: s3_download(key, bucket=""))
