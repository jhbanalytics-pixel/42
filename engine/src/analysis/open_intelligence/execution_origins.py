import hashlib
import json
import re
import stat
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal

OriginMode = Literal[
    "historical_read",
    "historical_replay",
    "new_approval",
    "new_consume",
]

_REGISTRY_VERSION = "open_intelligence_execution_origin_registry_v1"
_POLICY_VERSION = "open_intelligence_execution_origin_policy_v2"
_MANIFEST_V1 = "open_intelligence_execution_manifest_v1"
_MANIFEST_V2 = "open_intelligence_execution_manifest_v2"
_MODES = (
    "historical_read",
    "historical_replay",
    "new_approval",
    "new_consume",
)
_HISTORICAL_MODES = _MODES[:2]
_HISTORICAL_PROJECTION_SHA256 = "e5461b8ad97535a3328352f9c12651051bc4bcc2a6dfb9aada5d9436e2ca7d3c"
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_MAX_BYTES = 1024 * 1024
_REGISTRY_SEAL = object()
_ROW_FIELDS = {
    "manifest_version",
    "contract_sha256",
    "connected_repo",
    "image_repository",
    "image_name",
    "image_uri_regex",
    "job_resources",
    "service_identities",
    "datasets",
    "contract_digests",
    "allowed_execution_modes",
    "exact_operation_bindings",
    "contract_file",
}
_BINDING_FIELDS = {"job_resource", "service_identity", "datasets"}
_POLICY_FIELDS = {
    "contract_version",
    "purpose",
    "predecessor",
    "successor",
    "origin",
    "common_manifest_validation",
    "operation_validation",
    "preservation",
}
_MANIFEST_FIELDS = (
    "manifest_version",
    "operation",
    "contract_sha256",
    "project",
    "datasets",
    "location",
    "job_resource",
    "service_identity",
    "source_sha",
    "image_uri",
    "build_resource",
    "command",
    "arguments",
    "environment",
    "secrets",
    "max_retries",
    "timeout_seconds",
    "input_artifacts",
    "limits",
    "expires_at",
)


class OriginRefusal(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _OperationBinding:
    job_resource: str
    service_identity: str
    datasets: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExecutionOrigin:
    manifest_version: str
    contract_sha256: str
    connected_repo: str
    image_repository: str
    image_name: str
    image_uri_regex: str
    job_resources: tuple[str, ...]
    service_identities: tuple[str, ...]
    datasets: tuple[str, ...]
    contract_digests: tuple[str, ...]
    allowed_execution_modes: frozenset[OriginMode]
    operation_bindings: Mapping[str, _OperationBinding]
    contract_file: str | None


@dataclass(frozen=True, slots=True, init=False)
class OriginRegistry(Mapping[tuple[str, str], ExecutionOrigin]):
    _policy_digests: Mapping[str, str]
    _policy_bytes: Mapping[tuple[str, str], bytes]
    _rows: Mapping[tuple[str, str], ExecutionOrigin]
    _sha256: str

    def __init__(
        self,
        rows: Mapping[tuple[str, str], ExecutionOrigin],
        sha256: str,
        policy_digests: Mapping[str, str],
        policy_bytes: Mapping[tuple[str, str], bytes] | None = None,
        *,
        _seal: object = None,
    ) -> None:
        if _seal is not _REGISTRY_SEAL:
            _refuse("execution_origin_registry_invalid")
        object.__setattr__(self, "_rows", MappingProxyType(dict(rows)))
        object.__setattr__(self, "_sha256", sha256)
        object.__setattr__(
            self,
            "_policy_digests",
            MappingProxyType(dict(policy_digests)),
        )
        if policy_bytes is None:
            _refuse("execution_origin_registry_invalid")
        object.__setattr__(self, "_policy_bytes", MappingProxyType(dict(policy_bytes)))

    @property
    def sha256(self) -> str:
        return self._sha256

    @property
    def verified_successor_policy_digests(self) -> Mapping[str, str]:
        return self._policy_digests

    def __getitem__(self, key: tuple[str, str]) -> ExecutionOrigin:
        return self._rows[key]

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)


def _refuse(code: str) -> None:
    raise OriginRefusal(code)


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise OriginRefusal("execution_origin_registry_invalid") from error


def _duplicate_safe_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            _refuse("execution_origin_registry_invalid")
        value[key] = item
    return value


def _reject_nonfinite(_value: str) -> None:
    _refuse("execution_origin_registry_invalid")


def _parse_json(raw: bytes) -> object:
    if raw.startswith(b"\xef\xbb\xbf"):
        _refuse("execution_origin_registry_invalid")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_duplicate_safe_object,
            parse_constant=_reject_nonfinite,
        )
    except OriginRefusal:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise OriginRefusal("execution_origin_registry_invalid") from error


def _exact_string(value: object) -> bool:
    return isinstance(value, str) and bool(value) and unicodedata.normalize("NFC", value) == value


def _expect_fields(value: object, fields: set[str]) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        _refuse("execution_origin_registry_invalid")
    return value


def _string_tuple(
    value: object,
    *,
    allowed: tuple[str, ...] | None = None,
    nonempty: bool = True,
) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or (nonempty and not value)
        or any(not _exact_string(item) for item in value)
    ):
        _refuse("execution_origin_registry_invalid")
    result = tuple(value)
    if result != tuple(sorted(result)) or len(result) != len(set(result)):
        _refuse("execution_origin_registry_invalid")
    if allowed is not None and any(item not in allowed for item in result):
        _refuse("execution_origin_registry_invalid")
    return result


def _string_sequence(value: object, *, nonempty: bool = True) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or (nonempty and not value)
        or any(not _exact_string(item) for item in value)
    ):
        _refuse("execution_origin_registry_invalid")
    return tuple(value)


def _bounded_file(path: Path, root: Path) -> bytes:
    try:
        root = root.absolute()
        path = path.absolute()
        relative = path.relative_to(root)
    except (OSError, ValueError) as error:
        raise OriginRefusal("execution_origin_registry_invalid") from error
    current = root
    if current.is_symlink() or not current.is_dir():
        _refuse("execution_origin_registry_invalid")
    for part in relative.parts:
        if part in {"", ".", ".."}:
            _refuse("execution_origin_registry_invalid")
        current = current / part
        if current.is_symlink():
            _refuse("execution_origin_registry_invalid")
    try:
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _MAX_BYTES:
            _refuse("execution_origin_registry_invalid")
        return path.read_bytes()
    except OriginRefusal:
        raise
    except OSError as error:
        raise OriginRefusal("execution_origin_registry_invalid") from error


# Registered policies live in origin_contracts, which the store migration registers in
# full; a policy of an inactive candidate generation lives beside it until activation.
_POLICY_DIRECTORIES = frozenset(
    {
        ("configs", "open_intelligence", "origin_contracts"),
        ("configs", "open_intelligence", "candidate_contracts"),
    }
)


def _policy_path(contract_root: Path, value: object) -> Path:
    if not _exact_string(value):
        _refuse("execution_origin_registry_invalid")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or pure.parts[:3] not in _POLICY_DIRECTORIES
        or len(pure.parts) != 4
        or pure.name in {"", ".", ".."}
        or pure.suffix != ".json"
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        _refuse("execution_origin_registry_invalid")
    return contract_root.joinpath(*pure.parts)


def _compile_anchored(value: object) -> str:
    if not _exact_string(value) or not value.startswith("^") or not value.endswith("$"):
        _refuse("execution_origin_registry_invalid")
    try:
        re.compile(value)
    except re.error as error:
        raise OriginRefusal("execution_origin_registry_invalid") from error
    return value


def _binding(value: object) -> _OperationBinding:
    value = _expect_fields(value, _BINDING_FIELDS)
    if not _exact_string(value["job_resource"]) or not _exact_string(value["service_identity"]):
        _refuse("execution_origin_registry_invalid")
    datasets = _string_tuple(value["datasets"])
    return _OperationBinding(
        job_resource=value["job_resource"],
        service_identity=value["service_identity"],
        datasets=datasets,
    )


def _operation_bindings(value: object) -> Mapping[str, _OperationBinding]:
    if not isinstance(value, dict) or not value:
        _refuse("execution_origin_registry_invalid")
    result: dict[str, _OperationBinding] = {}
    for operation, item in value.items():
        if not _exact_string(operation):
            _refuse("execution_origin_registry_invalid")
        result[operation] = _binding(item)
    return MappingProxyType(result)


def _validate_constraint(value: object) -> None:
    if (
        not isinstance(value, dict)
        or not value
        or not set(value)
        <= {
            "exact",
            "minimum",
            "maximum",
        }
    ):
        _refuse("execution_origin_registry_invalid")
    if any(type(item) is not int or item < 0 for item in value.values()):
        _refuse("execution_origin_registry_invalid")


# Values no regex position of a prefix pattern may admit: an empty value, whitespace, a
# switch, a path step or a glob. A regex matching any of them is too wide to bind a
# position, so it refuses at load rather than widening the vector at approval.
_PATTERN_PROBES = ("", " ", "\t", "\n", "a b", "a\nb", "-", "--", "--x", "-x", "../a", "/a", "*")


def _validate_prefix_pattern(value: Mapping[str, object]) -> None:
    """A fixed prefix, then positions that are each an exact literal or a narrow regex."""
    _string_sequence(value["prefix"])
    pattern = value["pattern"]
    if not isinstance(pattern, list) or not pattern:
        _refuse("execution_origin_registry_invalid")
    for item in pattern:
        if not isinstance(item, dict) or set(item) not in ({"literal"}, {"regex"}):
            _refuse("execution_origin_registry_invalid")
        if "literal" in item:
            if not _exact_string(item["literal"]):
                _refuse("execution_origin_registry_invalid")
            continue
        regex = _compile_anchored(item["regex"])
        if any(re.fullmatch(regex, probe) is not None for probe in _PATTERN_PROBES):
            _refuse("execution_origin_registry_invalid")


def _validate_arguments(value: object) -> None:
    if not isinstance(value, dict) or not _exact_string(value.get("kind")):
        _refuse("execution_origin_registry_invalid")
    kind = value["kind"]
    fields = {
        "exact": {"kind", "value"},
        "r3_execution_reference": {"kind", "prefix", "execution_resource_regex"},
        "brain_read_v1": {
            "kind",
            "lengths",
            "prefix",
            "run_id_regex",
            "signal_switch",
            "signal_id_regex",
            "depth_switch",
            "depths",
            "decision_switch",
            "decision_max_characters",
            "decision_control_characters_forbidden",
            "decision_required_for",
        },
        "source_snapshot_capture_v2": {
            "kind",
            "length",
            "prefix",
            "cutoff_date_regex",
            "mode_switch",
            "modes",
            "grant_switch",
            "grant_id_regex",
            "plan_contract_version",
            "consume_routine",
            "consume_routine_sha256",
        },
        "prefix_pattern_v1": {"kind", "prefix", "pattern"},
    }.get(kind)
    if fields is None or set(value) != fields:
        _refuse("execution_origin_registry_invalid")
    if kind == "exact":
        _string_sequence(value["value"], nonempty=False)
    elif kind == "prefix_pattern_v1":
        _validate_prefix_pattern(value)
    elif kind == "r3_execution_reference":
        _string_sequence(value["prefix"])
        _compile_anchored(value["execution_resource_regex"])
    elif kind == "source_snapshot_capture_v2":
        # The capture routine reads the vector at fixed positions: the cutoff at 2, the
        # mode at 4 and the grant id at 6 after a two element prefix, seven in all.
        prefix = _string_sequence(value["prefix"])
        modes = _string_sequence(value["modes"])
        if (
            type(value["length"]) is not int
            or value["length"] != 7
            or len(prefix) != 2
            or len(modes) != len(set(modes))
            or set(modes) != {"initial", "recover"}
            or not isinstance(value["consume_routine_sha256"], str)
            or _HEX_64.fullmatch(value["consume_routine_sha256"]) is None
        ):
            _refuse("execution_origin_registry_invalid")
        for name in ("mode_switch", "grant_switch", "plan_contract_version", "consume_routine"):
            if not _exact_string(value[name]):
                _refuse("execution_origin_registry_invalid")
        _compile_anchored(value["cutoff_date_regex"])
        _compile_anchored(value["grant_id_regex"])
    else:
        if (
            value["lengths"] != [9, 11]
            or type(value["decision_max_characters"]) is not int
            or type(value["decision_control_characters_forbidden"]) is not bool
        ):
            _refuse("execution_origin_registry_invalid")
        _string_sequence(value["prefix"])
        depths = _string_sequence(value["depths"])
        required = _string_sequence(value["decision_required_for"])
        if (
            len(depths) != len(set(depths))
            or set(depths) != {"briefing", "scan", "investigation"}
            or len(required) != len(set(required))
            or set(required) != {"scan", "investigation"}
        ):
            _refuse("execution_origin_registry_invalid")
        for name in (
            "run_id_regex",
            "signal_switch",
            "signal_id_regex",
            "depth_switch",
            "decision_switch",
        ):
            if not _exact_string(value[name]):
                _refuse("execution_origin_registry_invalid")


def _validate_operation_rule(value: object) -> None:
    fields = {
        "job_resource",
        "service_identity",
        "datasets",
        "command",
        "arguments",
        "environment",
        "secrets",
        "input_artifact_names",
        "input_artifact_digest_regex",
        "limits",
    }
    value = _expect_fields(value, fields)
    _binding(
        {
            "job_resource": value["job_resource"],
            "service_identity": value["service_identity"],
            "datasets": value["datasets"],
        }
    )
    _string_sequence(value["command"])
    _validate_arguments(value["arguments"])
    if not isinstance(value["environment"], list):
        _refuse("execution_origin_registry_invalid")
    environment = []
    for item in value["environment"]:
        item = _expect_fields(item, {"name", "value"})
        if not _exact_string(item["name"]) or not _exact_string(item["value"]):
            _refuse("execution_origin_registry_invalid")
        environment.append((item["name"], item["value"]))
    if environment != sorted(environment) or len(environment) != len(set(environment)):
        _refuse("execution_origin_registry_invalid")
    _string_tuple(value["secrets"], nonempty=False)
    _string_tuple(value["input_artifact_names"])
    _compile_anchored(value["input_artifact_digest_regex"])
    limits = _expect_fields(
        value["limits"],
        {"max_bytes_billed", "max_credits", "max_model_calls", "max_rows_written"},
    )
    for constraint in limits.values():
        _validate_constraint(constraint)


def _validate_common(value: object) -> None:
    fields = {
        "top_level_fields",
        "extra_top_level_fields",
        "all_text_nfc",
        "project",
        "location",
        "source_sha_regex",
        "build_resource_regex",
        "max_retries",
        "timeout_seconds",
        "input_artifacts",
        "expires_at_regex",
        "canonical_json",
    }
    value = _expect_fields(value, fields)
    if tuple(value["top_level_fields"]) != _MANIFEST_FIELDS:
        _refuse("execution_origin_registry_invalid")
    if (
        value["extra_top_level_fields"] != "refuse"
        or value["all_text_nfc"] is not True
        or value["project"] != "ogilvy-trends-v2"
        or value["location"] != "US"
    ):
        _refuse("execution_origin_registry_invalid")
    for name in ("source_sha_regex", "build_resource_regex", "expires_at_regex"):
        _compile_anchored(value[name])
    if value["max_retries"] != {"exact_integer": 0} or value["timeout_seconds"] != {
        "integer_minimum": 1,
        "integer_maximum": 10800,
    }:
        _refuse("execution_origin_registry_invalid")
    artifacts = _expect_fields(
        value["input_artifacts"],
        {
            "exact_ordered_names_per_operation",
            "unique_names",
            "sorted_by_name",
            "sha256_regex",
            "dynamic_values_remain_manifest_specific",
        },
    )
    if artifacts != {
        "exact_ordered_names_per_operation": True,
        "unique_names": True,
        "sorted_by_name": True,
        "sha256_regex": "^[0-9a-f]{64}$",
        "dynamic_values_remain_manifest_specific": True,
    }:
        _refuse("execution_origin_registry_invalid")
    canonical = _expect_fields(
        value["canonical_json"],
        {"encoding", "ensure_ascii", "allow_nan", "sort_keys", "separators"},
    )
    if canonical != {
        "encoding": "utf-8",
        "ensure_ascii": False,
        "allow_nan": False,
        "sort_keys": True,
        "separators": [",", ":"],
    }:
        _refuse("execution_origin_registry_invalid")


def _validate_policy(value: object) -> Mapping[str, object]:
    value = _expect_fields(value, _POLICY_FIELDS)
    if (
        value["contract_version"] != _POLICY_VERSION
        or value["purpose"] != "C01 origin, version, job and identity routing only"
    ):
        _refuse("execution_origin_registry_invalid")
    predecessor = _expect_fields(
        value["predecessor"],
        {"manifest_version", "contract_sha256", "operations"},
    )
    if (
        predecessor["manifest_version"] != _MANIFEST_V1
        or not isinstance(predecessor["contract_sha256"], str)
        or _HEX_64.fullmatch(predecessor["contract_sha256"]) is None
    ):
        _refuse("execution_origin_registry_invalid")
    _string_tuple(predecessor["operations"])
    successor = _expect_fields(
        value["successor"],
        {"manifest_version", "allowed_modes", "operations", "excluded_predecessor_operations"},
    )
    if successor["manifest_version"] != _MANIFEST_V2:
        _refuse("execution_origin_registry_invalid")
    _string_tuple(successor["allowed_modes"], allowed=_MODES)
    _string_tuple(successor["operations"])
    _string_tuple(successor["excluded_predecessor_operations"], nonempty=False)
    origin = _expect_fields(
        value["origin"],
        {
            "connected_repository",
            "image_repository",
            "image_name_regex",
            "image_uri_regex",
            "fresh_build_source",
        },
    )
    if not _exact_string(origin["connected_repository"]) or not _exact_string(
        origin["image_repository"]
    ):
        _refuse("execution_origin_registry_invalid")
    _compile_anchored(origin["image_name_regex"])
    _compile_anchored(origin["image_uri_regex"])
    fresh = _expect_fields(
        origin["fresh_build_source"],
        {"required_shape", "repository", "revision_regex", "legacy_sha_only_envelope"},
    )
    if (
        fresh["required_shape"] != "source.connectedRepository"
        or fresh["repository"] != origin["connected_repository"]
        or fresh["legacy_sha_only_envelope"] != "refuse"
    ):
        _refuse("execution_origin_registry_invalid")
    _compile_anchored(fresh["revision_regex"])
    _validate_common(value["common_manifest_validation"])
    rules = value["operation_validation"]
    if not isinstance(rules, dict) or set(rules) != set(successor["operations"]):
        _refuse("execution_origin_registry_invalid")
    for rule in rules.values():
        _validate_operation_rule(rule)
    preservation = _expect_fields(
        value["preservation"],
        {
            "dynamic_input_artifact_digests_remain_per_manifest",
            "approval_horizon_maximum_seconds",
            "baseline_validator_sha256",
            "baseline_validator_refs",
            "historical_v1_records_unchanged",
            "new_v2_record_schemas",
        },
    )
    if (
        preservation["dynamic_input_artifact_digests_remain_per_manifest"] is not True
        or preservation["historical_v1_records_unchanged"] is not True
        or preservation["approval_horizon_maximum_seconds"] != 86400
        or _HEX_64.fullmatch(preservation["baseline_validator_sha256"]) is None
        or preservation["new_v2_record_schemas"]
        != "explicit_definition_and_review_required_with_implementation"
    ):
        _refuse("execution_origin_registry_invalid")
    _string_tuple(preservation["baseline_validator_refs"])
    return value


def _origin_from_row(value: object) -> ExecutionOrigin:
    value = _expect_fields(value, _ROW_FIELDS)
    version = value["manifest_version"]
    digest = value["contract_sha256"]
    if (
        version not in {_MANIFEST_V1, _MANIFEST_V2}
        or not isinstance(digest, str)
        or _HEX_64.fullmatch(digest) is None
    ):
        _refuse("execution_origin_registry_invalid")
    if not _exact_string(value["connected_repo"]) or not _exact_string(value["image_repository"]):
        _refuse("execution_origin_registry_invalid")
    image_name = _compile_anchored(value["image_name"])
    image_uri_regex = _compile_anchored(value["image_uri_regex"])
    job_resources = _string_tuple(value["job_resources"])
    identities = _string_tuple(value["service_identities"])
    datasets = _string_tuple(value["datasets"])
    contract_digests = _string_tuple(value["contract_digests"])
    if contract_digests != (digest,):
        _refuse("execution_origin_registry_invalid")
    modes = _string_tuple(value["allowed_execution_modes"], allowed=_MODES)
    expected_modes = _HISTORICAL_MODES if version == _MANIFEST_V1 else _MODES
    if modes != expected_modes:
        _refuse("execution_origin_registry_invalid")
    bindings = _operation_bindings(value["exact_operation_bindings"])
    if (
        job_resources != tuple(sorted({item.job_resource for item in bindings.values()}))
        or identities != tuple(sorted({item.service_identity for item in bindings.values()}))
        or datasets
        != tuple(sorted({dataset for item in bindings.values() for dataset in item.datasets}))
    ):
        _refuse("execution_origin_registry_invalid")
    contract_file = value["contract_file"]
    if (version == _MANIFEST_V1 and contract_file is not None) or (
        version == _MANIFEST_V2 and not _exact_string(contract_file)
    ):
        _refuse("execution_origin_registry_invalid")
    return ExecutionOrigin(
        manifest_version=version,
        contract_sha256=digest,
        connected_repo=value["connected_repo"],
        image_repository=value["image_repository"],
        image_name=image_name,
        image_uri_regex=image_uri_regex,
        job_resources=job_resources,
        service_identities=identities,
        datasets=datasets,
        contract_digests=contract_digests,
        allowed_execution_modes=frozenset(modes),
        operation_bindings=bindings,
        contract_file=contract_file,
    )


def _validate_policy_row(
    policy: Mapping[str, object],
    row: ExecutionOrigin,
) -> None:
    successor = policy["successor"]
    origin = policy["origin"]
    rules = policy["operation_validation"]
    if (
        successor["manifest_version"] != row.manifest_version
        or frozenset(successor["allowed_modes"]) != row.allowed_execution_modes
        or set(successor["operations"]) != set(row.operation_bindings)
        or origin["connected_repository"] != row.connected_repo
        or origin["image_repository"] != row.image_repository
        or origin["image_name_regex"] != row.image_name
        or origin["image_uri_regex"] != row.image_uri_regex
    ):
        _refuse("execution_origin_registry_invalid")
    for operation, binding in row.operation_bindings.items():
        rule = rules[operation]
        if (
            rule["job_resource"] != binding.job_resource
            or rule["service_identity"] != binding.service_identity
            or tuple(rule["datasets"]) != binding.datasets
        ):
            _refuse("execution_origin_registry_invalid")


def _load_origin_registry(
    path,
    *,
    expected_sha256: str,
    contract_root,
) -> OriginRegistry:
    if (
        not isinstance(path, Path)
        or not isinstance(contract_root, Path)
        or not isinstance(expected_sha256, str)
        or _HEX_64.fullmatch(expected_sha256) is None
    ):
        _refuse("execution_origin_registry_invalid")
    raw = _bounded_file(path, contract_root)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        _refuse("execution_origin_registry_digest_mismatch")
    payload = _expect_fields(_parse_json(raw), {"contract_version", "rows"})
    if payload["contract_version"] != _REGISTRY_VERSION or not isinstance(payload["rows"], list):
        _refuse("execution_origin_registry_invalid")
    historical_rows = [
        row
        for row in payload["rows"]
        if isinstance(row, dict) and row.get("manifest_version") == _MANIFEST_V1
    ]
    historical_rows.sort(key=lambda row: (row["manifest_version"], row["contract_sha256"]))
    if (
        hashlib.sha256(_canonical_bytes(historical_rows)).hexdigest()
        != _HISTORICAL_PROJECTION_SHA256
    ):
        _refuse("execution_origin_registry_invalid")
    rows: dict[tuple[str, str], ExecutionOrigin] = {}
    policy_digests: dict[str, str] = {}
    policy_bytes: dict[tuple[str, str], bytes] = {}
    operations: set[tuple[str, str]] = set()
    for row_value in payload["rows"]:
        row = _origin_from_row(row_value)
        key = (row.manifest_version, row.contract_sha256)
        if key in rows:
            _refuse("execution_origin_registry_invalid")
        for operation in row.operation_bindings:
            operation_key = (row.manifest_version, operation)
            if operation_key in operations:
                _refuse("execution_origin_registry_invalid")
            operations.add(operation_key)
        if row.manifest_version == _MANIFEST_V2:
            policy_path = _policy_path(contract_root, row.contract_file)
            policy_raw = _bounded_file(policy_path, contract_root)
            if hashlib.sha256(policy_raw).hexdigest() != row.contract_sha256:
                _refuse("execution_origin_registry_invalid")
            policy = _validate_policy(_parse_json(policy_raw))
            _validate_policy_row(policy, row)
            policy_digests[row.contract_file] = row.contract_sha256
            policy_bytes[key] = bytes(policy_raw)
        rows[key] = row
    if len(rows) != 9 or len(policy_digests) != 4:
        _refuse("execution_origin_registry_invalid")
    return OriginRegistry(
        rows,
        expected_sha256,
        policy_digests,
        policy_bytes,
        _seal=_REGISTRY_SEAL,
    )


def load_origin_registry(
    path,
    *,
    expected_sha256: str,
    contract_root,
) -> OriginRegistry:
    try:
        return _load_origin_registry(
            path,
            expected_sha256=expected_sha256,
            contract_root=contract_root,
        )
    except OriginRefusal:
        raise
    except Exception as error:
        raise OriginRefusal("execution_origin_registry_invalid") from error


def select_origin(
    *,
    manifest_version: str,
    contract_sha256: str,
    mode: OriginMode,
    registry: OriginRegistry,
) -> ExecutionOrigin:
    if type(registry) is not OriginRegistry:
        _refuse("execution_origin_pair_invalid")
    if mode not in _MODES:
        _refuse("execution_origin_mode_forbidden")
    if (
        not isinstance(manifest_version, str)
        or manifest_version not in {_MANIFEST_V1, _MANIFEST_V2}
        or not isinstance(contract_sha256, str)
        or _HEX_64.fullmatch(contract_sha256) is None
    ):
        _refuse("execution_origin_pair_invalid")
    try:
        origin = registry[(manifest_version, contract_sha256)]
    except KeyError as error:
        raise OriginRefusal("execution_origin_pair_invalid") from error
    if mode not in origin.allowed_execution_modes:
        _refuse("execution_origin_mode_forbidden")
    return origin


def _policy_bytes_for_origin(*, registry: OriginRegistry, origin: ExecutionOrigin) -> bytes:
    if type(registry) is not OriginRegistry or type(origin) is not ExecutionOrigin:
        _refuse("execution_origin_pair_invalid")
    key = (origin.manifest_version, origin.contract_sha256)
    if registry._rows.get(key) is not origin:
        _refuse("execution_origin_pair_invalid")
    try:
        return registry._policy_bytes[key]
    except KeyError as error:
        raise OriginRefusal("execution_origin_pair_invalid") from error


def resolve_origin(
    manifest: Mapping[str, object],
    mode: OriginMode,
    registry: OriginRegistry,
) -> ExecutionOrigin:
    if not isinstance(manifest, Mapping):
        _refuse("execution_origin_pair_invalid")
    origin = select_origin(
        manifest_version=manifest.get("manifest_version"),
        contract_sha256=manifest.get("contract_sha256"),
        mode=mode,
        registry=registry,
    )
    operation = manifest.get("operation")
    binding = origin.operation_bindings.get(operation) if isinstance(operation, str) else None
    datasets = manifest.get("datasets")
    image_uri = manifest.get("image_uri")
    if (
        binding is None
        or manifest.get("job_resource") != binding.job_resource
        or manifest.get("service_identity") != binding.service_identity
        or not isinstance(datasets, list)
        or tuple(datasets) != binding.datasets
        or tuple(datasets) != tuple(sorted(datasets))
        or len(datasets) != len(set(datasets))
        or not isinstance(image_uri, str)
        or re.fullmatch(origin.image_uri_regex, image_uri) is None
    ):
        _refuse("execution_origin_target_invalid")
    return origin


__all__ = [
    "ExecutionOrigin",
    "OriginMode",
    "OriginRefusal",
    "OriginRegistry",
    "load_origin_registry",
    "resolve_origin",
    "select_origin",
]
