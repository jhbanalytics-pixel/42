"""Runtime isolation checks for the approved Open Intelligence staging service."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from urllib.request import Request, urlopen


STAGING_PROFILE = "open-intelligence-staging"
METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email"
)
METADATA_TIMEOUT_SECONDS = 2.0
REQUIRED_VALUES = {
    "K_SERVICE": "listening-post-staging",
    "BQ_DATASET": "trends_v2_staging",
    "CACHE_BUCKET": "listening-post-staging-cache",
    "CACHE_PREFIX": "open-intelligence/v2/staging/",
    "APPLICATION_SOURCE": "open-intelligence-staging",
}
REQUIRED_SERVICE_ACCOUNT = "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
METADATA_SERVICE_ACCOUNT_RE = re.compile(r"[^@\s]+@[^@\s]+\.gserviceaccount\.com\Z")


class DeploymentContractError(RuntimeError):
    """Raised before serving when the staging process crosses its isolation fence."""


def _metadata_service_account() -> str:
    request = Request(METADATA_IDENTITY_URL, headers={"Metadata-Flavor": "Google"})
    with urlopen(request, timeout=METADATA_TIMEOUT_SECONDS) as response:  # noqa: S310
        if response.headers.get("Metadata-Flavor") != "Google":
            raise ValueError("metadata response omitted Metadata-Flavor")
        service_account = response.read().decode("utf-8").strip()
        if not METADATA_SERVICE_ACCOUNT_RE.fullmatch(service_account):
            raise ValueError("metadata response body is not a service account email")
        return service_account


def _unsafe_vendor_secret_names(env: Mapping[str, str]) -> list[str]:
    return sorted(
        name
        for name, value in env.items()
        if value
        and name != "UI_PASSCODE"
        and (name.endswith("_API_KEY") or name.endswith("_ACCESS_TOKEN") or name.endswith("_SECRET"))
    )


def _valid_cache_prefix(prefix: str) -> bool:
    parts = prefix.split("/")
    return bool(prefix) and prefix.endswith("/") and not prefix.startswith("/") and "\\" not in prefix and all(
        part not in {".", ".."} for part in parts
    )


def validate_runtime_contract(
    env: Mapping[str, str] | None = None,
    metadata_email_getter: Callable[[], str] | None = None,
) -> None:
    """Fail closed only for the dedicated staging deployment profile."""
    runtime_env = os.environ if env is None else env
    if runtime_env.get("DEPLOYMENT_PROFILE") != STAGING_PROFILE:
        return

    for name, expected in REQUIRED_VALUES.items():
        actual = runtime_env.get(name, "")
        if actual != expected:
            raise DeploymentContractError(f"{name} must be {expected!r}, got {actual!r}")
    if not _valid_cache_prefix(runtime_env.get("CACHE_PREFIX", "")):
        raise DeploymentContractError("CACHE_PREFIX is unsafe")
    source_sha = runtime_env.get("SOURCE_SHA", "")
    if not SOURCE_SHA_RE.fullmatch(source_sha):
        raise DeploymentContractError("SOURCE_SHA must be a full 40 character lowercase commit SHA")

    vendor_secrets = _unsafe_vendor_secret_names(runtime_env)
    if vendor_secrets:
        raise DeploymentContractError(f"vendor secret environment variables are forbidden: {', '.join(vendor_secrets)}")

    get_metadata_email = metadata_email_getter or _metadata_service_account
    try:
        metadata_email = get_metadata_email().strip()
    except Exception as exc:
        raise DeploymentContractError("service account metadata lookup failed") from exc
    if metadata_email != REQUIRED_SERVICE_ACCOUNT:
        raise DeploymentContractError(
            f"service account must be {REQUIRED_SERVICE_ACCOUNT!r}, got {metadata_email!r}"
        )


def validate_workspace_runtime_contract(
    env: Mapping[str, str] | None = None,
    metadata_email_getter: Callable[[], str] | None = None,
) -> None:
    runtime_env = os.environ if env is None else env
    if runtime_env.get("DEPLOYMENT_PROFILE") != STAGING_PROFILE:
        raise DeploymentContractError("workspace reads require the staging deployment profile")
    validate_runtime_contract(runtime_env, metadata_email_getter)
