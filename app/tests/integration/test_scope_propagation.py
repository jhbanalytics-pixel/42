"""Two configured scopes, two private evidence sets, and refusal on the production read paths.

Each kind is driven through the path production uses. Refusal is decided against
the stored binding of the referenced record, never against a label the caller
supplies. The general question family has a control index, so a cross scope
read touches no private object at all. The investigation family has no index:
the record is its own binding, so the refusal follows that single read and
precedes every dependent private read (history, dossier pointer, dossier body).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from src.api import (
    dossier_resolver,
    historical_workspace,
    investigation_scopes,
    workspace_scope,
)
from src.api.dossier_store import canonical_digest
from tests.unit.test_workspace_scope import FROZEN_TIME, Bucket, stored_record

SCOPE_A = "ogilvy_default"
SCOPE_B = "bsa_pulse"
OBJECT_PREFIX = "open-intelligence/v2/staging/investigations/"

ENGINE_PROBE = r"""
import json
from uuid import UUID

from src.analysis.open_intelligence.general_question_admission import admit_question_transport
from src.analysis.open_intelligence.general_question_request import normalize_question_request
from src.analysis.open_intelligence.general_question_store import QuestionStoreError
from tests.unit import test_general_question_store as f
from tests.unit.test_general_question_deployment import fixture

SEVEN = (
    "client_scope_id", "market_scope", "brand_config_id", "audience_lens_ids",
    "theme_id", "run_id", "contract_version",
)
store, bucket, invocation, runtime, binding = fixture()
policy, request_a, intake_a = f.prepared(1)
scope_a = f.scope()
scope_b = dict(scope_a, client_scope_id="bsa_pulse", brand_config_id="bsa")
request_b = normalize_question_request(
    {"message": "What is being said about Play Your Part ambassadors in South Africa?"},
    scope=scope_b,
    request_id=str(UUID(int=2)),
    admitted_at=f.NOW,
    policy_digest=policy["policy_digest"],
)
store.admit(request_b, f.build_intake_context(request_b, selected_market="za"), scope=scope_b, now=f.NOW)
reads = []
original = bucket.get_blob


def spy(name, *args, **kwargs):
    reads.append(name)
    return original(name, *args, **kwargs)


def private(names):
    return sorted({name.split("/requests/")[1] for name in names if "/requests/" in name})


bucket.get_blob = spy
out = {}
try:
    store.read_request(request_a["request_id"], scope=scope_b)
    out["forged_request"] = "read"
except QuestionStoreError as error:
    out["forged_request"] = str(error)
out["forged_request_private_reads"] = private(reads)
reads.clear()
control = store.read_request(request_a["request_id"], scope=scope_a)
out["control_request_private_reads"] = private(reads)
out["control"] = {key: control["request"][key] for key in SEVEN}
out["other"] = {key: store.read_request(request_b["request_id"], scope=scope_b)["request"][key] for key in SEVEN}
reads.clear()
follow_up = {
    "contract_version": "general_question_admission_v2",
    "request_id": str(UUID(int=3)),
    "transport": {"message": "And what changed since then?", "history": []},
    "scope": scope_b,
    "selected_market": "za",
    "policy_digest": policy["policy_digest"],
    "deployment_digest": binding["deployment_digest"],
    "parent_request_id": request_a["request_id"],
    "thread_anchor_request_id": None,
}
try:
    admit_question_transport(follow_up, store=store, runtime_identity=runtime, now=f.NOW)
    out["forged_parent"] = "admitted"
except Exception as error:
    out["forged_parent"] = type(error).__name__ + ":" + str(error)
out["forged_parent_private_reads"] = [n for n in private(reads) if n.startswith(request_a["request_id"])]
print(json.dumps(out))
"""


def resolved(client_scope_id, *, run_id):
    scope = investigation_scopes.resolve_investigation_scope(
        client_scope_id=client_scope_id,
        market_scope=("za",),
        brand_config_id="bsa" if client_scope_id == SCOPE_B else None,
        audience_lens_ids=(),
        theme_id=None,
    )
    return workspace_scope.ScopeBinding.from_values(
        {
            "client_scope_id": scope.client_scope_id,
            "market_scope": list(scope.market_scope),
            "brand_config_id": scope.brand_config_id,
            "audience_lens_ids": list(scope.audience_lens_ids),
            "theme_id": scope.theme_id,
            "run_id": run_id,
            "contract_version": "2.1.0",
        }
    )


def private_evidence(monkeypatch):
    """Two scopes, two private records; the bucket spy records every read."""
    record_a = stored_record(client_scope_id=SCOPE_A)
    record_b = stored_record(client_scope_id=SCOPE_B, brand_config_id="bsa")
    ids = {
        SCOPE_A: record_a["response"]["investigation_id"],
        SCOPE_B: record_b["response"]["investigation_id"],
    }
    bucket = Bucket(
        {
            OBJECT_PREFIX + ids[SCOPE_A] + ".json": json.dumps(record_a).encode(),
            OBJECT_PREFIX + ids[SCOPE_B] + ".json": json.dumps(record_b).encode(),
        }
    )
    monkeypatch.setattr(workspace_scope, "_workspace_bucket", lambda: bucket)
    return bucket, ids


def record_object(ids, scope_id):
    return OBJECT_PREFIX + ids[scope_id] + ".json"


def test_two_configured_scopes_resolve_with_distinct_digests_and_general_default():
    first = resolved(SCOPE_A, run_id="run_a")
    second = resolved(SCOPE_B, run_id="run_b")
    assert first.brand_config_id is None
    assert second.brand_config_id == "bsa"
    assert first.scope_digest != second.scope_digest
    assert investigation_scopes.default_client_scope_id() == SCOPE_A
    config = investigation_scopes._load(investigation_scopes.INVESTIGATION_SCOPES_PATH)
    assert set(config["scopes"][SCOPE_B]["market_scope"]) <= {"za", "ng", "ke"}


def test_investigation_id_outside_the_server_scope_is_refused_on_its_binding_read(
    monkeypatch,
):
    bucket, ids = private_evidence(monkeypatch)

    with pytest.raises(workspace_scope.WorkspaceScopeError) as caught:
        workspace_scope.resolve_workspace_scope(ids[SCOPE_B])
    assert caught.value.code == "scope_invalid"
    assert bucket.lookups == [record_object(ids, SCOPE_B)]

    bucket.lookups.clear()
    admitted = workspace_scope.resolve_workspace_scope(ids[SCOPE_A])
    assert bucket.lookups == [record_object(ids, SCOPE_A)]
    assert admitted.binding.client_scope_id == SCOPE_A
    assert (
        admitted.bind("investigation", time_binding=FROZEN_TIME)["scope"][
            "client_scope_id"
        ]
        == SCOPE_A
    )

    bucket.lookups.clear()
    with pytest.raises(workspace_scope.WorkspaceScopeError):
        workspace_scope.resolve_workspace_scope(ids[SCOPE_A], client_scope_id=SCOPE_B)
    other = workspace_scope.resolve_workspace_scope(
        ids[SCOPE_B], client_scope_id=SCOPE_B
    )
    assert other.binding.brand_config_id == "bsa"
    assert other.scope_digest != admitted.scope_digest
    assert bucket.lookups == [record_object(ids, SCOPE_A), record_object(ids, SCOPE_B)]


def test_historical_run_from_the_other_scope_is_refused_before_any_history_read(
    monkeypatch,
):
    bucket, ids = private_evidence(monkeypatch)

    with pytest.raises(workspace_scope.WorkspaceScopeError) as caught:
        historical_workspace.read_historical_workspace(ids[SCOPE_B], "replay")
    assert caught.value.code == "scope_invalid"
    assert bucket.lookups == [record_object(ids, SCOPE_B)]

    bucket.lookups.clear()
    payload = historical_workspace.read_historical_workspace(ids[SCOPE_A], "replay")
    assert payload == historical_workspace.unavailable_payload(ids[SCOPE_A], "replay")
    assert bucket.lookups == [record_object(ids, SCOPE_A)]


def test_dossier_of_the_other_scope_is_never_looked_up_and_the_genuine_pointer_is(
    monkeypatch,
):
    bucket, ids = private_evidence(monkeypatch)

    with pytest.raises(workspace_scope.WorkspaceScopeError):
        dossier_resolver.resolve_dossier(
            workspace_scope.resolve_workspace_scope(ids[SCOPE_B]), bucket=bucket
        )
    assert bucket.lookups == [record_object(ids, SCOPE_B)]
    assert not any("dossier" in name for name in bucket.lookups)

    bucket.lookups.clear()
    scope = workspace_scope.resolve_workspace_scope(ids[SCOPE_A])
    result = dossier_resolver.resolve_dossier(scope, bucket=bucket)
    assert "dossier_unpublished" in json.dumps(result)
    assert bucket.lookups[0] == record_object(ids, SCOPE_A)
    assert len(bucket.lookups) == 2
    assert ids[SCOPE_A] in bucket.lookups[1]
    assert "dossier" in bucket.lookups[1]


def test_cache_key_with_the_other_scope_digest_is_refused_before_read():
    first = resolved(SCOPE_A, run_id="run_a")
    second = resolved(SCOPE_B, run_id="run_b")
    keys = {
        scope.client_scope_id: workspace_scope.scope_cache_key(
            scope=scope, time_binding=FROZEN_TIME, subject="answer"
        )
        for scope in (first, second)
    }
    reads = []

    def read(key):
        reads.append(key)
        return "entry"

    with pytest.raises(workspace_scope.WorkspaceScopeError) as caught:
        workspace_scope.admit_cache_key(keys[SCOPE_B], scope=first, read=read)
    assert caught.value.code == "scope_invalid"
    assert reads == []
    assert (
        workspace_scope.admit_cache_key(keys[SCOPE_A], scope=first, read=read)
        == "entry"
    )
    assert reads == [keys[SCOPE_A]]
    # Relabelling the other scope's key with the caller's digest yields the caller's own
    # key: the key holds no other identity, so it can only ever name the caller's entry.
    relabelled = keys[SCOPE_B].replace(second.scope_digest, first.scope_digest, 1)
    assert relabelled == keys[SCOPE_A]
    version, scope_digest, time_digest, _subject = keys[SCOPE_A].split("/")
    assert (version, scope_digest, time_digest) == (
        "scope_cache_key_v1",
        first.scope_digest,
        canonical_digest(FROZEN_TIME),
    )
    moved = dict(
        FROZEN_TIME, as_of="2026-09-01T08:00:00.000000Z", source_cutoff="2026-08-31"
    )
    assert (
        workspace_scope.scope_cache_key(
            scope=first, time_binding=moved, subject="answer"
        )
        not in keys.values()
    )


def test_engine_store_refuses_request_and_parent_from_the_other_scope_before_private_reads():
    engine = os.environ.get("GENERAL_QUESTION_ENGINE_TEST_ROOT")
    interpreter = os.environ.get("GENERAL_QUESTION_ENGINE_TEST_PYTHON")
    if not engine or not interpreter:
        pytest.skip(
            "GENERAL_QUESTION_ENGINE_TEST_ROOT and _PYTHON select the engine checkout"
        )
    completed = subprocess.run(
        [interpreter, "-c", ENGINE_PROBE],
        cwd=Path(engine),
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    out = json.loads(completed.stdout.strip().splitlines()[-1])
    assert out["forged_request"] == "scope_invalid"
    assert out["forged_request_private_reads"] == []
    assert out["control_request_private_reads"] == [
        "00000000-0000-0000-0000-000000000001/intake.json",
        "00000000-0000-0000-0000-000000000001/request.json",
    ]
    assert out["control"] == {
        "client_scope_id": SCOPE_A,
        "market_scope": ["ke", "ng", "za"],
        "brand_config_id": None,
        "audience_lens_ids": [],
        "theme_id": None,
        "run_id": "question_00000000000000000000000000000001",
        "contract_version": "general_cultural_question_v1",
    }
    assert out["other"]["client_scope_id"] == SCOPE_B
    assert out["other"]["brand_config_id"] == "bsa"
    assert out["other"]["run_id"] == "question_00000000000000000000000000000002"
    assert canonical_digest(out["control"]) != canonical_digest(out["other"])
    assert workspace_scope.ScopeBinding.from_values(
        out["other"]
    ).scope_digest == canonical_digest(out["other"])
    assert out["forged_parent"] != "admitted"
    assert out["forged_parent"].split(":")[-1] in {"parent_unavailable", "parent_scope_mismatch", "scope_invalid"}
    assert out["forged_parent_private_reads"] == []


LENS_ENVELOPE = {
    "lens_binding_version": "client_lens_binding_v1",
    "client_lens_id": "bsa_pulse_lens",
    "configuration_digest": "e1baafa865b5c9419225102d752c651c5b6e4fada034a2317f2d6f8a395336bf",
}


def lens_bound(client_scope_id, *, run_id, client_lens=LENS_ENVELOPE):
    binding = resolved(client_scope_id, run_id=run_id)
    return workspace_scope.ScopeBinding.from_values(binding.values, client_lens=client_lens)


def test_a_lens_belongs_to_one_client_scope_and_never_to_the_other():
    from src.api import client_lenses

    assert client_lenses.authorized_client_lenses(client_scope_id=SCOPE_A) == []
    assert [row["client_lens_id"] for row in client_lenses.authorized_client_lenses(
        client_scope_id=SCOPE_B
    )] == ["bsa_pulse_lens"]
    with pytest.raises(client_lenses.ClientLensUnavailable):
        client_lenses.resolve_client_lens(
            client_scope_id=SCOPE_A, brand_config_id=None, client_lens_id="bsa_pulse_lens"
        )
    lens = client_lenses.resolve_client_lens(
        client_scope_id=SCOPE_B, brand_config_id="bsa", client_lens_id="bsa_pulse_lens"
    )
    assert lens.envelope == LENS_ENVELOPE


def test_a_cache_key_built_under_a_lens_is_refused_under_the_same_scope_without_it():
    """The lens is part of the binding, so its entries never reach the general run."""
    general = resolved(SCOPE_B, run_id="run_b")
    bound = lens_bound(SCOPE_B, run_id="run_b")
    reads = []
    keys = {
        "general": workspace_scope.scope_cache_key(
            scope=general, time_binding=FROZEN_TIME, subject="answer"
        ),
        "bound": workspace_scope.scope_cache_key(
            scope=bound, time_binding=FROZEN_TIME, subject="answer"
        ),
    }

    assert keys["general"] != keys["bound"]
    for key, scope in ((keys["bound"], general), (keys["general"], bound)):
        with pytest.raises(workspace_scope.WorkspaceScopeError) as caught:
            workspace_scope.admit_cache_key(key, scope=scope, read=reads.append)
        assert caught.value.code == "scope_invalid"
    assert reads == []
    assert workspace_scope.admit_cache_key(keys["bound"], scope=bound, read=reads.append) is None
    assert reads == [keys["bound"]]


def test_every_boundary_records_the_lens_and_the_two_scopes_stay_distinct():
    first = lens_bound(SCOPE_B, run_id="run_b")
    second = resolved(SCOPE_A, run_id="run_a")

    for row in workspace_scope.BOUNDARY_TABLE:
        bound = workspace_scope.bind_boundary(
            row.boundary, scope=first, time_binding=FROZEN_TIME
        )
        general = workspace_scope.bind_boundary(
            row.boundary, scope=second, time_binding=FROZEN_TIME
        )
        assert bound["client_lens"] == LENS_ENVELOPE
        assert general["client_lens"]["client_lens_id"] is None
        assert bound["binding_digest"] != general["binding_digest"]
        assert bound["scope"]["client_scope_id"] == SCOPE_B
        assert general["scope"]["client_scope_id"] == SCOPE_A
    # The seven values are unchanged by the lens; the binding digest is not.
    assert first.values["client_scope_id"] == SCOPE_B
    assert first.scope_digest != canonical_digest(first.values)
    assert second.scope_digest == canonical_digest(second.values)


def test_an_artifact_or_export_id_of_the_other_scope_is_never_looked_up(monkeypatch):
    """An export id names nothing on its own: its investigation binds it first."""
    from src.api import dossier_artifacts, dossier_store

    bucket, ids = private_evidence(monkeypatch)
    artifact_id = "art_" + "0" * 16

    with pytest.raises(workspace_scope.WorkspaceScopeError) as caught:
        scope = workspace_scope.resolve_workspace_scope(ids[SCOPE_B])
        dossier_artifacts.read_artifact(
            bucket=bucket,
            prefix=dossier_store.STAGING_PREFIX,
            investigation_id=scope.investigation_id,
            artifact_id=artifact_id,
        )
    assert caught.value.code == "scope_invalid"
    assert bucket.lookups == [record_object(ids, SCOPE_B)]
    assert not any("artifacts/" in name for name in bucket.lookups)

    bucket.lookups.clear()
    scope = workspace_scope.resolve_workspace_scope(ids[SCOPE_A])
    assert (
        dossier_artifacts.read_artifact(
            bucket=bucket,
            prefix=dossier_store.STAGING_PREFIX,
            investigation_id=scope.investigation_id,
            artifact_id=artifact_id,
        )
        is None
    )
    assert bucket.lookups[0] == record_object(ids, SCOPE_A)
    assert bucket.lookups[-1] == dossier_artifacts.artifact_object_name(
        dossier_store.STAGING_PREFIX, ids[SCOPE_A], artifact_id
    )
