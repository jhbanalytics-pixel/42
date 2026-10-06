"""Amendment g through the iam-e words: the one final apply.

Amendment g is the one final amendment, proposed after amendment f's stamp. The one apply
that follows Albert's single go writes every approved row still missing on staging and
nothing else: amendment e's six pending rows, amendment f's four, amendment d's rows 54,
56 and 57 (the funded account's run.viewer on its own pilot job, the ingest account's
dataEditor on intelligence_42_sources_staging and its bigquery.jobUser on the project,
all approved in amendment d but written by no tool until now) and amendment g's seven.
Project jobUser is admitted for the ingest account alone. Amendment d's other rows on the
pilot job, the deploy account's run.developer and the daily account's run.invoker, stay
with no applier: the owner deploys and runs that job.

G's rows bind two live custom roles the serving identity already holds,
QuestionVertexPredict and QuestionControlReplace, with a later time bound, and bind
QuestionVertexPredict to the daily account under the same bound for the daily compose's
model calls; the words bind them and never define them. The staging state is the one the amendment e apply
left on 24 September, with the funded pilot job deployed and the artifact bucket and
both retained 30 September bindings in place.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest
from scripts.migrations import create_open_intelligence_execution_approval_store as migration

from tests.unit.test_iam_amendment_e_apply import (
    ALBERT,
    BRAIN,
    CACHE_BUCKET,
    DEPLOY,
    ENGINE,
    FUNDED_JOB,
    LISTENING,
    ORCHESTRATION,
    PROJECT_ROWS,
    PROJECT_URL,
    RUN_URL,
    SOURCES_URL,
    STORAGE_URL,
    _added,
    _planned,
)
from tests.unit.test_iam_amendment_f_apply import (
    E_PENDING,
    F_APPROVED,
    F_ROWS,
    F_STAMPED_SHA256,
    PROPOSED,
    _canonical,
    _deploy_funded_job,
    _f_delta,
    _run,
    _today,
    _use,
)

# The fake cloud fixture amendment f's apply tests use.
from tests.unit.test_iam_amendment_f_apply import cloud as cloud

G_FIXTURE = ENGINE / "tests" / "fixtures" / "iam_delta_amendment_g.json"
# ops/deploy/iam_delta_v1.json with amendment g proposed; tests/repository holds the root
# file equal to what this module builds.
# Amendment g's seventh row, the daily account's Vertex role, moved it from 812d0b06.
G_PROPOSED_SHA256 = "edbb789d269451fa41f5b30826e55c575cb0f2236b768bca4cfd5d362e048335"
G_STAMP = {
    "applied": False,
    "approved_at": "2026-09-25T09:00:00+00:00",
    "approved_by": "Albert Meintjes, test stamp over amendment g",
    "state": "approved",
}
PROJECT = "ogilvy-trends-v2"
SA = f"@{PROJECT}.iam.gserviceaccount.com"
FUNDED = f"serviceAccount:intelligence-42-funded{SA}"
PROJECT_RESOURCE = f"//cloudresourcemanager.googleapis.com/projects/{PROJECT}"
FUNDED_RESOURCE = f"//run.googleapis.com/projects/{PROJECT}/locations/us-central1/jobs/{FUNDED_JOB}"
ARTIFACT_BUCKET = "ogilvy-trends-v2-oi-source-artifacts-staging"
ARTIFACTS = f"//storage.googleapis.com/projects/_/buckets/{ARTIFACT_BUCKET}"
CACHE = f"//storage.googleapis.com/projects/_/buckets/{CACHE_BUCKET}"
VERTEX_ID = "QuestionVertexPredict"
REPLACE_ID = "QuestionControlReplace"
VERTEX = f"projects/{PROJECT}/roles/{VERTEX_ID}"
REPLACE = f"projects/{PROJECT}/roles/{REPLACE_ID}"
OBJECT = "resource.type == 'storage.googleapis.com/Object'"
LEDGER = (
    f"projects/_/buckets/{CACHE_BUCKET}/objects/open-intelligence/v2/staging/"
    "general-questions/allowances/general_cultural_question_staging_eval_v1/ledger.json"
)
UNTIL = "request.time < timestamp('2026-12-31T23:59:59Z')"
SEPTEMBER = "request.time < timestamp('2026-09-30T23:59:59Z')"
ARTIFACT_OBJECTS = f"projects/_/buckets/{ARTIFACT_BUCKET}/objects/"
READ = (
    f"{OBJECT} && (resource.name.startsWith('{ARTIFACT_OBJECTS}inputs/')"
    f" || resource.name.startsWith('{ARTIFACT_OBJECTS}captures/'))"
)
CREATE = f"{OBJECT} && resource.name.startsWith('{ARTIFACT_OBJECTS}captures/')"
BUCKET_ONLY = (
    f"resource.type == 'storage.googleapis.com/Bucket' && resource.name == "
    f"'projects/_/buckets/{ARTIFACT_BUCKET}'"
)
# (member, resource, role, condition) for amendment g's seven rows.
G_ROWS = {
    (LISTENING, PROJECT_RESOURCE, VERTEX, UNTIL),
    (LISTENING, CACHE, REPLACE, f"{OBJECT} && resource.name == '{LEDGER}' && {UNTIL}"),
    (ORCHESTRATION, ARTIFACTS, "roles/storage.bucketViewer", BUCKET_ONLY),
    (ORCHESTRATION, ARTIFACTS, "roles/storage.objectViewer", READ),
    (ORCHESTRATION, ARTIFACTS, "roles/storage.objectCreator", CREATE),
    (LISTENING, ARTIFACTS, "roles/storage.objectViewer", READ),
    (ORCHESTRATION, PROJECT_RESOURCE, VERTEX, UNTIL),
}
# Amendment d's rows 54, 56 and 57, approved and until now written by no tool.
ROW_54 = (FUNDED, FUNDED_RESOURCE, "roles/run.viewer", None)
INGEST_EMAIL = f"intelligence-42-ingest{SA}"
INGEST = f"serviceAccount:{INGEST_EMAIL}"
SOURCES = f"//bigquery.googleapis.com/projects/{PROJECT}/datasets/intelligence_42_sources_staging"
ROW_56 = (INGEST, SOURCES, "roles/bigquery.dataEditor", None)
JOB_USER = "roles/bigquery.jobUser"
ROW_57 = (INGEST, PROJECT_RESOURCE, JOB_USER, None)
D_ROWS = {ROW_54, ROW_56, ROW_57}
# Amendment d's other rows on the pilot job stay with no applier.
UNSERVED_D_ROWS = {
    (DEPLOY, FUNDED_RESOURCE, "roles/run.developer", None),
    (ORCHESTRATION, FUNDED_RESOURCE, "roles/run.invoker", None),
}


def _g_delta(g_approval=PROPOSED) -> dict:
    delta = _f_delta(F_APPROVED)
    delta["amendments"].extend(json.loads(G_FIXTURE.read_text(encoding="utf-8")))
    delta["amendments"][1]["approval"] = deepcopy(g_approval)
    return delta


def _key(row) -> tuple:
    return (row.member, row.resource, row.role, row.condition)


def _staging_today(cloud) -> None:
    """The live pieces amendment g relies on: its two roles, the retained 30 September
    bindings and the artifact bucket; and the source dataset without row 56's grant."""

    cloud.add_role(VERTEX_ID, ["aiplatform.endpoints.predict"])
    cloud.add_role(REPLACE_ID, ["storage.objects.create", "storage.objects.delete"])
    cloud.project["bindings"].append(
        {
            "role": VERTEX,
            "members": [LISTENING],
            "condition": {"title": "until september", "expression": SEPTEMBER},
        }
    )
    cloud.buckets[CACHE_BUCKET]["bindings"].append(
        {
            "role": REPLACE,
            "members": [LISTENING],
            "condition": {
                "title": "ledger until september",
                "expression": f"{OBJECT} && resource.name == '{LEDGER}' && {SEPTEMBER}",
            },
        }
    )
    cloud.buckets[CACHE_BUCKET]["version"] = 3
    access = cloud.datasets[SOURCES_URL]["access"]
    access[:] = [entry for entry in access if entry.get("userByEmail") != INGEST_EMAIL]
    cloud.buckets[ARTIFACT_BUCKET] = {
        "etag": "artifacts-etag",
        "version": 1,
        "bindings": [{"role": "roles/storage.admin", "members": [ALBERT]}],
    }


# The file


def test_the_delta_built_here_is_the_f_stamped_delta_with_amendment_g_proposed():
    raw = _canonical(_g_delta())
    assert hashlib.sha256(raw).hexdigest() == G_PROPOSED_SHA256
    without_g = json.loads(raw)
    without_g["amendments"].pop()
    assert hashlib.sha256(_canonical(without_g)).hexdigest() == F_STAMPED_SHA256


def test_the_targets_are_e_s_rows_rows_54_56_and_57_f_s_four_and_g_s_seven(tmp_path, monkeypatch):
    digest = _use(tmp_path, monkeypatch, _g_delta())
    targets = migration.iam_e_targets()
    assert targets.delta_sha256 == digest == G_PROPOSED_SHA256
    assert targets.approval_state == "proposed"
    rows = [_key(row) for row in targets.bindings]
    e_rows = {
        (row["member"], row["resource"], row["role"], row["condition"])
        for row in _added("bindings")
    }
    f_rows = {(*key, None) for key in F_ROWS}
    assert len(rows) == len(set(rows)) == len(e_rows) + 3 + 4 + 7
    assert set(rows) == e_rows | D_ROWS | f_rows | G_ROWS
    assert not set(rows) & UNSERVED_D_ROWS
    # The two custom roles g binds are live roles, never defined by the words.
    assert {role.role for role in targets.custom_roles}.isdisjoint({VERTEX, REPLACE})
    kinds = {(row.kind, row.role) for row in targets.bindings if _key(row) in G_ROWS}
    assert kinds == {
        ("project", VERTEX),
        ("bucket", REPLACE),
        ("bucket", "roles/storage.bucketViewer"),
        ("bucket", "roles/storage.objectViewer"),
        ("bucket", "roles/storage.objectCreator"),
    }
    (row_57,) = [row for row in targets.bindings if _key(row) == ROW_57]
    assert (row_57.kind, row_57.role) == ("project", JOB_USER)


# The kinds, roles and conditions the words admit for g, and nothing near them


@pytest.mark.parametrize(
    ("kind", "role"),
    [("project", VERTEX), ("bucket", REPLACE), ("bucket", "roles/storage.bucketViewer")],
)
def test_the_words_bind_g_s_roles_without_defining_them(kind, role):
    assert migration._iam_e_role(kind, role, {}) == role


@pytest.mark.parametrize(
    ("kind", "role"),
    [
        ("bucket", VERTEX),
        ("runtime_job", VERTEX),
        ("project", REPLACE),
        ("table", REPLACE),
        ("project", "roles/storage.bucketViewer"),
        ("runtime_job", "roles/storage.bucketViewer"),
        ("routine", VERTEX),
        ("project", "roles/storage.objectViewer"),
        ("project", f"projects/{PROJECT}/roles/QuestionQuotaUse"),
    ],
)
def test_the_words_refuse_g_s_roles_on_any_other_kind(kind, role):
    with pytest.raises(migration.MigrationRefusal, match="role_refused"):
        migration._iam_e_role(kind, role, {})


# Row 57: project jobUser for the ingest account, and for no one else


def test_project_job_user_is_admitted_for_the_ingest_account_alone():
    assert migration._iam_e_role("project", JOB_USER, {}, member=INGEST) == JOB_USER
    # The project kind's own roles are unchanged: the builds viewer, and nothing else.
    assert migration._IAM_E_KIND_ROLES["project"] == frozenset({"roles/cloudbuild.builds.viewer"})
    member_roles = migration._IAM_E_MEMBER_ROLES
    assert member_roles == {"project": {JOB_USER: frozenset({INGEST})}}


@pytest.mark.parametrize(
    "member",
    [
        None,
        LISTENING,
        ORCHESTRATION,
        DEPLOY,
        FUNDED,
        ALBERT,
        f"serviceAccount:intelligence-42-brain{SA}",
        f"serviceAccount:intelligence-42-daily{SA}",
        INGEST_EMAIL,
        f"{INGEST} ",
        INGEST.upper(),
        "serviceAccount:intelligence-42-ingest@another-project.iam.gserviceaccount.com",
        f"group:intelligence-42-ingest{SA}",
    ],
)
def test_project_job_user_is_refused_for_any_other_member(member):
    # None stands for a call that names no member at all.
    named = {} if member is None else {"member": member}
    with pytest.raises(migration.MigrationRefusal, match="role_refused"):
        migration._iam_e_role("project", JOB_USER, {}, **named)


@pytest.mark.parametrize("kind", ["routine", "dataset", "table", "bucket", "runtime_job"])
def test_the_ingest_account_s_job_user_is_admitted_on_no_other_kind(kind):
    with pytest.raises(migration.MigrationRefusal, match="role_refused"):
        migration._iam_e_role(kind, JOB_USER, {}, member=INGEST)


@pytest.mark.parametrize(
    "role",
    [
        "roles/bigquery.dataEditor",
        "roles/bigquery.dataViewer",
        "roles/bigquery.dataOwner",
        "roles/bigquery.admin",
        "roles/bigquery.user",
        "roles/bigquery.jobuser",
        "roles/bigquery.jobUser ",
        "roles/bigquery.readSessionUser",
        "roles/bigquery.resourceViewer",
        "roles/run.viewer",
        "roles/storage.objectViewer",
        "roles/iam.serviceAccountUser",
        "roles/editor",
        "roles/owner",
        f"projects/{PROJECT}/roles/QuestionQuotaUse",
    ],
)
def test_every_other_project_role_is_still_refused_even_for_the_ingest_account(role):
    for member in (INGEST, LISTENING):
        with pytest.raises(migration.MigrationRefusal, match="role_refused"):
            migration._iam_e_role("project", role, {}, member=member)
    with pytest.raises(migration.MigrationRefusal, match="role_refused"):
        migration._iam_e_role("project", role, {})


def test_a_g_row_giving_project_job_user_to_another_member_is_refused(tmp_path, monkeypatch):
    # Members that hold no project jobUser row in the delta, and two that do, under the
    # 31 December bound so the grant is new.
    for member, condition in (
        (LISTENING, None),
        (DEPLOY, None),
        (ORCHESTRATION, UNTIL),
        (FUNDED, UNTIL),
    ):
        delta = _g_delta()
        delta["amendments"][1]["bindings"].append(
            {
                "condition": condition,
                "member": member,
                "purpose": "a project jobUser row for another member",
                "resource": PROJECT_RESOURCE,
                "role": JOB_USER,
            }
        )
        _use(tmp_path, monkeypatch, delta)
        with pytest.raises(migration.MigrationRefusal, match="role_refused"):
            migration.iam_e_targets()


def test_a_project_row_takes_only_the_31_december_bound():
    assert migration._iam_e_condition("project", PROJECT, UNTIL) == UNTIL
    assert migration._iam_e_condition("project", PROJECT, None) is None
    for condition in (
        SEPTEMBER,
        "request.time < timestamp('2027-12-31T23:59:59Z')",
        f"{UNTIL} ",
        f"{UNTIL} || true",
        "",
    ):
        with pytest.raises(migration.MigrationRefusal, match="condition_refused"):
            migration._iam_e_condition("project", PROJECT, condition)
    for kind, ident in (("runtime_job", FUNDED_JOB), ("table", "d/tables/t")):
        with pytest.raises(migration.MigrationRefusal, match="condition_refused"):
            migration._iam_e_condition(kind, ident, UNTIL)


def test_rows_54_56_and_57_are_served_only_by_the_final_apply(tmp_path, monkeypatch):
    # The file as f's stamp left it keeps its targets: rows 54, 56 and 57 join them with g.
    _use(tmp_path, monkeypatch, _f_delta(F_APPROVED))
    assert not D_ROWS & {_key(row) for row in migration.iam_e_targets().bindings}
    _use(tmp_path, monkeypatch, _g_delta())
    served = {_key(row) for row in migration.iam_e_targets().bindings}
    assert served >= D_ROWS


@pytest.mark.parametrize("wanted", sorted(D_ROWS, key=str))
def test_rows_54_56_and_57_are_served_only_as_amendment_d_approved_them(
    tmp_path, monkeypatch, wanted
):
    # Each is served only while the delta still carries it exactly as amendment d did.
    delta = _g_delta()
    (row,) = [
        item
        for item in delta["bindings"]
        if (item["member"], item["resource"], item["role"]) == wanted[:3]
    ]
    row["purpose"] = row["purpose"] + " changed"
    _use(tmp_path, monkeypatch, delta)
    with pytest.raises(migration.MigrationRefusal, match="delta_invalid"):
        migration.iam_e_targets()


def test_the_artifact_bucket_pin_is_the_bridge_manifest_row():
    raw = (ENGINE / "configs/open_intelligence/resource_manifest_bridge_v3.json").read_bytes()
    assert (
        hashlib.sha256(raw).hexdigest()
        == migration.REGISTRATION_DIGESTS["resource_manifest_bridge_v3.json"]
    )
    rows = [row for row in json.loads(raw)["resources"] if row["name"] == ARTIFACTS]
    assert rows == [{"actions": ["read", "write"], "name": ARTIFACTS}]
    pinned = migration._IAM_E_AMENDMENT_RESOURCES
    assert pinned == {"g": frozenset({ARTIFACTS})}
    # The reviewed amendment e manifest the delta binds does not carry it.
    reviewed = json.loads(migration.RESOURCE_MANIFEST_PATH.read_bytes())
    assert ARTIFACTS not in {row["name"] for row in reviewed["resources"]}


def test_the_artifact_bucket_opens_only_in_amendment_g(tmp_path, monkeypatch):
    delta = _g_delta()
    # Moved out of g into f, the same row names a resource f may not name.
    row = delta["amendments"][1]["bindings"].pop(3)
    delta["amendments"][0]["bindings"].append(row)
    _use(tmp_path, monkeypatch, delta)
    with pytest.raises(migration.MigrationRefusal, match="resource_refused"):
        migration.iam_e_targets()
    delta = _g_delta()
    # And moved into the main bindings.
    delta["bindings"].append(delta["amendments"][1]["bindings"].pop(4))
    _use(tmp_path, monkeypatch, delta)
    with pytest.raises(migration.MigrationRefusal, match="resource_refused"):
        migration.iam_e_targets()


# Today's staging state, then one apply


def test_the_plan_shows_e_s_six_f_s_four_rows_54_56_and_57_and_g_s_seven_and_nothing_else(
    cloud, capsys, tmp_path, monkeypatch
):
    _today(cloud, capsys, tmp_path, monkeypatch)
    _staging_today(cloud)
    _deploy_funded_job(cloud)
    before = cloud.grants()
    digest = _use(tmp_path, monkeypatch, _g_delta())
    code, plan = _run(capsys, "iam-e-plan")
    assert code == 0, plan
    assert plan["delta_sha256"] == digest == G_PROPOSED_SHA256
    assert plan["approval_state"] == "proposed"
    assert plan["resource_missing"] == []
    assert plan["routine_missing"] == []
    missing = {
        (row["member"], row["resource"], row["role"], row["condition"])
        for row in plan["rows"]
        if row["kind"] not in {"authorization", "custom_role"} and row["state"] != "present"
    }
    e_six = {(*key, None) for key in E_PENDING}
    assert len(e_six) == 6
    assert {row["state"] for row in plan["rows"] if row["state"] != "present"} == {"missing"}
    assert missing == e_six | {(*key, None) for key in F_ROWS} | D_ROWS | G_ROWS
    assert len(missing) == 20
    # While g is proposed the apply refuses before it reads anything.
    cloud.reads.clear()
    code, error = _run(capsys, "iam-e-apply", digest)
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_delta_unapproved"}
    assert cloud.reads == []
    assert cloud.writes == []
    assert cloud.grants() == before


def test_while_the_job_or_the_bucket_is_missing_the_plan_says_so(
    cloud, capsys, tmp_path, monkeypatch
):
    _today(cloud, capsys, tmp_path, monkeypatch)
    _staging_today(cloud)
    del cloud.buckets[ARTIFACT_BUCKET]
    _use(tmp_path, monkeypatch, _g_delta())
    code, plan = _run(capsys, "iam-e-plan")
    assert code == 0, plan
    assert plan["resource_missing"] == sorted([FUNDED_RESOURCE, ARTIFACTS])
    states = {
        (row["member"], row["resource"], row["role"], row["condition"]): row["state"]
        for row in plan["rows"]
        if row["kind"] not in {"authorization", "custom_role"}
    }
    assert states[ROW_54] == "resource_missing"
    assert states[ROW_56] == "missing"
    assert states[ROW_57] == "missing"
    for key in G_ROWS:
        expected = "resource_missing" if key[1] == ARTIFACTS else "missing"
        assert states[key] == expected, key


def test_once_g_is_stamped_one_apply_writes_exactly_the_twenty_rows(
    cloud, capsys, tmp_path, monkeypatch
):
    _today(cloud, capsys, tmp_path, monkeypatch)
    _staging_today(cloud)
    _deploy_funded_job(cloud)
    before = cloud.grants()
    digest = _use(tmp_path, monkeypatch, _g_delta(G_STAMP))
    code, receipt = _run(capsys, "iam-e-apply", digest)
    assert code == 0, receipt
    assert receipt["approval_state"] == "approved"
    added = cloud.grants() - before
    assert before <= cloud.grants()
    assert added == {
        *(
            (
                f"table:trends_v2_staging_approvals/tables/{resource.rsplit('/', 1)[1]}",
                role,
                member,
                "",
            )
            for member, resource, role in F_ROWS
            if "/tables/" in resource
        ),
        (f"job:{FUNDED_JOB}", "roles/run.viewer", LISTENING, ""),
        (f"job:{FUNDED_JOB}", "roles/run.viewer", ORCHESTRATION, ""),
        (f"job:{FUNDED_JOB}", "roles/run.viewer", FUNDED, ""),
        *(("project", row["role"], row["member"], "") for row in PROJECT_ROWS),
        ("project", VERTEX, LISTENING, UNTIL),
        ("project", VERTEX, ORCHESTRATION, UNTIL),
        ("project", JOB_USER, INGEST, ""),
        (
            f"bucket:{CACHE_BUCKET}",
            REPLACE,
            LISTENING,
            f"{OBJECT} && resource.name == '{LEDGER}' && {UNTIL}",
        ),
        (f"bucket:{ARTIFACT_BUCKET}", "roles/storage.bucketViewer", ORCHESTRATION, BUCKET_ONLY),
        (f"bucket:{ARTIFACT_BUCKET}", "roles/storage.objectViewer", ORCHESTRATION, READ),
        (f"bucket:{ARTIFACT_BUCKET}", "roles/storage.objectCreator", ORCHESTRATION, CREATE),
        (f"bucket:{ARTIFACT_BUCKET}", "roles/storage.objectViewer", LISTENING, READ),
        (
            f"dataset:{SOURCES_URL}",
            "",
            json.dumps({"role": "WRITER", "userByEmail": INGEST_EMAIL}, sort_keys=True),
            "",
        ),
    }
    assert len(added) == 20
    # The retained 30 September bindings are kept as they were, beside the new ones.
    assert ("project", VERTEX, LISTENING, SEPTEMBER) in cloud.grants()
    assert (
        f"bucket:{CACHE_BUCKET}",
        REPLACE,
        LISTENING,
        f"{OBJECT} && resource.name == '{LEDGER}' && {SEPTEMBER}",
    ) in cloud.grants()
    # No custom role was created or changed.
    assert not [
        url for _verb, url, _body in cloud.writes if "/roles" in url and "iam.googleapis" in url
    ]
    sent = [(verb, url) for verb, url, _body in cloud.writes]
    assert sorted(sent) == sorted(
        [
            *(
                (
                    "post",
                    f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}/datasets/"
                    f"trends_v2_staging_approvals/tables/{resource.rsplit('/', 1)[1]}:setIamPolicy",
                )
                for _member, resource, _role in F_ROWS
                if "/tables/" in resource
            ),
            ("patch", SOURCES_URL),
            ("put", STORAGE_URL + CACHE_BUCKET + "/iam"),
            ("put", STORAGE_URL + ARTIFACT_BUCKET + "/iam"),
            ("post", RUN_URL + FUNDED_JOB + ":setIamPolicy"),
            ("post", PROJECT_URL + ":setIamPolicy"),
        ]
    )
    assert receipt["writes_planned"] == _planned(
        dataset_access_patches=1,
        table_policy_sets=3,
        bucket_policy_sets=2,
        run_job_policy_sets=1,
        project_policy_sets=1,
    )
    cloud.writes.clear()
    code, again = _run(capsys, "iam-e-apply", digest)
    assert code == 0
    assert again["writes_planned"] == _planned()
    assert cloud.writes == []


def _project_job_users(cloud) -> list[dict]:
    return [item for item in cloud.project["bindings"] if item["role"] == JOB_USER]


def test_the_apply_writes_row_57_when_it_alone_is_missing(cloud, capsys, tmp_path, monkeypatch):
    _today(cloud, capsys, tmp_path, monkeypatch)
    _staging_today(cloud)
    _deploy_funded_job(cloud)
    digest = _use(tmp_path, monkeypatch, _g_delta(G_STAMP))
    code, receipt = _run(capsys, "iam-e-apply", digest)
    assert code == 0, receipt
    # Row 57 is lost again; the next apply writes it back, add only, and nothing else.
    for item in _project_job_users(cloud):
        item["members"] = [member for member in item["members"] if member != INGEST]
    cloud.project["bindings"] = [item for item in cloud.project["bindings"] if item["members"]]
    before = cloud.grants()
    cloud.writes.clear()
    code, receipt = _run(capsys, "iam-e-apply", digest)
    assert code == 0, receipt
    assert cloud.grants() - before == {("project", JOB_USER, INGEST, "")}
    assert before <= cloud.grants()
    assert [(verb, url) for verb, url, _body in cloud.writes] == [
        ("post", PROJECT_URL + ":setIamPolicy")
    ]
    assert receipt["writes_planned"] == _planned(project_policy_sets=1)
    # The one jobUser binding the fake started with keeps its member and its condition.
    held = sorted(
        (tuple(item["members"]), (item.get("condition") or {}).get("expression", ""))
        for item in _project_job_users(cloud)
    )
    assert held == [
        ((BRAIN,), "request.time < timestamp('2030-01-01T00:00:00Z')"),
        ((INGEST,), ""),
    ]


def test_the_apply_skips_row_57_when_it_is_live(cloud, capsys, tmp_path, monkeypatch):
    _today(cloud, capsys, tmp_path, monkeypatch)
    _staging_today(cloud)
    _deploy_funded_job(cloud)
    cloud.project["bindings"].append({"role": JOB_USER, "members": [INGEST]})
    _use(tmp_path, monkeypatch, _g_delta())
    code, plan = _run(capsys, "iam-e-plan")
    assert code == 0, plan
    (state,) = [
        row["state"]
        for row in plan["rows"]
        if row["kind"] == "project"
        and (row["member"], row["resource"], row["role"], row["condition"]) == ROW_57
    ]
    assert state == "present"
    before = cloud.grants()
    digest = _use(tmp_path, monkeypatch, _g_delta(G_STAMP))
    code, receipt = _run(capsys, "iam-e-apply", digest)
    assert code == 0, receipt
    added = cloud.grants() - before
    assert len(added) == 19
    assert ("project", JOB_USER, INGEST, "") not in added
    # The live binding is left as it was: ingest holds project jobUser exactly once.
    held = [member for item in _project_job_users(cloud) for member in item["members"]]
    assert held.count(INGEST) == 1
    (sent,) = [body for verb, url, body in cloud.writes if url == PROJECT_URL + ":setIamPolicy"]
    written = [
        member
        for item in sent["policy"]["bindings"]
        if item["role"] == JOB_USER
        for member in item["members"]
    ]
    assert written.count(INGEST) == 1
    cloud.writes.clear()
    code, again = _run(capsys, "iam-e-apply", digest)
    assert code == 0
    assert again["writes_planned"] == _planned()
    assert cloud.writes == []
