"""Release contract bytes and fixed metadata query, without authority issuance."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime

from google.cloud import bigquery
from scripts.staging.release_open_intelligence_run import _EXECUTION_PROOF_FIELDS

from src.analysis.open_intelligence import live_quality
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.persistence import candidate_projection_digest_sql
from src.analysis.open_intelligence.run_receipts import build_run_receipt, run_receipt_digest

_PROJECT = "ogilvy-trends-v2"
_DATASET = "trends_v2_staging"
_APPROVAL_DATASET = "trends_v2_staging_approvals"
_APPLY_JOB = "projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-apply-staging"
_APPLY_IDENTITY = "trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com"
_REVIEW_FIELDS = (
    "review_store_contract_version",
    "run_id",
    "canonical_review_receipt_json",
    "review_receipt_digest",
    "artifact_sha256",
    "source_window_digest",
    "candidate_projection_digest",
    "packet_digest",
    "registered_by",
    "registered_at",
)
_APPROVAL_FIELDS = (
    "approval_contract_version",
    "approval_id",
    "manifest_version",
    "operation",
    "contract_sha256",
    "manifest_sha256",
    "canonical_manifest_json",
    "approved_by",
    "approved_at",
    "expires_at",
    "approval_phrase_sha256",
)
_CONSUMPTION_FIELDS = (
    "consumption_contract_version",
    "consumption_id",
    "approval_id",
    "manifest_sha256",
    "operation",
    "execution_name",
    "job_resource",
    "source_sha",
    "image_uri",
    "consumed_at",
)
_RESULT_FIELDS = (
    "result_contract_version",
    "result_id",
    "consumption_id",
    "approval_id",
    "manifest_sha256",
    "operation",
    "execution_name",
    "result_reference",
    "canonical_result_json",
    "result_digest",
    "status",
    "completed_at",
)


def _digest(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("release_material_invalid")
    return value


def reconstruct_release_contract(
    *, receipt, batch, execution_proof_bytes, execution_proof_manifest_sha256, review_receipt
):
    try:
        receipt = build_run_receipt(**asdict(receipt))
        if receipt.status != "completed" or not receipt.complete_partitions:
            raise ValueError()
        blocked = replace(receipt, display_release_state="blocked")
        manifest_digest = _digest(execution_proof_manifest_sha256)
        if type(execution_proof_bytes) is not bytes:
            raise ValueError()
        proof = json.loads(execution_proof_bytes)
        if type(proof) is not dict or set(proof) != _EXECUTION_PROOF_FIELDS:
            raise ValueError()
        if canonical_bytes(proof) != execution_proof_bytes:
            raise ValueError()
        start = datetime.fromisoformat(proof["started_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(proof["completed_at"].replace("Z", "+00:00"))
        counts = [
            [family, count]
            for family, count in (
                ("candidates", receipt.candidate_count),
                ("evidence", receipt.evidence_count),
                ("membership", receipt.membership_count),
                ("lineage", receipt.lineage_count),
                ("predictions", receipt.prediction_count),
                ("outcomes", 0),
                ("analysis", receipt.analysis_count),
            )
        ]
        expected = {
            "execution_proof_contract_version": "r3-execution-proof-v1",
            "run_id": receipt.run_id,
            "signal_date": receipt.signal_date.isoformat(),
            "status": "succeeded",
            "source_sha": receipt.source_sha,
            "row_set_digest": receipt.row_set_digest,
            "run_receipt_digest": run_receipt_digest(blocked),
            "r3_pre_execution_addendum_sha256": manifest_digest,
            "job_resource": _APPLY_JOB,
            "service_identity": _APPLY_IDENTITY,
            "command": "python",
        }
        if any(proof[field] != value for field, value in expected.items()) or (
            canonical_bytes(proof["persisted_row_family_counts"]) != canonical_bytes(counts)
            or start.tzinfo is None
            or end.tzinfo is None
            or end < start
            or type(proof["execution_name"]) is not str
            or re.fullmatch(re.escape(_APPLY_JOB) + r"/executions/[^/]+", proof["execution_name"])
            is None
            or type(proof["image_digest"]) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", proof["image_digest"]) is None
            or type(proof["args"]) is not list
            or not proof["args"]
            or any(type(arg) is not str for arg in proof["args"])
            or proof["args"][0] != "scripts/staging/replay_open_intelligence.py"
        ):
            raise ValueError()
        _digest(proof["config_digest"])
        packet = live_quality.build_review_packet(receipt.run_id, batch)
        projection = live_quality.candidate_projection_digest(receipt.run_id, batch)
        review = live_quality.validate_review_receipt(
            review_receipt,
            packet=packet,
            source_window_digest=receipt.source_window_digest,
            candidate_projection_digest=projection,
        )
        contract = {
            "contract_version": "r3-release-contract-v1",
            "run_id": receipt.run_id,
            "source_sha": receipt.source_sha,
            "blocked_run_receipt_digest": run_receipt_digest(blocked),
            "candidate_projection_digest": projection,
            "packet_digest": packet["packet_digest"],
            "quality_review_receipt_digest": review.receipt_digest,
            "quality_review_receipt_artifact_sha256": hashlib.sha256(
                canonical_bytes(review_receipt)
            ).hexdigest(),
            "execution_proof_digest": hashlib.sha256(execution_proof_bytes).hexdigest(),
            "execution_proof_manifest_sha256": manifest_digest,
        }
        encoded = canonical_bytes(contract)
        return {
            "contract": contract,
            "contract_bytes": encoded,
            "contract_digest": hashlib.sha256(encoded).hexdigest(),
            "review_packet": packet,
            "missing_checks": [
                "producer_profile_admission",
                "execution_chain_binding",
                "release_record_binding",
                "complete_run_validation",
                "current_source_copy_validation",
                "physical_schema_validation",
                "runtime_authorization",
                "query_readback_and_sql_parity",
            ],
        }
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
        raise ValueError("release_material_invalid") from error


def _table(name, approvals=False):
    return f"`{_PROJECT}.{_APPROVAL_DATASET if approvals else _DATASET}.{name}`"


def _struct(alias, fields):
    return "STRUCT(" + ", ".join(f"{alias}.{field} AS {field}" for field in fields) + ")"


def _sql():
    from scripts.staging.replay_open_intelligence import build_partition_completeness_query

    # Release material is a retained v1 historical_read over the legacy execution tables only.
    # No 42 (v2) result can enter this reader: the admission it feeds is pinned to the legacy
    # run, contract, source and image constants, so no v2 material statement exists here.
    approvals = _table("open_intelligence_execution_approvals_v1", True)
    consumptions = _table("open_intelligence_execution_consumptions_v1", True)
    results = _table("open_intelligence_execution_results_v1", True)
    completeness = build_partition_completeness_query(
        project=_PROJECT, dataset=_DATASET, copy_run_id="__BOUND_COPY_RUN__"
    ).replace("'__BOUND_COPY_RUN__'", "(SELECT copy_run_id FROM copy_runs)")
    return f"""WITH target AS (SELECT @run_id AS run_id),
run_receipt AS (
SELECT observation_start, observation_end
FROM {_table("open_intelligence_run_receipts_v1")} WHERE run_id = @run_id
), copy_runs AS (
SELECT DISTINCT x.copy_run_id
FROM {_table("open_intelligence_source_copy_receipts_v1")} x JOIN run_receipt r
  ON x.window_start = r.observation_start AND x.window_end = r.observation_end
), source_copy_completeness AS (
{completeness}
),
release_rows AS (
SELECT {", ".join(live_quality.QUALITY_RELEASE_FIELDS)}
FROM {_table("open_intelligence_quality_release_records_v2")} WHERE run_id = @run_id
), review_rows AS (
SELECT {", ".join(_REVIEW_FIELDS)}
FROM {_table("open_intelligence_quality_review_receipts_v1")} WHERE run_id = @run_id
), chains AS (
SELECT a.operation,
  TO_JSON_STRING({_struct("a", _APPROVAL_FIELDS)}) AS approval_json,
  TO_JSON_STRING({_struct("c", _CONSUMPTION_FIELDS)}) AS consumption_json,
  TO_JSON_STRING({_struct("z", _RESULT_FIELDS)}) AS result_json,
  (SELECT COUNT(*) FROM {approvals} ax WHERE ax.approval_id = a.approval_id OR ax.manifest_sha256 = a.manifest_sha256) AS approval_identity_count,
  (SELECT COUNT(*) FROM {consumptions} cx WHERE cx.approval_id = a.approval_id OR cx.manifest_sha256 = a.manifest_sha256) AS consumption_identity_count,
  (SELECT COUNT(*) FROM {results} zx WHERE zx.consumption_id = c.consumption_id OR zx.approval_id = a.approval_id OR zx.manifest_sha256 = a.manifest_sha256) AS result_identity_count
FROM {approvals} a JOIN {consumptions} c
  ON c.approval_id = a.approval_id AND c.manifest_sha256 = a.manifest_sha256 AND c.operation = a.operation
JOIN {results} z ON z.consumption_id = c.consumption_id AND z.approval_id = a.approval_id
  AND z.manifest_sha256 = a.manifest_sha256 AND z.operation = a.operation AND z.execution_name = c.execution_name
WHERE a.operation IN ('r3_apply', 'r3_proof_issue', 'r3_release')
  AND JSON_VALUE(z.canonical_result_json, '$.run_id') = @run_id
)
SELECT target.run_id,
  (SELECT COUNT(*) FROM copy_runs) AS source_copy_run_count,
  ARRAY(SELECT AS STRUCT * FROM source_copy_completeness ORDER BY source_table) AS source_copy_completeness,
  (SELECT COUNT(*) FROM release_rows) AS release_count,
  ARRAY(SELECT AS STRUCT * FROM release_rows ORDER BY TO_JSON_STRING(release_rows) LIMIT 2) AS release_rows,
  (SELECT COUNT(*) FROM review_rows) AS review_count,
  ARRAY(SELECT AS STRUCT * FROM review_rows ORDER BY TO_JSON_STRING(review_rows) LIMIT 2) AS review_rows,
  {candidate_projection_digest_sql("target.run_id", sql_expression=True)} AS candidate_projection_digest,
  {live_quality.review_packet_digest_sql("target.run_id", sql_expression=True)} AS packet_digest,
  ARRAY(SELECT AS STRUCT op AS operation,
      (SELECT COUNT(*) FROM chains ch WHERE ch.operation = op) AS chain_count,
      ARRAY(SELECT AS STRUCT * FROM chains ch WHERE ch.operation = op
            ORDER BY approval_json, consumption_json, result_json LIMIT 2) AS rows
    FROM UNNEST(['r3_apply', 'r3_proof_issue', 'r3_release']) op ORDER BY op) AS execution_chains
FROM target"""


@dataclass(frozen=True)
class PreparedReleaseMaterialQuery:
    template_id: str
    sql: str
    parameters: tuple
    sql_digest: str
    parameters_digest: str
    candidate_limit: int = 0
    transport_row_limit: int = 1


def build_release_material_query(run_id):
    if type(run_id) is not str or re.fullmatch(r"[a-z0-9_][a-z0-9_-]{0,127}", run_id) is None:
        raise ValueError("release_material_invalid")
    sql = _sql()
    parameters = (bigquery.ScalarQueryParameter("run_id", "STRING", run_id),)
    return PreparedReleaseMaterialQuery(
        "released_review_v1",
        sql,
        parameters,
        hashlib.sha256(sql.encode()).hexdigest(),
        canonical_digest(tuple(p.to_api_repr() for p in parameters)),
    )
