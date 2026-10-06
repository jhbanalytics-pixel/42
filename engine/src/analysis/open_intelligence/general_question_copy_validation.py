"""Aggregate current copied bytes against the original frozen replay copy domain."""

import copy
import hashlib
from dataclasses import dataclass
from datetime import date

from scripts.migrations import create_open_intelligence_replay_copy_infra as infra
from scripts.staging import copy_open_intelligence_replay_sources as original

from src.analysis.open_intelligence.brain_contract import canonical_digest

_PROFILE = "legacy_r16_v2"
_CONTROLS = (
    "open_intelligence_source_copy_manifest_v1",
    "open_intelligence_source_copy_receipts_v1",
)
_COUNTS = (
    "target_rows",
    "target_distinct_ids",
    "target_null_keys",
    "manifest_rows",
    "manifest_distinct_ids",
    "manifest_null_ids",
    "manifest_header_mismatches",
    "target_manifest_mismatches",
    "receipt_rows",
    "receipt_header_mismatches",
    "unexpected_manifest_rows",
    "unexpected_receipt_rows",
)
_FIELDS = {
    "copy_run_id",
    "table_name",
    "target_source_set_digest",
    "manifest_source_set_digest",
    *_COUNTS,
}


@dataclass(frozen=True)
class PreparedCurrentSourceCopyQuery:
    template_id: str
    sql: str
    parameters: tuple
    sql_digest: str
    parameters_digest: str
    required_schema_relations: tuple[str, ...]
    candidate_limit: int = 0
    transport_row_limit: int = 4


def source_copy_schema_requirements():
    """Return declarations only; current table metadata must independently match them."""
    declarations = {
        name: original._schema_fields(table) for name, table in original._table_contracts().items()
    }
    declarations[_CONTROLS[0]] = infra.MANIFEST_SCHEMA
    declarations[_CONTROLS[1]] = infra.RECEIPT_SCHEMA
    return {
        f"{original.PROJECT}.{original.TARGET_DATASET}.{name}": fields
        for name, fields in declarations.items()
    }


def _literal(value):
    if value is None:
        return "CAST(NULL AS STRING)"
    if type(value) is int:
        return str(value)
    if isinstance(value, date):
        return f"DATE '{value.isoformat()}'"
    return "'" + value.replace("'", "''") + "'"


def _header(table):
    return {
        "copy_run_id": original.COPY_RUN_ID,
        "copy_contract_version": original.COPY_CONTRACT_VERSION,
        "source_project": original.PROJECT,
        "source_dataset": original.SOURCE_DATASET,
        "target_dataset": original.TARGET_DATASET,
        "source_table": table.name,
        "target_table": table.name,
        "window_start": original.START_DATE,
        "window_end": original.END_DATE,
        "filter_digest": table.filter_digest,
        "schema_digest": table.schema_digest,
        "source_set_digest": table.source_set_digest,
        "coverage_digest": table.coverage_digest,
        "source_rows": table.source_rows,
        "zero_row_status": table.zero_row_status,
        "hard_ceiling": table.hard_ceiling,
    }


def _header_mismatch(alias, expected):
    actual = ",".join(f"{alias}.{key} AS {key}" for key in expected)
    wanted = ",".join(f"{_literal(value)} AS {key}" for key, value in expected.items())
    return f"TO_JSON_STRING(STRUCT({actual})) IS DISTINCT FROM TO_JSON_STRING(STRUCT({wanted}))"


def _sql():
    tables = original._table_contracts()

    def relation(name):
        return f"`{original.PROJECT}.{original.TARGET_DATASET}.{name}`"

    scope = f"copy_run_id='{original.COPY_RUN_ID}'"
    names = ",".join(_literal(name) for name in tables)
    ctes = [
        f"all_manifest AS (SELECT * FROM {relation(_CONTROLS[0])} WHERE {scope})",
        f"all_receipts AS (SELECT * FROM {relation(_CONTROLS[1])} WHERE {scope})",
    ]
    outputs = []
    for name, table in tables.items():
        target_filter = original._target_filter(table)
        if name == "enriched_content":
            for dependency in ("seed_graph", "seed_candidates"):
                target_filter = target_filter.replace(
                    f"`{original.PROJECT}.{original.SOURCE_DATASET}.{dependency}`",
                    relation(dependency),
                )
        null_keys = " OR ".join(f"target.{field} IS NULL" for field in table.natural_key)
        ctes.append(
            f"target_{name} AS (SELECT {original._copy_id_expr(table, 'target')} AS copy_row_id, "
            f"{original._content_hash_expr(table, 'target')} AS source_content_sha256, "
            f"({null_keys}) AS null_natural_key FROM {relation(name)} target WHERE {target_filter})"
        )
        header = _header(table)
        manifest_header = {
            key: value
            for key, value in header.items()
            if key in {field for field, _, _ in infra.MANIFEST_SCHEMA}
        }
        ctes.append(
            f"manifest_{name} AS (SELECT m.copy_row_id,m.source_content_sha256,"
            f"({_header_mismatch('m', manifest_header)} OR m.inserted_at IS NULL) AS header_mismatch "
            f"FROM all_manifest m WHERE target_table='{name}')"
        )
        ctes.append(
            f"receipts_{name} AS (SELECT ({_header_mismatch('r', header)}) AS header_mismatch "
            f"FROM all_receipts r WHERE target_table='{name}')"
        )
        outputs.append(f"""SELECT '{original.COPY_RUN_ID}' AS copy_run_id, '{name}' AS table_name,
  (SELECT COUNT(*) FROM target_{name}) AS target_rows,
  (SELECT COUNT(DISTINCT copy_row_id) FROM target_{name}) AS target_distinct_ids,
  (SELECT COUNTIF(null_natural_key) FROM target_{name}) AS target_null_keys,
  (SELECT COUNT(*) FROM manifest_{name}) AS manifest_rows,
  (SELECT COUNT(DISTINCT copy_row_id) FROM manifest_{name}) AS manifest_distinct_ids,
  (SELECT COUNTIF(copy_row_id IS NULL OR source_content_sha256 IS NULL
    OR NOT REGEXP_CONTAINS(copy_row_id,r'^[0-9a-f]{{64}}$')
    OR NOT REGEXP_CONTAINS(source_content_sha256,r'^[0-9a-f]{{64}}$')) FROM manifest_{name}) AS manifest_null_ids,
  (SELECT COUNTIF(header_mismatch) FROM manifest_{name}) AS manifest_header_mismatches,
  (SELECT COUNT(*) FROM target_{name} t FULL OUTER JOIN manifest_{name} m USING(copy_row_id)
    WHERE t.copy_row_id IS NULL OR m.copy_row_id IS NULL
      OR t.source_content_sha256 IS DISTINCT FROM m.source_content_sha256) AS target_manifest_mismatches,
  (SELECT COUNT(*) FROM receipts_{name}) AS receipt_rows,
  (SELECT COUNTIF(header_mismatch) FROM receipts_{name}) AS receipt_header_mismatches,
  (SELECT COUNTIF(target_table IS NULL OR target_table NOT IN ({names})) FROM all_manifest) AS unexpected_manifest_rows,
  (SELECT COUNTIF(target_table IS NULL OR target_table NOT IN ({names})) FROM all_receipts) AS unexpected_receipt_rows,
  ({original._digest_select("target_" + name)}) AS target_source_set_digest,
  ({original._digest_select("manifest_" + name)}) AS manifest_source_set_digest""")
    return "WITH " + ",\n".join(ctes) + "\n" + "\nUNION ALL\n".join(outputs)


def build_current_source_copy_query(*, profile=_PROFILE):
    if profile != _PROFILE:
        raise ValueError("current_source_copy_invalid")
    sql = _sql()
    return PreparedCurrentSourceCopyQuery(
        "current_source_copy_v1",
        sql,
        (),
        hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        canonical_digest([]),
        tuple(source_copy_schema_requirements()),
    )


def validate_current_source_copy_proof(rows, *, query):
    """Validate query data against original pins; source admission requires owned readback."""
    if (
        type(query) is not PreparedCurrentSourceCopyQuery
        or query != build_current_source_copy_query()
    ):
        raise ValueError("current_source_copy_invalid")
    if type(rows) not in (list, tuple) or len(rows) != 4:
        raise ValueError("current_source_copy_invalid")
    tables, indexed = original._table_contracts(), {}
    for row in rows:
        if type(row) is not dict or set(row) != _FIELDS:
            raise ValueError("current_source_copy_invalid")
        name = row["table_name"]
        if (
            type(name) is not str
            or name not in tables
            or name in indexed
            or row["copy_run_id"] != original.COPY_RUN_ID
        ):
            raise ValueError("current_source_copy_invalid")
        table = tables[name]
        expected = dict.fromkeys(_COUNTS, 0)
        expected.update(
            target_rows=table.source_rows,
            target_distinct_ids=table.source_rows,
            manifest_rows=table.source_rows,
            manifest_distinct_ids=table.source_rows,
            receipt_rows=1,
        )
        if any(
            type(row[field]) is not int or row[field] != value for field, value in expected.items()
        ):
            raise ValueError("current_source_copy_invalid")
        if (
            row["target_source_set_digest"] != table.source_set_digest
            or row["manifest_source_set_digest"] != table.source_set_digest
        ):
            raise ValueError("current_source_copy_invalid")
        indexed[name] = copy.deepcopy(row)
    return {
        "profile": _PROFILE,
        "copy_run_id": original.COPY_RUN_ID,
        "sql_digest": query.sql_digest,
        "tables": [indexed[name] for name in tables],
        "content_validated": True,
        "physical_schema_required": True,
        "source_authority": False,
    }
