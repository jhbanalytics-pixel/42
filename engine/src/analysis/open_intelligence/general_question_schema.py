"""Pure destination schema comparison; supplied metadata grants no authority."""

import hashlib
import json
import re
from pathlib import Path

from src.contracts.bigquery_ddl import parse_table_ddl

_TABLES = (
    "signal_candidates_v2",
    "signal_evidence_v2",
    "signal_membership_v2",
    "signal_lineage_v2",
    "signal_predictions_v2",
    "signal_outcomes_v2",
    "signal_analysis_v2",
    "open_intelligence_run_receipts_v1",
)
_PROJECT = "ogilvy-trends-v2"
_DATASET = "trends_v2_staging"
_ALIASES = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL", "STRUCT": "RECORD"}
_TYPES = frozenset(
    (
        "BIGNUMERIC",
        "BOOL",
        "BYTES",
        "DATE",
        "DATETIME",
        "FLOAT64",
        "GEOGRAPHY",
        "INT64",
        "JSON",
        "NUMERIC",
        "STRING",
        "TIME",
        "TIMESTAMP",
        "RECORD",
    )
)
_ROOT = Path(__file__).resolve().parents[3] / "infra" / "bigquery_schemas"


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _fields(values, count, depth=0):
    if type(values) is not list or not values or depth > 8:
        raise ValueError("physical_schema_invalid")
    output = {}
    for value in values:
        count[0] += 1
        if count[0] > 1000 or type(value) is not dict:
            raise ValueError("physical_schema_invalid")
        name, kind, mode = value.get("name"), value.get("type"), value.get("mode", "NULLABLE")
        if (
            type(name) is not str
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
            or name in output
            or type(kind) is not str
            or kind not in _TYPES | _ALIASES.keys()
            or type(mode) is not str
            or mode not in ("NULLABLE", "REQUIRED", "REPEATED")
        ):
            raise ValueError("physical_schema_invalid")
        kind = _ALIASES.get(kind, kind)
        if kind == "RECORD":
            children = _fields(value.get("fields"), count, depth + 1)
        else:
            if value.get("fields", []) != []:
                raise ValueError("physical_schema_invalid")
            children = {}
        output[name] = (kind, mode, children)
    return output


def _declared(fields):
    return {
        name: (kind, mode, _declared(children))
        for name, kind, mode, _description, children in fields
    }


def _compare(actual, expected):
    for name, (kind, mode, children) in expected.items():
        observed = actual.get(name)
        if observed is None or observed[:2] != (kind, mode):
            raise ValueError("physical_schema_mismatch")
        _compare(observed[2], children)
    if any(value[1] != "NULLABLE" for name, value in actual.items() if name not in expected):
        raise ValueError("physical_schema_mismatch")


def validate_source_schemas(table_resources, *, cluster_build_version):
    """Compare all fixed destination schemas, returning detached metadata as data only."""
    if cluster_build_version not in ("hybrid_graph_v1", "hybrid_graph_v2", "hybrid_graph_v3"):
        raise ValueError("physical_schema_version_invalid")
    if type(table_resources) is not list or len(table_resources) != len(_TABLES):
        raise ValueError("physical_schema_invalid")
    try:
        raw = _encode(table_resources)
        if len(raw) > 1_000_000:
            raise ValueError("physical_schema_invalid")
        detached = json.loads(raw)
    except (TypeError, OverflowError, RecursionError) as error:
        raise ValueError("physical_schema_invalid") from error
    seen = set()
    schema_digests = {}
    declaration_digests = {}
    count = [0]
    for resource in detached:
        if type(resource) is not dict or type(resource.get("tableReference")) is not dict:
            raise ValueError("physical_schema_identity_invalid")
        reference = resource["tableReference"]
        table = reference.get("tableId")
        if (
            type(table) is not str
            or table not in _TABLES
            or table in seen
            or reference != {"projectId": _PROJECT, "datasetId": _DATASET, "tableId": table}
        ):
            raise ValueError("physical_schema_identity_invalid")
        seen.add(table)
        schema = resource.get("schema")
        if type(schema) is not dict:
            raise ValueError("physical_schema_invalid")
        actual = _fields(schema.get("fields"), count)
        ddl = (_ROOT / (table + ".sql")).read_text(encoding="utf-8")
        expected = _declared(
            parse_table_ddl(ddl.format(project=_PROJECT, dataset=_DATASET))["fields"]
        )
        if (
            table == "signal_membership_v2"
            and cluster_build_version != "hybrid_graph_v3"
            and "source_provenance_json" not in actual
        ):
            expected.pop("source_provenance_json")
        _compare(actual, expected)
        schema_digests[table] = hashlib.sha256(_encode(actual)).hexdigest()
        declaration_digests[table] = hashlib.sha256(ddl.encode("utf-8")).hexdigest()
    return {
        "table_resources": detached,
        "metadata_digest": hashlib.sha256(raw).hexdigest(),
        "schema_digests": schema_digests,
        "declaration_digests": declaration_digests,
        "source_authority": False,
    }
