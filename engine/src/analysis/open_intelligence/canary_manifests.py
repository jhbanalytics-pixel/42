"""Validated loader for the approved Gemini caller manifest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.utils.config_loader import CONFIGS_DIR

CANONICAL_MANIFEST_PATH = CONFIGS_DIR / "open_intelligence" / "gemini_3_7_caller_manifests_v1.json"
CANONICAL_MANIFEST_SHA256 = "0f90586306fae9932b88424a16699182e4fc3ecbda47015a63bb3491dbb833c7"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_canonical_manifest(path: Path | None = None) -> dict[str, object]:
    selected = CANONICAL_MANIFEST_PATH if path is None else Path(path)
    raw = selected.read_bytes()
    if path is None and hashlib.sha256(raw).hexdigest() != CANONICAL_MANIFEST_SHA256:
        raise ValueError("canonical manifest file digest is invalid")
    payload = json.loads(raw.decode("utf-8"))
    if (
        payload.get("manifest_version") != "caller_manifest_v1"
        or payload.get("task_count") != len(payload.get("tasks", ()))
        or payload.get("task_count") != 11
        or payload.get("stage_count") != len(payload.get("stages", ()))
        or payload.get("stage_count") != 23
    ):
        raise ValueError("canonical manifest shape is invalid")
    stage_ids = set()
    for stage in payload["stages"]:
        canonical = _canonical(stage["envelope"])
        if (
            stage["canonical_envelope_json"] != canonical
            or stage["envelope_sha256"] != hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            or stage["input_sha256"]
            != hashlib.sha256(stage["contents"].encode("utf-8")).hexdigest()
        ):
            raise ValueError("canonical manifest stage digest is invalid")
        if stage["stage_manifest_id"] in stage_ids:
            raise ValueError("canonical manifest stage identity is duplicated")
        stage_ids.add(stage["stage_manifest_id"])
    return payload


def stage_manifest(stage_manifest_id: str) -> dict[str, object]:
    matches = [
        stage
        for stage in load_canonical_manifest()["stages"]
        if stage["stage_manifest_id"] == stage_manifest_id
    ]
    if len(matches) != 1:
        raise ValueError("canonical stage manifest is missing or duplicated")
    return matches[0]


__all__ = [
    "CANONICAL_MANIFEST_PATH",
    "CANONICAL_MANIFEST_SHA256",
    "load_canonical_manifest",
    "stage_manifest",
]
