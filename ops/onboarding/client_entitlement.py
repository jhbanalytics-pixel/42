import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import jsonschema

TOP_PROOF_LEAVES = (
    "commercial_approval_ref",
    "expires_at",
    "funding_basis_ref",
    "permitted_uses",
    "retention_policy_ref",
)
_FEATURES = {
    "ai_answer_citation",
    "print",
    "radio",
    "search_demand",
    "search_rank",
    "social",
    "television",
}
_STATE_PRECEDENCE = ("revoked", "expired", "unproven", "current")
_SECRET_VERSION_PARTS = re.compile(
    r"^projects/(?:[a-z][a-z0-9-]{4,28}[a-z0-9]|[1-9][0-9]{5,})/"
    r"secrets/[A-Za-z0-9_-]{1,255}/versions/([^/]+)$"
)
_UTC_INSTANT = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z$")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ERROR_CODES = {
    "authorization_expired",
    "authorization_revoked",
    "contract_cell_invalid",
    "credential_declared_client_mismatch",
    "credential_resource_invalid",
    "credential_source_mismatch",
    "document_not_object",
    "duplicate_value",
    "invalid_client_scope_id",
    "invalid_contract_version",
    "invalid_document",
    "invalid_language_tag",
    "invalid_market_code",
    "invalid_proof_status",
    "invalid_type",
    "missing_field",
    "proof_expiry_required",
    "proof_ref_required",
    "raw_secret_forbidden",
    "requirements_incomplete",
    "secret_version_not_pinned",
    "unknown_field",
}
_SCHEMA_PATH = (
    Path(__file__).parents[2] / "engine/configs/client_onboarding.schema.json"
)
_SCHEMA = json.loads(_SCHEMA_PATH.read_bytes())
jsonschema.Draft202012Validator.check_schema(_SCHEMA)
_VALIDATOR = jsonschema.Draft202012Validator(
    _SCHEMA, format_checker=jsonschema.FormatChecker()
)


def _utc_now():
    return datetime.now(timezone.utc)


def _pointer(parts):
    encoded = []
    for part in parts:
        value = str(part).replace("~", "~0").replace("/", "~1")
        encoded.append(value)
    return "" if not encoded else "/" + "/".join(encoded)


def _error(code, path=""):
    if code not in _ERROR_CODES:
        code = "invalid_document"
    return {"code": code, "path": path}


def _invalid(errors):
    ordered = sorted(
        {(item["path"], item["code"]) for item in errors},
        key=lambda item: (item[0], item[1]),
    )
    if not ordered:
        ordered = [("", "invalid_document")]
    return {
        "contract_version": "client_entitlement_validation_v1",
        "structure_valid": False,
        "client_scope_id": None,
        "document_sha256": None,
        "document_claim_state": "invalid",
        "initial_digital_subset_uncovered_cells": [],
        "full_required_coverage_complete": False,
        "coverage_matrix_status": "invalid",
        "native_rights_verified": False,
        "activation_ready": False,
        "errors": [{"code": code, "path": path} for path, code in ordered],
    }


def _schema_error_code(error, document):
    path = list(error.absolute_path)
    if error.validator == "additionalProperties":
        return _error("unknown_field", _pointer(path))
    if error.validator == "required":
        return _error("missing_field", _pointer(path))
    leaf = path[-1] if path else None
    if leaf == "contract_version":
        return _error("invalid_contract_version", _pointer(path))
    if path == ["client_scope_id"]:
        return _error("invalid_client_scope_id", _pointer(path))
    if "supported_languages" in path and leaf in {"tag", 0, 1, 2, 3, 4, 5}:
        return _error("invalid_language_tag", _pointer(path[:-1]))
    if "source_credentials_refs" in path and leaf == "resource":
        value = document
        try:
            for part in path:
                value = value[part]
        except (KeyError, IndexError, TypeError):
            value = None
        if isinstance(value, str) and not value.startswith("projects/"):
            return _error("raw_secret_forbidden", _pointer(path))
        matched = (
            _SECRET_VERSION_PARTS.fullmatch(value) if isinstance(value, str) else None
        )
        if (
            matched is not None
            and re.fullmatch(r"[1-9][0-9]*", matched.group(1)) is None
        ):
            return _error("secret_version_not_pinned", _pointer(path))
        return _error("credential_resource_invalid", _pointer(path))
    if any(
        part in {"proof", "verified_at", "expires_at", "revoked_at"} for part in path
    ):
        proof_index = path.index("proof") if "proof" in path else len(path) - 1
        return _error("invalid_proof_status", _pointer(path[: proof_index + 1]))
    if error.validator == "type":
        return _error("invalid_type", _pointer(path))
    return _error("invalid_document", _pointer(path[:-1] if path else path))


def _schema_errors(document):
    return [
        _schema_error_code(error, document)
        for error in sorted(
            _VALIDATOR.iter_errors(document),
            key=lambda item: (_pointer(item.absolute_path), str(item.validator)),
        )
    ]


def _safe_strings(value, path=()):
    errors = []
    if isinstance(value, str):
        if value != unicodedata.normalize("NFC", value) or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            errors.append(_error("invalid_document", _pointer(path[:-1])))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(_safe_strings(item, (*path, index)))
    elif type(value) is dict:
        for key, item in value.items():
            errors.extend(_safe_strings(key, path))
            errors.extend(_safe_strings(item, (*path, key)))
    return errors


def _duplicate_errors(document):
    errors = []

    def unique(records, key, path, *, ignore_null=False):
        values = [record[key] for record in records]
        if ignore_null:
            values = [value for value in values if value is not None]
        if len(values) != len(set(values)):
            errors.append(_error("duplicate_value", path))

    unique(document["source_rights"], "right_id", "/source_rights")
    unique(
        document["source_rights"],
        "source_id",
        "/source_rights",
        ignore_null=True,
    )
    unique(document["identity_assets"], "asset_id", "/identity_assets")
    unique(document["supported_languages"], "tag", "/supported_languages")
    unique(document["market_roles"]["roles"], "role_id", "/market_roles/roles")
    unique(
        document["comparator_roles"]["roles"],
        "role_id",
        "/comparator_roles/roles",
    )
    unique(
        document["search"]["benchmark_domains"],
        "service",
        "/search/benchmark_domains",
    )
    unique(
        document["search"]["benchmark_domains"],
        "canonical_domain",
        "/search/benchmark_domains",
    )
    return errors


def _semantic_errors(document):
    errors = _safe_strings(document)
    errors.extend(_duplicate_errors(document))
    contracted = set(document["contracted_markets"])
    roles = document["market_roles"]["roles"]
    required_union = set(document["market_roles"]["required_union"])
    if contracted != required_union or any(
        not set(role["markets"]) <= required_union for role in roles
    ):
        errors.append(_error("requirements_incomplete", "/market_roles"))
    comparator_roles = {
        row["role_id"]: set(row["markets"])
        for row in document["comparator_roles"]["roles"]
    }
    if comparator_roles.get("client_contract") != set(document["comparator_markets"]):
        errors.append(_error("requirements_incomplete", "/comparator_roles"))
    if document["comparator_roles"]["sets_are_distinct"] and len(
        {tuple(sorted(value)) for value in comparator_roles.values()}
    ) != len(comparator_roles):
        errors.append(_error("requirements_incomplete", "/comparator_roles"))

    source_ids = {
        row["source_id"]
        for row in document["source_rights"]
        if row["source_id"] is not None
    }
    for index, row in enumerate(document["source_rights"]):
        if row["source_id"] is None and row["proof"]["status"] != "unproven":
            errors.append(
                _error("invalid_proof_status", f"/source_rights/{index}/proof")
            )
    for index, row in enumerate(document["source_credentials_refs"]):
        base = f"/source_credentials_refs/{index}"
        if row["client_scope_id"] != document["client_scope_id"]:
            errors.append(_error("credential_declared_client_mismatch", base))
        if row["source_id"] not in source_ids:
            errors.append(_error("credential_source_mismatch", base))
        unproven = row["proof"]["status"] == "unproven"
        if (row["resource"] is None) != unproven:
            errors.append(_error("credential_resource_invalid", f"{base}/resource"))

    authority = {
        "funding_basis_ref": document["funding_basis_ref"],
        "permitted_uses": document["permitted_uses"],
        "retention_policy_ref": document["retention_policy_ref"],
        "commercial_approval_ref": document["commercial_approval_ref"],
        "expires_at": document["expires_at"],
    }
    for key, value in authority.items():
        status = document["proof"][key]["status"]
        if (value is None) != (status == "unproven"):
            errors.append(_error("invalid_proof_status", f"/proof/{key}"))
    for index, asset in enumerate(document["identity_assets"]):
        empty = asset["asset_ref"] is None and asset["rights_ref"] is None
        if empty != (asset["proof"]["status"] == "unproven"):
            errors.append(
                _error("invalid_proof_status", f"/identity_assets/{index}/proof")
            )

    subset = document["coverage"]["initial_digital_subset"]
    required = {tuple(cell) for cell in subset["required_cells"]}
    claimed = {tuple(cell) for cell in subset["document_claimed_current_cells"]}
    native = {tuple(cell) for cell in subset["native_verified_cells"]}
    expected = {
        (market.lower(), feature)
        for market in subset["markets"]
        for feature in subset["features"]
    }
    try:
        gaps = uncovered_contract_cells(required, claimed)
    except ValueError:
        errors.append(
            _error("contract_cell_invalid", "/coverage/initial_digital_subset")
        )
        gaps = []
    if (
        required != expected
        or not set(subset["markets"]) <= contracted
        or not native <= required
        or [list(cell) for cell in gaps] != subset["uncovered_cells"]
        or subset["required_cells"] != [list(cell) for cell in sorted(required)]
        or subset["document_claimed_current_cells"]
        != [list(cell) for cell in sorted(claimed)]
        or subset["native_verified_cells"] != [list(cell) for cell in sorted(native)]
        or subset["subset_complete"] != (not gaps)
    ):
        errors.append(
            _error("contract_cell_invalid", "/coverage/initial_digital_subset")
        )
    full = document["coverage"]["full_required_coverage"]
    status_claims_complete = (
        document["coverage"]["matrix_status"] == "document_claimed_complete"
    )
    if full["complete"] != status_claims_complete or (
        full["complete"]
        and (
            full["matrix_ref"] is None
            or full["required_cells"] is None
            or full["unresolved_features"]
            or full["open_dependencies"]
        )
    ):
        errors.append(
            _error("requirements_incomplete", "/coverage/full_required_coverage")
        )
    return errors


def _instant(value):
    matched = _UTC_INSTANT.fullmatch(value) if isinstance(value, str) else None
    if matched is None:
        raise ValueError("invalid_proof_status")
    try:
        whole = datetime.strptime(matched.group(1), "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise ValueError("invalid_proof_status") from error
    delta = whole - _EPOCH
    seconds = delta.days * 86_400 + delta.seconds
    fraction = int((matched.group(2) or "").ljust(9, "0") or "0")
    return seconds * 1_000_000_000 + fraction


def _clock_nanoseconds(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("invalid_proof_status")
    current = value.astimezone(timezone.utc)
    whole = current.replace(microsecond=0)
    delta = whole - _EPOCH
    seconds = delta.days * 86_400 + delta.seconds
    return seconds * 1_000_000_000 + current.microsecond * 1_000


def _proof_leaves(document):
    leaves = [(f"/proof/{key}", document["proof"][key]) for key in TOP_PROOF_LEAVES]
    collections = (
        ("/source_rights", document["source_rights"]),
        ("/source_credentials_refs", document["source_credentials_refs"]),
        ("/identity_assets", document["identity_assets"]),
        ("/search/benchmark_domains", document["search"]["benchmark_domains"]),
    )
    for prefix, records in collections:
        leaves.extend(
            (f"{prefix}/{index}/proof", record["proof"])
            for index, record in enumerate(records)
        )
    return leaves


def _effective_proof_state(proof, validation_time):
    status = proof["status"]
    if status == "unproven":
        return "unproven"
    verified = _instant(proof["verified_at"])
    expires = _instant(proof["expires_at"]) if proof["expires_at"] else None
    revoked = _instant(proof["revoked_at"]) if proof["revoked_at"] else None
    if verified > validation_time:
        raise ValueError("invalid_proof_status")
    if expires is not None and verified > expires:
        raise ValueError("invalid_proof_status")
    if revoked is not None and revoked > validation_time:
        raise ValueError("invalid_proof_status")
    if status == "expired" and (expires is None or expires > validation_time):
        raise ValueError("invalid_proof_status")
    if status == "revoked" and revoked is None:
        raise ValueError("invalid_proof_status")
    if revoked is not None:
        return "revoked"
    if expires is not None and expires <= validation_time:
        return "expired"
    return "current"


def _aggregate_state(document, validation_time):
    try:
        validation_nanoseconds = _clock_nanoseconds(validation_time)
    except ValueError:
        return None, [_error("invalid_proof_status")]
    states = []
    for path, proof in _proof_leaves(document):
        try:
            states.append(_effective_proof_state(proof, validation_nanoseconds))
        except ValueError:
            return None, [_error("invalid_proof_status", path)]
    if document["expires_at"] is not None:
        try:
            if _instant(document["expires_at"]) <= validation_nanoseconds:
                states.append("expired")
        except ValueError:
            return None, [_error("invalid_proof_status", "/proof/expires_at")]
    for state in _STATE_PRECEDENCE:
        if state in states:
            return ("claimed_current" if state == "current" else state), []
    return None, [_error("invalid_proof_status")]


def uncovered_contract_cells(required, proven):
    if type(required) is not set or type(proven) is not set:
        raise ValueError("contract_cell_invalid")

    def valid(cell):
        return (
            type(cell) is tuple
            and len(cell) == 2
            and type(cell[0]) is str
            and re.fullmatch(r"[a-z]{2}", cell[0]) is not None
            and type(cell[1]) is str
            and cell[1] in _FEATURES
        )

    if not all(valid(cell) for cell in required | proven) or not proven <= required:
        raise ValueError("contract_cell_invalid")
    return sorted(required - proven)


def validate_client_entitlement(document: dict) -> dict:
    validation_time = _utc_now()
    if type(document) is not dict:
        return _invalid([_error("document_not_object")])
    schema_errors = _schema_errors(document)
    if schema_errors:
        return _invalid(schema_errors)
    semantic_errors = _semantic_errors(document)
    if semantic_errors:
        return _invalid(semantic_errors)
    claim_state, proof_errors = _aggregate_state(document, validation_time)
    if proof_errors:
        return _invalid(proof_errors)
    subset = document["coverage"]["initial_digital_subset"]
    gaps = uncovered_contract_cells(
        {tuple(cell) for cell in subset["required_cells"]},
        {tuple(cell) for cell in subset["document_claimed_current_cells"]},
    )
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    full = document["coverage"]["full_required_coverage"]
    return {
        "contract_version": "client_entitlement_validation_v1",
        "structure_valid": True,
        "client_scope_id": document["client_scope_id"],
        "document_sha256": hashlib.sha256(canonical).hexdigest(),
        "document_claim_state": claim_state,
        "initial_digital_subset_uncovered_cells": [list(cell) for cell in gaps],
        "full_required_coverage_complete": bool(
            full["complete"] and claim_state == "claimed_current"
        ),
        "coverage_matrix_status": document["coverage"]["matrix_status"],
        "native_rights_verified": False,
        "activation_ready": False,
        "errors": [],
    }
