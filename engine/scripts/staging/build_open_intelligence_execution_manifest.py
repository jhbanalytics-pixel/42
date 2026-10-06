"""Build one validated canonical Open Intelligence execution manifest."""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.analysis.open_intelligence.execution_approval import (
    ApprovalRefusal,
    ExecutionManifest,
    canonical_manifest_bytes,
    manifest_sha256,
    validate_execution_manifest,
)
from src.analysis.open_intelligence.execution_origins import OriginRefusal, OriginRegistry

_CLI_FLAGS = ("--request-file", "--output-file")
_RECEIPT_VERSION = "open_intelligence_execution_manifest_build_v1"
_CLI_ERROR = "execution_approval_cli_invalid"
_MANIFEST_ERROR = "execution_approval_manifest_invalid"
_MODE_ERROR = "execution_origin_mode_forbidden"
_GENERATION_ERROR = "execution_generation_unavailable"
_MANIFEST_V1 = "open_intelligence_execution_manifest_v1"
_MANIFEST_V2 = "open_intelligence_execution_manifest_v2"
# The exact version literal selects the mode. Retained v1 bytes are read validation
# only; a v2 manifest is prepared for approval against the active trusted generation.
_MODE_BY_VERSION = {_MANIFEST_V1: "historical_read", _MANIFEST_V2: "new_approval"}


class ManifestBuilderRefusal(ValueError):
    def __init__(self, code: str, exit_code: int = 1):
        super().__init__(code)
        self.exit_code = exit_code


def _emit(stream, payload: dict[str, object]) -> None:
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    stream.write(f"{rendered}\n")


def _parse_exact_cli(argv: Sequence[str]) -> tuple[Path, Path]:
    values = tuple(argv)
    if (
        len(values) != 4
        or values[0::2] != _CLI_FLAGS
        or any(type(value) is not str or not value for value in values[1::2])
    ):
        raise ManifestBuilderRefusal(_CLI_ERROR, exit_code=2)
    request_path, output_path = (Path(value) for value in values[1::2])
    if not request_path.is_absolute() or not output_path.is_absolute():
        raise ManifestBuilderRefusal(_CLI_ERROR, exit_code=2)
    return request_path, output_path


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ManifestBuilderRefusal(_MANIFEST_ERROR)
        payload[key] = value
    return payload


def _reject_nonfinite(_value: str) -> None:
    raise ManifestBuilderRefusal(_MANIFEST_ERROR)


def _load_request(request_path: Path) -> object:
    try:
        raw = request_path.read_bytes()
    except OSError as error:
        raise ManifestBuilderRefusal("execution_approval_manifest_unreadable") from error
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ManifestBuilderRefusal(_MANIFEST_ERROR)
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except ManifestBuilderRefusal:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestBuilderRefusal(_MANIFEST_ERROR) from error


def _remove_created_empty_output(output_path: Path, created: bool) -> None:
    if not created:
        return
    try:
        if output_path.stat().st_size == 0:
            output_path.unlink()
    except OSError:
        return


def _write_canonical(output_path: Path, canonical: bytes) -> None:
    created = False
    try:
        with output_path.open("xb") as handle:
            created = True
            handle.write(canonical)
    except FileExistsError as error:
        raise ManifestBuilderRefusal("execution_approval_output_exists") from error
    except OSError as error:
        _remove_created_empty_output(output_path, created)
        raise ManifestBuilderRefusal("execution_approval_manifest_write_failed") from error
    try:
        written = output_path.read_bytes()
    except OSError as error:
        raise ManifestBuilderRefusal("execution_approval_manifest_mismatch") from error
    if written != canonical:
        raise ManifestBuilderRefusal("execution_approval_manifest_mismatch")


def _active_generation():
    """Load the active trusted generation from packaged source.

    The generation module is owned by the authority adapter; an import failure is a
    refusal, never a fallback to another registry or digest.
    """
    try:
        from src.analysis.open_intelligence.execution_generations import active_generation
    except ImportError as error:
        raise ManifestBuilderRefusal(_GENERATION_ERROR) from error
    try:
        return active_generation()
    except ValueError:
        raise
    except Exception as error:
        raise ManifestBuilderRefusal(_GENERATION_ERROR) from error


def _registry_of(generation) -> OriginRegistry:
    registry = getattr(generation, "registry", None)
    if (
        type(registry) is not OriginRegistry
        or getattr(generation, "origin_registry_sha256", None) != registry.sha256
    ):
        raise ManifestBuilderRefusal(_GENERATION_ERROR)
    return registry


def _admit_manifest(
    payload: object, *, mode: str, registry: OriginRegistry
) -> tuple[ExecutionManifest, bytes, str]:
    version = payload.get("manifest_version") if isinstance(payload, Mapping) else None
    if _MODE_BY_VERSION.get(version) != mode:
        raise ManifestBuilderRefusal(_MODE_ERROR)
    manifest = validate_execution_manifest(payload, mode=mode, registry=registry)
    canonical = canonical_manifest_bytes(payload, mode=mode, registry=registry)
    digest = manifest_sha256(payload, mode=mode, registry=registry)
    if manifest.manifest_version != version or digest != hashlib.sha256(canonical).hexdigest():
        raise ManifestBuilderRefusal("execution_approval_manifest_mismatch")
    return manifest, canonical, digest


def _build(argv: Sequence[str]) -> dict[str, object]:
    request_path, output_path = _parse_exact_cli(argv)
    if output_path.exists():
        raise ManifestBuilderRefusal("execution_approval_output_exists")
    payload = _load_request(request_path)
    version = payload.get("manifest_version") if isinstance(payload, Mapping) else None
    mode = _MODE_BY_VERSION.get(version)
    if mode is None:
        raise ManifestBuilderRefusal(_MANIFEST_ERROR)
    registry = _registry_of(_active_generation())
    manifest, canonical, digest = _admit_manifest(payload, mode=mode, registry=registry)
    _write_canonical(output_path, canonical)
    return {
        "contract_version": _RECEIPT_VERSION,
        "manifest_file": str(output_path),
        "manifest_sha256": digest,
        "operation": manifest.operation,
    }


def main(argv: Sequence[str] | None = None) -> int:
    values = tuple(sys.argv[1:] if argv is None else argv)
    try:
        receipt = _build(values)
    except ManifestBuilderRefusal as error:
        _emit(sys.stderr, {"error": str(error)})
        return error.exit_code
    except (ApprovalRefusal, OriginRefusal) as error:
        _emit(sys.stderr, {"error": str(error)})
        return 1
    except Exception:
        _emit(sys.stderr, {"error": "execution_approval_manifest_builder_internal"})
        return 1
    _emit(sys.stdout, receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
