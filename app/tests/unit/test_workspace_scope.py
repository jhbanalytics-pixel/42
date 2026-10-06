from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime

import pytest

from src.api import investigations
from tests.unit.object_creator_bucket import listed_names


class Blob:
    def __init__(self, payload):
        self.payload = payload

    def download_as_bytes(self):
        return self.payload


class Bucket:
    name = "listening-post-staging-cache"

    def __init__(self, objects=None):
        self.objects = objects or {}
        self.lookups = []

    def get_blob(self, name):
        self.lookups.append(name)
        value = self.objects.get(name)
        return Blob(value) if value is not None else None

    # A listing is a read as well, so the spy records its prefix.
    def list_blobs(self, *, prefix, max_results=None, delimiter=None):
        self.lookups.append(prefix)
        return [
            Listed(name)
            for name in listed_names(self.objects, prefix, max_results, delimiter)
        ]


class Listed:
    def __init__(self, name):
        self.name = name


def stored_record(*, client_scope_id="ogilvy_default", brand_config_id=None):
    frame = investigations.InvestigationFrame(
        client_scope_id=client_scope_id,
        market_scope=("za", "ng", "ke"),
        brand_config_id=brand_config_id,
        audience_lens_ids=(),
        theme_id=None,
        run_id="investigation_run_001",
        contract_version="2.1.0",
        decision_question="Which emerging behaviour warrants deeper investigation?",
        time_horizon_days=42,
        brand_context=None,
        known_assumptions=(),
        change_my_mind_if=(),
        research_role_id=None,
        research_role_version=None,
        output_mode="internal_working_paper",
    )
    response = investigations.build_plan_ready(
        frame=frame,
        plan=investigations.default_plan_for_frame(frame),
        investigation_id=investigations.investigation_id_for_frame(frame),
        created_at=datetime(2026, 8, 28, 8, tzinfo=UTC),
        active_role_versions={},
    )
    return investigations.build_investigation_storage_record(frame, response)


def test_resolves_exact_stored_frame_and_derives_scope_digest(monkeypatch):
    from src.api import workspace_scope

    record = stored_record()
    investigation_id = record["response"]["investigation_id"]
    object_name = f"open-intelligence/v2/staging/investigations/{investigation_id}.json"
    bucket = Bucket({object_name: json.dumps(record).encode()})
    monkeypatch.setattr(workspace_scope, "_workspace_bucket", lambda: bucket)

    resolved = workspace_scope.resolve_workspace_scope(investigation_id)

    assert resolved.investigation_id == investigation_id
    assert resolved.scope_payload == {
        "client_scope_id": "ogilvy_default",
        "market_scope": ["za", "ng", "ke"],
        "brand_config_id": None,
        "audience_lens_ids": [],
        "theme_id": None,
        "run_id": "investigation_run_001",
        "contract_version": "2.1.0",
    }
    canonical = json.dumps(resolved.scope_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert resolved.scope_digest == hashlib.sha256(canonical.encode()).hexdigest()
    assert resolved.output_mode == "internal_working_paper"
    assert bucket.lookups == [object_name]


@pytest.mark.parametrize("case", ["missing", "foreign", "malformed"])
def test_missing_foreign_and_malformed_records_share_scope_invalid(monkeypatch, case):
    from src.api import workspace_scope

    record = stored_record(client_scope_id="foreign_scope" if case == "foreign" else "ogilvy_default")
    investigation_id = record["response"]["investigation_id"]
    object_name = f"open-intelligence/v2/staging/investigations/{investigation_id}.json"
    if case == "missing":
        objects = {}
    elif case == "malformed":
        objects = {object_name: b"{}"}
    else:
        objects = {object_name: json.dumps(record).encode()}
    bucket = Bucket(objects)
    monkeypatch.setattr(workspace_scope, "_workspace_bucket", lambda: bucket)

    with pytest.raises(workspace_scope.WorkspaceScopeError) as caught:
        workspace_scope.resolve_workspace_scope(investigation_id)

    assert caught.value.code == "scope_invalid"
    assert caught.value.status_code == 404
    assert caught.value.detail == {
        "code": "scope_invalid",
        "message": "Workspace is unavailable for this scope.",
    }
    assert bucket.lookups == [object_name]


def test_invalid_identifier_refuses_before_bucket_lookup(monkeypatch):
    from src.api import workspace_scope

    called = []
    monkeypatch.setattr(workspace_scope, "_workspace_bucket", lambda: called.append(True))

    with pytest.raises(workspace_scope.WorkspaceScopeError) as caught:
        workspace_scope.resolve_workspace_scope("../other")

    assert caught.value.code == "workspace_request_invalid"
    assert caught.value.status_code == 400
    assert called == []


def test_valid_runtime_validates_metadata_identity_before_storage_client(monkeypatch):
    from google.cloud import storage

    from src.api import deployment_contract, workspace_scope

    values = {
        "DEPLOYMENT_PROFILE": "open-intelligence-staging",
        "K_SERVICE": "listening-post-staging",
        "BQ_DATASET": "trends_v2_staging",
        "CACHE_BUCKET": "listening-post-staging-cache",
        "CACHE_PREFIX": "open-intelligence/v2/staging/",
        "APPLICATION_SOURCE": "open-intelligence-staging",
        "SOURCE_SHA": "a" * 40,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    for name in tuple(os.environ):
        if name != "UI_PASSCODE" and (
            name.endswith("_API_KEY")
            or name.endswith("_ACCESS_TOKEN")
            or name.endswith("_SECRET")
        ):
            monkeypatch.delenv(name, raising=False)

    events = []
    bucket = Bucket()
    monkeypatch.setattr(
        deployment_contract,
        "_metadata_service_account",
        lambda: events.append("metadata") or deployment_contract.REQUIRED_SERVICE_ACCOUNT,
    )
    monkeypatch.setattr(
        storage,
        "Client",
        lambda: events.append("storage_client")
        or type("Client", (), {"bucket": lambda _self, _name: bucket})(),
    )

    assert workspace_scope._workspace_bucket() is bucket
    assert events == ["metadata", "storage_client"]


FROZEN_SCOPE = {
    "client_scope_id": "ogilvy_default",
    "market_scope": ["za", "ng", "ke"],
    "brand_config_id": None,
    "audience_lens_ids": [],
    "theme_id": None,
    "run_id": "investigation_run_001",
    "contract_version": "2.1.0",
}
FROZEN_TIME = {
    "window_start": "2026-08-14",
    "window_end": "2026-08-27",
    "as_of": "2026-08-28T08:00:00.000000Z",
    "source_cutoff": "2026-08-27",
}
BOUNDARIES = (
    "request",
    "plan",
    "retrieval",
    "snapshot",
    "answer",
    "cache",
    "history",
    "investigation",
    "artifact",
    "export",
)


def frozen_binding():
    from src.api import workspace_scope

    return workspace_scope.ScopeBinding.from_values(FROZEN_SCOPE)


def test_boundary_table_is_frozen_and_names_seven_scope_values_per_boundary():
    from src.api import workspace_scope

    table = workspace_scope.BOUNDARY_TABLE
    assert tuple(row.boundary for row in table) == BOUNDARIES
    assert workspace_scope.SCOPE_FIELDS == tuple(FROZEN_SCOPE)
    assert workspace_scope.TIME_BINDING_FIELDS == tuple(FROZEN_TIME)
    for row in table:
        assert row.scope_fields == workspace_scope.SCOPE_FIELDS
        assert row.time_binding_version == "c02_time_binding_v1"
        assert row.adapter
    with pytest.raises((AttributeError, TypeError)):
        table[0].adapter = "changed"
    assert len({row.adapter for row in table}) == len(table)


def test_every_boundary_projection_matches_one_frozen_frame():
    from src.api import dossier_store, workspace_scope

    binding = frozen_binding()
    assert binding.values == FROZEN_SCOPE
    assert binding.scope_digest == dossier_store.canonical_digest(FROZEN_SCOPE)
    time_binding = workspace_scope.adapt_time_binding(
        FROZEN_TIME, version="c02_time_binding_v1"
    )
    for row in workspace_scope.BOUNDARY_TABLE:
        projection = workspace_scope.bind_boundary(
            row.boundary, scope=binding, time_binding=time_binding
        )
        assert projection["boundary"] == row.boundary
        assert projection["adapter"] == row.adapter
        assert projection["scope"] == FROZEN_SCOPE
        assert projection["scope_digest"] == binding.scope_digest
        assert projection["time_binding"] == FROZEN_TIME
        assert projection["time_binding_version"] == "c02_time_binding_v1"
        unsigned = {
            key: value for key, value in projection.items() if key != "binding_digest"
        }
        assert projection["binding_digest"] == dossier_store.canonical_digest(unsigned)


@pytest.mark.parametrize("field", list(FROZEN_SCOPE))
def test_omitted_scope_value_fails_at_every_boundary(field):
    from src.api import workspace_scope

    values = {key: value for key, value in FROZEN_SCOPE.items() if key != field}
    with pytest.raises(workspace_scope.WorkspaceScopeError) as caught:
        workspace_scope.ScopeBinding.from_values(values)
    assert caught.value.code == "scope_invalid"


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_omitted_time_binding_fails_at_every_boundary(boundary):
    from src.api import workspace_scope

    with pytest.raises(workspace_scope.WorkspaceScopeError) as caught:
        workspace_scope.bind_boundary(
            boundary, scope=frozen_binding(), time_binding=None
        )
    assert caught.value.code == "time_binding_invalid"
    with pytest.raises(workspace_scope.WorkspaceScopeError):
        workspace_scope.bind_boundary(
            "unknown", scope=frozen_binding(), time_binding=FROZEN_TIME
        )


def test_legacy_nullable_brand_and_theme_stay_valid_and_bsa_values_bind():
    from src.api import workspace_scope

    legacy = workspace_scope.ScopeBinding.from_values(FROZEN_SCOPE)
    assert (
        legacy.values["brand_config_id"] is None and legacy.values["theme_id"] is None
    )
    configured = workspace_scope.ScopeBinding.from_values(
        dict(
            FROZEN_SCOPE,
            client_scope_id="bsa_pulse",
            brand_config_id="bsa",
            market_scope=["za"],
        )
    )
    assert configured.values["brand_config_id"] == "bsa"
    assert configured.scope_digest != legacy.scope_digest
    with pytest.raises(workspace_scope.WorkspaceScopeError):
        workspace_scope.ScopeBinding.from_values(dict(FROZEN_SCOPE, brand_config_id=""))
    with pytest.raises(workspace_scope.WorkspaceScopeError):
        workspace_scope.ScopeBinding.from_values(
            dict(FROZEN_SCOPE, market_scope=["ZA"])
        )


def test_time_binding_adapter_projects_the_engine_window_and_refuses_unknown_versions():
    from src.api import workspace_scope

    engine_shape = {
        "window": {"start": "2026-08-14", "end": "2026-08-27", "closed": True},
        "as_of": "2026-08-28T08:00:00.000000Z",
        "source_cutoff": "2026-08-27",
    }
    assert (
        workspace_scope.adapt_time_binding(engine_shape, version="engine_window_v1")
        == FROZEN_TIME
    )
    for broken in (
        dict(FROZEN_TIME, window_start="2026-08-30"),
        dict(FROZEN_TIME, source_cutoff="2026-08-29"),
        dict(FROZEN_TIME, as_of="2026-08-28"),
        {key: value for key, value in FROZEN_TIME.items() if key != "source_cutoff"},
    ):
        with pytest.raises(workspace_scope.WorkspaceScopeError) as caught:
            workspace_scope.adapt_time_binding(broken, version="c02_time_binding_v1")
        assert caught.value.code == "time_binding_invalid"
    with pytest.raises(workspace_scope.WorkspaceScopeError):
        workspace_scope.adapt_time_binding(FROZEN_TIME, version="unknown_v9")


def test_scope_cache_key_carries_scope_digest_and_time_binding():
    from src.api import dossier_store, workspace_scope

    first = frozen_binding()
    second = workspace_scope.ScopeBinding.from_values(
        dict(FROZEN_SCOPE, client_scope_id="bsa_pulse", brand_config_id="bsa")
    )
    key = workspace_scope.scope_cache_key(
        scope=first, time_binding=FROZEN_TIME, subject="answer"
    )
    assert first.scope_digest in key
    assert dossier_store.canonical_digest(FROZEN_TIME) in key
    assert key != workspace_scope.scope_cache_key(
        scope=second, time_binding=FROZEN_TIME, subject="answer"
    )
    moved = dict(
        FROZEN_TIME, as_of="2026-08-29T08:00:00.000000Z", source_cutoff="2026-08-28"
    )
    assert key != workspace_scope.scope_cache_key(
        scope=first, time_binding=moved, subject="answer"
    )
    assert key == workspace_scope.scope_cache_key(
        scope=first, time_binding=dict(FROZEN_TIME), subject="answer"
    )


def test_scope_binding_changes_for_every_authoritative_field():
    from src.api.dossier_store import canonical_digest

    scope = {
        "client_scope_id": "fixture-a",
        "market_scope": ["za"],
        "brand_config_id": None,
        "audience_lens_ids": [],
        "theme_id": None,
        "run_id": "fixture-run",
        "contract_version": "2.1.0",
    }
    changes = {
        "client_scope_id": "fixture-b",
        "market_scope": ["ng"],
        "brand_config_id": "bsa",
        "audience_lens_ids": ["fixture-lens"],
        "theme_id": "fixture-theme",
        "run_id": "other-run",
        "contract_version": "fixture-new",
    }
    for key, value in changes.items():
        assert canonical_digest({**scope, key: value}) != canonical_digest(scope)


def test_resolved_workspace_scope_exposes_investigation_boundary_binding(monkeypatch):
    from src.api import workspace_scope

    record = stored_record()
    investigation_id = record["response"]["investigation_id"]
    object_name = f"open-intelligence/v2/staging/investigations/{investigation_id}.json"
    monkeypatch.setattr(
        workspace_scope,
        "_workspace_bucket",
        lambda: Bucket({object_name: json.dumps(record).encode()}),
    )

    resolved = workspace_scope.resolve_workspace_scope(investigation_id)

    assert resolved.binding.values == resolved.scope_payload
    assert resolved.binding.scope_digest == resolved.scope_digest
    projection = resolved.bind("investigation", time_binding=FROZEN_TIME)
    assert projection["adapter"] == "investigation_record_v1"
    assert projection["scope"] == resolved.scope_payload


def test_scope_fields_are_the_dossier_store_fields():
    from src.api import dossier_store, workspace_scope

    assert workspace_scope.SCOPE_FIELDS is dossier_store.SCOPE_FIELDS


def test_record_outside_the_server_scope_is_refused_on_its_own_binding(monkeypatch):
    from src.api import investigation_scopes, workspace_scope

    record = stored_record(client_scope_id="bsa_pulse", brand_config_id="bsa")
    investigation_id = record["response"]["investigation_id"]
    object_name = f"open-intelligence/v2/staging/investigations/{investigation_id}.json"
    bucket = Bucket({object_name: json.dumps(record).encode()})
    monkeypatch.setattr(workspace_scope, "_workspace_bucket", lambda: bucket)

    assert investigation_scopes.default_client_scope_id() == "ogilvy_default"
    with pytest.raises(workspace_scope.WorkspaceScopeError) as caught:
        workspace_scope.resolve_workspace_scope(investigation_id)
    assert caught.value.code == "scope_invalid"
    assert bucket.lookups == [object_name]
    resolved = workspace_scope.resolve_workspace_scope(investigation_id, client_scope_id="bsa_pulse")
    assert resolved.binding.client_scope_id == "bsa_pulse"
    with pytest.raises(workspace_scope.WorkspaceScopeError):
        workspace_scope.resolve_workspace_scope(investigation_id, client_scope_id="")


def test_cache_key_admission_verifies_the_embedded_digest_before_any_read():
    from src.api import workspace_scope

    first = frozen_binding()
    second = workspace_scope.ScopeBinding.from_values(
        dict(FROZEN_SCOPE, client_scope_id="bsa_pulse", brand_config_id="bsa")
    )
    reads = []
    key = workspace_scope.scope_cache_key(scope=first, time_binding=FROZEN_TIME, subject="answer")
    assert workspace_scope.admit_cache_key(key, scope=first, read=reads.append) is None
    assert reads == [key]
    for forged in (
        workspace_scope.scope_cache_key(scope=second, time_binding=FROZEN_TIME, subject="answer"),
        key.replace("scope_cache_key_v1", "scope_cache_key_v0", 1),
        "not/a/key",
        "",
    ):
        with pytest.raises(workspace_scope.WorkspaceScopeError) as caught:
            workspace_scope.admit_cache_key(forged, scope=first, read=reads.append)
        assert caught.value.code == "scope_invalid"
    assert reads == [key]


LENS_ID = "bsa_pulse_lens"
LENS_DIGEST = "e1baafa865b5c9419225102d752c651c5b6e4fada034a2317f2d6f8a395336bf"
BSA_SCOPE = dict(FROZEN_SCOPE, client_scope_id="bsa_pulse", brand_config_id="bsa")


def lens_binding():
    from src.api import workspace_scope

    return workspace_scope.ClientLensBinding.from_values(
        {
            "lens_binding_version": "client_lens_binding_v1",
            "client_lens_id": LENS_ID,
            "configuration_digest": LENS_DIGEST,
        }
    )


def test_general_42_is_the_default_lens_and_keeps_the_legacy_seven_field_digest():
    """Nothing changes for a request that names no lens, including its digest."""
    from src.api import dossier_store, workspace_scope

    binding = frozen_binding()

    assert binding.client_lens == workspace_scope.ClientLensBinding.general()
    assert binding.client_lens.client_lens_id is None
    assert binding.client_lens.configuration_digest is None
    assert binding.values == FROZEN_SCOPE
    assert binding.scope_digest == dossier_store.canonical_digest(FROZEN_SCOPE)
    assert binding.lens_values == {
        "lens_binding_version": "client_lens_binding_v1",
        "client_lens_id": None,
        "configuration_digest": None,
    }


def test_a_bound_client_lens_is_recorded_at_every_boundary_beside_the_seven_values():
    from src.api import dossier_store, workspace_scope

    bound = workspace_scope.ScopeBinding.from_values(BSA_SCOPE, client_lens=lens_binding())

    assert bound.values == BSA_SCOPE
    assert bound.client_lens.client_lens_id == LENS_ID
    for row in workspace_scope.BOUNDARY_TABLE:
        projection = workspace_scope.bind_boundary(
            row.boundary, scope=bound, time_binding=FROZEN_TIME
        )
        assert projection["scope"] == BSA_SCOPE
        assert projection["lens_binding_version"] == "client_lens_binding_v1"
        assert projection["client_lens"] == {
            "lens_binding_version": "client_lens_binding_v1",
            "client_lens_id": LENS_ID,
            "configuration_digest": LENS_DIGEST,
        }
        unsigned = {
            key: value for key, value in projection.items() if key != "binding_digest"
        }
        assert projection["binding_digest"] == dossier_store.canonical_digest(unsigned)


def test_a_boundary_that_omits_the_lens_binding_is_a_failure():
    """Every boundary row names the lens adapter, general 42 included."""
    from src.api import workspace_scope

    for row in workspace_scope.BOUNDARY_TABLE:
        assert row.lens_binding_version == "client_lens_binding_v1"
        projection = workspace_scope.bind_boundary(
            row.boundary, scope=frozen_binding(), time_binding=FROZEN_TIME
        )
        assert "client_lens" in projection
        assert projection["client_lens"]["client_lens_id"] is None
    with pytest.raises((AttributeError, TypeError)):
        workspace_scope.BOUNDARY_TABLE[0].lens_binding_version = "changed"


def test_the_lens_changes_the_scope_digest_and_the_cache_key_it_builds():
    from src.api import workspace_scope

    general = workspace_scope.ScopeBinding.from_values(BSA_SCOPE)
    bound = workspace_scope.ScopeBinding.from_values(BSA_SCOPE, client_lens=lens_binding())
    other = workspace_scope.ScopeBinding.from_values(
        BSA_SCOPE,
        client_lens=workspace_scope.ClientLensBinding.from_values(
            {
                "lens_binding_version": "client_lens_binding_v1",
                "client_lens_id": "other_lens",
                "configuration_digest": LENS_DIGEST,
            }
        ),
    )
    moved = workspace_scope.ScopeBinding.from_values(
        BSA_SCOPE,
        client_lens=workspace_scope.ClientLensBinding.from_values(
            {
                "lens_binding_version": "client_lens_binding_v1",
                "client_lens_id": LENS_ID,
                "configuration_digest": "f" * 64,
            }
        ),
    )

    digests = {
        binding.scope_digest for binding in (general, bound, other, moved)
    }
    assert len(digests) == 4
    keys = {
        workspace_scope.scope_cache_key(
            scope=binding, time_binding=FROZEN_TIME, subject="answer"
        )
        for binding in (general, bound, other, moved)
    }
    assert len(keys) == 4
    assert all(len(key.split("/")) == 4 for key in keys)


def test_a_cache_entry_never_crosses_a_lens():
    from src.api import workspace_scope

    general = workspace_scope.ScopeBinding.from_values(BSA_SCOPE)
    bound = workspace_scope.ScopeBinding.from_values(BSA_SCOPE, client_lens=lens_binding())
    reads = []
    general_key = workspace_scope.scope_cache_key(
        scope=general, time_binding=FROZEN_TIME, subject="answer"
    )
    bound_key = workspace_scope.scope_cache_key(
        scope=bound, time_binding=FROZEN_TIME, subject="answer"
    )

    for key, scope in ((bound_key, general), (general_key, bound)):
        with pytest.raises(workspace_scope.WorkspaceScopeError) as caught:
            workspace_scope.admit_cache_key(key, scope=scope, read=reads.append)
        assert caught.value.code == "scope_invalid"
    assert reads == []
    assert workspace_scope.admit_cache_key(bound_key, scope=bound, read=reads.append) is None
    assert reads == [bound_key]


@pytest.mark.parametrize(
    "values",
    [
        {"client_lens_id": LENS_ID, "configuration_digest": LENS_DIGEST},
        {
            "lens_binding_version": "client_lens_binding_v0",
            "client_lens_id": LENS_ID,
            "configuration_digest": LENS_DIGEST,
        },
        {
            "lens_binding_version": "client_lens_binding_v1",
            "client_lens_id": LENS_ID,
            "configuration_digest": None,
        },
        {
            "lens_binding_version": "client_lens_binding_v1",
            "client_lens_id": None,
            "configuration_digest": LENS_DIGEST,
        },
        {
            "lens_binding_version": "client_lens_binding_v1",
            "client_lens_id": LENS_ID,
            "configuration_digest": "not-a-digest",
        },
        {
            "lens_binding_version": "client_lens_binding_v1",
            "client_lens_id": "",
            "configuration_digest": LENS_DIGEST,
        },
    ],
)
def test_a_malformed_lens_binding_is_refused(values):
    from src.api import workspace_scope

    with pytest.raises(workspace_scope.WorkspaceScopeError) as caught:
        workspace_scope.ClientLensBinding.from_values(values)
    assert caught.value.code == "scope_invalid"


def test_a_question_binding_carries_the_lens_the_route_resolved():
    from src.api import workspace_scope

    scope = {key: BSA_SCOPE[key] for key in workspace_scope.SCOPE_FIELDS[:5]}
    request_id = "00000000-0000-0000-0000-000000000009"
    general = workspace_scope.ScopeBinding.for_question(scope, request_id=request_id)
    bound = workspace_scope.ScopeBinding.for_question(
        scope, request_id=request_id, client_lens=lens_binding()
    )

    assert general.values == bound.values
    assert general.scope_digest != bound.scope_digest
    assert bound.client_lens.client_lens_id == LENS_ID


@pytest.mark.parametrize(
    "values",
    [
        pytest.param(
            {"client_lens_id": LENS_ID, "configuration_digest": LENS_DIGEST}, id="missing_version"
        ),
        pytest.param(
            {
                "lens_binding_version": "client_lens_binding_v1",
                "client_lens_id": LENS_ID,
                "configuration_digest": LENS_DIGEST,
                "label": "Brand South Africa Pulse",
            },
            id="extra_field",
        ),
        pytest.param(
            {
                "lens_binding_version": "client_lens_binding_v1",
                "client_lens_id": LENS_ID,
                "unresolved_client_inputs": [],
            },
            id="renamed_field",
        ),
        pytest.param({}, id="empty"),
    ],
)
def test_a_lens_binding_whose_field_set_is_not_exact_is_refused(values):
    """The field set is exact in both directions: nothing missing, nothing extra."""
    from src.api import workspace_scope

    with pytest.raises(workspace_scope.WorkspaceScopeError) as caught:
        workspace_scope.ClientLensBinding.from_values(values)
    assert caught.value.code == "scope_invalid"


def test_only_both_lens_values_absent_together_is_the_general_default():
    """A half filled binding is not general 42; it is a refusal."""
    from src.api import workspace_scope

    general = workspace_scope.ClientLensBinding.from_values(
        {
            "lens_binding_version": "client_lens_binding_v1",
            "client_lens_id": None,
            "configuration_digest": None,
        }
    )
    assert general.is_general
    assert general == workspace_scope.ClientLensBinding.general()
    for half in (
        {"client_lens_id": None, "configuration_digest": LENS_DIGEST},
        {"client_lens_id": LENS_ID, "configuration_digest": None},
    ):
        with pytest.raises(workspace_scope.WorkspaceScopeError):
            workspace_scope.ClientLensBinding.from_values(
                {"lens_binding_version": "client_lens_binding_v1", **half}
            )
