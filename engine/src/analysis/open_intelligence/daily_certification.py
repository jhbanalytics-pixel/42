"""The certification record store and the certifier client the certify stage dispatches over.

``ObjectCertificationRecords`` keeps one immutable certification record per run id as a
create only object in the evidence bucket, under the object client's generation
precondition, the same pattern as the collection ledger: the first writer creates, a
second writer carrying the same bytes is a no op, and differing bytes under the same run
refuse. ``NativeCertifier`` answers the four calls the certify handler makes: ``readiness``
rebuilds the readiness inputs of every candidate of the run from the persisted candidate
and evidence rows, ``artifacts`` reads the six certification artifacts through the release
script's own readers (the apply and proof result chains, the control digests, the review
packet and the quality review receipt), and ``record`` and ``read`` are the record store.

Readiness is not release: the handler re-evaluates every candidate under the explicit
origin policy and the release stage consumes the record only through the release script's
live profile path. Nothing here opens a connection or constructs a cloud client; the
release script is imported inside the methods that use it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime

from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.daily_stages import StageRefusal
from src.analysis.open_intelligence.daily_store import PreconditionFailed
from src.analysis.open_intelligence.readiness import EvidenceRecord, ReadinessRules

CERTIFICATION_PREFIX = "42/daily/certification"
CERTIFICATION_READ_MODE = "historical_read"
# The most rows one readiness read admits; one more than this is a truncated read.
CERTIFICATION_ROW_CAP = 50_000
CANDIDATE_READINESS_FIELDS = ("signal_id", "evidence_state")
EVIDENCE_READINESS_FIELDS = (
    "signal_id",
    "row_id",
    "source_family",
    "direction",
    "published_at",
    "availability",
    "geo_confidence",
    "vendor_family",
    "channel_family",
)


def _text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(code)
    return value


def _unpack(raw: bytes, code: str) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(code) from error
    if not isinstance(value, dict):
        raise ValueError(code)
    return value


def _instant(value: object, code: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(code) from error
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(code)
    return value


class CertificationEvidenceTruncated(StageRefusal):
    """A candidate or evidence read came back capped; readiness cannot be re-evaluated.

    A stage refusal raised before the certifier's only irreversible act, so the handler's
    native boundary passes it through and the stage records
    ``certify_refused:certification_evidence_truncated:<table>`` retry safe, never
    ``certify_readiness_differs`` and never an unknown state.
    """

    def __init__(self, table: str) -> None:
        super().__init__(f"certification_evidence_truncated:{table}")


class ObjectCertificationRecords:
    """Certification records as create only objects, one per run id, never replaced."""

    def __init__(self, client, *, prefix: str = CERTIFICATION_PREFIX) -> None:
        self._client = client
        self._prefix = prefix

    def _name(self, run_id: str) -> str:
        return f"{self._prefix}/{_text(run_id, 'certification_run_invalid')}.json"

    def record(self, run_id: str, evidence: Mapping[str, object]) -> None:
        if not isinstance(evidence, Mapping):
            raise ValueError("certification_record_invalid")
        name = self._name(run_id)
        payload = canonical_bytes(dict(evidence))
        found = self._client.read(name)
        if found is None:
            try:
                self._client.write(name, payload, if_generation_match=0)
            except PreconditionFailed:
                found = self._client.read(name)
                if found is None:
                    raise ValueError("certification_conflict") from None
            else:
                return
        if found[0] != payload:
            raise ValueError("certification_conflict")

    def read(self, run_id: str) -> dict | None:
        found = self._client.read(self._name(run_id))
        if found is None:
            return None
        return _unpack(found[0], "certification_record_invalid")


def readiness_records(rows: object) -> dict[str, list[EvidenceRecord]]:
    """The persisted evidence rows of one run as readiness records, grouped by signal id."""
    grouped: dict[str, list[EvidenceRecord]] = {}
    if not isinstance(rows, list):
        raise ValueError("certify_evidence_rows_invalid")
    for row in rows:
        if not isinstance(row, Mapping) or set(EVIDENCE_READINESS_FIELDS) - set(row):
            raise ValueError("certify_evidence_rows_invalid")
        signal_id = _text(row["signal_id"], "certify_evidence_rows_invalid")
        grouped.setdefault(signal_id, []).append(
            EvidenceRecord(
                row_id=row["row_id"],
                source_family=row["source_family"],
                direction=row["direction"],
                published_at=_instant(row["published_at"], "certify_evidence_rows_invalid"),
                availability=row["availability"],
                geo_confidence=row["geo_confidence"],
                vendor_family=row["vendor_family"],
                channel_family=row["channel_family"],
            )
        )
    return grouped


class NativeCertifier:
    """The certify stage client over the release script's readers and the record store.

    ``readiness`` reads the run's candidates and evidence through the release script's
    query seam over the injected BigQuery client and hands back the readiness inputs the
    handler re-evaluates: per candidate its persisted evidence state and its evidence
    records, the readiness rules of the closed day, and whether a quality review receipt
    is registered for the run. ``artifacts`` reads exactly what the release script's own
    release input reader reads, through the same functions, for the live profile.
    """

    def __init__(
        self, *, object_client, warehouse, release_profile, generation, readiness_rules
    ) -> None:
        if not isinstance(readiness_rules, ReadinessRules):
            raise ValueError("certify_readiness_rules_invalid")
        self._records = ObjectCertificationRecords(object_client)
        self._warehouse = warehouse
        self._profile = release_profile
        self._generation = generation
        self._rules = readiness_rules

    def _release(self):
        from scripts.staging import release_open_intelligence_run

        return release_open_intelligence_run

    def _rows(self, table: str, fields: tuple[str, ...], run_id: str, order: str) -> list[dict]:
        from google.cloud import bigquery

        release = self._release()
        columns = ", ".join(f"`{field}`" for field in fields)
        rows = release._query(
            self._warehouse,
            (
                f"SELECT {columns} FROM `{release.PROJECT}.{release.DATASET}.{table}` "
                "WHERE run_id = @run_id AND client_scope_id != 'qa_canary' "
                f"ORDER BY {order}"
            ),
            parameters=(bigquery.ScalarQueryParameter("run_id", "STRING", run_id),),
            max_results=CERTIFICATION_ROW_CAP + 1,
        )
        if len(rows) > CERTIFICATION_ROW_CAP:
            raise CertificationEvidenceTruncated(table)
        return [dict(row) for row in rows]

    def _quality_evaluated(self, run_id: str) -> bool:
        """Whether the quality review receipt authority holds exactly one receipt for the run.

        The same procedure the release script's receipt reader calls first; the receipt
        itself is validated later by ``artifacts`` through that reader.
        """
        release = self._release()
        rows = list(
            release._query(
                self._warehouse,
                f"CALL `{release.PROJECT}.{release.DATASET}."
                "sp_read_open_intelligence_quality_review_receipt_v1`(@run_id)",
                parameters=(self._parameter(run_id),),
                max_results=2,
            )
        )
        return len(rows) == 1 and dict(rows[0]).get("run_id") == run_id

    def _parameter(self, run_id: str):
        from google.cloud import bigquery

        return bigquery.ScalarQueryParameter("run_id", "STRING", run_id)

    def readiness(self, run_id: str) -> dict:
        release = self._release()
        run = _text(run_id, "certify_run_invalid")
        if run != self._profile.run_id:
            raise ValueError("certify_run_differs")
        candidates = self._rows(
            release.CANDIDATES_TABLE, CANDIDATE_READINESS_FIELDS, run, "signal_id"
        )
        records = readiness_records(
            self._rows(release.EVIDENCE_TABLE, EVIDENCE_READINESS_FIELDS, run, "signal_id, row_id")
        )
        return {
            "candidates": [
                {
                    "candidate": {field: candidate[field] for field in CANDIDATE_READINESS_FIELDS},
                    "records": list(records.get(candidate["signal_id"], ())),
                }
                for candidate in candidates
            ],
            "rules": self._rules,
            "quality_evaluated": self._quality_evaluated(run),
        }

    def artifacts(self, receipt) -> dict:
        from src.analysis.open_intelligence import execution_approval

        release = self._release()
        client, profile = self._warehouse, self._profile
        if getattr(receipt, "run_id", None) != profile.run_id:
            raise ValueError("certify_receipt_differs")
        chain = {
            operation: release._read_operation_result_chain(
                client,
                operation,
                version=execution_approval._RESULT_VERSION_V2,
                mode=CERTIFICATION_READ_MODE,
                generation=self._generation,
                profile=profile,
            )
            for operation in ("r3_apply", "r3_proof_issue")
        }
        proof_bytes, proof_manifest_sha256 = release._proof_ledger_binding(
            chain["r3_proof_issue"],
            chain["r3_apply"],
            mode=CERTIFICATION_READ_MODE,
            registry=chain["r3_proof_issue"]["registry"],
            profile=profile,
        )
        candidate_digest, packet_digest = release._control_digests(client, profile=profile)
        packet = release._read_review_packet(client, profile=profile)
        if packet.get("packet_digest") != packet_digest:
            raise ValueError("certify_packet_digest_differs")
        review = release._read_quality_review_receipt(
            client,
            packet=packet,
            source_window_digest=receipt.source_window_digest,
            candidate_projection_digest=candidate_digest,
            profile=profile,
        )
        return {
            "execution_proof_bytes": proof_bytes,
            "execution_proof_manifest_sha256": proof_manifest_sha256,
            "quality_review_receipt": review,
            "review_packet": packet,
            "candidate_projection_digest": candidate_digest,
            "apply_binding": chain["r3_apply"]["binding"],
        }

    def record(self, run_id: str, evidence: Mapping[str, object]) -> None:
        self._records.record(run_id, evidence)

    def read(self, run_id: str) -> dict | None:
        return self._records.read(run_id)


__all__ = [
    "CANDIDATE_READINESS_FIELDS",
    "CERTIFICATION_PREFIX",
    "CERTIFICATION_ROW_CAP",
    "EVIDENCE_READINESS_FIELDS",
    "CertificationEvidenceTruncated",
    "NativeCertifier",
    "ObjectCertificationRecords",
    "readiness_records",
]
