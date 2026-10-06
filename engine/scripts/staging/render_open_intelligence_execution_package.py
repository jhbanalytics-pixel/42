from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.staging.deploy_open_intelligence_job import (
    DeploymentError,
    resolve_secret_versions,
)
from src.analysis.open_intelligence import execution_approval
from src.analysis.open_intelligence.execution_origins import OriginRefusal, OriginRegistry

_HEX_64 = re.compile(r"[0-9a-f]{64}")
_MANIFEST_V1 = "open_intelligence_execution_manifest_v1"
_MANIFEST_V2 = "open_intelligence_execution_manifest_v2"
# The exact version literal selects the mode: retained v1 packaging is historical read,
# a v2 package is prepared for approval against the active trusted generation.
_MODE_BY_VERSION = {_MANIFEST_V1: "historical_read", _MANIFEST_V2: "new_approval"}


class ExecutionPackageRefusal(RuntimeError):
    pass


def _parse_cli(argv: tuple[str, ...]) -> tuple[Path, str, Path]:
    if (
        len(argv) != 6
        or argv[0] != "--manifest-file"
        or argv[2] != "--manifest-sha256"
        or argv[4] != "--output-file"
    ):
        raise ExecutionPackageRefusal("execution_approval_manifest_invalid")
    manifest_path = Path(argv[1])
    digest = argv[3]
    output_path = Path(argv[5])
    if (
        not manifest_path.is_absolute()
        or not output_path.is_absolute()
        or _HEX_64.fullmatch(digest) is None
    ):
        raise ExecutionPackageRefusal("execution_approval_manifest_invalid")
    return manifest_path, digest, output_path


def _active_generation():
    """Load the active trusted generation from packaged source.

    The generation module belongs to the authority adapter; an import failure refuses
    rather than falling back to another registry or digest.
    """
    try:
        from src.analysis.open_intelligence.execution_generations import active_generation
    except ImportError as exc:
        raise ExecutionPackageRefusal("execution_generation_unavailable") from exc
    try:
        return active_generation()
    except ValueError:
        raise
    except Exception as exc:
        raise ExecutionPackageRefusal("execution_generation_unavailable") from exc


def _require_generation(generation):
    registry = getattr(generation, "registry", None)
    digest = getattr(generation, "origin_registry_sha256", None)
    resource_digest = getattr(generation, "resource_manifest_sha256", None)
    resource_manifest = getattr(generation, "resource_manifest", None)
    if (
        type(registry) is not OriginRegistry
        or digest != registry.sha256
        or not isinstance(resource_digest, str)
        or _HEX_64.fullmatch(resource_digest) is None
        or not isinstance(resource_manifest, Mapping)
        or resource_manifest.get("origin_registry_sha256") != digest
    ):
        raise ExecutionPackageRefusal("execution_generation_unavailable")
    return generation


def _load_manifest(path: Path, digest: str, *, generation) -> tuple[dict[str, object], bytes, str]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutionPackageRefusal("execution_approval_manifest_invalid") from exc
    version = payload.get("manifest_version") if isinstance(payload, Mapping) else None
    mode = _MODE_BY_VERSION.get(version)
    if mode is None:
        raise ExecutionPackageRefusal("execution_approval_manifest_invalid")
    registry = _require_generation(generation).registry
    try:
        canonical = execution_approval.canonical_manifest_bytes(
            payload, mode=mode, registry=registry
        )
    except (execution_approval.ApprovalRefusal, OriginRefusal) as exc:
        raise ExecutionPackageRefusal("execution_approval_manifest_invalid") from exc
    if (
        raw != canonical
        or hashlib.sha256(canonical).hexdigest() != digest
        or execution_approval.manifest_sha256(payload, mode=mode, registry=registry) != digest
    ):
        raise ExecutionPackageRefusal("execution_approval_manifest_mismatch")
    return payload, canonical, mode


def _bootstrap_annotations(operation: str, digest: str) -> dict[str, str]:
    if operation != "bootstrap_migration_apply":
        return {}
    manifest_generation = os.getenv("OPEN_INTELLIGENCE_BOOTSTRAP_MANIFEST_GENERATION")
    signature_generation = os.getenv("OPEN_INTELLIGENCE_BOOTSTRAP_SIGNATURE_GENERATION")
    if (
        not isinstance(manifest_generation, str)
        or not manifest_generation.isdecimal()
        or int(manifest_generation) <= 0
        or not isinstance(signature_generation, str)
        or not signature_generation.isdecimal()
        or int(signature_generation) <= 0
    ):
        raise ExecutionPackageRefusal("execution_approval_bootstrap_unapproved")
    return {
        "42.ogilvy/bootstrap-manifest-sha256": digest,
        "42.ogilvy/bootstrap-manifest-generation": manifest_generation,
        "42.ogilvy/bootstrap-signature-generation": signature_generation,
    }


def _secret_versions(payload: Mapping[str, object], *, generation) -> dict[str, str]:
    # Retained v1 packaging keeps its original latest representation. A v2 package binds
    # each secret to exactly one approved numeric version from the selected resources.
    if payload["manifest_version"] != _MANIFEST_V2:
        return dict.fromkeys(payload["secrets"], "latest")
    try:
        return dict(
            resolve_secret_versions(
                payload["secrets"], resource_manifest=generation.resource_manifest
            )
        )
    except DeploymentError as exc:
        raise ExecutionPackageRefusal("execution_approval_secret_invalid") from exc


def _resource(payload: Mapping[str, object], digest: str, *, generation) -> dict[str, object]:
    annotations = {
        "42.ogilvy/execution-approval-sha256": digest,
        "42.ogilvy/source-sha": payload["source_sha"],
    }
    if payload["manifest_version"] == _MANIFEST_V2:
        annotations["42.ogilvy/origin-registry-sha256"] = generation.origin_registry_sha256
        annotations["42.ogilvy/resource-manifest-sha256"] = generation.resource_manifest_sha256
    else:
        annotations.update(_bootstrap_annotations(payload["operation"], digest))
    secret_versions = _secret_versions(payload, generation=generation)
    environment = [dict(item) for item in payload["environment"]]
    environment.extend(
        {
            "name": name,
            "valueFrom": {
                "secretKeyRef": {
                    "name": name,
                    "key": secret_versions[name],
                }
            },
        }
        for name in payload["secrets"]
    )
    return {
        "apiVersion": "run.googleapis.com/v1",
        "kind": "Job",
        "metadata": {
            "name": payload["job_resource"].rsplit("/", 1)[1],
        },
        "spec": {
            "template": {
                "metadata": {"annotations": annotations},
                "spec": {
                    "template": {
                        "spec": {
                            "serviceAccountName": payload["service_identity"],
                            "maxRetries": payload["max_retries"],
                            "timeoutSeconds": payload["timeout_seconds"],
                            "containers": [
                                {
                                    "image": payload["image_uri"],
                                    "command": list(payload["command"]),
                                    "args": list(payload["arguments"]),
                                    "env": environment,
                                }
                            ],
                        }
                    }
                },
            }
        },
    }


def _yaml_bytes(payload: Mapping[str, object], digest: str, *, generation) -> bytes:
    return yaml.safe_dump(
        _resource(payload, digest, generation=generation),
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=4096,
    ).encode("utf-8")


def _emit(payload: Mapping[str, object], *, error: bool = False) -> None:
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    (sys.stderr if error else sys.stdout).write(line + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    output_path = None
    created = False
    try:
        manifest_path, digest, output_path = _parse_cli(
            tuple(sys.argv[1:] if argv is None else argv)
        )
    except ExecutionPackageRefusal as exc:
        _emit({"error": str(exc)}, error=True)
        return 2
    try:
        generation = _active_generation()
        payload, _canonical, _mode = _load_manifest(manifest_path, digest, generation=generation)
        if payload.get("operation") == "brain_read":
            raise ExecutionPackageRefusal("execution_approval_manifest_invalid")
        rendered = _yaml_bytes(payload, digest, generation=generation)
        with output_path.open("xb") as handle:
            created = True
            handle.write(rendered)
        if output_path.read_bytes() != rendered:
            raise ExecutionPackageRefusal("execution_approval_schema_mismatch")
        _emit(
            {
                "contract_version": "open_intelligence_execution_package_v1",
                "manifest_sha256": digest,
                "operation": payload["operation"],
                "output_file": str(output_path),
                "resource_digest": hashlib.sha256(rendered).hexdigest(),
            }
        )
        return 0
    except FileExistsError:
        _emit({"error": "execution_approval_conflict"}, error=True)
        return 1
    except ExecutionPackageRefusal as exc:
        if created and output_path is not None and output_path.exists():
            try:
                if output_path.stat().st_size == 0:
                    output_path.unlink()
            except OSError:
                pass
        _emit({"error": str(exc)}, error=True)
        return 1
    except Exception:
        _emit({"error": "execution_approval_internal_refusal"}, error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
