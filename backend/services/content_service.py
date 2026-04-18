from urllib.parse import urlparse
import os
import boto3
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from botocore.config import Config
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
import base64

from core import config

logger = logging.getLogger(__name__)

# S3 supports single PUT up to 5GB
# For files >5GB, multipart is required
# Setting to 100MB threshold for now - use multipart for anything larger
SINGLE_PUT_MAX = 100 * 1024 * 1024  # 100 MiB


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Edge store: browser-visible origin, typically MinIO in local/self-host.
_EDGE_BUCKET = os.getenv("CONTENT_EDGE_BUCKET") or config.CONTENT_BUCKET
_EDGE_ACCESS_KEY_ID = os.getenv("CONTENT_EDGE_ACCESS_KEY_ID") or os.getenv("CONTENT_ROOT_USER")
_EDGE_SECRET_ACCESS_KEY = os.getenv("CONTENT_EDGE_SECRET_ACCESS_KEY") or os.getenv("CONTENT_ROOT_PASSWORD")
_EDGE_REGION = os.getenv("CONTENT_EDGE_REGION") or "us-east-1"
_EDGE_ENDPOINT_URL_INTERNAL = (
    os.getenv("CONTENT_EDGE_ENDPOINT_URL_INTERNAL")
    or os.getenv("AWS_ENDPOINT_URL_INTERNAL")
    or os.getenv("AWS_ENDPOINT_URL")
)
_EDGE_ENDPOINT_URL_PUBLIC = (
    os.getenv("CONTENT_EDGE_ENDPOINT_URL_PUBLIC")
    or os.getenv("AWS_ENDPOINT_URL_PUBLIC")
    or os.getenv("AWS_ENDPOINT_URL")
    or _EDGE_ENDPOINT_URL_INTERNAL
)
# Optional override for presigned URLs consumed by MCP servers.
# When servers run in a different network context than the backend, presigned
# URLs need a hostname reachable from the server's network.
_EDGE_ENDPOINT_URL_SERVER = os.getenv("CONTENT_EDGE_ENDPOINT_URL")

# Durable store: hidden persistence layer, typically AWS S3.
_DURABLE_BUCKET = (os.getenv("CONTENT_DURABLE_BUCKET") or "").strip()
_DURABLE_ACCESS_KEY_ID = (
    os.getenv("CONTENT_DURABLE_ACCESS_KEY_ID")
    or os.getenv("CONTENT_DURABLE_AWS_ACCESS_KEY_ID")
)
_DURABLE_SECRET_ACCESS_KEY = (
    os.getenv("CONTENT_DURABLE_SECRET_ACCESS_KEY")
    or os.getenv("CONTENT_DURABLE_AWS_SECRET_ACCESS_KEY")
)
_DURABLE_REGION = (
    os.getenv("CONTENT_DURABLE_REGION")
    or os.getenv("CONTENT_DURABLE_AWS_REGION")
    or "ca-central-1"
)
_DURABLE_ENDPOINT_URL = (
    os.getenv("CONTENT_DURABLE_ENDPOINT_URL")
    or os.getenv("CONTENT_DURABLE_AWS_ENDPOINT_URL")
    or None
)
_EVICT_EDGE_AFTER_DURABLE_SYNC = _env_flag("CONTENT_EDGE_EVICT_AFTER_DURABLE_SYNC", default=False)


def get_content_storage_mode() -> str:
    """Return the effective storage topology used for content artifacts."""
    cloudfront_enabled = bool(
        os.getenv("CLOUDFRONT_KEY_ID")
        and (os.getenv("CLOUDFRONT_PRIVATE_KEY") or os.getenv("CLOUDFRONT_PRIVATE_KEY_PATH"))
    )
    if cloudfront_enabled and _durable_store_enabled():
        return "cloudfront-s3"
    if _durable_store_enabled():
        return "minio-s3-backed"
    return "minio-only"


def _make_s3_client(
    *,
    access_key_id: Optional[str],
    secret_access_key: Optional[str],
    region_name: str,
    endpoint_url: Optional[str] = None,
):
    config = Config(s3={"addressing_style": "path"}) if endpoint_url else None
    kwargs = {
        "service_name": "s3",
        "aws_access_key_id": access_key_id,
        "aws_secret_access_key": secret_access_key,
        "region_name": region_name,
    }
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    if config is not None:
        kwargs["config"] = config
    return boto3.client(**kwargs)


_s3_edge_internal = _make_s3_client(
    access_key_id=_EDGE_ACCESS_KEY_ID,
    secret_access_key=_EDGE_SECRET_ACCESS_KEY,
    region_name=_EDGE_REGION,
    endpoint_url=_EDGE_ENDPOINT_URL_INTERNAL,
)
_s3_edge_public = _make_s3_client(
    access_key_id=_EDGE_ACCESS_KEY_ID,
    secret_access_key=_EDGE_SECRET_ACCESS_KEY,
    region_name=_EDGE_REGION,
    endpoint_url=_EDGE_ENDPOINT_URL_PUBLIC,
)
_s3_edge_server = (
    _make_s3_client(
        access_key_id=_EDGE_ACCESS_KEY_ID,
        secret_access_key=_EDGE_SECRET_ACCESS_KEY,
        region_name=_EDGE_REGION,
        endpoint_url=_EDGE_ENDPOINT_URL_SERVER,
    )
    if _EDGE_ENDPOINT_URL_SERVER
    else None
)
_s3_durable = (
    _make_s3_client(
        access_key_id=_DURABLE_ACCESS_KEY_ID,
        secret_access_key=_DURABLE_SECRET_ACCESS_KEY,
        region_name=_DURABLE_REGION,
        endpoint_url=_DURABLE_ENDPOINT_URL,
    )
    if _DURABLE_BUCKET
    else None
)

_BUCKET_CHECKED: bool = False
_BUCKET_WARNING_EMITTED: bool = False
_CORS_APPLIED: bool = False
_CORS_UNSUPPORTED: bool = False


def reinit_edge_clients() -> None:
    """Re-create edge S3 clients using credentials from key_manager and current config.

    Called at startup after key initialization, and again after platform settings are
    loaded from the DB, so the endpoint URL and credentials always reflect the live
    config rather than the module-level env-var snapshot.
    """
    global _s3_edge_internal, _s3_edge_public, _s3_edge_server, _BUCKET_CHECKED, _CORS_APPLIED, _CORS_UNSUPPORTED
    try:
        from core.key_manager import get_minio_pass
        secret_key = get_minio_pass()
    except RuntimeError:
        return
    access_key = os.getenv("MINIO_ROOT_USER") or os.getenv("CONTENT_ROOT_USER") or "agience"

    # Use explicit env-var overrides first; fall back to config.CONTENT_URI so that
    # the endpoint is always populated after load_settings_from_db() runs.
    endpoint_internal = (
        _EDGE_ENDPOINT_URL_INTERNAL
        or config.CONTENT_URI
        or None
    )
    endpoint_public = (
        _EDGE_ENDPOINT_URL_PUBLIC
        or config.CONTENT_URI
        or None
    )

    _s3_edge_internal = _make_s3_client(
        access_key_id=access_key,
        secret_access_key=secret_key,
        region_name=_EDGE_REGION,
        endpoint_url=endpoint_internal,
    )
    _s3_edge_public = _make_s3_client(
        access_key_id=access_key,
        secret_access_key=secret_key,
        region_name=_EDGE_REGION,
        endpoint_url=endpoint_public,
    )
    # Server-facing client for presigned URLs consumed by MCP servers in a
    # different network context.  Only created when CONTENT_EDGE_ENDPOINT_URL
    # is set.
    if _EDGE_ENDPOINT_URL_SERVER:
        _s3_edge_server = _make_s3_client(
            access_key_id=access_key,
            secret_access_key=secret_key,
            region_name=_EDGE_REGION,
            endpoint_url=_EDGE_ENDPOINT_URL_SERVER,
        )
    else:
        _s3_edge_server = None
    # Reset so the next operation re-checks (and creates if needed) the bucket
    # with the freshly configured clients.
    _BUCKET_CHECKED = False
    _CORS_APPLIED = False
    _CORS_UNSUPPORTED = False
    logger.info("Edge S3 clients reinitialized (endpoint: %s)", endpoint_internal)


def _apply_bucket_cors() -> None:
    """Apply a permissive CORS policy so browsers can PUT presigned upload requests directly."""
    global _CORS_APPLIED, _CORS_UNSUPPORTED
    if _CORS_APPLIED or _CORS_UNSUPPORTED:
        return
    try:
        _s3_edge_internal.put_bucket_cors(
            Bucket=_EDGE_BUCKET,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedOrigins": ["*"],
                        "AllowedMethods": ["GET", "PUT", "POST", "DELETE", "HEAD"],
                        "AllowedHeaders": ["*"],
                        "ExposeHeaders": ["ETag", "Content-Length", "Content-Type"],
                        "MaxAgeSeconds": 3600,
                    }
                ]
            },
        )
        _CORS_APPLIED = True
        logger.info("Applied CORS policy to content bucket '%s'", _EDGE_BUCKET)
    except ClientError as exc:
        error_code = (exc.response or {}).get("Error", {}).get("Code")
        if error_code == "NotImplemented":
            _CORS_UNSUPPORTED = True
            logger.info(
                "Skipping bucket CORS apply for '%s': storage endpoint does not support PutBucketCors",
                _EDGE_BUCKET,
            )
            return
        logger.warning("Could not apply CORS policy to content bucket '%s': %s", _EDGE_BUCKET, exc)
    except Exception as exc:
        logger.warning("Could not apply CORS policy to content bucket '%s': %s", _EDGE_BUCKET, exc)


def _ensure_bucket_exists_once() -> None:
    """Ensure the edge content bucket exists (creating it if needed), then apply CORS."""
    global _BUCKET_CHECKED, _BUCKET_WARNING_EMITTED
    if _BUCKET_CHECKED:
        return

    try:
        _s3_edge_internal.head_bucket(Bucket=_EDGE_BUCKET)
        _BUCKET_CHECKED = True
        _apply_bucket_cors()
        return
    except Exception:
        pass

    try:
        kwargs: dict = {"Bucket": _EDGE_BUCKET}
        if _EDGE_REGION and _EDGE_REGION != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": _EDGE_REGION}
        _s3_edge_internal.create_bucket(**kwargs)
        logger.info("Created content bucket '%s'", _EDGE_BUCKET)
        _apply_bucket_cors()
    except Exception as exc:
        if not _BUCKET_WARNING_EMITTED:
            logger.warning(
                "Could not access or create content bucket '%s': %s",
                _EDGE_BUCKET,
                exc,
            )
            _BUCKET_WARNING_EMITTED = True
    finally:
        _BUCKET_CHECKED = True


def ensure_content_bucket() -> None:
    """Public entry point: (re-)check and create the content bucket.

    Call this after reinit_edge_clients() to eagerly provision the bucket
    at setup / startup rather than waiting for the first upload.
    """
    global _BUCKET_CHECKED, _BUCKET_WARNING_EMITTED
    _BUCKET_CHECKED = False
    _BUCKET_WARNING_EMITTED = False
    _ensure_bucket_exists_once()


def put_text_direct(content_key: str, text: str, content_type: str = "text/plain") -> None:
    """Write a text/bytes payload directly to the edge store (server-side, no presigned URL).

    Used when the backend itself is the uploader — e.g. auto-migrating inline
    artifact content to S3 on create/update.
    """
    _ensure_bucket_exists_once()
    _s3_edge_internal.put_object(
        Bucket=_EDGE_BUCKET,
        Key=content_key,
        Body=text.encode("utf-8") if isinstance(text, str) else text,
        ContentType=content_type,
    )


def get_text_direct(content_key: str) -> str:
    """Fetch a text object directly from the edge store (server-side, no presigned URL).

    Used by agents and MCP server tools that need the raw content of an artifact
    (operator config JSON, authorizer config, etc.) when artifact.content is empty
    because it was stored in S3.
    """
    response = _s3_edge_internal.get_object(Bucket=_EDGE_BUCKET, Key=content_key)
    return response["Body"].read().decode("utf-8")


def _durable_store_enabled() -> bool:
    return bool(_DURABLE_BUCKET and _s3_durable is not None)


def _copy_between_clients(src_client, src_bucket: str, dst_client, dst_bucket: str, key: str) -> None:
    source = src_client.get_object(Bucket=src_bucket, Key=key)
    body = source["Body"]
    try:
        put_kwargs = {
            "Bucket": dst_bucket,
            "Key": key,
            "Body": body,
        }
        content_type = source.get("ContentType")
        cache_control = source.get("CacheControl")
        if content_type:
            put_kwargs["ContentType"] = content_type
        if cache_control:
            put_kwargs["CacheControl"] = cache_control
        dst_client.put_object(**put_kwargs)
    finally:
        try:
            body.close()
        except Exception:
            pass


def ensure_edge_object_present(key: str) -> bool:
    try:
        _s3_edge_internal.head_object(Bucket=_EDGE_BUCKET, Key=key)
        return True
    except Exception:
        pass

    if not _durable_store_enabled():
        return False

    try:
        _ensure_bucket_exists_once()
        _copy_between_clients(_s3_durable, _DURABLE_BUCKET, _s3_edge_internal, _EDGE_BUCKET, key)
        logger.info("Hydrated edge content from durable store for key=%s", key)
        return True
    except Exception as exc:
        logger.warning("Failed to hydrate edge content for key=%s: %s", key, exc)
        return False


def persist_object_to_durable(key: str) -> bool:
    if not _durable_store_enabled():
        return False

    _copy_between_clients(_s3_edge_internal, _EDGE_BUCKET, _s3_durable, _DURABLE_BUCKET, key)
    logger.info("Persisted content to durable store for key=%s", key)
    if _EVICT_EDGE_AFTER_DURABLE_SYNC:
        _s3_edge_internal.delete_object(Bucket=_EDGE_BUCKET, Key=key)
        logger.info("Evicted edge content after durable sync for key=%s", key)
    return True


def build_public_content_url(file_id: str, filename: str, tenant_domain: Optional[str] = None) -> str:
    """Build a public content URL for agience-hosted public files.

    Always derives the base from CONTENT_URI -- supports both single-domain
    path-prefix deployments (https://domain.com/content) and subdomain
    deployments (https://content.domain.com). The tenant_domain parameter
    is deprecated and ignored.
    """
    safe_name = filename.replace("/", "_")
    base = config.CONTENT_URI.rstrip("/")
    return f"{base}/files/{file_id}/{safe_name}"


def generate_signed_url(
    key: str,
    expires_in: Optional[int] = None,
    filename: Optional[str] = None,
    content_type: Optional[str] = None,
    as_attachment: bool = False,
    server_facing: bool = False,
) -> str:
    """Generate a signed content URL from the edge origin, hydrating from durable storage when needed.

    When *server_facing* is True the URL is generated using the server-facing
    S3 client so that MCP servers in a different network context can reach the
    content store.  Browser callers should use the default (public) URL.
    """
    if expires_in is None:
        expires_in = config.CONTENT_DOWNLOAD_URL_EXPIRY
    effective_content_type = content_type
    cloudfront_key_id = os.getenv("CLOUDFRONT_KEY_ID")
    cloudfront_private_key = os.getenv("CLOUDFRONT_PRIVATE_KEY")
    cloudfront_private_key_path = os.getenv("CLOUDFRONT_PRIVATE_KEY_PATH")

    if cloudfront_key_id and (cloudfront_private_key or cloudfront_private_key_path):
        try:
            return _generate_cloudfront_signed_url(
                key,
                expires_in,
                cloudfront_key_id,
                cloudfront_private_key,
                cloudfront_private_key_path,
                filename,
                effective_content_type,
            )
        except Exception as exc:
            logger.warning("Error generating CloudFront signed URL for %s: %s", key, exc)

    try:
        if not ensure_edge_object_present(key):
            logger.info("Edge object missing for key=%s and could not be hydrated before signing", key)
        params = {
            "Bucket": _EDGE_BUCKET,
            "Key": key,
        }

        if filename and as_attachment:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        if effective_content_type:
            if effective_content_type.startswith("text/") and "charset" not in effective_content_type.lower():
                params["ResponseContentType"] = f"{effective_content_type}; charset=utf-8"
            else:
                params["ResponseContentType"] = effective_content_type

        s3_client = _s3_edge_public
        if server_facing:
            s3_client = _s3_edge_server or _s3_edge_internal
        return s3_client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=expires_in,
        )
    except Exception as exc:
        logger.warning("Error generating edge presigned URL for %s: %s", key, exc)
        u = urlparse(config.CONTENT_URI)
        scheme = u.scheme or "https"
        host = u.netloc or u.path
        return f"{scheme}://{host}/{key}"


def _generate_cloudfront_signed_url(
    key: str,
    expires_in: int,
    key_id: str,
    private_key_str: Optional[str] = None,
    private_key_path: Optional[str] = None,
    filename: Optional[str] = None,
    content_type: Optional[str] = None,
) -> str:
    """Generate CloudFront signed URL using RSA-SHA1 signature."""
    if private_key_str:
        private_key_bytes = private_key_str.encode("utf-8")
    elif private_key_path:
        with open(private_key_path, "rb") as f:
            private_key_bytes = f.read()
    else:
        raise ValueError("Either CLOUDFRONT_PRIVATE_KEY or CLOUDFRONT_PRIVATE_KEY_PATH must be set")

    from cryptography.hazmat.backends import default_backend

    private_key = serialization.load_pem_private_key(
        private_key_bytes,
        password=None,
        backend=default_backend(),
    )

    u = urlparse(config.CONTENT_URI)
    scheme = u.scheme or "https"
    host = u.netloc or u.path
    resource_url = f"{scheme}://{host}/{key}"

    expire_time = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    expire_timestamp = int(expire_time.timestamp())

    policy = {
        "Statement": [{
            "Resource": resource_url,
            "Condition": {
                "DateLessThan": {
                    "AWS:EpochTime": expire_timestamp,
                }
            },
        }]
    }

    import json

    policy_json = json.dumps(policy, separators=(",", ":"))

    signature = private_key.sign(  # type: ignore[union-attr]
        policy_json.encode("utf-8"),
        padding.PKCS1v15(),  # type: ignore[call-arg]
        hashes.SHA1(),  # type: ignore[call-arg]
    )

    signature_b64 = base64.b64encode(signature).decode("utf-8")
    signature_b64 = signature_b64.replace("+", "-").replace("=", "_").replace("/", "~")

    policy_b64 = base64.b64encode(policy_json.encode("utf-8")).decode("utf-8")
    policy_b64 = policy_b64.replace("+", "-").replace("=", "_").replace("/", "~")

    return f"{resource_url}?Policy={policy_b64}&Signature={signature_b64}&Key-Pair-Id={key_id}"


def presign_put_or_multipart(key: str, content_type: str, size: int):
    _ensure_bucket_exists_once()
    if size <= SINGLE_PUT_MAX:
        url = _s3_edge_public.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": _EDGE_BUCKET,
                "Key": key,
                "ContentType": content_type,
                "CacheControl": "private, max-age=31536000, immutable",
            },
            ExpiresIn=config.CONTENT_UPLOAD_URL_EXPIRY,
        )
        return {"mode": "put", "url": url}
    resp = _s3_edge_internal.create_multipart_upload(
        Bucket=_EDGE_BUCKET,
        Key=key,
        ContentType=content_type,
        CacheControl="private, max-age=31536000, immutable",
    )
    return {"mode": "multipart", "uploadId": resp["UploadId"]}


def generate_multipart_part_url(key: str, upload_id: str, part_number: int, expires_in: Optional[int] = None) -> str:
    """Generate presigned URL for uploading a single part in multipart upload."""
    if expires_in is None:
        expires_in = config.CONTENT_MULTIPART_PART_URL_EXPIRY

    _ensure_bucket_exists_once()

    return _s3_edge_public.generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": _EDGE_BUCKET,
            "Key": key,
            "UploadId": upload_id,
            "PartNumber": part_number,
        },
        ExpiresIn=expires_in,
    )


def complete_multipart(key: str, upload_id: str, parts: list[dict]):
    parts_sorted = sorted(parts, key=lambda p: p["PartNumber"])
    return _s3_edge_internal.complete_multipart_upload(
        Bucket=_EDGE_BUCKET,
        Key=key,
        MultipartUpload={"Parts": parts_sorted},
        UploadId=upload_id,
    )


def head_object(key: str):
    try:
        return _s3_edge_internal.head_object(Bucket=_EDGE_BUCKET, Key=key)
    except Exception:
        return None


def delete_object(key: str) -> bool:
    """Delete an object from edge storage and durable storage when configured."""
    deleted = False
    try:
        _s3_edge_internal.delete_object(Bucket=_EDGE_BUCKET, Key=key)
        deleted = True
    except Exception as exc:
        logger.warning("Error deleting edge object %s: %s", key, exc)

    if _durable_store_enabled():
        try:
            _s3_durable.delete_object(Bucket=_DURABLE_BUCKET, Key=key)
            deleted = True
        except Exception as exc:
            logger.warning("Error deleting durable object %s: %s", key, exc)

    return deleted
