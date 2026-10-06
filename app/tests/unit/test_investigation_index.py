"""The two reads that give the investigation layer an entrance.

Nothing in the product created or listed an investigation, so the Evidence
Room, the Historical workspace and the Client Read preview all read an identity
no screen could produce. These are the reads that fix that: one that says which
scopes exist and whether a run has been released, and one that lists the
investigations already in scope.

Both are read only, both are bounded, and both refuse rather than guess.
"""

from __future__ import annotations

import pytest

from src.api import investigation_index as index
from src.api import investigations

SCOPE_VERSION = "investigation_scope_index_v1"
LIST_VERSION = "investigation_index_v1"


# --- The scope read -----------------------------------------------------------


def test_the_scope_read_returns_the_exact_field_order():
    payload = index.read_scopes()
    assert tuple(payload) == ("contract_version", "resource_version", "default_scope_id", "scopes")
    assert payload["resource_version"] == SCOPE_VERSION


def test_each_scope_carries_exactly_the_fields_the_creation_request_needs():
    payload = index.read_scopes()
    assert payload["scopes"]
    for scope in payload["scopes"]:
        assert tuple(scope) == (
            "client_scope_id",
            "market_labels",
            "market_scope",
            "brand_config_id",
            "audience_lens_ids",
            "theme_id",
            "latest_run_id",
        )


def test_market_labels_are_human_and_market_scope_is_exact():
    scope = index.read_scopes()["scopes"][0]
    assert scope["market_scope"] == ["za", "ng", "ke"]
    assert scope["market_labels"] == ["South Africa", "Nigeria", "Kenya"]


def test_the_default_scope_is_named_and_present_in_the_list():
    payload = index.read_scopes()
    ids = [scope["client_scope_id"] for scope in payload["scopes"]]
    assert payload["default_scope_id"] in ids


def test_a_disabled_scope_is_never_returned():
    config = {
        "contract_version": "2.1.0",
        "default_scope_id": "on",
        "scopes": {
            "on": {"client_scope_id": "on", "market_scope": ["za"], "brand_config_id": None,
                   "audience_lens_ids": [], "theme_id": None, "enabled": True},
            "off": {"client_scope_id": "off", "market_scope": ["za"], "brand_config_id": None,
                    "audience_lens_ids": [], "theme_id": None, "enabled": False},
        },
    }
    ids = [s["client_scope_id"] for s in index.read_scopes(config=config)["scopes"]]
    assert ids == ["on"]


def test_no_released_run_reports_no_run_rather_than_an_empty_string():
    """Section 7. With no released run the framing surface must refuse to
    create, so the read has to say there is none rather than hand back a blank
    that a caller could send as an identity."""
    payload = index.read_scopes(latest_run_lookup=lambda scope_id: None)
    assert payload["scopes"][0]["latest_run_id"] is None


def test_a_released_run_is_carried_through_exactly():
    payload = index.read_scopes(latest_run_lookup=lambda scope_id: "run_2026_08_29_a")
    assert payload["scopes"][0]["latest_run_id"] == "run_2026_08_29_a"


@pytest.mark.parametrize("bad", ["", "   ", 0, False, [], {}])
def test_a_blank_or_non_string_run_identity_is_treated_as_no_run(bad):
    """An identity that is only technically present is not an identity. Sending
    it would satisfy the creation request's required field while meaning
    nothing, which is the failure this whole programme forbids."""
    payload = index.read_scopes(latest_run_lookup=lambda scope_id: bad)
    assert payload["scopes"][0]["latest_run_id"] is None


def test_the_scope_read_carries_no_private_identity():
    payload = index.read_scopes()
    rendered = repr(payload)
    for forbidden in ("digest", "sha256", "scope_digest", "run_receipt", "@"):
        assert forbidden not in rendered


# --- The list read ------------------------------------------------------------


def _record(**overrides):
    record = {
        "investigation_id": "inv_one",
        "decision_question": "Is repair becoming social?",
        "client_scope_id": "ogilvy_default",
        "market_scope": ["za"],
        "created_at": "2026-08-28T09:00:00Z",
        "status": "plan_ready",
        "readiness_state": "blocked",
        "candidate_artifact_id": None,
    }
    record.update(overrides)
    return record


def test_the_list_read_returns_the_exact_field_order():
    payload = index.read_index([_record()])
    # truncated and total_count are not decoration. The approval requires the
    # bound to be stated when it bites, and a surface cannot state what the
    # payload does not carry, so a bounded list without them would read as the
    # whole set.
    assert tuple(payload) == (
        "contract_version",
        "resource_version",
        "investigations",
        "total_count",
        "truncated",
    )
    assert payload["resource_version"] == LIST_VERSION


def test_each_entry_carries_exactly_the_contract_fields():
    payload = index.read_index([_record()])
    for entry in payload["investigations"]:
        assert tuple(entry) == (
            "investigation_id",
            "decision_question",
            "market_labels",
            "created_at",
            "status",
            "readiness_state",
            "candidate_artifact_id",
        )


def test_an_empty_estate_is_an_empty_list_and_not_a_failure():
    payload = index.read_index([])
    assert payload["investigations"] == []


def test_entries_are_most_recent_first():
    records = [
        _record(investigation_id="inv_old", created_at="2026-08-01T00:00:00Z"),
        _record(investigation_id="inv_new", created_at="2026-08-28T00:00:00Z"),
        _record(investigation_id="inv_mid", created_at="2026-08-14T00:00:00Z"),
    ]
    ids = [e["investigation_id"] for e in index.read_index(records)["investigations"]]
    assert ids == ["inv_new", "inv_mid", "inv_old"]


def test_the_list_is_bounded_and_says_when_it_truncated():
    records = [
        _record(investigation_id=f"inv_{n:03d}", created_at=f"2026-08-{(n % 28) + 1:02d}T00:00:00Z")
        for n in range(80)
    ]
    payload = index.read_index(records)
    assert len(payload["investigations"]) == index.INDEX_LIMIT
    assert payload["truncated"] is True
    assert payload["total_count"] == 80


def test_a_short_list_is_not_marked_truncated():
    payload = index.read_index([_record()])
    assert payload["truncated"] is False
    assert payload["total_count"] == 1


def test_a_record_missing_its_identity_is_excluded_rather_than_shown_blank():
    payload = index.read_index([_record(investigation_id=None), _record()])
    assert [e["investigation_id"] for e in payload["investigations"]] == ["inv_one"]


def test_a_record_from_another_scope_is_never_listed():
    """Server owned scope. A caller cannot widen its own view, and a record
    belonging elsewhere is not this desk's to show."""
    payload = index.read_index(
        [_record(client_scope_id="someone_else"), _record()], client_scope_id="ogilvy_default"
    )
    assert [e["investigation_id"] for e in payload["investigations"]] == ["inv_one"]


def test_the_list_carries_no_private_identity_or_digest():
    payload = index.read_index([_record(scope_digest="abc", run_receipt="x", graph_score=0.9)])
    rendered = repr(payload)
    for forbidden in ("scope_digest", "run_receipt", "graph_score", "0.9"):
        assert forbidden not in rendered


def test_a_candidate_artifact_is_carried_so_the_client_read_becomes_reachable():
    payload = index.read_index([_record(candidate_artifact_id="ra_1")])
    assert payload["investigations"][0]["candidate_artifact_id"] == "ra_1"


# --- The routes ---------------------------------------------------------------
#
# Both are private, bodyless and passcode gated. Neither takes a caller
# parameter: scope is server owned, so a caller must not be able to widen its
# own view by asking.

from fastapi.testclient import TestClient  # noqa: E402

from src.api import main  # noqa: E402

client = TestClient(main.app)
SCOPES_ROUTE = "/api/v2/investigations/scopes"
LIST_ROUTE = "/api/v2/investigations/list"


@pytest.fixture(autouse=True)
def open_gate(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)
    # No unit test may reach live GCS. The default estate is empty and a test
    # that needs records overrides this with its own fake bucket.
    class _EmptyBucket:
        name = "listening-post-staging-cache"

        def list_blobs(self, prefix):
            return []

    monkeypatch.setattr(main, "_investigation_bucket", lambda: _EmptyBucket())
    # Same rule for the run lookup: no unit test reaches live BigQuery. The
    # default is no released run; a test that wants one overrides this.
    monkeypatch.setattr(main.bq, "fetch_dynamic_run_receipt", lambda market: [])


@pytest.mark.parametrize("route", [SCOPES_ROUTE, LIST_ROUTE])
def test_the_route_answers_with_its_contract_version(route):
    response = client.post(f"{route}?contract_version=intelligence_dossier_v1")
    assert response.status_code == 200
    assert response.json()["contract_version"] == "intelligence_dossier_v1"


@pytest.mark.parametrize("route", [SCOPES_ROUTE, LIST_ROUTE])
@pytest.mark.parametrize(
    "query",
    ["", "?contract_version=", "?contract_version=a&contract_version=a", "?client_scope_id=other"],
    ids=["missing", "blank", "duplicated", "caller_scope"],
)
def test_only_the_exact_request_grammar_is_accepted(route, query):
    response = client.post(f"{route}{query}")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "workspace_request_invalid"


@pytest.mark.parametrize("route", [SCOPES_ROUTE, LIST_ROUTE])
def test_an_unsupported_version_is_named_as_such(route):
    response = client.post(f"{route}?contract_version=intelligence_dossier_v2")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "contract_version_unsupported"


@pytest.mark.parametrize("route", [SCOPES_ROUTE, LIST_ROUTE])
def test_authentication_fails_before_any_read(route, monkeypatch):
    monkeypatch.delenv("LP_ALLOW_OPEN_GATE", raising=False)
    monkeypatch.setenv("UI_PASSCODE", "fixture-passcode")
    response = client.post(f"{route}?contract_version=intelligence_dossier_v1")
    assert response.status_code == 401


@pytest.mark.parametrize("route", [SCOPES_ROUTE, LIST_ROUTE])
def test_neither_route_enters_the_public_api_surface(route):
    generated = main.app.openapi()
    assert not any("investigations/scopes" in path for path in generated.get("paths", {}))
    assert not any("investigations/list" in path for path in generated.get("paths", {}))


@pytest.mark.parametrize("route", [SCOPES_ROUTE, LIST_ROUTE])
def test_the_read_never_writes(route):
    """Opening the entrance must not create anything."""
    for method in (client.get, client.put, client.patch, client.delete):
        assert method(f"{route}?contract_version=intelligence_dossier_v1").status_code in (404, 405)


# --- Durable listing ----------------------------------------------------------
#
# The route returned a hardcoded empty list, so the landing would say "no
# investigations" forever, including the moment after a strategist framed one.
# Listing reads the same store creation writes to, bounded, and a storage
# failure is unavailable, never an empty estate.


class _Blob:
    def __init__(self, name, payload):
        self.name = name
        self._payload = payload

    def download_as_bytes(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Bucket:
    def __init__(self, blobs):
        self._blobs = blobs
        self.name = "listening-post-staging-cache"

    def list_blobs(self, prefix):
        return [b for b in self._blobs if b.name.startswith(prefix)]


def _canonical(question, created_at="2026-08-28T09:00:00Z", client_scope_id="ogilvy_default"):
    """The exact record create_investigation writes: nested, validated shape."""
    from datetime import datetime

    frame = investigations.InvestigationFrame(
        client_scope_id=client_scope_id,
        market_scope=("za", "ng", "ke"),
        brand_config_id=None,
        audience_lens_ids=(),
        theme_id=None,
        run_id="investigation_run_001",
        contract_version="2.1.0",
        decision_question=question,
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
        created_at=datetime.fromisoformat(created_at.replace("Z", "+00:00")),
        active_role_versions={},
    )
    return investigations.build_investigation_storage_record(frame, response)


def _stored(question, created_at="2026-08-28T09:00:00Z", time_created=None, **over):
    import json as _json

    record = _canonical(question, created_at, **over)
    investigation_id = record["response"]["investigation_id"]
    blob = _Blob(
        f"open-intelligence/v2/staging/investigations/{investigation_id}.json",
        _json.dumps(record).encode("utf-8"),
    )
    blob.time_created = time_created
    return blob


def test_stored_records_are_listed():
    """The reader admits exactly the shape the creation route writes.

    Found by independent review: the reader expected flat records while the
    writer stored nested ones, so every real record was silently excluded and
    only foreign flat blobs could ever be listed."""
    bucket = _Bucket([_stored("First question?"), _stored("Second question?", "2026-08-29T09:00:00Z")])
    records = index.read_stored_investigations(bucket=bucket)
    assert len(records) == 2
    assert all(r["investigation_id"].startswith("inv_") for r in records)
    assert sorted(r["decision_question"] for r in records) == ["First question?", "Second question?"]


def test_a_planted_flat_record_is_never_listed():
    """A blob shaped for the old flat reader is not a record this store wrote."""
    import json as _json

    planted = _Blob(
        "open-intelligence/v2/staging/investigations/inv_planted.json",
        _json.dumps({
            "investigation_id": "inv_planted",
            "client_scope_id": "ogilvy_default",
            "decision_question": "Planted?",
            "market_scope": ["za"],
            "created_at": "2026-08-30T00:00:00Z",
            "status": "plan_ready",
        }).encode("utf-8"),
    )
    bucket = _Bucket([planted, _stored("Real question?")])
    records = index.read_stored_investigations(bucket=bucket)
    assert [r["decision_question"] for r in records] == ["Real question?"]


def test_an_unreadable_blob_is_skipped_not_fatal_and_not_invented():
    bucket = _Bucket(
        [
            _stored("Real question?"),
            _Blob(
                "open-intelligence/v2/staging/investigations/broken.json",
                b"not json at all",
            ),
        ]
    )
    records = index.read_stored_investigations(bucket=bucket)
    assert [r["decision_question"] for r in records] == ["Real question?"]


def test_a_blob_outside_the_investigations_prefix_is_never_read():
    """Defended in the reader itself, not delegated to list_blobs.

    A real bucket filters by prefix, so this fake deliberately does not: the
    guard exists for exactly the client that stops filtering, and a test whose
    fake filters would never exercise it."""

    class _IgnoresPrefix:
        name = "listening-post-staging-cache"

        def __init__(self, blobs):
            self._blobs = blobs

        def list_blobs(self, prefix):
            return list(self._blobs)

    import json as _json

    # Valid JSON on purpose. A blob that fails to download is skipped by the
    # corrupt-blob path anyway, so it cannot prove this guard; only a readable
    # record at the wrong path can.
    outside = _Blob(
        "open-intelligence/v2/staging/other/inv_outside.json",
        _json.dumps({"investigation_id": "inv_outside", "client_scope_id": "ogilvy_default",
                     "decision_question": "Q?", "market_scope": ["za"],
                     "created_at": "2026-08-30T00:00:00Z", "status": "plan_ready",
                     "readiness_state": "blocked", "candidate_artifact_id": None}).encode("utf-8"),
    )
    bucket = _IgnoresPrefix([outside, _stored("Real question?")])
    records = index.read_stored_investigations(bucket=bucket)
    assert [r["decision_question"] for r in records] == ["Real question?"]


def test_the_read_is_bounded():
    bucket = _Bucket([_stored(f"Question {n}?") for n in range(300)])
    records = index.read_stored_investigations(bucket=bucket)
    assert len(records) == index.STORE_READ_LIMIT


def test_the_bound_keeps_the_newest_blobs_not_the_first_by_name():
    """Found by independent review: the 200-blob bound was applied in listing
    order, so once the estate outgrew it the newest records could vanish while
    truncated stayed false. The bound must follow recency, not object name."""
    from datetime import UTC, datetime, timedelta

    base = datetime(2026, 8, 1, tzinfo=UTC)
    old = [
        _stored(f"Old question {n}?", time_created=base + timedelta(minutes=n))
        for n in range(index.STORE_READ_LIMIT)
    ]
    newest = _stored(
        "The newest question?",
        "2026-08-29T09:00:00Z",
        time_created=base + timedelta(days=20),
    )
    # Listed first by the fake would hide it under a listing-order bound, so
    # the old two hundred go first.
    bucket = _Bucket(old + [newest])
    records = index.read_stored_investigations(bucket=bucket)
    assert len(records) == index.STORE_READ_LIMIT
    assert any(r["decision_question"] == "The newest question?" for r in records)


def test_a_storage_failure_raises_rather_than_returning_an_empty_estate():
    class _Broken:
        name = "listening-post-staging-cache"

        def list_blobs(self, prefix):
            raise RuntimeError("gcs down")

    with pytest.raises(index.InvestigationIndexError):
        index.read_stored_investigations(bucket=_Broken())


def test_the_list_route_serves_stored_records(monkeypatch):
    bucket = _Bucket([_stored("Live question?", "2026-08-29T10:00:00Z")])
    monkeypatch.setattr(main, "_investigation_bucket", lambda: bucket)
    response = client.post(f"{LIST_ROUTE}?contract_version=intelligence_dossier_v1")
    assert response.status_code == 200
    entries = response.json()["investigations"]
    assert [e["decision_question"] for e in entries] == ["Live question?"]


def test_the_list_route_filters_to_the_desk_scope(monkeypatch):
    """Found by independent review: the route called read_index without a
    scope, so the filter was dead and a second enabled scope would leak."""
    monkeypatch.setattr(
        main.investigation_index,
        "read_stored_investigations",
        lambda *, bucket: [
            {
                "investigation_id": "inv_other",
                "client_scope_id": "another_scope",
                "decision_question": "Other client?",
                "market_scope": ["za"],
                "created_at": "2026-08-29T10:00:00Z",
                "status": "plan_ready",
            }
        ],
    )
    monkeypatch.setattr(main, "_investigation_bucket", lambda: object())
    response = client.post(f"{LIST_ROUTE}?contract_version=intelligence_dossier_v1")
    assert response.status_code == 200
    assert response.json()["investigations"] == []


def test_the_list_route_reports_storage_failure_as_unavailable_not_empty(monkeypatch):
    class _Broken:
        name = "listening-post-staging-cache"

        def list_blobs(self, prefix):
            raise RuntimeError("gcs down")

    monkeypatch.setattr(main, "_investigation_bucket", lambda: _Broken())
    response = client.post(f"{LIST_ROUTE}?contract_version=intelligence_dossier_v1")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "workspace_unavailable"


# --- The scopes route learns about released runs from the receipts table ------
#
# latest_run_id was always null because nothing looked. It now reuses the same
# qualifying-receipt reader Open Discover uses, so the framing surface unlocks
# automatically the moment a released run exists, and refuses until then.
# Every failure direction collapses to None, which drives refusal: a lookup
# that breaks can suppress framing, never enable it.


def test_scopes_route_carries_the_released_run(monkeypatch):
    monkeypatch.setattr(
        main.bq, "fetch_dynamic_run_receipt", lambda market: [{"run_id": "run_2026_08_29_a"}]
    )
    response = client.post(f"{SCOPES_ROUTE}?contract_version=intelligence_dossier_v1")
    assert response.status_code == 200
    assert response.json()["scopes"][0]["latest_run_id"] == "run_2026_08_29_a"


def test_scopes_route_reports_no_run_when_none_qualifies(monkeypatch):
    monkeypatch.setattr(main.bq, "fetch_dynamic_run_receipt", lambda market: [])
    response = client.post(f"{SCOPES_ROUTE}?contract_version=intelligence_dossier_v1")
    assert response.json()["scopes"][0]["latest_run_id"] is None


def test_a_broken_lookup_reports_no_run_never_a_value(monkeypatch):
    def boom(market):
        raise RuntimeError("bq down")

    monkeypatch.setattr(main.bq, "fetch_dynamic_run_receipt", boom)
    response = client.post(f"{SCOPES_ROUTE}?contract_version=intelligence_dossier_v1")
    assert response.status_code == 200
    assert response.json()["scopes"][0]["latest_run_id"] is None


def test_a_receipt_missing_its_run_id_reports_no_run(monkeypatch):
    monkeypatch.setattr(main.bq, "fetch_dynamic_run_receipt", lambda market: [{"row_set_digest": "x"}])
    response = client.post(f"{SCOPES_ROUTE}?contract_version=intelligence_dossier_v1")
    assert response.json()["scopes"][0]["latest_run_id"] is None
