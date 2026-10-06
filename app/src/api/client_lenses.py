"""Authorized client lenses: the optional configuration a request may name.

General 42 is the default and names no lens. A lens is authorized by a registry
entry naming the client scope it belongs to and the brand configuration it
carries, never by matching a brand name, so a console selection can only ever
reach an entry that already exists for the caller's own scope. The entry also
pins the digest of the configuration document the engine holds; the app carries
that digest to every boundary and into the admission envelope, and the engine
re-resolves it against the bytes it reads before the configuration is used.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parents[2]
CLIENT_LENSES_PATH = ROOT_DIR / "configs" / "client_lenses.yaml"
CONFIG_DIGEST = "e1a2727e5bd95c260a49c56a94d088ca6039f130825fbb887763253777d0f410"
REGISTRY_VERSION = "client_lens_registry_v1"
LENS_BINDING_VERSION = "client_lens_binding_v1"
DEFAULT_CLIENT_LENS_ID = None
ENTRY_FIELDS = {
    "client_lens_id",
    "label",
    "client_scope_id",
    "brand_config_id",
    "overlay_id",
    "configuration_digest",
    "enabled",
}


class ClientLensUnavailable(ValueError):
    def __init__(self, code: str = "client_lens_unavailable") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ResolvedClientLens:
    client_lens_id: str
    label: str
    client_scope_id: str
    brand_config_id: str
    configuration_digest: str

    @property
    def envelope(self) -> dict[str, str]:
        """What the admission envelope carries; the engine re-resolves both values."""
        return {
            "lens_binding_version": LENS_BINDING_VERSION,
            "client_lens_id": self.client_lens_id,
            "configuration_digest": self.configuration_digest,
        }


def _invalid(code: str = "client_lens_unavailable") -> None:
    raise ClientLensUnavailable(code)


def _load(path: str | Path | None) -> dict:
    location = CLIENT_LENSES_PATH if path is None else Path(path)
    try:
        raw_bytes = location.read_bytes()
    except OSError:
        _invalid("client_lens_registry_invalid")
    if hashlib.sha256(raw_bytes).hexdigest() != CONFIG_DIGEST:
        _invalid("client_lens_registry_invalid")
    try:
        document = yaml.safe_load(raw_bytes)
    except yaml.YAMLError:
        _invalid("client_lens_registry_invalid")
    if not isinstance(document, dict) or set(document) != {
        "contract_version",
        "default_client_lens_id",
        "lenses",
    }:
        _invalid("client_lens_registry_invalid")
    if (
        document["contract_version"] != REGISTRY_VERSION
        or document["default_client_lens_id"] is not DEFAULT_CLIENT_LENS_ID
        or not isinstance(document["lenses"], dict)
        or not document["lenses"]
    ):
        _invalid("client_lens_registry_invalid")
    for key, entry in document["lenses"].items():
        if (
            not isinstance(entry, dict)
            or set(entry) != ENTRY_FIELDS
            or entry["client_lens_id"] != key
            or entry["enabled"] not in (True, False)
        ):
            _invalid("client_lens_registry_invalid")
        for field in ("client_lens_id", "label", "client_scope_id", "brand_config_id"):
            if not isinstance(entry[field], str) or not entry[field].strip():
                _invalid("client_lens_registry_invalid")
    return document


def authorized_client_lenses(
    *, client_scope_id: object, path: str | Path | None = None
) -> list[dict[str, str]]:
    """The lenses this client scope may choose; general 42 is the unnamed default."""
    document = _load(path)
    return [
        {
            "client_lens_id": entry["client_lens_id"],
            "label": entry["label"],
            "configuration_digest": entry["configuration_digest"],
        }
        for entry in document["lenses"].values()
        if entry["enabled"] and entry["client_scope_id"] == client_scope_id
    ]


def resolve_client_lens(
    *,
    client_scope_id: object,
    brand_config_id: object,
    client_lens_id: object,
    path: str | Path | None = None,
) -> ResolvedClientLens | None:
    """Resolve one named lens under this client scope, or general 42 when none is named."""
    if client_lens_id is DEFAULT_CLIENT_LENS_ID:
        return None
    if type(client_lens_id) is not str or not client_lens_id:
        _invalid("client_lens_id_invalid")
    document = _load(path)
    entry = document["lenses"].get(client_lens_id)
    if (
        entry is None
        or not entry["enabled"]
        or type(client_scope_id) is not str
        or entry["client_scope_id"] != client_scope_id
    ):
        _invalid("client_lens_unauthorized")
    if entry["brand_config_id"] != brand_config_id:
        _invalid("client_lens_binding_invalid")
    return ResolvedClientLens(
        client_lens_id=entry["client_lens_id"],
        label=entry["label"],
        client_scope_id=entry["client_scope_id"],
        brand_config_id=entry["brand_config_id"],
        configuration_digest=entry["configuration_digest"],
    )


def registered_label(
    client_lens_id: object,
    configuration_digest: object,
    *,
    client_scope_id: object,
    path: str | Path | None = None,
) -> str | None:
    """The reader's name for a stored lens binding, or None when the registry cannot vouch for it.

    A label is only given when the entry still pins the exact configuration the
    binding carries, so a copy of an old answer never borrows the name of a
    configuration that has since changed. It is also only given while the entry
    is enabled and belongs to the client scope the stored request was read
    under, the same entries resolve_client_lens admits, so a disabled lens or
    another client's lens is named by its stored identity alone. An unreadable
    registry names nothing.
    """
    try:
        entry = _load(path)["lenses"].get(client_lens_id)
    except ClientLensUnavailable:
        return None
    if (
        entry is None
        or not entry["enabled"]
        or type(client_scope_id) is not str
        or entry["client_scope_id"] != client_scope_id
        or entry["configuration_digest"] != configuration_digest
    ):
        return None
    return entry["label"]
