"""Parity proof for the v3 approval routines: v2 extended by the capture branch and the grant kind."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from scripts.migrations import create_open_intelligence_execution_approval_store as migration
from scripts.staging import approve_open_intelligence_execution as approval_cli
from src.analysis.open_intelligence import execution_approval, recurring_grant_phrases

ROOT = Path(__file__).resolve().parents[2]
ROUTINES = ROOT / "infra" / "bigquery_routines"
APPROVE_V2 = "sp_approve_open_intelligence_execution_v2"
APPROVE_V3 = "sp_approve_open_intelligence_execution_v3"
READ_V2 = "sp_read_open_intelligence_execution_approval_v2"
READ_V3 = "sp_read_open_intelligence_execution_approval_v3"
APPROVED_BY = "usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab"
GRANT_CONTRACT = "42_recurring_execution_grant_v1"
GRANT_KIND = "recurring_grant_v1"
REVOCATION_KIND = "recurring_grant_revocation_v1"
APPROVAL_V2 = "open_intelligence_execution_approval_v2"
APPROVAL_V3 = "open_intelligence_execution_approval_v3"
# Recorded at c2debc8 before this packet. The v2 pair must stay byte identical.
V2_SHA256 = {
    f"{APPROVE_V2}.sql": "52e0bb73fde96bc334a3a0c98b4e816582cd6587e9a4ff0dae544cddde079826",
    f"{READ_V2}.sql": "2605e99d13cd5b9bb464a03b71a7f792c6b4e5e1a3e1098000a8ea3653e0f758",
}
CAPTURE_WHEN_LINES = (
    "WHEN 'source_snapshot_capture' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.source_snapshot_capture')",
    "WHEN 'source_snapshot_capture' THEN JSON_QUERY(v_policy_json, '$.operation_validation.source_snapshot_capture')",
)
CAPTURE_KIND_BRANCH = "WHEN 'source_snapshot_capture_v2' THEN ARRAY_LENGTH(v_arguments) = 7"
V2_REFUSAL_CODES = {
    "execution_approval_identity_invalid",
    "execution_approval_v1_lock_not_disabled",
    "execution_approval_lock_not_ready",
    "execution_approval_concurrent_conflict",
    "execution_approval_generation_invalid",
    "execution_approval_generation_inactive",
    "execution_origin_registry_digest_mismatch",
    "execution_origin_registry_invalid",
    "execution_approval_manifest_invalid",
    "execution_approval_manifest_mismatch",
    "execution_origin_pair_invalid",
    "execution_origin_policy_invalid",
    "execution_approval_target_invalid",
    "execution_approval_artifact_mismatch",
    "execution_approval_expired",
    "execution_approval_conflict",
    "execution_approval_consumed",
    "execution_approval_schema_mismatch",
    "execution_approval_lock_invalid",
}
PROPOSAL_SHA256 = "c1a75451" + "0" * 56
GRANT_ID = "recurring_execution_20260915_v1"
VALID_FROM = datetime(2026, 9, 15, tzinfo=UTC)
EXECUTING_PRINCIPALS = sorted(
    f"intelligence-42-{name}@ogilvy-trends-v2.iam.gserviceaccount.com"
    for name in ("orchestration", "ingest", "brain", "proof", "release")
)


def _sql(name: str) -> str:
    return (ROUTINES / f"{name}.sql").read_text(encoding="utf-8")


def _codes(sql: str) -> set[str]:
    return set(re.findall(r"AS '([a-z0-9_]+)'", sql))


def _resource_sha256() -> str:
    # Grants bind the active generation's manifest, the bridge manifest.
    return migration.REGISTRATION_DIGESTS["resource_manifest_bridge_v3.json"]


def sample_grant(**overrides) -> dict:
    cutoffs = [(VALID_FROM + timedelta(days=offset)).date().isoformat() for offset in range(30)]
    grant = {
        "contract_version": GRANT_CONTRACT,
        "grant_id": GRANT_ID,
        "environment": "staging",
        "issuing_principal": APPROVED_BY,
        "executing_principals": EXECUTING_PRINCIPALS,
        "resource_manifest_digest": _resource_sha256(),
        "allowed_operations": [
            "collection",
            "immutable_capture",
            "composition",
            "quality_proof_issuance",
            "release_evidence_qualified_staging_results",
        ],
        "source_policy_digest": "1" * 64,
        "schema_version": "42_daily_v1",
        "approved_release_manifest_digest": "2" * 64,
        "permitted_image_digests": ["sha256:" + "3" * 64],
        "run_allowance_micro_usd": 450000,
        "monthly_allowance_micro_usd": 15000000,
        "reserved_micro_usd_per_capture": 500000,
        "cumulative_ceiling_micro_usd": 15000000,
        "permitted_cutoffs": cutoffs,
        "valid_from": VALID_FROM.isoformat(),
        "valid_until": (VALID_FROM + timedelta(days=30)).isoformat(),
        "retry_rules": {
            "failed_stage": "retry_safe_only",
            "unknown_or_partial": "hold_for_reconciliation",
            "max_attempt_versions": 3,
        },
        "revocation_state": "active",
        "revocation_phrase_sha256": hashlib.sha256(
            recurring_grant_phrases.revocation_phrase(GRANT_ID).encode("utf-8")
        ).hexdigest(),
    }
    grant.update(overrides)
    return grant


def sample_row(*, approved_at=None, revocation_state="active", revoked_at=None, **overrides):
    grant = sample_grant()
    generation = execution_approval.execution_generations.active_generation()
    canonical = recurring_grant_phrases.canonical_grant_bytes(grant).decode("utf-8")
    digest = recurring_grant_phrases.grant_digest(grant)
    approved_at = approved_at or datetime(2026, 9, 15, 8, 30, tzinfo=UTC)
    row = {
        "approval_contract_version": APPROVAL_V2,
        "approval_id": execution_approval.approval_id_v2(
            digest,
            APPROVED_BY,
            approved_at,
            origin_registry_sha256=generation.origin_registry_sha256,
            resource_manifest_sha256=generation.resource_manifest_sha256,
        ),
        "manifest_version": GRANT_CONTRACT,
        "operation": GRANT_KIND,
        "contract_sha256": PROPOSAL_SHA256,
        "manifest_sha256": digest,
        "canonical_manifest_json": canonical,
        "approved_by": APPROVED_BY,
        "approved_at": approved_at,
        "expires_at": VALID_FROM + timedelta(days=30),
        "approval_phrase_sha256": recurring_grant_phrases.approval_phrase_sha256(PROPOSAL_SHA256),
        "origin_registry_sha256": generation.origin_registry_sha256,
        "resource_manifest_sha256": generation.resource_manifest_sha256,
        "revocation_state": revocation_state,
        "revoked_at": revoked_at,
    }
    row.update(overrides)
    return row


def test_v2_routines_are_byte_identical_to_the_pinned_review():
    for name, digest in V2_SHA256.items():
        assert hashlib.sha256((ROUTINES / name).read_bytes()).hexdigest() == digest, name


def test_v3_routines_exist_under_their_own_names_with_the_v2_parameter_lists():
    approve = _sql(APPROVE_V3)
    read = _sql(READ_V3)
    assert approve.startswith(
        f"CREATE OR REPLACE PROCEDURE `{{project}}.{{dataset}}.{APPROVE_V3}`("
    )
    assert read.startswith(f"CREATE OR REPLACE PROCEDURE `{{project}}.{{dataset}}.{READ_V3}`(")
    specs = {name: (role, parameters) for name, role, parameters in migration.V2_ROUTINE_SPECS}
    assert specs[APPROVE_V3] == specs[APPROVE_V2]
    assert specs[READ_V3] == specs[READ_V2]
    assert specs[APPROVE_V3][0] == "roles/bigquery.routineDataEditor"
    assert specs[READ_V3][0] == "roles/bigquery.routineDataViewer"
    assert APPROVE_V2 not in approve
    assert READ_V2 not in read
    assert "'open_intelligence_execution_approval_receipt_v3' AS contract_version" in approve


@pytest.mark.parametrize("v2,v3", [(APPROVE_V2, APPROVE_V3), (READ_V2, READ_V3)])
def test_v3_is_a_copy_of_v2_every_v2_line_survives(v2, v3):
    """Every v2 statement line is present in v3, reindented at most. The lines the tail
    re-flows are named: the CREATE line, the two manifest_version readbacks that become
    a variable so a grant row can carry the grant contract, and the receipt line."""
    reflowed = {
        f"CREATE OR REPLACE PROCEDURE `{{project}}.{{dataset}}.{v2}`(",
        "JSON_VALUE(v_canonical_manifest_json, '$.manifest_version'), v_operation,",
        "AND open_intelligence_execution_approvals_v2.manifest_version = JSON_VALUE(v_canonical_manifest_json, '$.manifest_version')",
        "SELECT 'open_intelligence_execution_approval_receipt_v2' AS contract_version,",
    }
    v3_lines = {line.strip() for line in _sql(v3).splitlines()}
    missing = [
        line.strip()
        for line in _sql(v2).splitlines()
        if line.strip() and line.strip() not in v3_lines and line.strip() not in reflowed
    ]
    assert missing == []


def test_v3_carries_every_v2_refusal_code_and_the_capture_branches():
    approve = _sql(APPROVE_V3)
    read = _sql(READ_V3)
    assert _codes(_sql(APPROVE_V2)) == V2_REFUSAL_CODES
    assert _codes(approve) >= V2_REFUSAL_CODES
    assert _codes(_sql(READ_V2)) <= _codes(read)
    assert "execution_approval_unavailable" in _codes(approve)
    for line in CAPTURE_WHEN_LINES:
        assert approve.count(line) == 1, line
        assert read.count(line) == 1, line
        assert line not in _sql(APPROVE_V2)
        assert line not in _sql(READ_V2)
    assert CAPTURE_KIND_BRANCH in approve
    assert CAPTURE_KIND_BRANCH not in _sql(APPROVE_V2)
    consume = _sql("sp_consume_open_intelligence_source_snapshot_v2")
    assert (
        "'$.arguments[0]') = 'scripts/staging/capture_protected_production_snapshot.py'" in consume
    )
    for check in (
        "REGEXP_CONTAINS(v_arguments[SAFE_OFFSET(2)], JSON_VALUE(v_rule, '$.arguments.cutoff_date_regex'))",
        "SAFE.PARSE_DATE('%Y-%m-%d', v_arguments[SAFE_OFFSET(2)]) IS NOT NULL",
        "v_arguments[SAFE_OFFSET(3)] = JSON_VALUE(v_rule, '$.arguments.mode_switch')",
        "v_arguments[SAFE_OFFSET(4)] IN UNNEST(JSON_VALUE_ARRAY(v_rule, '$.arguments.modes'))",
        "v_arguments[SAFE_OFFSET(5)] = JSON_VALUE(v_rule, '$.arguments.grant_switch')",
        "REGEXP_CONTAINS(v_arguments[SAFE_OFFSET(6)], JSON_VALUE(v_rule, '$.arguments.grant_id_regex'))",
    ):
        assert check in approve, check


def test_grant_kind_rows_are_named_in_both_v3_routines_and_bind_the_durable_identity():
    approve = _sql(APPROVE_V3)
    read = _sql(READ_V3)
    for sql in (approve, read):
        assert f"'{GRANT_KIND}'" in sql
        assert f"'{REVOCATION_KIND}'" in sql
        assert f"'{GRANT_CONTRACT}'" in sql
        assert f"'{APPROVED_BY}'" in sql
    assert "JSON_VALUE(v_canonical_manifest_json, '$.issuing_principal') = v_actor" in approve
    assert (
        "JSON_VALUE(v_canonical_manifest_json, '$.resource_manifest_digest') = v_resource_manifest_sha256"
        in approve
    )
    assert "JSON_VALUE(v_canonical_manifest_json, '$.revocation_state') = 'active'" in approve
    fields = ",".join(recurring_grant_phrases.GRANT_FIELDS)
    assert f"= '{fields}'" in approve
    assert ',"revocation_state":"active"' in approve
    assert "SESSION_USER() IN UNNEST(JSON_VALUE_ARRAY(" in read
    assert "'$.executing_principals'" in read
    assert "AS revocation_state" in read
    assert "AS revoked_at" in read


def test_grant_phrase_formulas_in_sql_equal_the_python_functions():
    approve = _sql(APPROVE_V3)
    read = _sql(READ_V3)
    prefix = re.search(r"CONCAT\('([^']+)', v_contract_sha256\)", approve).group(1)
    assert prefix == recurring_grant_phrases.APPROVAL_PHRASE_PREFIX
    assert prefix + PROPOSAL_SHA256 == recurring_grant_phrases.approval_phrase(PROPOSAL_SHA256)
    assert prefix + PROPOSAL_SHA256 == "Approve recurring execution grant " + PROPOSAL_SHA256
    assert f"CONCAT('{prefix}', v_contract_sha256)" in read
    assert re.search(r"REGEXP_CONTAINS\(v_approval_phrase, r'\^" + re.escape(prefix), approve)
    prefixes = set(re.findall(r"STARTS_WITH\(v_approval_phrase, '([^']+)'\)", approve))
    assert prefixes == {prefix, recurring_grant_phrases.REVOCATION_PHRASE_PREFIX}
    revoke = next(item for item in prefixes if item.startswith("Revoke"))
    assert revoke == recurring_grant_phrases.REVOCATION_PHRASE_PREFIX
    assert revoke + GRANT_ID == recurring_grant_phrases.revocation_phrase(GRANT_ID)
    assert (
        "LOWER(TO_HEX(SHA256(v_approval_phrase))) = JSON_VALUE(v_canonical_manifest_json, '$.revocation_phrase_sha256')"
        in approve
    )
    assert (
        recurring_grant_phrases.approval_phrase_sha256(PROPOSAL_SHA256)
        == hashlib.sha256(
            ("Approve recurring execution grant " + PROPOSAL_SHA256).encode("utf-8")
        ).hexdigest()
    )
    for bad in ("A" * 64, "a" * 63, "", None):
        with pytest.raises(ValueError, match="recurring_grant_phrase_invalid"):
            recurring_grant_phrases.approval_phrase(bad)
    for bad in ("Grant-1", "", None):
        with pytest.raises(ValueError, match="recurring_grant_phrase_invalid"):
            recurring_grant_phrases.revocation_phrase(bad)


def test_grant_digest_excludes_revocation_state_and_requires_the_exact_field_set():
    grant = sample_grant()
    digest = recurring_grant_phrases.grant_digest(grant)
    terms = {key: value for key, value in grant.items() if key != "revocation_state"}
    expected = hashlib.sha256(
        json.dumps(terms, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert digest == expected
    assert recurring_grant_phrases.grant_digest(sample_grant(revocation_state="revoked")) == digest
    canonical = recurring_grant_phrases.canonical_grant_bytes(grant)
    assert canonical == json.dumps(
        grant, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    assert canonical.count(b',"revocation_state":"active"') == 1
    assert (
        hashlib.sha256(canonical.replace(b',"revocation_state":"active"', b"")).hexdigest()
        == digest
    )
    for broken in (
        {**grant, "extra": 1},
        {key: value for key, value in grant.items() if key != "grant_id"},
        {**grant, "contract_version": "42_recurring_execution_grant_v2"},
        {**grant, "revocation_state": "paused"},
        [],
        None,
    ):
        with pytest.raises(ValueError, match="recurring_grant_invalid"):
            recurring_grant_phrases.grant_digest(broken)
    assert (
        tuple(sorted(recurring_grant_phrases.GRANT_FIELDS)) == recurring_grant_phrases.GRANT_FIELDS
    )
    assert len(recurring_grant_phrases.GRANT_FIELDS) == 21


def test_reader_v3_serves_a_grant_row_through_the_default_reader():
    row = sample_row()
    calls = []

    def query(procedure, parameters):
        calls.append((procedure, tuple((item.name, item.value) for item in parameters)))
        return row

    approval = execution_approval._default_approval_reader(
        row["manifest_sha256"], version=APPROVAL_V3, mode="new_consume", query=query
    )
    assert calls == [(READ_V3, (("manifest_sha256", row["manifest_sha256"]),))]
    assert type(approval) is execution_approval.RecurringGrantApproval
    assert approval.approved_by == APPROVED_BY
    assert approval.approved_at == row["approved_at"]
    assert approval.approval_phrase_sha256 == row["approval_phrase_sha256"]
    assert approval.manifest_sha256 == row["manifest_sha256"]
    assert approval.operation == GRANT_KIND
    assert approval.revocation_state == "active"
    assert approval.revoked_at is None
    assert approval.grant()["grant_id"] == GRANT_ID


def test_reader_v3_refuses_a_revoked_or_forged_grant_row():
    def reader(row):
        return execution_approval._default_approval_reader(
            row["manifest_sha256"], version=APPROVAL_V3, mode="new_consume", query=lambda *_: row
        )

    with pytest.raises(execution_approval.ApprovalRefusal, match=r"^execution_approval_revoked$"):
        reader(sample_row(revocation_state="revoked", revoked_at=datetime(2026, 9, 16, tzinfo=UTC)))
    forged = (
        {"approved_by": "usr_" + "0" * 64},
        {
            "approval_phrase_sha256": hashlib.sha256(
                b"Approve recurring execution grant"
            ).hexdigest()
        },
        {"manifest_sha256": "f" * 64},
        {"contract_sha256": "not a digest"},
        {"manifest_version": "open_intelligence_execution_manifest_v2"},
        {"approval_contract_version": APPROVAL_V3},
        {"expires_at": VALID_FROM + timedelta(days=31)},
        {"approval_id": "exa_" + "0" * 64},
        {"revocation_state": "paused"},
        {"canonical_manifest_json": json.dumps(sample_grant(), sort_keys=True)},
        {
            "canonical_manifest_json": recurring_grant_phrases.canonical_grant_bytes(
                sample_grant(resource_manifest_digest="4" * 64)
            ).decode("utf-8")
        },
        {
            "canonical_manifest_json": recurring_grant_phrases.canonical_grant_bytes(
                sample_grant(issuing_principal="Albert Meintjes")
            ).decode("utf-8")
        },
    )
    for override in forged:
        with pytest.raises(
            execution_approval.ApprovalRefusal, match=r"^execution_approval_schema_mismatch$"
        ):
            reader(sample_row(**override))
    with pytest.raises(
        execution_approval.ApprovalRefusal, match=r"^execution_approval_schema_mismatch$"
    ):
        execution_approval._default_approval_reader(
            "a" * 64, version=APPROVAL_V2, mode="new_consume", query=lambda *_: sample_row()
        )


def test_v3_version_selects_the_v3_reader_and_still_serves_manifest_rows_through_v2_parsing():
    seen = []

    def query(procedure, parameters):
        seen.append(procedure)
        return {"operation": "brain_read"}

    with pytest.raises(
        execution_approval.ApprovalRefusal, match=r"^execution_approval_schema_mismatch$"
    ):
        execution_approval._default_approval_reader(
            "a" * 64, version=APPROVAL_V3, mode="new_consume", query=query
        )
    assert seen == [READ_V3]
    assert execution_approval._APPROVAL_VERSION_V3 == APPROVAL_V3
    assert execution_approval._RECURRING_GRANT_KIND == GRANT_KIND
    assert execution_approval._RECURRING_GRANT_REVOCATION_KIND == REVOCATION_KIND


def test_registration_installs_v3_beside_v2_with_the_same_bindings():
    plan = migration.build_v2_plan()
    names = [item.name for item in plan.routines]
    assert names.index(APPROVE_V3) == names.index(APPROVE_V2) + 1
    assert names.index(READ_V3) == names.index(READ_V2) + 1
    assert APPROVE_V3 in migration.V2_OPERATOR_ROUTINES
    assert READ_V3 in migration.V2_RUNTIME_ROUTINES
    human = "user:albert.meintjes@ogilvy.co.za"
    by_principal: dict[str, set[str]] = {}
    for binding in plan.iam_plan.principal_routine_bindings:
        by_principal.setdefault(binding.principal, set()).add(binding.resource.rsplit("/", 1)[1])
    assert {APPROVE_V2, APPROVE_V3} <= by_principal[human]
    assert READ_V3 not in by_principal[human]
    for principal, routines in by_principal.items():
        if principal == human:
            continue
        assert (READ_V2 in routines) == (READ_V3 in routines)
        assert APPROVE_V3 not in routines
    roles = {
        item.routine.rsplit("/", 1)[1]: item.role for item in plan.iam_plan.routine_authorizations
    }
    assert roles[APPROVE_V3] == roles[APPROVE_V2]
    assert roles[READ_V3] == roles[READ_V2]
    manifest = json.loads(migration.RESOURCE_MANIFEST_PATH.read_bytes())
    rows = {item["name"].rsplit("/", 1)[1]: item["actions"] for item in manifest["resources"]}
    assert rows[APPROVE_V3] == rows[APPROVE_V2] == ["deploy", "invoke", "read"]
    assert rows[READ_V3] == rows[READ_V2] == ["deploy", "invoke", "read"]
    ordered = [item["name"] for item in manifest["resources"]]
    assert ordered == sorted(ordered)
    packaged = hashlib.sha256(migration.RESOURCE_MANIFEST_PATH.read_bytes()).hexdigest()
    assert packaged == migration.REGISTRATION_DIGESTS["resource_manifest.json"]
    assert packaged != "93a6669546a145929c86d10ed67c0e0884bea8260175d55503dc4dff1c377845"


def test_cli_grant_words_call_v3_under_the_pinned_actor_only(tmp_path, monkeypatch):
    grant = sample_grant()
    path = tmp_path / "grant.json"
    path.write_bytes(recurring_grant_phrases.canonical_grant_bytes(grant))
    digest = recurring_grant_phrases.grant_digest(grant)
    generation = execution_approval.execution_generations.active_generation()
    approved_at = datetime(2026, 9, 15, 8, 30, tzinfo=UTC)
    receipt_row = {
        "contract_version": "open_intelligence_execution_approval_receipt_v3",
        "approval_id": execution_approval.approval_id_v2(
            digest,
            APPROVED_BY,
            approved_at,
            origin_registry_sha256=generation.origin_registry_sha256,
            resource_manifest_sha256=generation.resource_manifest_sha256,
        ),
        "approved_at": approved_at,
        "approved_by": APPROVED_BY,
        "expires_at": VALID_FROM + timedelta(days=30),
        "manifest_sha256": digest,
        "operation": GRANT_KIND,
        "origin_registry_sha256": generation.origin_registry_sha256,
        "resource_manifest_sha256": generation.resource_manifest_sha256,
    }
    calls = []

    class Client:
        def query(self, sql, job_config=None, **kwargs):
            calls.append((sql, job_config))

            class Job:
                def result(self, **_kwargs):
                    if "SESSION_USER()" in sql:
                        return [{"session_user": "albert.meintjes@ogilvy.co.za"}]
                    return [receipt_row]

            return Job()

    monkeypatch.setattr(approval_cli, "_load_credentials", lambda: object())
    monkeypatch.setattr(approval_cli, "_bigquery_client", lambda _credentials: Client())
    monkeypatch.setattr(
        approval_cli,
        "_read_approval_phrase",
        lambda: recurring_grant_phrases.approval_phrase(PROPOSAL_SHA256),
    )
    receipt = approval_cli.approve_grant(path.resolve(), digest)
    assert receipt["manifest_sha256"] == digest
    assert receipt["operation"] == GRANT_KIND
    assert receipt["approved_by"] == APPROVED_BY
    assert receipt["origin_mode"] == "new_approval"
    assert f"`ogilvy-trends-v2.trends_v2_staging_approvals.{APPROVE_V3}`(" in calls[-1][0]
    parameters = calls[-1][1].query_parameters
    assert tuple(item.name for item in parameters) == (
        "canonical_manifest_json",
        "manifest_sha256",
        "approval_phrase",
        "origin_registry_sha256",
        "resource_manifest_sha256",
    )
    assert parameters[0].value == path.read_bytes().decode("utf-8")
    assert parameters[1].value == digest
    assert parameters[2].value == "Approve recurring execution grant " + PROPOSAL_SHA256
    # The revocation word carries the same grant bytes and the revocation phrase.
    receipt_row["operation"] = REVOCATION_KIND
    monkeypatch.setattr(
        approval_cli,
        "_read_approval_phrase",
        lambda: recurring_grant_phrases.revocation_phrase(GRANT_ID),
    )
    receipt = approval_cli.revoke_grant(path.resolve(), digest)
    assert receipt["operation"] == REVOCATION_KIND
    assert calls[-1][1].query_parameters[2].value == "Revoke recurring execution grant " + GRANT_ID
    # A wrong receipt operation or a non pinned actor refuses before any write.
    receipt_row["operation"] = GRANT_KIND
    with pytest.raises(approval_cli.ApprovalCliRefusal, match="execution_approval_schema_mismatch"):
        approval_cli.revoke_grant(path.resolve(), digest)
    writes = len(calls)

    class OtherClient(Client):
        def query(self, sql, job_config=None, **kwargs):
            calls.append((sql, job_config))

            class Job:
                def result(self, **_kwargs):
                    return [{"session_user": "someone-else@example.com"}]

            return Job()

    monkeypatch.setattr(
        approval_cli,
        "_read_approval_phrase",
        lambda: recurring_grant_phrases.approval_phrase(PROPOSAL_SHA256),
    )
    monkeypatch.setattr(approval_cli, "_bigquery_client", lambda _credentials: OtherClient())
    with pytest.raises(
        approval_cli.ApprovalCliRefusal, match="execution_approval_identity_invalid"
    ):
        approval_cli.approve_grant(path.resolve(), digest)
    assert len(calls) == writes + 1
    assert "SESSION_USER()" in calls[-1][0]


def test_cli_grant_words_refuse_drifted_bytes_or_digest_before_any_client_call(
    tmp_path, monkeypatch
):
    grant = sample_grant()
    digest = recurring_grant_phrases.grant_digest(grant)
    monkeypatch.setattr(approval_cli, "_load_credentials", lambda: pytest.fail("client built"))
    pretty = tmp_path / "pretty.json"
    pretty.write_text(json.dumps(grant, indent=2), encoding="utf-8")
    with pytest.raises(
        approval_cli.ApprovalCliRefusal, match="execution_approval_manifest_mismatch"
    ):
        approval_cli.approve_grant(pretty.resolve(), digest)
    canonical = tmp_path / "grant.json"
    canonical.write_bytes(recurring_grant_phrases.canonical_grant_bytes(grant))
    with pytest.raises(
        approval_cli.ApprovalCliRefusal, match="execution_approval_manifest_mismatch"
    ):
        approval_cli.revoke_grant(canonical.resolve(), "0" * 64)
    with pytest.raises(
        approval_cli.ApprovalCliRefusal, match="execution_approval_manifest_invalid"
    ):
        approval_cli.approve_grant(Path("grant.json"), digest)
    revoked = tmp_path / "revoked.json"
    revoked.write_bytes(
        recurring_grant_phrases.canonical_grant_bytes(sample_grant(revocation_state="revoked"))
    )
    with pytest.raises(
        approval_cli.ApprovalCliRefusal, match="execution_approval_manifest_invalid"
    ):
        approval_cli.approve_grant(revoked.resolve(), digest)
    for argv in (
        ["approve-grant", "--grant-file", "x", "--grant-sha256", "a" * 64],
        [
            "revoke-grant",
            "--grant-sha256",
            "a" * 64,
            "--grant-file",
            "x",
            "--approval-phrase-stdin",
        ],
        ["approve-grant", "--grant-file", "x", "--approval-phrase-stdin"],
    ):
        assert approval_cli.main(argv) == 2
    assert approval_cli._parse_cli(
        (
            "approve-grant",
            "--grant-file",
            "x",
            "--grant-sha256",
            "a" * 64,
            "--approval-phrase-stdin",
        )
    ) == ("approve-grant", Path("x"), "a" * 64)
    assert approval_cli._parse_cli(
        ("revoke-grant", "--grant-file", "x", "--grant-sha256", "a" * 64, "--approval-phrase-stdin")
    ) == ("revoke-grant", Path("x"), "a" * 64)


def test_capture_manifests_approve_through_v3_and_other_operations_stay_on_v2():
    assert approval_cli._approve_routine("source_snapshot_capture") == APPROVE_V3
    for operation in ("brain_read", "r3_apply", "migration_apply", "wave1_pilot"):
        assert approval_cli._approve_routine(operation) == APPROVE_V2
    assert APPROVE_V3 in approval_cli._ROUTINES
    assert READ_V3 not in approval_cli._ROUTINES


MANIFEST_PHRASE = (
    "I approve one 42 staging execution of brain_read for manifest SHA256 "
    + "a" * 64
    + ", origin registry SHA256 "
    + "b" * 64
    + " and resource manifest SHA256 "
    + "c" * 64
    + ". Production remains unchanged."
)


def test_grant_words_refuse_a_phrase_of_another_kind_before_any_client_call(tmp_path, monkeypatch):
    """The word names the kind; a phrase of another kind refuses before a client exists,
    so the routine can never record a kind the operator did not name."""
    grant = sample_grant()
    path = tmp_path / "grant.json"
    path.write_bytes(recurring_grant_phrases.canonical_grant_bytes(grant))
    digest = recurring_grant_phrases.grant_digest(grant)
    monkeypatch.setattr(approval_cli, "_load_credentials", lambda: pytest.fail("client built"))
    cases = (
        (approval_cli.approve_grant, recurring_grant_phrases.revocation_phrase(GRANT_ID)),
        (approval_cli.approve_grant, MANIFEST_PHRASE),
        (approval_cli.approve_grant, "approve recurring execution grant " + PROPOSAL_SHA256),
        (approval_cli.revoke_grant, recurring_grant_phrases.approval_phrase(PROPOSAL_SHA256)),
        (approval_cli.revoke_grant, MANIFEST_PHRASE),
        (approval_cli.revoke_grant, "Revoke recurring execution grant"),
    )
    for word, phrase in cases:
        monkeypatch.setattr(approval_cli, "_read_approval_phrase", lambda phrase=phrase: phrase)
        with pytest.raises(
            approval_cli.ApprovalCliRefusal, match=r"^approval_phrase_kind_mismatch$"
        ):
            word(path.resolve(), digest)
    assert (
        approval_cli._require_phrase_kind(
            recurring_grant_phrases.approval_phrase(PROPOSAL_SHA256), GRANT_KIND
        )
        is None
    )
    assert (
        approval_cli._require_phrase_kind(
            recurring_grant_phrases.revocation_phrase(GRANT_ID), REVOCATION_KIND
        )
        is None
    )
    assert approval_cli._require_phrase_kind(MANIFEST_PHRASE, "execution_manifest_v2") is None
    with pytest.raises(approval_cli.ApprovalCliRefusal, match=r"^approval_phrase_kind_mismatch$"):
        approval_cli._require_phrase_kind(MANIFEST_PHRASE, "unknown_kind")


def test_grant_document_spellings_are_pinned_to_the_renderer():
    """The SQL admits allowed_operations in any order and any castable instant spelling;
    the Python side refuses both so a hand edited grant cannot be recorded under a digest
    the integration module never computes."""
    assert recurring_grant_phrases.ALLOWED_OPERATIONS == (
        "collection",
        "immutable_capture",
        "composition",
        "quality_proof_issuance",
        "release_evidence_qualified_staging_results",
    )
    good = sample_grant()
    recurring_grant_phrases.grant_digest(good)
    recurring_grant_phrases.grant_digest(
        sample_grant(
            allowed_operations=["collection", "release_evidence_qualified_staging_results"]
        )
    )
    for broken in (
        {"allowed_operations": list(reversed(good["allowed_operations"]))},
        {"allowed_operations": sorted(good["allowed_operations"])},
        {"allowed_operations": ["collection", "collection"]},
        {"allowed_operations": []},
        {"allowed_operations": ["migration"]},
        {"valid_from": "2026-09-15T00:00:00Z"},
        {"valid_from": "2026-09-15T02:00:00+02:00"},
        {"valid_from": "2026-09-15 00:00:00+00:00"},
        {"valid_from": "2026-09-15T00:00:00"},
        {"valid_until": "2026-10-15T00:00:00.000000+00:00"},
        {"valid_until": "2026-10-15"},
        {"valid_until": 20261015},
    ):
        with pytest.raises(ValueError, match="recurring_grant_invalid"):
            recurring_grant_phrases.grant_digest(sample_grant(**broken))
        with pytest.raises(ValueError, match="recurring_grant_invalid"):
            recurring_grant_phrases.canonical_grant_bytes(sample_grant(**broken))


def test_cli_loader_refuses_a_respelled_grant_before_any_client_call(tmp_path, monkeypatch):
    monkeypatch.setattr(approval_cli, "_load_credentials", lambda: pytest.fail("client built"))
    monkeypatch.setattr(approval_cli, "_read_approval_phrase", lambda: pytest.fail("phrase read"))
    respelled = sample_grant(valid_from="2026-09-15T00:00:00Z")
    raw = json.dumps(respelled, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    path = tmp_path / "grant.json"
    path.write_bytes(raw)
    terms = {key: value for key, value in respelled.items() if key != "revocation_state"}
    digest = hashlib.sha256(
        json.dumps(terms, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    for word in (approval_cli.approve_grant, approval_cli.revoke_grant):
        with pytest.raises(
            approval_cli.ApprovalCliRefusal, match=r"^execution_approval_manifest_invalid$"
        ):
            word(path.resolve(), digest)
    row = sample_row(canonical_manifest_json=raw.decode("utf-8"), manifest_sha256=digest)
    with pytest.raises(
        execution_approval.ApprovalRefusal, match=r"^execution_approval_schema_mismatch$"
    ):
        execution_approval._default_approval_reader(
            digest, version=APPROVAL_V3, mode="new_consume", query=lambda *_: row
        )


DAILY_OPERATIONS = ("source_collection", "daily_composition_apply")
DAILY_CONTRACT = (
    ROOT / "configs" / "open_intelligence" / "candidate_contracts" / "source-bridge-capture-v3.json"
)
PREFIX_PATTERN_BRANCH = (
    "WHEN 'prefix_pattern_v1' THEN ARRAY_LENGTH(v_rule_prefix) > 0",
    "AND ARRAY_LENGTH(JSON_QUERY_ARRAY(v_rule, '$.arguments.pattern')) > 0",
    "AND ARRAY_LENGTH(v_arguments) = ARRAY_LENGTH(v_rule_prefix) + ARRAY_LENGTH(JSON_QUERY_ARRAY(v_rule, '$.arguments.pattern'))",
    "SELECT 1 FROM UNNEST(JSON_QUERY_ARRAY(v_rule, '$.arguments.pattern')) AS item WITH OFFSET position",
    "WHERE NOT IFNULL(CASE ARRAY_TO_STRING(JSON_KEYS(SAFE.PARSE_JSON(item, wide_number_mode => 'exact'), 1, mode => 'strict'), ',')",
    "WHEN 'literal' THEN JSON_TYPE(JSON_QUERY(SAFE.PARSE_JSON(item, wide_number_mode => 'exact'), '$.literal')) = 'string'",
    "AND v_arguments[SAFE_OFFSET(ARRAY_LENGTH(v_rule_prefix) + position)] = JSON_VALUE(item, '$.literal')",
    "WHEN 'regex' THEN STARTS_WITH(JSON_VALUE(item, '$.regex'), '^')",
    "AND ENDS_WITH(JSON_VALUE(item, '$.regex'), '$')",
    # Wrapped so a top level alternation such as ^a|b$ matches the whole value, as
    # Python's re.fullmatch does, rather than any prefix or suffix of it.
    "AND REGEXP_CONTAINS(v_arguments[SAFE_OFFSET(ARRAY_LENGTH(v_rule_prefix) + position)], CONCAT('^(?:', JSON_VALUE(item, '$.regex'), ')$'))",
    "END, FALSE)",
)


def _case_operations(sql: str, path: str) -> list[str]:
    return re.findall(rf"WHEN '([a-z0-9_]+)' THEN JSON_QUERY\(v_[a-z_]+, '\$\.{path}\.\1'\)", sql)


def _kind_branches(sql: str) -> set[str]:
    return set(
        re.findall(r"WHEN '([a-z0-9_]+)' THEN (?!JSON_QUERY\(v_(?:origin_row|policy_json), )", sql)
    ) & {
        "exact",
        "r3_execution_reference",
        "brain_read_v1",
        "source_snapshot_capture_v2",
        "prefix_pattern_v1",
    }


def test_daily_operations_are_bound_in_both_v3_routines_and_absent_from_v2():
    for operation in DAILY_OPERATIONS:
        for path, source in (
            ("exact_operation_bindings", "v_origin_row"),
            ("operation_validation", "v_policy_json"),
        ):
            line = f"WHEN '{operation}' THEN JSON_QUERY({source}, '$.{path}.{operation}')"
            assert _sql(APPROVE_V3).count(line) == 1, line
            assert _sql(READ_V3).count(line) == 1, line
            assert line not in _sql(APPROVE_V2)
            assert line not in _sql(READ_V2)


def test_both_v3_routines_bind_the_same_operations_in_both_cases():
    for sql in (_sql(APPROVE_V3), _sql(READ_V3)):
        bound = _case_operations(sql, "exact_operation_bindings")
        ruled = _case_operations(sql, "operation_validation")
        assert bound == ruled
        assert len(bound) == len(set(bound))
    assert _case_operations(_sql(APPROVE_V3), "exact_operation_bindings") == _case_operations(
        _sql(READ_V3), "exact_operation_bindings"
    )


def test_every_contract_operation_the_v3_routine_binds_has_an_argument_branch():
    contract = json.loads(DAILY_CONTRACT.read_text(encoding="utf-8"))
    approve = _sql(APPROVE_V3)
    branches = _kind_branches(approve)
    for operation in _case_operations(approve, "operation_validation"):
        rule = contract["operation_validation"].get(operation)
        if rule is None:
            continue
        assert rule["arguments"]["kind"] in branches, operation
    for operation in DAILY_OPERATIONS:
        assert operation in contract["successor"]["operations"]
    assert contract["operation_validation"]["daily_composition_apply"]["arguments"]["kind"] == (
        "prefix_pattern_v1"
    )
    assert contract["operation_validation"]["source_collection"]["arguments"]["kind"] == "exact"


def test_prefix_pattern_branch_mirrors_the_python_rule():
    """Length is prefix plus pattern, the prefix is exact, and each position is an exact
    literal or an anchored regex; any other item shape fails closed, as in
    execution_approval._validate_policy_arguments and execution_origins._validate_prefix_pattern."""
    approve = _sql(APPROVE_V3)
    lines = [line.strip() for line in approve.splitlines()]
    start = lines.index(PREFIX_PATTERN_BRANCH[0])
    block = lines[start : start + 17]
    for check in PREFIX_PATTERN_BRANCH:
        assert check in block, check
    assert block[4] == "AND NOT EXISTS ("
    assert "ELSE FALSE" in block
    assert block[3].startswith("AND TO_JSON_STRING(ARRAY(SELECT argument FROM UNNEST(v_arguments)")
    assert block[3].endswith("= TO_JSON_STRING(v_rule_prefix)")
    assert "prefix_pattern_v1" not in _sql(APPROVE_V2)
    assert "prefix_pattern_v1" not in _sql(READ_V3)


def test_daily_manifests_approve_through_v3():
    for operation in DAILY_OPERATIONS:
        assert approval_cli._approve_routine(operation) == APPROVE_V3
    for operation in (
        "legacy_chain_replay",
        "r3_proof_issue",
        "r3_release",
        "collection_exposure_issue",
    ):
        assert approval_cli._approve_routine(operation) == APPROVE_V2
