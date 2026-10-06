"""Deterministic text restrictions for Gemini canary outputs."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

_STRUCTURAL_STRING_FIELDS = frozenset(
    {
        "audience_lens_ids",
        "claim_ids",
        "evidence_ids",
        "plan_id",
        "required_source_families",
        "source_families",
        "supporting_claim_ids",
        "url",
    }
)

_DEMOGRAPHIC = re.compile(
    r"\b(?:women|woman|men|man|female|male|gender|demographic|aged?\s+\d+|"
    r"\d+\s*(?:to|-)\s*\d+\s*(?:years?\s+old)?|gen\s*z|millennials?)\b",
    re.IGNORECASE,
)
_CAUSAL = re.compile(
    r"\b(?:cause|causes|caused|causing|causal|drives?|driven\s+by|leads?\s+to|"
    r"results?\s+in|because\s+of)\b",
    re.IGNORECASE,
)
_ELECTION = re.compile(
    r"\b(?:vote\s+for|vote\s+against|should\s+vote|must\s+vote|"
    r"endorse\s+(?:the\s+)?candidate|support\s+(?:the\s+)?candidate|"
    r"oppose\s+(?:the\s+)?candidate|polling\s+(?:shows?|proves?|indicates?)|"
    r"polls?\s+(?:show|prove|indicate)|majority\s+of\s+voters)\b|"
    r"\b\d+(?:\.\d+)?\s*(?:%|percent)\s+(?:support|oppose|voters?)\b",
    re.IGNORECASE,
)


def _measured_lenses(value: Iterable[Mapping[str, object]]) -> frozenset[str]:
    return frozenset(
        item["lens_id"]
        for item in value
        if item.get("basis") == "measured" and isinstance(item.get("lens_id"), str)
    )


def validate_claim_semantics(
    claims: Iterable[Mapping[str, object]],
    *,
    text_field: str,
    audience_lenses: Iterable[Mapping[str, object]],
) -> None:
    for claim in claims:
        text = claim.get(text_field)
        if not isinstance(text, str):
            raise ValueError("claim text is invalid")
        if _DEMOGRAPHIC.search(text):
            raise ValueError("unsupported demographic language")
        if _CAUSAL.search(text):
            raise ValueError("unsupported causal language")


def _is_prose_string(schema: Mapping[str, object], path: str) -> bool:
    field = path.removesuffix("[]").rsplit(".", 1)[-1]
    return (
        not any(key in schema for key in ("enum", "pattern", "format"))
        and field not in _STRUCTURAL_STRING_FIELDS
    )


def _schema_paths(schema: Mapping[str, object], path: str) -> tuple[str, ...]:
    schema_type = schema.get("type")
    if schema_type == "STRING":
        return (path,) if _is_prose_string(schema, path) else ()
    if schema_type == "ARRAY":
        items = schema.get("items")
        return _schema_paths(items, path + "[]") if isinstance(items, Mapping) else ()
    if schema_type != "OBJECT":
        return ()
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return ()
    output = []
    for field, child in properties.items():
        if isinstance(child, Mapping):
            output.extend(_schema_paths(child, f"{path}.{field}" if path else field))
    return tuple(output)


def schema_prose_paths(schema: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(sorted(_schema_paths(schema, "")))


def _extract(
    value: object,
    schema: Mapping[str, object],
    path: str,
) -> tuple[tuple[str, str], ...]:
    schema_type = schema.get("type")
    if schema_type == "STRING":
        if _is_prose_string(schema, path) and isinstance(value, str):
            return ((path, value),)
        return ()
    if schema_type == "ARRAY":
        items = schema.get("items")
        if not isinstance(value, list) or not isinstance(items, Mapping):
            return ()
        return tuple(result for item in value for result in _extract(item, items, path + "[]"))
    if schema_type != "OBJECT" or not isinstance(value, Mapping):
        return ()
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return ()
    output = []
    for field, child in properties.items():
        if field in value and isinstance(child, Mapping):
            output.extend(_extract(value[field], child, f"{path}.{field}" if path else field))
    return tuple(output)


def extract_generated_prose(
    output: Mapping[str, object], schema: Mapping[str, object]
) -> tuple[tuple[str, str], ...]:
    return _extract(output, schema, "")


def validate_generated_prose(
    output: Mapping[str, object],
    schema: Mapping[str, object],
    *,
    audience_lenses: Iterable[Mapping[str, object]],
    election: bool,
) -> None:
    for _path, text in extract_generated_prose(output, schema):
        if _DEMOGRAPHIC.search(text):
            raise ValueError("unsupported demographic language")
        if _CAUSAL.search(text):
            raise ValueError("unsupported causal language")
        if election and _ELECTION.search(text):
            raise ValueError("unsupported election language")


__all__ = [
    "extract_generated_prose",
    "schema_prose_paths",
    "validate_claim_semantics",
    "validate_generated_prose",
]
