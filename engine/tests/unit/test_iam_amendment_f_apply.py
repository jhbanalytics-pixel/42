"""Amendment f through the iam-e words: one apply on one approved digest.

Amendment f stands in the delta's amendments list under its own approval block, beside
amendment e's stamp, so the stamp keeps covering exactly the bytes it covered. The
iam-e words serve every row the delta adds over the pinned amendment d bytes, the
amendment rows among them, and report the file approved only when the root block and
every amendment block are approved. So while amendment f is proposed the plan shows
amendment e's pending rows and f's rows and the apply refuses; once Albert approves
f's digest and the stamp lands, one apply writes every missing e and f row and nothing
else. Albert approved a792f21e with his go in session_01SGzy35qkbHF8xf2AFuo5nW at
2026-09-25T05:36:23Z, and the root file now carries that stamp, so the apply below
runs on the stamped digest. The staging state is the one the apply on 35dd1c2 left:
18 writes landed, the funded pilot job did not exist, and the project write was never
sent.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest
from scripts.migrations import create_open_intelligence_execution_approval_store as migration

from tests.unit.test_iam_amendment_e_apply import (
    BUILDS_VIEWER,
    DEPLOY,
    ENGINE,
    FIXTURE,
    FUNDED_JOB,
    FUNDED_ROW,
    LISTENING,
    ORCHESTRATION,
    PROJECT_ROWS,
    PROJECT_URL,
    RUN_URL,
    SECRET_TOKEN,
    TABLE_KEYS,
    _added,
    _counts,
    _Credentials,
    _FakeCloud,
    _planned,
)

F_FIXTURE = ENGINE / "tests" / "fixtures" / "iam_delta_amendment_f.json"
E_STAMPED_SHA256 = "a2bdcfdbe19a94e76699e7cac696f086084cb59a2bbf2102cabafb85aaec3ef1"
# ops/deploy/iam_delta_v1.json with amendment f proposed; tests/repository holds the
# root file equal to what this module builds.
F_PROPOSED_SHA256 = "a792f21e3340258a0c2dd017e5f6c6a2d7cb1242952359bb7a3cec2087673a22"
# The same file under Albert's stamp over amendment f.
F_STAMPED_SHA256 = "29d0f741a5295bddf2ef0e4cbce8c4a2c53bdfd5164b869878bf8f36a221131b"
E_STAMP = {
    "applied": False,
    "approved_at": "2026-09-24T06:53:16+00:00",
    "approved_by": (
        "Albert Meintjes, in the session chat, document 36b26fca392ed3267e67419ed03adcbd242fa4714008bc85e96ea6284533031c; "
        "amendment b PROVISIONING_DELTA_AMENDMENT_B_APPROVAL_20260914.json, document ea2579481ab074ce175f14e34db9e799d60ffc546ed3a8bd675e6f79bcf194df; "
        "amendment c PROVISIONING_DELTA_AMENDMENT_C_APPROVAL_20260914.json, document 3dce68ab20ecb238e10e15ea3e80bc962edb038d0d1f222e7e4ef72a7989e6ca; "
        "amendment d PROVISIONING_DELTA_AMENDMENT_D_APPROVAL_20260920.json, document 35023bcfc88eae60fe042e053dcf558a4bc375d2de881f8ff8a5543f61e9d13f; "
        "amendment e in the session chat, delta 33ad00c0254fd0e514c56bf17b98571f95608ab30604a2523499a7ddf37bda9f "
        "at commit b24e27cb01a89ad2631af0f3a0e9b076006b4cb9"
    ),
    "state": "approved",
}
PROPOSED = {"applied": False, "approved_at": None, "approved_by": None, "state": "proposed"}
F_APPROVED = {
    "applied": False,
    "approved_at": "2026-09-25T05:36:23+00:00",
    "approved_by": (
        "Albert Meintjes, go in session_01SGzy35qkbHF8xf2AFuo5nW "
        "at 2026-09-25T05:36:23Z"
    ),
    "state": "approved",
}
F_STAMP = {
    "applied": False,
    "approved_at": "2026-09-24T12:00:00+00:00",
    "approved_by": "Albert Meintjes, test stamp over amendment f",
    "state": "approved",
}
PROJECT = "ogilvy-trends-v2"
BQ = "//bigquery.googleapis.com/"
LEDGER_TABLES = [f"{BQ}projects/{PROJECT}/datasets/{key}" for key in TABLE_KEYS]
FUNDED_RESOURCE = FUNDED_ROW["resource"]
F_ROWS = {
    *((ORCHESTRATION, table, "roles/bigquery.dataViewer") for table in LEDGER_TABLES),
    (ORCHESTRATION, FUNDED_RESOURCE, "roles/run.viewer"),
}
E_PENDING = {(FUNDED_ROW["member"], FUNDED_ROW["resource"], FUNDED_ROW["role"])} | {
    (row["member"], row["resource"], row["role"]) for row in PROJECT_ROWS
}


def _canonical(delta: dict) -> bytes:
    return (json.dumps(delta, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _e_stamped() -> dict:
    delta = json.loads(FIXTURE.read_text(encoding="utf-8"))
    delta["approval"] = deepcopy(E_STAMP)
    return delta


def _f_delta(f_approval=PROPOSED) -> dict:
    delta = _e_stamped()
    delta["amendments"] = json.loads(F_FIXTURE.read_text(encoding="utf-8"))
    delta["amendments"][0]["approval"] = deepcopy(f_approval)
    return delta


def _use(tmp_path, monkeypatch, delta: dict, name: str = "iam_delta_v1.json") -> str:
    path = tmp_path / name
    raw = _canonical(delta)
    path.write_bytes(raw)
    monkeypatch.setattr(migration, "IAM_E_DELTA_PATH", path)
    return hashlib.sha256(raw).hexdigest()


def _run(capsys, *words: str) -> tuple[int, dict]:
    code = migration.main(list(words))
    captured = capsys.readouterr()
    assert SECRET_TOKEN not in captured.out + captured.err
    text = captured.out if code == 0 else captured.err
    return code, json.loads(text)


@pytest.fixture
def cloud(monkeypatch):
    fake = _FakeCloud()
    fake.install_all_routines()
    monkeypatch.setattr(migration, "_load_credentials", lambda: _Credentials())
    monkeypatch.setattr(migration, "_authorized_session", lambda _credentials: fake)
    return fake


def _today(cloud, capsys, tmp_path, monkeypatch) -> None:
    """The staging state the amendment e apply left on 24 September."""

    e_digest = _use(tmp_path, monkeypatch, _e_stamped(), "e.json")
    assert e_digest == E_STAMPED_SHA256
    cloud.refuse_writes.add(RUN_URL + FUNDED_JOB + ":setIamPolicy")
    code, error = _run(capsys, "iam-e-apply", e_digest)
    assert code == 1
    assert len(error["applied"]["writes"]) == 18
    cloud.refuse_writes.clear()
    del cloud.jobs[FUNDED_JOB]
    cloud.writes.clear()
    cloud.reads.clear()


def _deploy_funded_job(cloud) -> None:
    cloud.jobs[FUNDED_JOB] = {
        "etag": "funded-deployed",
        "version": 1,
        "bindings": [{"role": "roles/run.developer", "members": [DEPLOY]}],
    }


# The file


def test_the_delta_built_here_is_the_root_delta_with_amendment_f_proposed():
    raw = _canonical(_f_delta())
    assert hashlib.sha256(raw).hexdigest() == F_PROPOSED_SHA256
    without_f = json.loads(raw)
    del without_f["amendments"]
    assert hashlib.sha256(_canonical(without_f)).hexdigest() == E_STAMPED_SHA256


def test_the_delta_built_here_under_albert_s_stamp_is_the_stamped_root_delta():
    raw = _canonical(_f_delta(F_APPROVED))
    assert hashlib.sha256(raw).hexdigest() == F_STAMPED_SHA256


def test_the_targets_are_the_e_rows_and_the_four_f_rows_and_the_file_is_proposed(
    tmp_path, monkeypatch
):
    digest = _use(tmp_path, monkeypatch, _f_delta())
    targets = migration.iam_e_targets()
    assert targets.delta_sha256 == digest == F_PROPOSED_SHA256
    assert targets.approval_state == "proposed"
    rows = [(row.member, row.resource, row.role) for row in targets.bindings]
    e_rows = [(row["member"], row["resource"], row["role"]) for row in _added("bindings")]
    assert rows[: len(e_rows)] == e_rows
    assert set(rows[len(e_rows) :]) == F_ROWS
    assert len(rows) == len(e_rows) + 4
    kinds = {(row.kind, row.ident) for row in targets.bindings[len(e_rows) :]}
    assert kinds == {
        *(("table", key) for key in TABLE_KEYS),
        ("runtime_job", FUNDED_JOB),
    }


def test_the_file_is_approved_only_when_every_block_is(tmp_path, monkeypatch):
    _use(tmp_path, monkeypatch, _f_delta(F_STAMP))
    assert migration.iam_e_targets().approval_state == "approved"
    delta = _f_delta(F_STAMP)
    delta["approval"] = deepcopy(PROPOSED)
    _use(tmp_path, monkeypatch, delta)
    with pytest.raises(migration.MigrationRefusal, match="delta_invalid"):
        migration.iam_e_targets()
    delta["amendments"][0]["approval"] = deepcopy(PROPOSED)
    _use(tmp_path, monkeypatch, delta)
    assert migration.iam_e_targets().approval_state == "proposed"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda delta: delta.update(amendments=[]),
        lambda delta: delta.update(amendments={}),
        lambda delta: delta["amendments"][0].update(note="x"),
        lambda delta: delta["amendments"][0].pop("approval"),
        lambda delta: delta["amendments"][0].update(bindings=[]),
        lambda delta: delta["amendments"][0].update(amendment="F"),
        lambda delta: delta["amendments"][0]["approval"].update(applied=True),
        lambda delta: delta["amendments"][0]["approval"].update(state="approved"),
        lambda delta: delta["amendments"][0]["approval"].update(approved_by="Albert"),
        lambda delta: delta["amendments"].append(deepcopy(delta["amendments"][0])),
        lambda delta: delta["amendments"][0]["bindings"].append(deepcopy(delta["bindings"][0])),
    ],
    ids=[
        "empty",
        "not_a_list",
        "extra_key",
        "no_approval",
        "no_bindings",
        "bad_name",
        "applied",
        "approved_unrecorded",
        "proposed_recorded",
        "named_twice",
        "restates_a_row",
    ],
)
def test_a_malformed_amendment_is_refused(tmp_path, monkeypatch, mutate):
    delta = _f_delta()
    mutate(delta)
    _use(tmp_path, monkeypatch, delta)
    with pytest.raises(migration.MigrationRefusal, match="delta_invalid"):
        migration.iam_e_targets()


def test_an_amendment_row_meets_every_rule_a_root_row_meets(cloud, capsys, tmp_path, monkeypatch):
    delta = _f_delta()
    delta["amendments"][0]["bindings"][3]["role"] = "roles/run.developer"
    _use(tmp_path, monkeypatch, delta)
    code, error = _run(capsys, "iam-e-plan")
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_role_refused"}


# Today's staging state, then f's approval, then one apply


def test_on_today_s_staging_the_plan_shows_e_s_six_and_f_s_four_and_the_apply_refuses(
    cloud, capsys, tmp_path, monkeypatch
):
    _today(cloud, capsys, tmp_path, monkeypatch)
    partial = cloud.grants()
    digest = _use(tmp_path, monkeypatch, _f_delta())
    code, plan = _run(capsys, "iam-e-plan")
    assert code == 0, plan
    assert plan["delta_sha256"] == digest == F_PROPOSED_SHA256
    assert plan["approval_state"] == "proposed"
    assert plan["apply_ready"] is False
    assert plan["resource_missing"] == [FUNDED_RESOURCE]
    assert plan["routine_missing"] == []
    bindings = [row for row in plan["rows"] if row["kind"] not in {"authorization", "custom_role"}]
    assert len(bindings) == 39
    for row in bindings:
        key = (row["member"], row["resource"], row["role"])
        if key in E_PENDING | F_ROWS:
            expected = "resource_missing" if key[1] == FUNDED_RESOURCE else "missing"
        else:
            expected = "present"
        assert row["state"] == expected, row
    assert plan["counts"]["table"] == _counts(6, present=3, missing=3)
    assert plan["counts"]["runtime_job"] == _counts(3, present=1, resource_missing=2)
    assert plan["counts"]["project"] == _counts(5, missing=5)
    assert {
        row["state"] for row in plan["rows"] if row["kind"] in {"authorization", "custom_role"}
    } == {"present"}
    # While f is proposed the apply refuses before it reads anything, on either digest.
    for word in (digest, E_STAMPED_SHA256):
        cloud.reads.clear()
        code, error = _run(capsys, "iam-e-apply", word)
        assert code == 1
        expected = "delta_unapproved" if word == digest else "delta_digest_mismatch"
        assert error == {"error": f"execution_approval_iam_e_{expected}"}
        assert cloud.reads == []
    assert cloud.writes == []
    assert cloud.grants() == partial


def test_once_f_is_stamped_and_the_job_is_deployed_one_apply_writes_exactly_e_s_six_and_f_s_four(
    cloud, capsys, tmp_path, monkeypatch
):
    _today(cloud, capsys, tmp_path, monkeypatch)
    digest = _use(tmp_path, monkeypatch, _f_delta(F_APPROVED))
    assert digest == F_STAMPED_SHA256
    code, plan = _run(capsys, "iam-e-plan")
    assert code == 0, plan
    assert plan["delta_sha256"] == F_STAMPED_SHA256
    assert plan["approval_state"] == "approved"
    # The digest Albert approved is the proposed file's; the apply binds to the
    # stamped bytes and refuses the proposed digest before it reads anything.
    cloud.reads.clear()
    code, error = _run(capsys, "iam-e-apply", F_PROPOSED_SHA256)
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_delta_digest_mismatch"}
    assert cloud.reads == []
    # While the job is missing the approved apply still refuses and sends nothing.
    code, error = _run(capsys, "iam-e-apply", digest)
    assert code == 1
    assert error["error"] == "execution_approval_iam_e_resource_missing"
    assert error["resource_missing"] == [FUNDED_RESOURCE]
    assert cloud.writes == []
    _deploy_funded_job(cloud)
    before = cloud.grants()
    code, receipt = _run(capsys, "iam-e-apply", digest)
    assert code == 0, receipt
    assert receipt["approval_state"] == "approved"
    sent = [(verb, url) for verb, url, _body in cloud.writes]
    assert sent == [
        *(
            (
                "post",
                f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}/datasets/{key}:setIamPolicy",
            )
            for key in sorted(TABLE_KEYS)
        ),
        ("post", RUN_URL + FUNDED_JOB + ":setIamPolicy"),
        ("post", PROJECT_URL + ":setIamPolicy"),
    ]
    (funded_body,) = [body for _verb, url, body in cloud.writes if FUNDED_JOB in url]
    assert funded_body == {
        "policy": {
            "etag": "funded-deployed",
            "version": 1,
            "bindings": [
                {"role": "roles/run.developer", "members": [DEPLOY]},
                {"role": "roles/run.viewer", "members": sorted([LISTENING, ORCHESTRATION])},
            ],
        }
    }
    added = cloud.grants() - before
    assert before <= cloud.grants()
    assert added == {
        *((f"table:{key}", "roles/bigquery.dataViewer", ORCHESTRATION, "") for key in TABLE_KEYS),
        (f"job:{FUNDED_JOB}", "roles/run.viewer", LISTENING, ""),
        (f"job:{FUNDED_JOB}", "roles/run.viewer", ORCHESTRATION, ""),
        *(("project", row["role"], row["member"], "") for row in PROJECT_ROWS),
    }
    assert {row["role"] for row in PROJECT_ROWS} >= {BUILDS_VIEWER}
    assert receipt["writes_planned"] == _planned(
        table_policy_sets=3, run_job_policy_sets=1, project_policy_sets=1
    )
    assert receipt["counts"]["table"] == {
        "target": 6,
        "present_before": 3,
        "added": 3,
        "present_after": 6,
    }
    assert receipt["counts"]["runtime_job"] == {
        "target": 3,
        "present_before": 1,
        "added": 2,
        "present_after": 3,
    }
    assert receipt["counts"]["project"] == {
        "target": 5,
        "present_before": 0,
        "added": 5,
        "present_after": 5,
    }
    for kind, value in receipt["counts"].items():
        if kind not in {"table", "runtime_job", "project"}:
            assert value["added"] == 0, kind
            assert value["present_before"] == value["target"], kind
    cloud.writes.clear()
    code, again = _run(capsys, "iam-e-apply", digest)
    assert code == 0
    assert again["writes_planned"] == _planned()
    assert cloud.writes == []
