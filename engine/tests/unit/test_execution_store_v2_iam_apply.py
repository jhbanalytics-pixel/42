"""Owner run v2 routine IAM words: iam-v2-plan, iam-v2-dry-run and iam-v2-apply.

The v2 routines were created with an empty routine IAM policy and no dataset
authorization. These words grant exactly the approved provisioning delta's routine
authorizations and routine level bindings, add only, with etag guarded writes and a
full readback, and apply-v2 re-runs the same step after it replaces the routines.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from scripts.migrations import create_open_intelligence_execution_approval_store as migration

# Amendment d as approved and applied; ops/deploy/iam_delta_v1.json now carries amendment e,
# so the approved amendment d bytes are retained beside the amendment d manifest.
DELTA_PATH = (
    Path(__file__).resolve().parents[2]
    / "configs"
    / "open_intelligence"
    / "iam_delta_amendment_d.json"
)
DELTA_SHA256 = "1d2e8ead5a7c17c51b53ca640ed4c295294aef204ce4abeee9f03c7e822d5e3e"
BQ = "//bigquery.googleapis.com/"
APPROVALS = "projects/ogilvy-trends-v2/datasets/trends_v2_staging_approvals"
DATASET_URL = f"https://bigquery.googleapis.com/bigquery/v2/{APPROVALS}"
PROJECT_URL = "https://cloudresourcemanager.googleapis.com/v1/projects/ogilvy-trends-v2"
SA = "@ogilvy-trends-v2.iam.gserviceaccount.com"
SECRET_TOKEN = "ya29.fake-access-token-never-printed"


def _delta() -> dict:
    return json.loads(DELTA_PATH.read_text(encoding="utf-8"))


def _delta_authorizations() -> set[tuple[str, str, str]]:
    return {
        (
            row["routine"].removeprefix(BQ),
            "ogilvy-trends-v2.trends_v2_staging_approvals",
            row["role"],
        )
        for row in _delta()["routine_authorizations"]
    }


def _delta_routine_bindings() -> set[tuple[str, str, str]]:
    return {
        (row["member"], row["resource"].removeprefix(BQ), row["role"])
        for row in _delta()["bindings"]
        if row["resource"].startswith(BQ + APPROVALS + "/routines/")
    }


def _delta_project_bindings() -> set[tuple[str, str]]:
    return {
        (row["member"], row["role"])
        for row in _delta()["bindings"]
        if row["resource"] == "//cloudresourcemanager.googleapis.com/projects/ogilvy-trends-v2"
    }


V1_ACCESS = [
    {"role": "OWNER", "specialGroup": "projectOwners"},
    {"role": "READER", "specialGroup": "projectReaders"},
    {
        "role": "roles/bigquery.routineDataEditor",
        "routine": {
            "projectId": "ogilvy-trends-v2",
            "datasetId": "trends_v2_staging_approvals",
            "routineId": "sp_consume_open_intelligence_execution_v1",
        },
    },
]


class _Response:
    def __init__(self, status: int, payload: object):
        self.status_code = status
        self._payload = deepcopy(payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self) -> object:
        return deepcopy(self._payload)


class _FakeGoogle:
    """An in memory BigQuery dataset, routine policies and project policy with etags."""

    def __init__(self, *, access=None, routine_policies=None, project_bindings=None):
        self.dataset = {
            "id": "ogilvy-trends-v2:trends_v2_staging_approvals",
            "etag": "dataset-etag-1",
            "access": deepcopy(V1_ACCESS if access is None else access),
        }
        self.routines: dict[str, dict] = deepcopy(routine_policies or {})
        if project_bindings is None:
            project_bindings = [
                {
                    "role": role,
                    "members": sorted(m for m, r in _delta_project_bindings() if r == role),
                }
                for role in sorted({r for _m, r in _delta_project_bindings()})
            ]
        self.project = {"etag": "project-etag-1", "version": 1, "bindings": project_bindings}
        self.writes: list[tuple[str, str, object]] = []
        self.reads: list[tuple[str, str]] = []
        self.drop_routine_writes: set[str] = set()
        self.bump_dataset_etag_before_write = False
        # BigQuery need not echo the role of an authorized routine entry on read.
        self.echo_routine_role = True
        self.absent_routines: set[str] = set()
        self.refuse_routine_writes: set[str] = set()
        self.policy_versions: list[object] = []
        # routines.get routineType; live on 23 Sept only the canonical JSON UDF is a function.
        self.routine_types: dict[str, str] = {
            "fn_is_canonical_execution_json_v1": "SCALAR_FUNCTION"
        }
        from tests.unit.test_execution_store_v2_routine_authorisation import (
            _plan,
            _routine_payload,
        )

        self.routine_payloads = {
            item.name: _routine_payload(item, 1_000_000) for item in _plan().routines
        }
        self._counter = 1

    def _next(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def _routine_type(self, name: str) -> str:
        return self.routine_types.get(name, "PROCEDURE")

    def get(self, url, **_kwargs):
        self.reads.append(("get", url))
        if url.startswith(DATASET_URL + "/routines/"):
            name = url.removeprefix(DATASET_URL + "/routines/")
            if f"{APPROVALS}/routines/{name}" in self.absent_routines:
                return _Response(404, {"error": "not found"})
            payload = deepcopy(
                self.routine_payloads.get(name, {"routineType": self._routine_type(name)})
            )
            payload["routineType"] = self._routine_type(name)
            return _Response(200, payload)
        if url == DATASET_URL:
            payload = deepcopy(self.dataset)
            if not self.echo_routine_role:
                for entry in payload["access"]:
                    if "routine" in entry:
                        entry.pop("role", None)
            return _Response(200, payload)
        return _Response(404, {"error": "not found"})

    def post(self, url, json=None, **_kwargs):
        if url.endswith(":getIamPolicy"):
            self.reads.append(("post", url))
            self.policy_versions.append(
                (json or {}).get("options", {}).get("requestedPolicyVersion")
            )
            if url == PROJECT_URL + ":getIamPolicy":
                return _Response(200, self.project)
            resource = url.removeprefix("https://bigquery.googleapis.com/bigquery/v2/")
            resource = resource.removesuffix(":getIamPolicy")
            if resource in self.absent_routines:
                return _Response(404, {"error": "not found"})
            policy = self.routines.setdefault(resource, {"etag": "BwE="})
            return _Response(200, policy)
        if url.endswith(":setIamPolicy"):
            self.writes.append(("post", url, deepcopy(json)))
            if url == PROJECT_URL + ":setIamPolicy":
                return _Response(403, {"error": "project writes are out of scope"})
            resource = url.removeprefix("https://bigquery.googleapis.com/bigquery/v2/")
            resource = resource.removesuffix(":setIamPolicy")
            current = self.routines.setdefault(resource, {"etag": "BwE="})
            policy = dict(json["policy"])
            if policy.get("etag") != current.get("etag"):
                return _Response(409, {"error": "etag mismatch"})
            if resource in self.refuse_routine_writes:
                return _Response(403, {"error": "refused"})
            if resource in self.drop_routine_writes:
                return _Response(200, current)
            policy["etag"] = self._next("routine-etag")
            self.routines[resource] = policy
            return _Response(200, policy)
        return _Response(404, {"error": "not found"})

    def patch(self, url, *, params=None, json=None, headers=None):
        self.writes.append(
            ("patch", url, {"params": params, "json": deepcopy(json), "headers": headers})
        )
        if url != DATASET_URL:
            return _Response(404, {"error": "not found"})
        if self.bump_dataset_etag_before_write:
            self.dataset["etag"] = self._next("dataset-etag")
        if (headers or {}).get("If-Match") != self.dataset["etag"]:
            return _Response(412, {"error": "precondition failed"})
        for entry in json["access"]:
            routine = entry.get("routine")
            if (
                routine
                and "role" in entry
                and self._routine_type(routine["routineId"]) == "SCALAR_FUNCTION"
            ):
                # Live 23 Sept: "Role is not supported for routine ..." (status 3).
                return _Response(400, {"error": "Role is not supported for routine"})
        self.dataset["access"] = deepcopy(json["access"])
        self.dataset["etag"] = self._next("dataset-etag")
        return _Response(200, self.dataset)


class _Credentials:
    token = SECRET_TOKEN


@pytest.fixture(autouse=True)
def _authorisation_receipts(tmp_path, monkeypatch):
    # An add records a receipt beside the checkout; tests keep theirs in a temporary folder.
    monkeypatch.setattr(migration, "V2_AUTHORISATION_RECEIPTS_DIR", tmp_path / "receipts")


@pytest.fixture
def google(monkeypatch):
    fake = _FakeGoogle()
    monkeypatch.setattr(migration, "_load_credentials", lambda: _Credentials())
    monkeypatch.setattr(migration, "_authorized_session", lambda _credentials: fake)
    return fake


def _run(capsys, word: str) -> tuple[int, dict]:
    code = migration.main([word])
    captured = capsys.readouterr()
    text = captured.out if code == 0 else captured.err
    assert SECRET_TOKEN not in captured.out + captured.err
    return code, json.loads(text)


def _granted_routine_bindings(fake: _FakeGoogle) -> set[tuple[str, str, str]]:
    rows = set()
    for resource, policy in fake.routines.items():
        for item in policy.get("bindings", ()):
            for member in item.get("members", ()):
                rows.add((member, resource, item["role"]))
    return rows


def _granted_authorizations(fake: _FakeGoogle) -> set[tuple[str, str, str]]:
    return {
        (
            f"projects/{entry['routine']['projectId']}/datasets/{entry['routine']['datasetId']}"
            f"/routines/{entry['routine']['routineId']}",
            f"{entry['routine']['projectId']}.{entry['routine']['datasetId']}",
            entry.get("role"),
        )
        for entry in fake.dataset["access"]
        if "routine" in entry
        and entry["routine"]["datasetId"] == "trends_v2_staging_approvals"
        and entry["routine"]["routineId"] != "sp_consume_open_intelligence_execution_v1"
    }


def _expected_written_authorizations() -> set[tuple[str, str, str | None]]:
    # BigQuery takes no role on a function's dataset entry, so the UDF entry is roleless.
    return {
        (routine, dataset, None if routine.endswith("/fn_is_canonical_execution_json_v1") else role)
        for routine, dataset, role in _delta_authorizations()
    }


def _sorted_binding_routines() -> list[str]:
    return sorted({resource.rsplit("/", 1)[1] for _m, resource, _r in _delta_routine_bindings()})


UDF = "fn_is_canonical_execution_json_v1"
UDF_RESOURCE = f"{APPROVALS}/routines/{UDF}"
ALBERT = "user:albert.meintjes@ogilvy.co.za"
ORCHESTRATION = f"serviceAccount:intelligence-42-orchestration{SA}"


# Target set


def test_delta_file_is_the_pinned_approved_amendment():
    assert hashlib.sha256(DELTA_PATH.read_bytes()).hexdigest() == DELTA_SHA256
    assert migration.V2_IAM_DELTA_SHA256 == DELTA_SHA256
    assert _delta()["approval"]["state"] == "approved"


def test_v2_iam_targets_equal_the_delta_and_the_plan_disagreement_is_reported():
    plan = migration.build_v2_plan()
    targets = migration.v2_iam_targets(plan)
    assert {(a.routine, a.dataset, a.role) for a in targets.authorizations} == (
        _delta_authorizations()
    )
    assert len(targets.authorizations) == 21
    assert {(b.principal, b.resource, b.role) for b in targets.bindings} == (
        _delta_routine_bindings()
    )
    assert len(targets.bindings) == 30
    assert all(b.principal.startswith("serviceAccount:") for b in targets.bindings)
    assert {(b.principal, b.role) for b in targets.project_bindings} == _delta_project_bindings()
    disagreement = targets.disagreement
    # The plan installs the amendment e reconcile and v3 source snapshot routines, which
    # the amendment d delta does not authorize; iam-e-apply writes those authorizations
    # from its own delta.
    assert disagreement["authorizations_plan_only"] == [
        {
            "routine": "sp_consume_open_intelligence_source_snapshot_v3",
            "role": "roles/bigquery.routineDataEditor",
        },
        {
            "routine": "sp_reconcile_open_intelligence_daily_consumption_v1",
            "role": "roles/bigquery.routineDataEditor",
        },
    ]
    assert disagreement["authorizations_delta_only"] == []
    assert disagreement["routine_bindings_delta_only"] == []
    plan_only = {
        (row["member"], row["routine"]) for row in disagreement["routine_bindings_plan_only"]
    }
    human = "user:albert.meintjes@ogilvy.co.za"
    udf = "fn_is_canonical_execution_json_v1"
    assert plan_only == {
        *(
            (human, name)
            for name in (
                "sp_approve_open_intelligence_execution_v2",
                "sp_approve_open_intelligence_execution_v3",
                "sp_disable_open_intelligence_execution_approval_v2",
                udf,
                "sp_approve_open_intelligence_recurring_grant_v2",
                "sp_read_open_intelligence_recurring_grant_v2",
                "sp_disable_open_intelligence_recurring_grant_v2",
            )
        ),
        (f"serviceAccount:intelligence-42-brain{SA}", udf),
        (f"serviceAccount:intelligence-42-funded{SA}", udf),
        (f"serviceAccount:intelligence-42-migration{SA}", udf),
    }


def test_a_changed_delta_file_is_refused(tmp_path, monkeypatch):
    delta = _delta()
    delta["bindings"][0]["purpose"] = "changed"
    path = tmp_path / "iam_delta_v1.json"
    path.write_text(json.dumps(delta), encoding="utf-8")
    monkeypatch.setattr(migration, "V2_IAM_DELTA_PATH", path)
    with pytest.raises(
        migration.MigrationRefusal, match=r"^execution_approval_v2_iam_delta_invalid$"
    ):
        migration.v2_iam_targets(migration.build_v2_plan())


def _tampered_delta(tmp_path, monkeypatch, mutate) -> None:
    delta = _delta()
    mutate(delta)
    path = tmp_path / "iam_delta_v1.json"
    raw = json.dumps(delta).encode("utf-8")
    path.write_bytes(raw)
    monkeypatch.setattr(migration, "V2_IAM_DELTA_PATH", path)
    monkeypatch.setattr(migration, "V2_IAM_DELTA_SHA256", hashlib.sha256(raw).hexdigest())


def _routine_row(member: str, routine: str = "sp_read_open_intelligence_execution_result_v2"):
    return {
        "condition": None,
        "member": member,
        "purpose": "test",
        "resource": f"{BQ}{APPROVALS}/routines/{routine}",
        "role": "roles/bigquery.dataViewer",
    }


@pytest.mark.parametrize(
    "member",
    [
        "user:albert.meintjes@ogilvy.co.za",
        "user:jhb.analytics@gmail.com",
        "group:everyone@ogilvy.co.za",
        f"serviceAccount:not-in-the-delta{SA}",
        "allUsers",
    ],
)
def test_a_member_outside_the_delta_service_identities_is_refused(tmp_path, monkeypatch, member):
    _tampered_delta(tmp_path, monkeypatch, lambda d: d["bindings"].append(_routine_row(member)))
    with pytest.raises(
        migration.MigrationRefusal, match=r"^execution_approval_v2_iam_member_refused$"
    ):
        migration.v2_iam_targets(migration.build_v2_plan())


def test_a_delta_grant_on_a_routine_the_plan_does_not_install_is_refused(tmp_path, monkeypatch):
    _tampered_delta(
        tmp_path,
        monkeypatch,
        lambda d: d["bindings"].append(
            _routine_row(
                f"serviceAccount:intelligence-42-brain{SA}",
                "sp_consume_open_intelligence_execution_v1",
            )
        ),
    )
    with pytest.raises(
        migration.MigrationRefusal, match=r"^execution_approval_v2_iam_delta_invalid$"
    ):
        migration.v2_iam_targets(migration.build_v2_plan())


def test_a_conditional_or_non_viewer_routine_binding_is_refused(tmp_path, monkeypatch):
    def mutate(delta):
        row = _routine_row(f"serviceAccount:intelligence-42-brain{SA}")
        row["role"] = "roles/bigquery.dataOwner"
        delta["bindings"].append(row)

    _tampered_delta(tmp_path, monkeypatch, mutate)
    with pytest.raises(
        migration.MigrationRefusal, match=r"^execution_approval_v2_iam_delta_invalid$"
    ):
        migration.v2_iam_targets(migration.build_v2_plan())


# Plan and dry run are read only


def test_iam_v2_plan_lists_every_missing_item_and_writes_nothing(google, capsys):
    code, receipt = _run(capsys, "iam-v2-plan")
    assert code == 0
    assert google.writes == []
    assert receipt["mode"] == "plan"
    assert receipt["delta_sha256"] == DELTA_SHA256
    assert receipt["authorizations"]["target"] == 21
    assert receipt["authorizations"]["present"] == 0
    assert len(receipt["authorizations"]["missing"]) == 21
    assert receipt["routine_bindings"]["target"] == 30
    assert len(receipt["routine_bindings"]["missing"]) == 30
    missing = {
        (row["member"], f"{APPROVALS}/routines/{row['routine']}", row["role"])
        for row in receipt["routine_bindings"]["missing"]
    }
    assert missing == _delta_routine_bindings()
    assert receipt["project_level"]["missing"] == []
    assert receipt["disagreement"]["routine_bindings_plan_only_count"] == 10


def test_iam_v2_plan_reports_a_missing_project_binding_and_never_grants_it(monkeypatch, capsys):
    fake = _FakeGoogle(project_bindings=[])
    monkeypatch.setattr(migration, "_load_credentials", lambda: _Credentials())
    monkeypatch.setattr(migration, "_authorized_session", lambda _credentials: fake)
    code, receipt = _run(capsys, "iam-v2-plan")
    assert code == 0
    assert len(receipt["project_level"]["missing"]) == len(_delta_project_bindings())
    code, receipt = _run(capsys, "iam-v2-apply")
    assert code == 0
    assert not any(url.startswith(PROJECT_URL) for _m, url, _b in fake.writes)
    assert len(receipt["project_level"]["missing"]) == len(_delta_project_bindings())


def test_iam_v2_dry_run_computes_the_writes_and_sends_none(google, capsys):
    code, receipt = _run(capsys, "iam-v2-dry-run")
    assert code == 0
    assert google.writes == []
    assert receipt["mode"] == "dry_run"
    assert receipt["writes_planned"] == {"dataset_access_patches": 1, "routine_policy_sets": 15}
    assert receipt["authorizations"]["added"] == 0
    assert receipt["routine_bindings"]["added"] == 0
    assert len(receipt["planned_payload_sha256"]) == 64


# Apply


def test_iam_v2_apply_adds_exactly_the_delta_with_etags_and_removes_nothing(monkeypatch, capsys):
    brain = f"serviceAccount:intelligence-42-brain{SA}"
    other = f"serviceAccount:someone-else{SA}"
    resource = f"{APPROVALS}/routines/sp_read_open_intelligence_execution_result_v2"
    fake = _FakeGoogle(
        routine_policies={
            resource: {
                "etag": "BwX=",
                "version": 1,
                "bindings": [
                    {"role": "roles/bigquery.dataViewer", "members": [brain]},
                    {"role": "roles/bigquery.metadataViewer", "members": [other]},
                ],
            }
        }
    )
    monkeypatch.setattr(migration, "_load_credentials", lambda: _Credentials())
    monkeypatch.setattr(migration, "_authorized_session", lambda _credentials: fake)
    before_access = deepcopy(fake.dataset["access"])

    code, receipt = _run(capsys, "iam-v2-apply")

    assert code == 0, receipt
    assert receipt["mode"] == "apply"
    assert _granted_authorizations(fake) == _expected_written_authorizations()
    granted = _granted_routine_bindings(fake)
    assert _delta_routine_bindings() <= granted
    assert granted - _delta_routine_bindings() == {
        (other, resource, "roles/bigquery.metadataViewer")
    }
    assert not any(member.startswith("user:") for member, _r, _role in granted)
    for entry in before_access:
        assert entry in fake.dataset["access"]
    assert len(fake.dataset["access"]) == len(before_access) + 21
    patches = [w for w in fake.writes if w[0] == "patch"]
    assert len(patches) == 1
    assert patches[0][2]["headers"] == {"If-Match": "dataset-etag-1"}
    sets = [w for w in fake.writes if w[0] == "post"]
    assert len(sets) == 15
    for _method, _url, body in sets:
        assert "etag" in body["policy"]
    assert receipt["authorizations"] == {
        "target": 21,
        "present_before": 0,
        "added": 21,
        "present_after": 21,
    }
    assert receipt["routine_bindings"] == {
        "target": 30,
        "present_before": 1,
        "added": 29,
        "present_after": 30,
    }
    assert len(receipt["target_sha256"]) == 64
    assert len(receipt["readback_sha256"]) == 64


def test_a_second_iam_v2_apply_adds_nothing(google, capsys):
    code, first = _run(capsys, "iam-v2-apply")
    assert code == 0
    writes = len(google.writes)
    code, second = _run(capsys, "iam-v2-apply")
    assert code == 0
    assert len(google.writes) == writes
    assert second["authorizations"]["added"] == 0
    assert second["routine_bindings"]["added"] == 0
    assert second["authorizations"]["present_before"] == 21
    assert second["routine_bindings"]["present_before"] == 30
    assert second["readback_sha256"] == first["readback_sha256"]
    assert second["target_sha256"] == first["target_sha256"]


def test_iam_v2_apply_fails_closed_on_a_concurrent_dataset_change(google, capsys):
    google.bump_dataset_etag_before_write = True
    code, error = _run(capsys, "iam-v2-apply")
    assert code == 1
    assert error == {
        "error": "execution_approval_v2_iam_write_refused",
        "applied": {
            "dataset_access_patched": False,
            "authorizations_added": 0,
            "routine_policies_set": [],
            "failed_at": "dataset_access",
        },
    }
    assert [w[0] for w in google.writes] == ["patch"]
    assert google.routines == {} or all("bindings" not in p for p in google.routines.values())


def test_iam_v2_apply_fails_closed_when_the_readback_misses_a_binding(google, capsys):
    google.drop_routine_writes.add(
        f"{APPROVALS}/routines/sp_record_open_intelligence_daily_result_v1"
    )
    code, error = _run(capsys, "iam-v2-apply")
    assert code == 1
    assert error["error"] == "execution_approval_v2_iam_readback_mismatch"
    assert error["applied"]["dataset_access_patched"] is True
    assert error["applied"]["authorizations_added"] == 21
    assert error["applied"]["routine_policies_set"] == _sorted_binding_routines()
    assert error["applied"]["failed_at"] == "readback"
    assert set(error) == {"error", "applied"}


def test_iam_v2_apply_fails_closed_when_a_readback_loses_an_existing_entry(google, capsys):
    original_patch = google.patch

    def lossy_patch(url, *, params=None, json=None, headers=None):
        response = original_patch(url, params=params, json=json, headers=headers)
        google.dataset["access"] = [
            entry
            for entry in google.dataset["access"]
            if entry.get("specialGroup") != "projectReaders"
        ]
        return response

    google.patch = lossy_patch
    code, error = _run(capsys, "iam-v2-apply")
    assert code == 1
    assert error["error"] == "execution_approval_v2_iam_readback_mismatch"
    assert error["applied"]["failed_at"] == "readback"
    assert set(error) == {"error", "applied"}


def test_iam_v2_receipts_carry_no_token_or_etag(google, capsys):
    for word in ("iam-v2-plan", "iam-v2-dry-run", "iam-v2-apply"):
        migration.main([word])
        out = capsys.readouterr().out
        assert SECRET_TOKEN not in out
        assert "etag" not in out.lower()
        assert "dataset-etag" not in out
        json.loads(out)


def test_iam_v2_words_take_no_options(google, capsys):
    assert migration.main(["iam-v2-apply", "now"]) == 2
    assert google.writes == []


# apply-v2 replaces every routine with CREATE OR REPLACE, which drops routine IAM


def test_apply_v2_reapplies_and_reads_back_the_routine_iam_after_installing(google, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: object())
    monkeypatch.setattr(
        migration, "_read_v1_lock_row", lambda _client: ("lock", "v1", 7, "active", None, 1, 2)
    )

    def install(_plan, _credentials):
        calls.append("install")
        # CREATE OR REPLACE drops the routine level policies.
        google.routines.clear()

    monkeypatch.setattr(migration, "_apply_v2_installation", install)
    monkeypatch.setattr(
        migration, "_readback_v2_store", lambda *_args: calls.append("store_readback")
    )
    real = migration._run_v2_iam

    def iam(plan, credentials, mode, *rest):
        calls.append(f"iam_{mode}")
        return real(plan, credentials, mode, *rest)

    monkeypatch.setattr(migration, "_run_v2_iam", iam)
    receipt = migration._apply_v2_plan(migration.build_v2_plan())
    assert calls == ["install", "store_readback", "iam_apply"]
    assert receipt["iam"]["routine_bindings"]["present_after"] == 30
    assert receipt["iam"]["authorizations"]["present_after"] == 21
    assert _delta_routine_bindings() <= _granted_routine_bindings(google)


def test_apply_v2_refuses_to_finish_when_the_routine_iam_readback_fails(
    google, monkeypatch, capsys
):
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: object())
    monkeypatch.setattr(
        migration, "_read_v1_lock_row", lambda _client: ("lock", "v1", 7, "active", None, 1, 2)
    )
    monkeypatch.setattr(migration, "_apply_v2_installation", lambda *_args: None)
    monkeypatch.setattr(migration, "_readback_v2_store", lambda *_args: None)
    google.drop_routine_writes.add(
        f"{APPROVALS}/routines/sp_consume_open_intelligence_execution_v2"
    )
    code = migration.main(["apply-v2"])
    captured = capsys.readouterr()
    assert code == 1
    error = json.loads(captured.err)
    assert error["error"] == "execution_approval_v2_iam_readback_mismatch"
    assert error["applied"]["failed_at"] == "readback"
    assert error["preserved_bindings"] == []
    assert set(error) == {"error", "applied", "preserved_bindings"}
    assert captured.out == ""


# Review follow ups: roleless routine entries, preserved bindings, partial writes


def _v2_routine_entry(name: str, role: str | None) -> dict:
    entry = {
        "routine": {
            "projectId": "ogilvy-trends-v2",
            "datasetId": "trends_v2_staging_approvals",
            "routineId": name,
        }
    }
    if role is not None:
        entry["role"] = role
    return entry


def _routine_refs(fake: _FakeGoogle) -> list[str]:
    return [entry["routine"]["routineId"] for entry in fake.dataset["access"] if "routine" in entry]


def test_roleless_procedure_entries_on_read_fail_closed_and_never_duplicate(google, capsys):
    # A procedure's entry must carry its delta role; if BigQuery did not echo it, the
    # role cannot be proven, so the readback refuses rather than counting it present.
    google.echo_routine_role = False
    code, first = _run(capsys, "iam-v2-apply")
    assert code == 1
    assert first["error"] == "execution_approval_v2_iam_readback_mismatch"
    assert first["applied"]["authorizations_added"] == 21
    assert first["applied"]["failed_at"] == "readback"
    code, second = _run(capsys, "iam-v2-apply")
    assert code == 1
    assert second == {"error": "execution_approval_v2_iam_authorization_role_mismatch"}
    refs = _routine_refs(google)
    assert len(refs) == len(set(refs)) == 22
    assert len(google.dataset["access"]) == len(V1_ACCESS) + 21


def test_iam_v2_plan_reports_how_existing_routine_entries_come_back(google, capsys):
    code, receipt = _run(capsys, "iam-v2-plan")
    assert code == 0
    assert receipt["dataset_routine_entries"] == {"count": 1, "with_role": 1, "without_role": 0}
    google.echo_routine_role = False
    code, receipt = _run(capsys, "iam-v2-plan")
    assert code == 0
    assert receipt["dataset_routine_entries"] == {"count": 1, "with_role": 0, "without_role": 1}


def test_a_v2_routine_entry_already_present_with_another_role_is_refused(monkeypatch, capsys):
    fake = _FakeGoogle(
        access=[
            *V1_ACCESS,
            _v2_routine_entry(
                "sp_consume_open_intelligence_execution_v2", "roles/bigquery.routineDataViewer"
            ),
        ]
    )
    monkeypatch.setattr(migration, "_load_credentials", lambda: _Credentials())
    monkeypatch.setattr(migration, "_authorized_session", lambda _credentials: fake)
    for word in ("iam-v2-plan", "iam-v2-dry-run", "iam-v2-apply"):
        code, error = _run(capsys, word)
        assert code == 1
        assert error == {"error": "execution_approval_v2_iam_authorization_role_mismatch"}
    assert fake.writes == []


def _fake_with_albert_on_udf(monkeypatch) -> _FakeGoogle:
    fake = _FakeGoogle(
        routine_policies={
            UDF_RESOURCE: {
                "etag": "BwUdf=",
                "version": 1,
                "bindings": [{"role": "roles/bigquery.dataViewer", "members": [ALBERT]}],
            }
        }
    )
    monkeypatch.setattr(migration, "_load_credentials", lambda: _Credentials())
    monkeypatch.setattr(migration, "_authorized_session", lambda _credentials: fake)
    return fake


def test_iam_v2_apply_keeps_an_existing_user_binding_on_the_canonical_json_function(
    monkeypatch, capsys
):
    fake = _fake_with_albert_on_udf(monkeypatch)
    code, dry = _run(capsys, "iam-v2-dry-run")
    assert code == 0
    assert fake.writes == []
    assert dry["policies_to_write"][UDF] == [
        {"role": "roles/bigquery.dataViewer", "members": sorted([ALBERT, ORCHESTRATION])}
    ]
    code, receipt = _run(capsys, "iam-v2-apply")
    assert code == 0, receipt
    assert fake.routines[UDF_RESOURCE]["bindings"] == [
        {"role": "roles/bigquery.dataViewer", "members": sorted([ALBERT, ORCHESTRATION])}
    ]
    users = {m for m, _r, _role in _granted_routine_bindings(fake) if m.startswith("user:")}
    assert users == {ALBERT}
    assert receipt["restored"] == []


def test_iam_v2_dry_run_prints_every_policy_it_will_write_without_etags(google, capsys):
    code, dry = _run(capsys, "iam-v2-dry-run")
    assert code == 0
    assert sorted(dry["policies_to_write"]) == _sorted_binding_routines()
    expected: dict[str, dict[str, set[str]]] = {}
    for member, resource, role in _delta_routine_bindings():
        expected.setdefault(resource.rsplit("/", 1)[1], {}).setdefault(role, set()).add(member)
    for name, rows in dry["policies_to_write"].items():
        assert rows == [
            {"role": role, "members": sorted(members)}
            for role, members in sorted(expected[name].items())
        ]
    assert "etag" not in json.dumps(dry).lower()


def test_a_partial_write_failure_reports_what_already_landed(google, capsys):
    names = _sorted_binding_routines()
    google.refuse_routine_writes.add(f"{APPROVALS}/routines/{names[2]}")
    code, error = _run(capsys, "iam-v2-apply")
    assert code == 1
    assert error == {
        "error": "execution_approval_v2_iam_write_refused",
        "applied": {
            "dataset_access_patched": True,
            "authorizations_added": 21,
            "routine_policies_set": names[:2],
            "failed_at": names[2],
        },
    }


def _apply_v2_fakes(monkeypatch, fake: _FakeGoogle, calls: list[str]) -> None:
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: object())
    monkeypatch.setattr(
        migration, "_read_v1_lock_row", lambda _client: ("lock", "v1", 7, "active", None, 1, 2)
    )

    def install(_plan, _credentials):
        calls.append("install")
        # CREATE OR REPLACE drops every routine level policy.
        fake.routines.clear()
        fake.absent_routines.clear()

    monkeypatch.setattr(migration, "_apply_v2_installation", install)
    monkeypatch.setattr(migration, "_readback_v2_store", lambda *_args: None)


def test_apply_v2_restores_a_hand_applied_binding_that_the_replace_dropped(monkeypatch):
    fake = _fake_with_albert_on_udf(monkeypatch)
    extra = f"{APPROVALS}/routines/sp_approve_open_intelligence_execution_v2"
    fake.routines[extra] = {
        "etag": "BwApprove=",
        "bindings": [{"role": "roles/bigquery.dataViewer", "members": [ALBERT]}],
    }
    calls: list[str] = []
    _apply_v2_fakes(monkeypatch, fake, calls)
    receipt = migration._apply_v2_plan(migration.build_v2_plan())
    assert calls == ["install"]
    assert fake.routines[UDF_RESOURCE]["bindings"] == [
        {"role": "roles/bigquery.dataViewer", "members": sorted([ALBERT, ORCHESTRATION])}
    ]
    assert fake.routines[extra]["bindings"] == [
        {"role": "roles/bigquery.dataViewer", "members": [ALBERT]}
    ]
    assert receipt["iam"]["restored"] == [
        {
            "member": ALBERT,
            "routine": "fn_is_canonical_execution_json_v1",
            "role": "roles/bigquery.dataViewer",
        },
        {
            "member": ALBERT,
            "routine": "sp_approve_open_intelligence_execution_v2",
            "role": "roles/bigquery.dataViewer",
        },
    ]
    assert receipt["iam"]["routine_bindings"]["present_after"] == 30


def test_apply_v2_snapshot_takes_the_routine_policies_before_the_install(monkeypatch):
    fake = _fake_with_albert_on_udf(monkeypatch)
    calls: list[str] = []
    _apply_v2_fakes(monkeypatch, fake, calls)
    real = migration._snapshot_v2_iam

    def snapshot(plan, credentials):
        calls.append("snapshot")
        return real(plan, credentials)

    monkeypatch.setattr(migration, "_snapshot_v2_iam", snapshot)
    migration._apply_v2_plan(migration.build_v2_plan())
    assert calls == ["snapshot", "install"]


def test_apply_v2_snapshot_accepts_routines_that_do_not_exist_yet(google, monkeypatch):
    plan = migration.build_v2_plan()
    google.absent_routines.update(f"{APPROVALS}/routines/{item.name}" for item in plan.routines)
    calls: list[str] = []
    _apply_v2_fakes(monkeypatch, google, calls)
    receipt = migration._apply_v2_plan(plan)
    assert receipt["iam"]["restored"] == []
    assert receipt["iam"]["routine_bindings"]["present_after"] == 30


def test_apply_v2_refuses_before_installing_when_a_routine_policy_cannot_be_restored(
    google, monkeypatch
):
    google.routines[UDF_RESOURCE] = {
        "etag": "BwC=",
        "version": 3,
        "bindings": [
            {
                "role": "roles/bigquery.dataViewer",
                "members": [ALBERT],
                "condition": {"expression": "request.time < timestamp('2027-01-01T00:00:00Z')"},
            }
        ],
    }
    calls: list[str] = []
    _apply_v2_fakes(monkeypatch, google, calls)
    with pytest.raises(
        migration.MigrationRefusal, match=r"^execution_approval_v2_iam_snapshot_unsupported$"
    ):
        migration._apply_v2_plan(migration.build_v2_plan())
    assert calls == []
    assert google.writes == []


def test_a_transport_error_mid_write_still_reports_what_already_landed(google, capsys):
    names = _sorted_binding_routines()
    original_post = google.post

    def failing_post(url, json=None, **kwargs):
        if url.endswith(f"/routines/{names[1]}:setIamPolicy"):
            raise ConnectionError("connection reset")
        return original_post(url, json=json, **kwargs)

    google.post = failing_post
    code, error = _run(capsys, "iam-v2-apply")
    assert code == 1
    assert error == {
        "error": "execution_approval_v2_iam_write_refused",
        "applied": {
            "dataset_access_patched": True,
            "authorizations_added": 21,
            "routine_policies_set": names[:1],
            "failed_at": names[1],
        },
    }


# Second review: apply-v2 never loses the snapshot on a refusal after it


ALBERT_UDF_ROW = {
    "member": ALBERT,
    "routine": "fn_is_canonical_execution_json_v1",
    "role": "roles/bigquery.dataViewer",
}


def _run_apply_v2(capsys) -> tuple[int, dict, str]:
    code = migration.main(["apply-v2"])
    captured = capsys.readouterr()
    assert SECRET_TOKEN not in captured.out + captured.err
    assert "etag" not in captured.err.lower()
    return code, json.loads(captured.err) if code else {}, captured.out


def test_apply_v2_store_readback_refusal_prints_the_snapshot_to_restore(monkeypatch, capsys):
    fake = _fake_with_albert_on_udf(monkeypatch)
    calls: list[str] = []
    _apply_v2_fakes(monkeypatch, fake, calls)

    def refuse(*_args):
        raise migration.MigrationRefusal("execution_approval_schema_mismatch")

    monkeypatch.setattr(migration, "_readback_v2_store", refuse)
    code, error, out = _run_apply_v2(capsys)
    assert code == 1
    assert out == ""
    assert calls == ["install"]
    assert error == {
        "error": "execution_approval_schema_mismatch",
        "preserved_bindings": [ALBERT_UDF_ROW],
    }


def test_apply_v2_refuses_a_changed_delta_before_installing(tmp_path, monkeypatch, capsys):
    fake = _fake_with_albert_on_udf(monkeypatch)
    calls: list[str] = []
    _apply_v2_fakes(monkeypatch, fake, calls)
    path = tmp_path / "iam_delta_v1.json"
    path.write_bytes(DELTA_PATH.read_bytes().replace(b"\n", b"\r\n"))
    monkeypatch.setattr(migration, "V2_IAM_DELTA_PATH", path)
    code, error, _out = _run_apply_v2(capsys)
    assert code == 1
    assert error == {"error": "execution_approval_v2_iam_delta_invalid"}
    assert calls == []
    assert fake.writes == []
    assert fake.routines[UDF_RESOURCE]["bindings"] == [
        {"role": "roles/bigquery.dataViewer", "members": [ALBERT]}
    ]


def test_apply_v2_refuses_a_wrong_role_entry_before_installing(monkeypatch, capsys):
    fake = _fake_with_albert_on_udf(monkeypatch)
    fake.dataset["access"].append(
        _v2_routine_entry(
            "sp_record_open_intelligence_execution_result_v2", "roles/bigquery.routineDataViewer"
        )
    )
    calls: list[str] = []
    _apply_v2_fakes(monkeypatch, fake, calls)
    code, error, _out = _run_apply_v2(capsys)
    assert code == 1
    assert error == {"error": "execution_approval_v2_iam_authorization_role_mismatch"}
    assert calls == []
    assert fake.writes == []
    assert fake.routines[UDF_RESOURCE]["bindings"] == [
        {"role": "roles/bigquery.dataViewer", "members": [ALBERT]}
    ]


def test_apply_v2_iam_refusal_after_install_prints_applied_and_the_snapshot(monkeypatch, capsys):
    fake = _fake_with_albert_on_udf(monkeypatch)
    calls: list[str] = []
    _apply_v2_fakes(monkeypatch, fake, calls)
    fake.refuse_routine_writes.add(UDF_RESOURCE)
    code, error, _out = _run_apply_v2(capsys)
    assert code == 1
    assert error["error"] == "execution_approval_v2_iam_write_refused"
    assert error["applied"]["failed_at"] == UDF
    assert error["preserved_bindings"] == [ALBERT_UDF_ROW]
    assert set(error) == {"error", "applied", "preserved_bindings"}


def test_apply_v2_unexpected_error_after_install_still_prints_the_snapshot(monkeypatch, capsys):
    fake = _fake_with_albert_on_udf(monkeypatch)
    calls: list[str] = []
    _apply_v2_fakes(monkeypatch, fake, calls)

    def boom(*_args):
        raise RuntimeError("network")

    monkeypatch.setattr(migration, "_readback_v2_store", boom)
    code, error, _out = _run_apply_v2(capsys)
    assert code == 1
    assert error == {
        "error": "execution_approval_internal_refusal",
        "preserved_bindings": [ALBERT_UDF_ROW],
    }


def test_routine_policies_are_read_at_policy_version_3(google, monkeypatch, capsys):
    code, _receipt = _run(capsys, "iam-v2-plan")
    assert code == 0
    migration._snapshot_v2_iam(migration.build_v2_plan(), object())
    assert google.policy_versions
    assert set(google.policy_versions) == {3}


# Live 23 Sept: BigQuery refuses a role on a function's authorized routine entry


UDF_DROP = {"routine": UDF, "delta_role": "roles/bigquery.routineDataViewer"}


def test_plan_and_dry_run_show_which_entries_are_functions_written_without_a_role(google, capsys):
    code, plan = _run(capsys, "iam-v2-plan")
    assert code == 0
    assert plan["role_dropped_for_function"] == [UDF_DROP]
    written = {row["routine"]: row["written_role"] for row in plan["authorizations"]["missing"]}
    assert written[UDF] is None
    delta_roles = {row["routine"]: row["role"] for row in plan["authorizations"]["missing"]}
    assert all(written[name] == delta_roles[name] for name in written if name != UDF)
    code, dry = _run(capsys, "iam-v2-dry-run")
    assert code == 0
    assert dry["role_dropped_for_function"] == [UDF_DROP]
    assert google.writes == []


def test_apply_writes_the_function_entry_without_a_role_and_matches_it_on_rerun(google, capsys):
    code, receipt = _run(capsys, "iam-v2-apply")
    assert code == 0, receipt
    assert receipt["role_dropped_for_function"] == [UDF_DROP]
    udf_entries = [
        entry
        for entry in google.dataset["access"]
        if entry.get("routine", {}).get("routineId") == UDF
    ]
    assert udf_entries == [_v2_routine_entry(UDF, None)]
    assert _granted_authorizations(google) == _expected_written_authorizations()
    code, second = _run(capsys, "iam-v2-apply")
    assert code == 0, second
    assert second["authorizations"]["added"] == 0
    assert second["authorizations"]["present_before"] == 21
    assert len(google.dataset["access"]) == len(V1_ACCESS) + 21


def test_the_routine_type_is_read_live_not_taken_from_the_name(google, capsys):
    # The same name read back as a PROCEDURE keeps the delta role.
    google.routine_types[UDF] = "PROCEDURE"
    code, plan = _run(capsys, "iam-v2-plan")
    assert code == 0
    assert plan["role_dropped_for_function"] == []
    written = {row["routine"]: row["written_role"] for row in plan["authorizations"]["missing"]}
    assert written[UDF] == "roles/bigquery.routineDataViewer"
    assert google.writes == []


@pytest.mark.parametrize("routine", [UDF, "sp_read_open_intelligence_daily_chain_v1"])
def test_a_table_valued_function_authorization_is_refused_as_unsupported(google, capsys, routine):
    # Only the scalar function refusal was observed live; a TVF is not guessed at.
    google.routine_types[routine] = "TABLE_VALUED_FUNCTION"
    for word in ("iam-v2-plan", "iam-v2-dry-run", "iam-v2-apply"):
        code, error = _run(capsys, word)
        assert code == 1
        assert error == {"error": "execution_approval_v2_iam_routine_type_unsupported"}
    assert google.writes == []


@pytest.mark.parametrize("routine_type", ["AGGREGATE_FUNCTION", "", None])
def test_an_unknown_routine_type_is_refused(google, capsys, routine_type):
    google.routine_types[UDF] = routine_type
    for word in ("iam-v2-plan", "iam-v2-dry-run", "iam-v2-apply"):
        code, error = _run(capsys, word)
        assert code == 1
        assert error == {"error": "execution_approval_v2_iam_routine_type_invalid"}
    assert google.writes == []


def test_a_function_entry_already_holding_a_role_is_refused(monkeypatch, capsys):
    fake = _FakeGoogle(
        access=[*V1_ACCESS, _v2_routine_entry(UDF, "roles/bigquery.routineDataViewer")]
    )
    monkeypatch.setattr(migration, "_load_credentials", lambda: _Credentials())
    monkeypatch.setattr(migration, "_authorized_session", lambda _credentials: fake)
    code, error = _run(capsys, "iam-v2-plan")
    assert code == 1
    assert error == {"error": "execution_approval_v2_iam_authorization_role_mismatch"}


# Third review: roleless procedure entries, preflight, readback and missing routines


def test_a_roleless_procedure_entry_present_live_is_refused(monkeypatch, capsys):
    fake = _FakeGoogle(
        access=[*V1_ACCESS, _v2_routine_entry("sp_approve_open_intelligence_execution_v2", None)]
    )
    monkeypatch.setattr(migration, "_load_credentials", lambda: _Credentials())
    monkeypatch.setattr(migration, "_authorized_session", lambda _credentials: fake)
    for word in ("iam-v2-plan", "iam-v2-dry-run", "iam-v2-apply"):
        code, error = _run(capsys, word)
        assert code == 1
        assert error == {"error": "execution_approval_v2_iam_authorization_role_mismatch"}
    assert fake.writes == []


def test_apply_v2_refuses_a_udf_entry_holding_a_role_before_installing(monkeypatch, capsys):
    fake = _fake_with_albert_on_udf(monkeypatch)
    fake.dataset["access"].append(_v2_routine_entry(UDF, "roles/bigquery.routineDataViewer"))
    calls: list[str] = []
    _apply_v2_fakes(monkeypatch, fake, calls)
    code, error, _out = _run_apply_v2(capsys)
    assert code == 1
    assert error == {"error": "execution_approval_v2_iam_authorization_role_mismatch"}
    assert calls == []
    assert fake.writes == []


def test_apply_v2_refuses_a_table_valued_function_before_installing(monkeypatch, capsys):
    fake = _fake_with_albert_on_udf(monkeypatch)
    fake.routine_types[UDF] = "TABLE_VALUED_FUNCTION"
    calls: list[str] = []
    _apply_v2_fakes(monkeypatch, fake, calls)
    code, error, _out = _run_apply_v2(capsys)
    assert code == 1
    assert error == {"error": "execution_approval_v2_iam_routine_type_unsupported"}
    assert calls == []


def test_a_patch_that_returns_200_without_landing_the_udf_entry_fails_the_readback(google, capsys):
    original_patch = google.patch

    def silent_patch(url, *, params=None, json=None, headers=None):
        kept = [
            entry for entry in json["access"] if entry.get("routine", {}).get("routineId") != UDF
        ]
        return original_patch(url, params=params, json={"access": kept}, headers=headers)

    google.patch = silent_patch
    code, error = _run(capsys, "iam-v2-apply")
    assert code == 1
    assert error["error"] == "execution_approval_v2_iam_readback_mismatch"
    assert error["applied"]["failed_at"] == "readback"


def test_a_v2_routine_missing_live_is_refused_as_missing(google, capsys):
    google.absent_routines.add(f"{APPROVALS}/routines/sp_consume_open_intelligence_execution_v2")
    for word in ("iam-v2-plan", "iam-v2-dry-run", "iam-v2-apply"):
        code, error = _run(capsys, word)
        assert code == 1
        assert error == {"error": "execution_approval_v2_iam_routine_missing"}
    assert google.writes == []


def test_the_readback_digest_covers_the_live_role_of_each_entry():
    plan = migration.build_v2_plan()
    targets = migration.v2_iam_targets(plan)
    udf = next(item for item in targets.authorizations if item.routine.endswith(UDF))
    functions = frozenset({udf.routine})
    access = [migration._v2_authorization_entry(item, functions) for item in targets.authorizations]
    state = migration._V2IamState("e", access, {}, {"etag": "p"}, functions)
    expected = hashlib.sha256(
        json.dumps(
            {
                "authorizations": sorted(
                    [item.routine, "" if item is udf else item.role]
                    for item in targets.authorizations
                ),
                "policies": {},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert migration._v2_iam_readback_digest(targets, state) == expected
