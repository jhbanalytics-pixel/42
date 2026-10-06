"""Secret management. GCP Secret Manager in prod, .env locally."""

import logging
import os
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")


# Secrets read at a named version rather than the latest alias, keyed by their
# exact Secret Manager name. The funded SocialCrawl grant in
# ops/deploy/iam_delta_v1.json is conditioned on version 1 of that secret, so
# the reader asks for version 1 by name. Module data, never a caller argument.
PINNED_SECRET_VERSIONS = MappingProxyType({"SOCIALCRAWL_OGILVY_API_KEY": "1"})


def _pinned_name(secret_id: str) -> str | None:
    """The exact pinned secret name this id spells, or None when it is not pinned."""
    canonical = secret_id.upper().replace("-", "_")
    return canonical if canonical in PINNED_SECRET_VERSIONS else None


def _use_secret_manager() -> bool:
    """True when Secret Manager should be queried before env vars."""
    if os.environ.get("TRENDS_ENV") in ("staging", "prod"):
        return True
    # Cloud Run jobs run with TRENDS_ENV=dev and ADC credentials. Connector
    # secrets added after the cron_flags.env baseline (e.g. SOCIALCRAWL_API_KEY)
    # live in Secret Manager only, so the cron must resolve SM here.
    return bool(
        os.environ.get("K_SERVICE")
        or os.environ.get("CLOUD_RUN_JOB")
        or os.environ.get("CLOUD_RUN_EXECUTION")
    )


@lru_cache
def _get_secret_client():
    """Lazy-load Secret Manager client (only in GCP environments)."""
    if _use_secret_manager():
        from google.cloud import secretmanager

        return secretmanager.SecretManagerServiceClient()
    return None


def _secret_name_candidates(secret_id: str) -> list[str]:
    """The Secret Manager names to try, in order, for a caller's secret id.

    Callers ask for the env-var spelling (``YOUTUBE_API_KEY``) but the live
    secrets are named ``youtube-api-key``, ``ensembledata-api-token`` and
    ``brand24-api-key``. Only ``SOCIALCRAWL_API_KEY`` is stored under the
    upper-snake name. Secret Manager answers 403 rather than 404 for a name
    that does not exist, so the miss read as a permission problem for months
    while the env fallback quietly covered it.

    Trying the id as given first keeps SOCIALCRAWL_API_KEY a single call.
    """
    hyphenated = secret_id.lower().replace("_", "-")
    return [secret_id] if hyphenated == secret_id else [secret_id, hyphenated]


def get_secret(secret_id: str, default: str = "") -> str:
    """Retrieve a secret. Uses Secret Manager in staging/prod, .env locally."""
    sm_error: Exception | None = None
    if _use_secret_manager():
        client = _get_secret_client()
        if client:
            project = os.environ.get("GCP_PROJECT")
            pinned = _pinned_name(secret_id)
            candidates = [pinned] if pinned else _secret_name_candidates(secret_id)
            version = PINNED_SECRET_VERSIONS[pinned] if pinned else "latest"
            for name_id in candidates:
                name = f"projects/{project}/secrets/{name_id}/versions/{version}"
                try:
                    response = client.access_secret_version(request={"name": name})
                    return response.payload.data.decode("UTF-8")
                except Exception as exc:
                    sm_error = exc

    # Fallback to environment variable. Cloud Run mounts these from the same
    # secrets, so a Secret Manager miss covered here is not a failure. Log it
    # at ERROR only when both sources come back empty, because an error nobody
    # can act on is what hides the error somebody can.
    env_key = secret_id.upper().replace("-", "_")
    value = os.environ.get(env_key, default)
    if sm_error is not None:
        if value:
            logger.debug(
                "Secret Manager miss for %s (%s); resolved from the environment instead",
                secret_id,
                sm_error,
            )
        else:
            logger.error("Secret Manager fetch failed for %s: %s", secret_id, sm_error)
    return value
