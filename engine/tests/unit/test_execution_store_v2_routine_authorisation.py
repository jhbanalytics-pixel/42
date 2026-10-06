"""apply-v2 keeps the dataset authorization of every routine it does not change.

Live on 26 September a CREATE OR REPLACE of the v2 routines voided their dataset
authorizations while the access list kept listing the entries, so the plan read them
as present and the approver was denied at the INSERT. apply-v2 now leaves an unchanged
routine alone and refuses, before any write, to replace one that holds an entry; the
plan names a stale or possibly stale entry and never counts it present; the state word
prints the verdict per routine; and the re-authorisation adds the dropped entries back,
add only, under the dataset etag. No path here ever removes an access entry.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy

import pytest
from scripts.migrations import create_open_intelligence_execution_approval_store as migration

from tests.unit.test_execution_store_v2_iam_apply import (
    APPROVALS,
    DATASET_URL,
    SECRET_TOKEN,
    UDF,
    V1_ACCESS,
    _Credentials,
    _FakeGoogle,
    _Response,
)

ROUTINE_TIME = 1_000_000
DATASET_TIME = 2_000_000
REPLACED_TIME = 3_000_000
APPROVE_V2 = "sp_approve_open_intelligence_execution_v2"
APPROVE_V3 = "sp_approve_open_intelligence_execution_v3"
CREATE_ROUTINE = re.compile(
    r"CREATE OR REPLACE (?:PROCEDURE|FUNCTION) "
    r"`ogilvy-trends-v2\.trends_v2_staging_approvals\.([a-z0-9_]+)`"
)


def _plan():
    return migration.build_v2_plan()


def _targets():
    return migration.v2_iam_targets(_plan())


def _routine_payload(item, modified: int) -> dict:
    """A routines.get resource as BigQuery returns it: no mode, no kind, no default."""

    expected = migration._v2_routine_expected(item)
    payload = {
        "etag": "routine-etag",
        "routineReference": {
            "projectId": "ogilvy-trends-v2",
            "datasetId": "trends_v2_staging_approvals",
            "routineId": item.name,
        },
        "routineType": expected["routineType"],
        "language": expected["language"],
        "arguments": [
            {"name": row["name"], "dataType": row["dataType"]} for row in expected["arguments"]
        ],
        "definitionBody": expected["definitionBody"],
        "creationTime": "500",
        "lastModifiedTime": str(modified),
    }
    if expected["returnType"] is not None:
        payload["returnType"] = expected["returnType"]
    if expected["description"] is not None:
        payload["description"] = expected["description"]
    return payload


class _LiveFake(_FakeGoogle):
    """The v2 IAM fake with full routines.get resources and lastModifiedTime on both."""

    def __init__(self, *, access=None, routine_time: int = ROUTINE_TIME, dataset_time=DATASET_TIME):
        super().__init__(access=access)
        self.dataset["lastModifiedTime"] = str(dataset_time)
        self.clock = max(routine_time, dataset_time)
        self.payloads = {
            item.name: _routine_payload(item, routine_time) for item in _plan().routines
        }

    def tick(self) -> int:
        self.clock += 1000
        return self.clock

    def get(self, url, **kwargs):
        if url.startswith(DATASET_URL + "/routines/"):
            self.reads.append(("get", url))
            name = url.removeprefix(DATASET_URL + "/routines/")
            if f"{APPROVALS}/routines/{name}" in self.absent_routines or name not in self.payloads:
                return _Response(404, {"error": "not found"})
            return _Response(200, self.payloads[name])
        return super().get(url, **kwargs)

    def patch(self, url, *, params=None, json=None, headers=None):
        response = super().patch(url, params=params, json=json, headers=headers)
        if response.status_code == 200:
            self.dataset["lastModifiedTime"] = str(self.tick())
        return response

    def replace_routine(self, name: str, body: str | None = None) -> None:
        """What a CREATE OR REPLACE does: a new lastModifiedTime, routine IAM dropped."""

        if body is not None:
            self.payloads[name]["definitionBody"] = body
        self.payloads[name]["lastModifiedTime"] = str(self.tick())
        self.routines.pop(f"{APPROVALS}/routines/{name}", None)


class _Job:
    def result(self):
        return ()


class _Client:
    """Records every statement; a routine CREATE lands on the fake as BigQuery would."""

    def __init__(self, fake: _LiveFake):
        self.fake = fake
        self.queries: list[str] = []

    def query(self, sql, job_config=None):
        self.queries.append(sql)
        match = CREATE_ROUTINE.search(sql)
        if match is not None:
            item = next(item for item in _plan().routines if item.name == match.group(1))
            self.fake.replace_routine(item.name, migration._v2_routine_definition(item))
        return _Job()


def _entries(names=None) -> list[dict]:
    """The dataset entries the iam-v2 words write, for every delta target or the named ones."""

    targets = _targets()
    functions = frozenset(
        item.routine for item in targets.authorizations if item.routine.endswith(UDF)
    )
    return [
        migration._v2_authorization_entry(item, functions)
        for item in targets.authorizations
        if names is None or item.routine.rsplit("/", 1)[1] in names
    ]


@pytest.fixture(autouse=True)
def receipts(tmp_path, monkeypatch):
    folder = tmp_path / "receipts"
    monkeypatch.setattr(migration, "V2_AUTHORISATION_RECEIPTS_DIR", folder)
    return folder


def _use(monkeypatch, fake: _LiveFake) -> _Client:
    client = _Client(fake)
    monkeypatch.setattr(migration, "_load_credentials", lambda: _Credentials())
    monkeypatch.setattr(migration, "_authorized_session", lambda _credentials: fake)
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)
    monkeypatch.setattr(
        migration, "_read_v1_lock_row", lambda _client: ("lock", "v1", 7, "active", None, 1, 2)
    )
    monkeypatch.setattr(migration, "_readback_v2_store", lambda *_args: None)
    return client


def _run(capsys, word: str) -> tuple[int, dict]:
    code = migration.main([word])
    captured = capsys.readouterr()
    assert SECRET_TOKEN not in captured.out + captured.err
    return code, json.loads(captured.out if code == 0 else captured.err)


def _created(client: _Client) -> list[str]:
    return [m.group(1) for sql in client.queries if (m := CREATE_ROUTINE.search(sql))]


def _patches(fake: _FakeGoogle) -> list[dict]:
    return [body for method, _url, body in fake.writes if method == "patch"]


# The comparison


def test_a_routines_get_resource_without_defaults_equals_the_rendered_routine():
    for item in _plan().routines:
        assert migration._v2_routine_differences(item, _routine_payload(item, 1)) == [], item.name


def test_the_compared_view_is_read_from_the_sql_header_and_the_proven_body():
    plan = {item.name: item for item in _plan().routines}
    udf = migration._v2_routine_expected(plan[UDF])
    assert udf["routineType"] == "SCALAR_FUNCTION"
    assert udf["language"] == "JAVASCRIPT"
    assert udf["returnType"] == {"typeKind": "BOOL"}
    assert udf["arguments"] == [
        {
            "name": "raw",
            "dataType": {"typeKind": "STRING"},
            "mode": None,
            "argumentKind": "FIXED_TYPE",
        }
    ]
    approve = migration._v2_routine_expected(plan[APPROVE_V2])
    assert approve["routineType"] == "PROCEDURE"
    assert approve["language"] == "SQL"
    assert approve["returnType"] is None
    assert approve["description"] is None
    assert [row["name"] for row in approve["arguments"]] == [
        "canonical_manifest_json",
        "manifest_sha256",
        "approval_phrase",
        "origin_registry_sha256",
        "resource_manifest_sha256",
    ]
    assert {row["mode"] for row in approve["arguments"]} == {"IN"}
    # The body is the one the install readback proves against INFORMATION_SCHEMA.
    for item in plan.values():
        expected = migration._v2_routine_expected(item)
        assert expected["definitionBody"] == migration._v2_routine_definition(item)
        assert (
            tuple((row["name"], row["dataType"]["typeKind"]) for row in expected["arguments"])
            == item.parameters
        )


@pytest.mark.parametrize(
    "change,field",
    [
        (lambda p: p.update(definitionBody=p["definitionBody"] + "\n"), "definitionBody"),
        (lambda p: p["arguments"].pop(), "arguments"),
        (lambda p: p["arguments"][0].update(dataType={"typeKind": "JSON"}), "arguments"),
        (lambda p: p["arguments"][0].update(mode="INOUT"), "arguments"),
        (lambda p: p["arguments"][0].update(argumentKind="ANY_TYPE"), "arguments"),
        (lambda p: p["arguments"][0].update(name="renamed"), "arguments"),
        (lambda p: p.update(language="JAVASCRIPT"), "language"),
        (lambda p: p.update(routineType="SCALAR_FUNCTION"), "routineType"),
        (lambda p: p.update(returnType={"typeKind": "BOOL"}), "returnType"),
        (lambda p: p.update(description="changed"), "description"),
    ],
)
def test_each_compared_field_that_differs_is_named(change, field):
    item = next(item for item in _plan().routines if item.name == APPROVE_V2)
    payload = _routine_payload(item, 1)
    change(payload)
    assert migration._v2_routine_differences(item, payload) == [field]


def test_an_explicit_in_mode_and_fixed_kind_read_back_equal_their_defaults():
    item = next(item for item in _plan().routines if item.name == APPROVE_V2)
    payload = _routine_payload(item, 1)
    for row in payload["arguments"]:
        row.update(mode="IN", argumentKind="FIXED_TYPE")
    assert migration._v2_routine_differences(item, payload) == []


# apply-v2


def test_apply_v2_skips_every_unchanged_routine_and_writes_no_access(monkeypatch):
    fake = _LiveFake(access=[*V1_ACCESS, *_entries()])
    client = _use(monkeypatch, fake)
    before = deepcopy(fake.dataset["access"])
    receipt = migration._apply_v2_plan(_plan())
    assert _created(client) == []
    assert not any("CREATE OR REPLACE" in sql for sql in client.queries)
    # The tables, the lock seed and the registration still run, all idempotent.
    assert len(client.queries) == len(_plan().tables) + 2
    assert receipt["routines"] == {
        "created": [],
        "replaced": [],
        "unchanged": sorted(item.name for item in _plan().routines),
    }
    assert _patches(fake) == []
    assert fake.dataset["access"] == before
    assert all(p["lastModifiedTime"] == str(ROUTINE_TIME) for p in fake.payloads.values())


def test_apply_v2_replaces_only_the_changed_routine_that_holds_no_entry(monkeypatch):
    held = {item.routine.rsplit("/", 1)[1] for item in _targets().authorizations} - {APPROVE_V3}
    fake = _LiveFake(access=[*V1_ACCESS, *_entries(held)])
    fake.payloads[APPROVE_V3]["definitionBody"] = "BEGIN\nSELECT 1;\nEND"
    client = _use(monkeypatch, fake)
    receipt = migration._apply_v2_plan(_plan())
    assert _created(client) == [APPROVE_V3]
    assert receipt["routines"]["replaced"] == [APPROVE_V3]
    assert len(receipt["routines"]["unchanged"]) == len(_plan().routines) - 1
    # The IAM step adds the one missing entry and records it against the new routine time.
    assert receipt["iam"]["authorizations"]["added"] == 1
    assert receipt["iam"]["authorisation_record"]["state"] == "recorded"
    assert receipt["iam"]["authorisation_record"]["recorded"] == 1
    staleness = receipt["iam"]["authorisation_staleness"]
    assert staleness["present"] == 1
    assert staleness["stale"] == []
    assert len(staleness["possibly_stale"]) == 20


def test_apply_v2_creates_an_absent_routine(monkeypatch):
    fake = _LiveFake(access=[*V1_ACCESS, *_entries()])
    name = "sp_reconcile_open_intelligence_daily_consumption_v1"
    del fake.payloads[name]
    client = _use(monkeypatch, fake)
    original = client.query

    def query(sql, job_config=None):
        match = CREATE_ROUTINE.search(sql)
        if match is not None and match.group(1) not in fake.payloads:
            item = next(item for item in _plan().routines if item.name == match.group(1))
            fake.payloads[item.name] = _routine_payload(item, fake.tick())
            client.queries.append(sql)
            return _Job()
        return original(sql, job_config)

    client.query = query
    receipt = migration._apply_v2_plan(_plan())
    assert receipt["routines"]["created"] == [name]
    assert receipt["routines"]["replaced"] == []


@pytest.mark.parametrize("routine", [APPROVE_V2, APPROVE_V3, UDF])
def test_apply_v2_refuses_a_changed_authorised_routine_before_any_write(
    monkeypatch, capsys, routine
):
    fake = _LiveFake(access=[*V1_ACCESS, *_entries()])
    fake.payloads[routine]["definitionBody"] += "\n-- changed"
    client = _use(monkeypatch, fake)
    installs: list[object] = []
    monkeypatch.setattr(migration, "_apply_v2_installation", lambda *args: installs.append(args))
    snapshots: list[object] = []
    monkeypatch.setattr(migration, "_snapshot_v2_iam", lambda *args: snapshots.append(args) or ())
    before = deepcopy(fake.dataset["access"])
    code, error = _run(capsys, "apply-v2")
    assert code == 1
    assert error == {
        "error": "execution_approval_v2_routine_replace_voids_authorization",
        "routines": [{"routine": routine, "differs": ["definitionBody"]}],
    }
    assert installs == []
    assert snapshots == []
    assert client.queries == []
    assert fake.writes == []
    assert fake.dataset["access"] == before


def test_apply_v2_refuses_on_any_entry_the_routine_holds_not_only_delta_ones(monkeypatch, capsys):
    # An amendment e entry, outside the amendment d delta, is voided the same way.
    name = "sp_consume_open_intelligence_source_snapshot_v3"
    entry = {
        "role": "roles/bigquery.routineDataEditor",
        "routine": {
            "projectId": "ogilvy-trends-v2",
            "datasetId": "trends_v2_staging_approvals",
            "routineId": name,
        },
    }
    fake = _LiveFake(access=[*V1_ACCESS, entry])
    fake.payloads[name]["arguments"].pop()
    fake.payloads[APPROVE_V3]["definitionBody"] += "\n"
    client = _use(monkeypatch, fake)
    code, error = _run(capsys, "apply-v2")
    assert code == 1
    assert error["error"] == "execution_approval_v2_routine_replace_voids_authorization"
    # Only the routine holding an entry is named; the other would have been replaced.
    assert error["routines"] == [{"routine": name, "differs": ["arguments"]}]
    assert client.queries == []
    assert fake.writes == []


def test_the_live_finding_every_entry_listed_after_a_replace_reads_stale(monkeypatch, capsys):
    # 06:53:07Z: CREATE OR REPLACE moved every routine past the dataset's last change.
    fake = _LiveFake(
        access=[*V1_ACCESS, *_entries()], routine_time=REPLACED_TIME, dataset_time=DATASET_TIME
    )
    _use(monkeypatch, fake)
    code, plan = _run(capsys, "iam-v2-plan")
    assert code == 0
    assert plan["authorizations"]["present"] == 0
    assert plan["authorizations"]["missing"] == []
    assert len(plan["authorizations"]["stale"]) == 21
    assert plan["authorizations"]["possibly_stale"] == []
    approve = next(row for row in plan["authorizations"]["stale"] if row["routine"] == APPROVE_V2)
    assert approve == {
        "routine": APPROVE_V2,
        "role": "roles/bigquery.routineDataEditor",
        "written_role": "roles/bigquery.routineDataEditor",
        "routine_last_modified_time": "1970-01-01T00:50:00.000Z",
    }
    assert plan["authorizations"]["dataset_last_modified_time"] == "1970-01-01T00:33:20.000Z"


# Staleness


def test_an_entry_the_dataset_changed_after_is_only_possibly_stale_without_a_receipt(
    monkeypatch, capsys
):
    fake = _LiveFake(access=[*V1_ACCESS, *_entries()])
    _use(monkeypatch, fake)
    code, plan = _run(capsys, "iam-v2-plan")
    assert code == 0
    assert plan["authorizations"]["present"] == 0
    assert plan["authorizations"]["stale"] == []
    assert len(plan["authorizations"]["possibly_stale"]) == 21


def test_a_routine_without_a_readable_time_is_possibly_stale(monkeypatch, capsys):
    fake = _LiveFake(access=[*V1_ACCESS, *_entries()])
    for payload in fake.payloads.values():
        del payload["lastModifiedTime"]
    _use(monkeypatch, fake)
    code, plan = _run(capsys, "iam-v2-plan")
    assert code == 0
    assert plan["authorizations"]["present"] == 0
    assert len(plan["authorizations"]["possibly_stale"]) == 21


@pytest.mark.parametrize(
    "routine_time,dataset_time,recorded,verdict",
    [
        (5, 4, frozenset(), "authorisation_stale"),
        (5, 4, frozenset({5}), "authorisation_stale"),
        (4, 5, frozenset(), "authorisation_possibly_stale"),
        (5, 5, frozenset(), "authorisation_possibly_stale"),
        (4, 5, frozenset({3}), "authorisation_possibly_stale"),
        (4, 5, frozenset({4}), "present"),
        (4, None, frozenset({4}), "present"),
        (4, None, frozenset(), "authorisation_possibly_stale"),
        (None, 5, frozenset({4}), "authorisation_possibly_stale"),
    ],
)
def test_the_staleness_rule(routine_time, dataset_time, recorded, verdict):
    assert migration._staleness(routine_time, dataset_time, recorded) == verdict


def test_an_add_records_a_receipt_that_proves_the_entry_until_the_routine_changes(
    monkeypatch, capsys, receipts
):
    fake = _LiveFake()
    _use(monkeypatch, fake)
    code, applied = _run(capsys, "iam-v2-apply")
    assert code == 0, applied
    assert applied["authorisation_record"]["state"] == "recorded"
    assert applied["authorisation_record"]["recorded"] == 21
    assert applied["authorisation_staleness"]["present"] == 21
    [path] = sorted(receipts.glob("*.json"))
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["contract_version"] == "open_intelligence_v2_routine_authorisation_receipt_v1"
    assert {row["routine_last_modified_time"] for row in record["authorisations"]} == {
        str(ROUTINE_TIME)
    }
    code, plan = _run(capsys, "iam-v2-plan")
    assert plan["authorizations"]["present"] == 21
    # A replace after the add voids the entry, and the plan says so.
    fake.replace_routine(APPROVE_V2)
    code, plan = _run(capsys, "iam-v2-plan")
    assert plan["authorizations"]["present"] == 20
    assert [row["routine"] for row in plan["authorizations"]["stale"]] == [APPROVE_V2]


def test_a_foreign_or_unreadable_receipt_proves_nothing(monkeypatch, capsys, receipts):
    fake = _LiveFake(access=[*V1_ACCESS, *_entries()])
    _use(monkeypatch, fake)
    receipts.mkdir()
    rows = [{"routine": APPROVE_V2, "routine_last_modified_time": str(ROUTINE_TIME)}]
    (receipts / "a.json").write_text("{not json", encoding="utf-8")
    (receipts / "b.json").write_text(
        json.dumps(
            {
                "contract_version": "other",
                "project": "ogilvy-trends-v2",
                "dataset": "trends_v2_staging_approvals",
                "authorisations": rows,
            }
        ),
        encoding="utf-8",
    )
    (receipts / "c.json").write_text(
        json.dumps(
            {
                "contract_version": "open_intelligence_v2_routine_authorisation_receipt_v1",
                "project": "elsewhere",
                "dataset": "trends_v2_staging_approvals",
                "authorisations": rows,
            }
        ),
        encoding="utf-8",
    )
    code, plan = _run(capsys, "iam-v2-plan")
    assert code == 0
    assert plan["authorizations"]["present"] == 0


def test_a_receipt_that_cannot_be_written_leaves_the_entries_possibly_stale(
    monkeypatch, capsys, tmp_path
):
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(migration, "V2_AUTHORISATION_RECEIPTS_DIR", blocker / "receipts")
    fake = _LiveFake()
    _use(monkeypatch, fake)
    code, applied = _run(capsys, "iam-v2-apply")
    assert code == 0
    assert applied["authorisation_record"]["state"] == "unwritten"
    assert applied["authorisation_staleness"]["present"] == 0
    assert len(applied["authorisation_staleness"]["possibly_stale"]) == 21


def test_a_routine_that_moved_during_the_add_is_not_recorded(monkeypatch, capsys, receipts):
    fake = _LiveFake()
    _use(monkeypatch, fake)
    original = fake.patch

    def patch(url, **kwargs):
        response = original(url, **kwargs)
        fake.payloads[APPROVE_V2]["lastModifiedTime"] = str(fake.clock - 1)
        return response

    fake.patch = patch
    code, applied = _run(capsys, "iam-v2-apply")
    assert code == 0
    assert applied["authorisation_record"]["recorded"] == 20
    assert applied["authorisation_record"]["left_out"] == [APPROVE_V2]


# The state word


def test_the_state_word_prints_every_authorised_routine_and_writes_nothing(monkeypatch, capsys):
    extra = "sp_reconcile_open_intelligence_daily_consumption_v1"
    entry = {
        "role": "roles/bigquery.routineDataEditor",
        "routine": {
            "projectId": "ogilvy-trends-v2",
            "datasetId": "trends_v2_staging_approvals",
            "routineId": extra,
        },
    }
    held = {item.routine.rsplit("/", 1)[1] for item in _targets().authorizations} - {APPROVE_V3}
    fake = _LiveFake(access=[*V1_ACCESS, *_entries(held), entry], routine_time=REPLACED_TIME)
    _use(monkeypatch, fake)
    code, state = _run(capsys, "iam-v2-authorisation-state")
    assert code == 0
    assert fake.writes == []
    assert state["mode"] == "state"
    assert state["dataset_last_modified_time"] == "1970-01-01T00:33:20.000Z"
    rows = {row["routine"]: row for row in state["routines"]}
    # The 21 amendment d routines plus the listed amendment e one; the v1 entry is not v2.
    assert len(rows) == 22
    assert rows[APPROVE_V2] == {
        "routine": APPROVE_V2,
        "entry_present": True,
        "entry_count": 1,
        "entry_role": ["roles/bigquery.routineDataEditor"],
        "delta_role": "roles/bigquery.routineDataEditor",
        "routine_type": "PROCEDURE",
        "routine_last_modified_time": "1970-01-01T00:50:00.000Z",
        "recorded_routine_times": [],
        "differs": [],
        "verdict": "authorisation_stale",
    }
    assert rows[APPROVE_V3]["entry_present"] is False
    assert rows[APPROVE_V3]["verdict"] == "missing"
    assert rows[UDF]["entry_role"] == []
    assert rows[UDF]["routine_type"] == "SCALAR_FUNCTION"
    assert rows[extra]["delta_role"] is None
    assert rows[extra]["verdict"] == "authorisation_stale"
    assert state["counts"] == {"authorisation_stale": 21, "missing": 1}


def test_the_state_word_takes_no_options(monkeypatch, capsys):
    fake = _LiveFake()
    _use(monkeypatch, fake)
    assert migration.main(["iam-v2-authorisation-state", "now"]) == 2
    assert fake.writes == []


def test_state_word_reports_live_routine_differences(monkeypatch, capsys):
    fake = _LiveFake()
    fake.payloads[APPROVE_V3]["definitionBody"] += "\n"
    _use(monkeypatch, fake)
    code, state = _run(capsys, "iam-v2-authorisation-state")
    assert code == 0
    rows = {row["routine"]: row for row in state["routines"]}
    assert all(isinstance(row["differs"], list) for row in rows.values())
    assert rows[APPROVE_V3]["differs"] == ["definitionBody"]
    assert rows[APPROVE_V2]["differs"] == []


def test_reauthorise_refuses_a_missing_entry_for_a_changed_routine_before_any_patch(
    monkeypatch, capsys
):
    fake = _LiveFake(access=list(V1_ACCESS))
    fake.payloads[APPROVE_V3]["definitionBody"] += "\n"
    _use(monkeypatch, fake)
    code, error = _run(capsys, "iam-v2-reauthorise-apply")
    assert code == 1
    assert error == {
        "error": "execution_approval_v2_reauthorise_routine_not_current",
        "routines": [{"routine": APPROVE_V3, "differs": ["definitionBody"]}],
    }
    assert fake.writes == []


def test_standalone_iam_apply_refuses_a_missing_entry_for_a_changed_routine_before_any_patch(
    monkeypatch, capsys
):
    fake = _LiveFake(access=list(V1_ACCESS))
    fake.payloads[APPROVE_V3]["definitionBody"] += "\n"
    _use(monkeypatch, fake)
    code, error = _run(capsys, "iam-v2-apply")
    assert code == 1
    assert error == {
        "error": "execution_approval_v2_reauthorise_routine_not_current",
        "routines": [{"routine": APPROVE_V3, "differs": ["definitionBody"]}],
    }
    assert fake.writes == []


# The re-authorisation


def test_reauthorise_refuses_while_a_stale_entry_is_still_listed(monkeypatch, capsys):
    fake = _LiveFake(access=[*V1_ACCESS, *_entries()], routine_time=REPLACED_TIME)
    _use(monkeypatch, fake)
    for word in ("iam-v2-reauthorise-dry-run", "iam-v2-reauthorise-apply"):
        code, error = _run(capsys, word)
        assert code == 1
        assert error["error"] == "execution_approval_v2_iam_authorisation_still_stale"
        assert len(error["stale"]) == 21
    assert fake.writes == []


def test_reauthorise_dry_run_names_the_entries_and_sends_nothing(monkeypatch, capsys):
    # BigQuery dropped the stale entries, which moved the dataset's time.
    fake = _LiveFake(routine_time=REPLACED_TIME, dataset_time=REPLACED_TIME + 5000)
    _use(monkeypatch, fake)
    code, dry = _run(capsys, "iam-v2-reauthorise-dry-run")
    assert code == 0
    assert fake.writes == []
    assert dry["writes_planned"] == {"dataset_access_patches": 1, "routine_policy_sets": 0}
    assert len(dry["entries_to_add"]) == 21
    assert dry["authorizations"] == {"target": 21, "missing_before": 21, "added": 0}


def test_reauthorise_adds_the_dropped_entries_add_only_under_the_etag(
    monkeypatch, capsys, receipts
):
    fake = _LiveFake(routine_time=REPLACED_TIME, dataset_time=REPLACED_TIME + 5000)
    _use(monkeypatch, fake)
    before = deepcopy(fake.dataset["access"])
    code, receipt = _run(capsys, "iam-v2-reauthorise-apply")
    assert code == 0, receipt
    [patch] = _patches(fake)
    assert patch["headers"] == {"If-Match": "dataset-etag-1"}
    assert patch["params"] == {"fields": "access"}
    sent = patch["json"]["access"]
    # Every entry read is sent back unchanged and in order; only routine entries follow.
    assert sent[: len(before)] == before
    assert all("routine" in entry for entry in sent[len(before) :])
    assert len(sent) == len(before) + 21
    # No routine policy is written by this word.
    assert [method for method, _url, _body in fake.writes] == ["patch"]
    assert receipt["authorizations"] == {"target": 21, "missing_before": 21, "added": 21}
    assert receipt["authorisation_record"]["recorded"] == 21
    assert receipt["authorisation_staleness"]["present"] == 21
    code, plan = _run(capsys, "iam-v2-plan")
    assert plan["authorizations"]["present"] == 21
    assert plan["authorizations"]["stale"] == plan["authorizations"]["possibly_stale"] == []
    code, again = _run(capsys, "iam-v2-reauthorise-apply")
    assert code == 0
    assert again["writes_planned"]["dataset_access_patches"] == 0
    assert len(_patches(fake)) == 1


def test_reauthorise_leaves_a_possibly_stale_entry_listed_and_adds_the_rest(monkeypatch, capsys):
    kept = {APPROVE_V2}
    fake = _LiveFake(
        access=[*V1_ACCESS, *_entries(kept)],
        routine_time=REPLACED_TIME,
        dataset_time=REPLACED_TIME + 5000,
    )
    _use(monkeypatch, fake)
    before = deepcopy(fake.dataset["access"])
    code, receipt = _run(capsys, "iam-v2-reauthorise-apply")
    assert code == 0, receipt
    assert receipt["authorizations"]["added"] == 20
    assert [row["routine"] for row in receipt["authorisation_staleness"]["possibly_stale"]] == [
        APPROVE_V2
    ]
    [patch] = _patches(fake)
    assert patch["json"]["access"][: len(before)] == before


def test_reauthorise_fails_closed_on_a_concurrent_dataset_change(monkeypatch, capsys):
    fake = _LiveFake(routine_time=REPLACED_TIME, dataset_time=REPLACED_TIME + 5000)
    fake.bump_dataset_etag_before_write = True
    _use(monkeypatch, fake)
    code, error = _run(capsys, "iam-v2-reauthorise-apply")
    assert code == 1
    assert error == {
        "error": "execution_approval_v2_iam_write_refused",
        "applied": {
            "dataset_access_patched": False,
            "authorizations_added": 0,
            "failed_at": "dataset_access",
        },
    }


def test_reauthorise_fails_closed_when_a_readback_loses_an_existing_entry(monkeypatch, capsys):
    fake = _LiveFake(routine_time=REPLACED_TIME, dataset_time=REPLACED_TIME + 5000)
    _use(monkeypatch, fake)
    original = fake.patch

    def lossy(url, **kwargs):
        response = original(url, **kwargs)
        fake.dataset["access"] = [
            entry
            for entry in fake.dataset["access"]
            if entry.get("specialGroup") != "projectReaders"
        ]
        return response

    fake.patch = lossy
    code, error = _run(capsys, "iam-v2-reauthorise-apply")
    assert code == 1
    assert error["error"] == "execution_approval_v2_iam_readback_mismatch"
    assert error["applied"]["failed_at"] == "readback"


def test_the_reauthorisation_guard_refuses_a_payload_that_drops_or_rewrites_an_entry():
    targets = _targets()
    added = list(targets.authorizations[:1])
    functions = frozenset()
    good = [*deepcopy(V1_ACCESS), migration._v2_authorization_entry(added[0], functions)]
    migration._guard_reauthorisation(V1_ACCESS, good, added, functions)
    for bad in (
        good[1:],
        [{**good[0], "role": "READER"}, *good[1:]],
        [*good, {"role": "READER", "specialGroup": "allAuthenticatedUsers"}],
    ):
        with pytest.raises(migration.MigrationRefusal, match="v2_iam_write_refused"):
            migration._guard_reauthorisation(V1_ACCESS, bad, added, functions)


# No word removes an entry


def test_no_word_ever_sends_an_access_list_without_every_entry_it_read(monkeypatch, capsys):
    other = {"role": "WRITER", "userByEmail": f"someone{migration._V2_IAM_SERVICE_SUFFIX}"}
    stale_elsewhere = {
        "routine": {
            "projectId": "ogilvy-trends-v2",
            "datasetId": "trends_v2_staging_approvals",
            "routineId": "sp_consume_open_intelligence_execution_v1",
        }
    }
    fake = _LiveFake(
        access=[*V1_ACCESS, other, stale_elsewhere],
        routine_time=REPLACED_TIME,
        dataset_time=REPLACED_TIME + 5000,
    )
    fake.payloads[APPROVE_V3]["definitionBody"] += "\n"
    _use(monkeypatch, fake)
    reads: list[list[dict]] = []
    original = fake.patch

    def patch(url, **kwargs):
        reads.append(deepcopy(fake.dataset["access"]))
        return original(url, **kwargs)

    fake.patch = patch
    migration._apply_v2_plan(_plan())
    for word in ("iam-v2-apply", "iam-v2-reauthorise-apply", "iam-v2-apply"):
        code, _receipt = _run(capsys, word)
        assert code == 0
    patches = _patches(fake)
    assert len(patches) == len(reads) >= 1
    for read, body in zip(reads, patches, strict=True):
        sent = body["json"]["access"]
        assert sent[: len(read)] == read
    for entry in [*V1_ACCESS, other, stale_elsewhere]:
        assert entry in fake.dataset["access"]
