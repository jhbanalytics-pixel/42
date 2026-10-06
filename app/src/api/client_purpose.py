"""The client purpose policy at the export and review boundary.

The policy is the ``purpose_policy`` block of the engine's client overlay for the
scope's ``brand_config_id``; general 42 names no brand and this module returns
no policy for it. The app reads the same document the engine compiles, from the
engine tree beside the app in development and from the bundled engine tree in
the image, and pins the block by its canonical digest so both sides enforce one
policy version. A prohibited purpose blocks artifact preparation before any
digest exists, and no review role can move a resource past it.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parents[2]
OVERLAY_DIRS = (
    ROOT_DIR.parent / "engine" / "configs" / "topic_overlays",
    Path("/app/engine/configs/topic_overlays"),
)
POLICY_VERSION = "client_purpose_policy_v1"
PURPOSE_CODES = (
    "political_party_purpose",
    "electoral_purpose",
    "voter_targeting_purpose",
    "agency_position_live_regulatory_matter",
)
EXPORT_REFUSED = "client_purpose_refused_at_export"
REVIEW_OVERRIDE_REFUSED = "client_purpose_review_override_refused"
_BRAND = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class ClientPurposePolicyUnavailable(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ClientPurposeProhibited(Exception):
    """A prohibited client purpose at one boundary; carries the purposes matched."""

    def __init__(self, code: str, evaluation: Mapping[str, object]) -> None:
        self.code = code
        self.purpose_codes = tuple(evaluation["purpose_codes"])
        self.policy_version = evaluation["policy_version"]
        self.evaluation = dict(evaluation)
        super().__init__(code)


def _canonical_digest(value: object) -> str:
    data = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _patterns(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ClientPurposePolicyUnavailable("client_purpose_policy_invalid")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ClientPurposePolicyUnavailable("client_purpose_policy_invalid")
        try:
            re.compile(item)
        except re.error:
            raise ClientPurposePolicyUnavailable(
                "client_purpose_policy_invalid"
            ) from None
    return list(value)


def validate_client_purpose_policy(value: object) -> dict:
    """The purpose policy block, exactly the engine compiler's shape, with its digest."""
    if (
        not isinstance(value, dict)
        or set(value) != {"policy_version", "source_definition", "prohibited_purposes"}
        or value["policy_version"] != POLICY_VERSION
        or not isinstance(value["source_definition"], str)
        or not value["source_definition"].strip()
        or not isinstance(value["prohibited_purposes"], list)
    ):
        raise ClientPurposePolicyUnavailable("client_purpose_policy_invalid")
    purposes = value["prohibited_purposes"]
    codes = [p.get("code") for p in purposes if isinstance(p, dict)]
    if codes != list(PURPOSE_CODES):
        raise ClientPurposePolicyUnavailable("client_purpose_policy_invalid")
    for purpose in purposes:
        if purpose["code"] == "agency_position_live_regulatory_matter":
            if set(purpose) != {
                "code",
                "position_claim_patterns",
                "regulatory_matter_patterns",
            }:
                raise ClientPurposePolicyUnavailable("client_purpose_policy_invalid")
            _patterns(purpose["position_claim_patterns"])
            _patterns(purpose["regulatory_matter_patterns"])
        else:
            if set(purpose) != {"code", "patterns"}:
                raise ClientPurposePolicyUnavailable("client_purpose_policy_invalid")
            _patterns(purpose["patterns"])
    return {**json.loads(json.dumps(value)), "policy_digest": _canonical_digest(value)}


def _overlay_path(brand_config_id: str) -> Path:
    for directory in OVERLAY_DIRS:
        path = directory / f"{brand_config_id}.yaml"
        if path.is_file():
            return path
    raise ClientPurposePolicyUnavailable("client_purpose_policy_unavailable")


def load_client_purpose_policy(brand_config_id: object) -> dict | None:
    """The brand's purpose policy, or None for general 42, which names no brand."""
    if brand_config_id is None:
        return None
    if (
        not isinstance(brand_config_id, str)
        or _BRAND.fullmatch(brand_config_id) is None
    ):
        raise ClientPurposePolicyUnavailable("client_purpose_policy_unavailable")
    path = _overlay_path(brand_config_id)
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raise ClientPurposePolicyUnavailable(
            "client_purpose_policy_unavailable"
        ) from None
    if (
        not isinstance(document, dict)
        or document.get("brand_config_id") != brand_config_id
        or "purpose_policy" not in document
    ):
        raise ClientPurposePolicyUnavailable("client_purpose_policy_unavailable")
    return validate_client_purpose_policy(document["purpose_policy"])


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _first(patterns: list[str], text: str) -> re.Match[str] | None:
    for pattern in patterns:
        match = _compiled(pattern).search(text)
        if match is not None:
            return match
    return None


def evaluate_client_purpose(policy: Mapping[str, object], texts: list[str]) -> dict:
    """Which prohibited purposes the texts serve; the same rule the engine applies."""
    evidence = []
    for index, text in enumerate(texts):
        if not isinstance(text, str) or not text.strip():
            continue
        for purpose in policy["prohibited_purposes"]:
            if purpose["code"] == "agency_position_live_regulatory_matter":
                position = _first(purpose["position_claim_patterns"], text)
                matter = _first(purpose["regulatory_matter_patterns"], text)
                if position is not None and matter is not None:
                    evidence.append(
                        {
                            "purpose_code": purpose["code"],
                            "text_index": index,
                            "matched": [position.group(0), matter.group(0)],
                        }
                    )
                continue
            match = _first(purpose["patterns"], text)
            if match is not None:
                evidence.append(
                    {
                        "purpose_code": purpose["code"],
                        "text_index": index,
                        "matched": [match.group(0)],
                    }
                )
    codes = sorted({item["purpose_code"] for item in evidence})
    return {
        "policy_version": policy["policy_version"],
        "state": "prohibited" if codes else "permitted",
        "purpose_codes": codes,
        "evidence": evidence,
        "review_override": "refused",
    }


def client_read_texts(client_read: Mapping[str, object]) -> list[str]:
    """Every text the export renders: the answer, each finding and each unresolved question."""
    texts = []
    answer = client_read.get("concise_answer")
    if isinstance(answer, str):
        texts.append(answer)
    claims = client_read.get("claims")
    if isinstance(claims, list):
        for item in claims:
            if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                texts.append(item["text"])
    questions = client_read.get("unresolved_questions")
    if isinstance(questions, list):
        for item in questions:
            if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                texts.append(item["text"])
    return texts


def refuse_prohibited_export(
    policy: Mapping[str, object] | None, client_read: Mapping
) -> dict | None:
    """Evaluate the export payload; a prohibited purpose refuses before any digest exists."""
    if policy is None:
        return None
    evaluation = evaluate_client_purpose(policy, client_read_texts(client_read))
    if evaluation["state"] == "prohibited":
        raise ClientPurposeProhibited(EXPORT_REFUSED, evaluation)
    return evaluation


__all__ = [
    "EXPORT_REFUSED",
    "POLICY_VERSION",
    "PURPOSE_CODES",
    "REVIEW_OVERRIDE_REFUSED",
    "ClientPurposePolicyUnavailable",
    "ClientPurposeProhibited",
    "client_read_texts",
    "evaluate_client_purpose",
    "load_client_purpose_policy",
    "refuse_prohibited_export",
    "validate_client_purpose_policy",
]
