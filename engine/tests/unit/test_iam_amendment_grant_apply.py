"""The last amendment applied under Albert's grant: iam-e-apply-granted DIGEST GRANT_PATH.

A delta whose root block and every earlier amendment block are approved may carry one
last amendment still proposed. The stamped word iam-e-apply refuses that file as it
always has. The granted word applies it once, under a grant file the executor writes
byte for byte after Albert's go: the grant names the amendment, the digest of the
committed file with that amendment proposed, the resource manifest, the commit and an
eight hour window opened by the go. Every grant check runs before credentials load
and before anything is read; a write spends the grant through an exclusive claim file
beside it, and the receipt lands beside the claim. The pending amendment here is
amendment f over amendment e's stamp, built in a temporary repository, and the
staging state is the one the amendment e apply left on 24 September.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from scripts.migrations import create_open_intelligence_execution_approval_store as migration

from tests.unit.test_iam_amendment_e_apply import (
    FUNDED_JOB,
    LISTENING,
    ORCHESTRATION,
    PROJECT_ROWS,
    PROJECT_URL,
    RUN_URL,
    TABLE_KEYS,
    _Credentials,
    _FakeCloud,
    _planned,
)
from tests.unit.test_iam_amendment_f_apply import (
    E_STAMPED_SHA256,
    F_APPROVED,
    F_PROPOSED_SHA256,
    F_STAMPED_SHA256,
    PROPOSED,
    _canonical,
    _deploy_funded_job,
    _e_stamped,
    _f_delta,
    _run,
    _today,
)

PROJECT = "ogilvy-trends-v2"
SESSION = "session_TESTGRANT0001"
GO = "2026-09-25T09:00:00Z"
EXPIRES = "2026-09-25T17:00:00Z"
NOW = datetime(2026, 9, 25, 10, 0, 0, tzinfo=UTC)
MANIFEST_SHA256 = "33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09"
WORD = "iam-e-apply-granted"
# Git long options, spelled from their parts.
LONG = "-" * 2


@pytest.fixture
def cloud(monkeypatch):
    fake = _FakeCloud()
    fake.install_all_routines()
    fake.credential_loads = []

    def load():
        fake.credential_loads.append(1)
        return _Credentials()

    monkeypatch.setattr(migration, "_load_credentials", load)
    monkeypatch.setattr(migration, "_authorized_session", lambda _credentials: fake)
    return fake


class _Repo:
    """A checkout holding the delta at ops/deploy/iam_delta_v1.json."""

    def __init__(self, root: Path, monkeypatch):
        self.root = root
        root.mkdir()
        self.git("init", "-q")
        (root / "ops" / "deploy").mkdir(parents=True)
        (root / "engine" / "scripts" / "migrations").mkdir(parents=True)
        self.delta = root / "ops" / "deploy" / "iam_delta_v1.json"
        monkeypatch.setattr(migration, "IAM_E_REPO", root)
        monkeypatch.setattr(migration, "IAM_E_DELTA_PATH", self.delta)

    def git(self, *args: str) -> str:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            check=True,
            capture_output=True,
        )
        return completed.stdout.decode("utf-8").strip()

    def write(self, delta: dict) -> str:
        raw = _canonical(delta)
        self.delta.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def commit(self, delta: dict) -> str:
        self.write(delta)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "delta")
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def repo(cloud, capsys, tmp_path, monkeypatch):
    """Today's staging state, and a checkout at amendment f proposed."""

    _today(cloud, capsys, tmp_path, monkeypatch)
    checkout = _Repo(tmp_path / "repo", monkeypatch)
    checkout.head = checkout.commit(_f_delta())
    monkeypatch.setattr(migration, "_iam_e_now", lambda: NOW)
    return checkout


def _grant(repo: _Repo, **changes: str) -> dict:
    grant = {
        "amendment": "f",
        "approved_by": f"Albert Meintjes, go in {SESSION} at {GO}",
        "contract_version": "42_iam_amendment_grant_v1",
        "delta_sha256": F_PROPOSED_SHA256,
        "expires_at": EXPIRES,
        "granted_at": GO,
        "grantor": "Albert Meintjes",
        "head_commit": repo.head,
        "purpose": "iam_amendment_apply",
        "resource_manifest_sha256": MANIFEST_SHA256,
    }
    grant.update(changes)
    return grant


def _save(tmp_path, grant, name: str = "f.json") -> Path:
    folder = tmp_path / "grants"
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_bytes(grant if isinstance(grant, bytes) else _canonical(grant))
    return path


def _claims() -> Path:
    """The one claims folder, beside the checkout and never inside it."""

    return Path(migration.IAM_E_REPO).resolve().parent / "42-iam-grant-claims"


def _spent(path: Path, suffix: str) -> Path:
    digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "0" * 64
    return _claims() / f"{digest}{suffix}"


def _lock(amendment: str = "f", delta: str = F_PROPOSED_SHA256) -> Path:
    return _claims() / f"{amendment}.{delta}.lock.json"


def _claim(path: Path) -> Path:
    return _spent(path, ".claim.json")


def _receipt(path: Path) -> Path:
    return _spent(path, ".receipt.json")


def _marker(path: Path) -> Path:
    return _spent(path, ".spent.json")


def _refused(cloud, capsys, code: str, *words: str, exit_code: int = 1) -> dict:
    cloud.reads.clear()
    cloud.writes.clear()
    loads = len(cloud.credential_loads)
    status, error = _run(capsys, *words)
    assert status == exit_code, error
    assert error == {"error": f"execution_approval_iam_e_{code}"}
    assert cloud.reads == []
    assert cloud.writes == []
    assert len(cloud.credential_loads) == loads
    return error


E_AND_F_ADDED = {
    *((f"table:{key}", "roles/bigquery.dataViewer", ORCHESTRATION, "") for key in TABLE_KEYS),
    (f"job:{FUNDED_JOB}", "roles/run.viewer", LISTENING, ""),
    (f"job:{FUNDED_JOB}", "roles/run.viewer", ORCHESTRATION, ""),
    *(("project", row["role"], row["member"], "") for row in PROJECT_ROWS),
}


# 1. The stamped word is untouched


def test_without_a_grant_the_proposed_file_still_refuses_before_any_read(
    cloud, capsys, repo, tmp_path
):
    _deploy_funded_job(cloud)
    _refused(cloud, capsys, "delta_unapproved", "iam-e-apply", F_PROPOSED_SHA256)
    path = _save(tmp_path, _grant(repo))
    # The stamped word never takes a grant.
    _refused(
        cloud,
        capsys,
        "digest_required",
        "iam-e-apply",
        F_PROPOSED_SHA256,
        str(path),
        exit_code=2,
    )
    assert not _claim(path).exists()


# 2. A live grant applies the pending amendment once


def test_a_live_grant_applies_every_missing_row_once_and_is_spent(cloud, capsys, repo, tmp_path):
    _deploy_funded_job(cloud)
    path = _save(tmp_path, _grant(repo))
    before = cloud.grants()
    code, receipt = _run(capsys, WORD, F_PROPOSED_SHA256, str(path))
    assert code == 0, receipt
    assert receipt["mode"] == "apply"
    assert receipt["delta_sha256"] == F_PROPOSED_SHA256
    assert receipt["approval_state"] == "proposed"
    assert receipt["approval"] == {
        "path": "grant",
        "amendment": "f",
        "grant_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "head_commit": repo.head,
        "approved_by": f"Albert Meintjes, go in {SESSION} at {GO}",
    }
    # Amendment e's six pending rows and amendment f's four, and nothing else.
    assert cloud.grants() - before == E_AND_F_ADDED
    assert before <= cloud.grants()
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
    assert receipt["writes_planned"] == _planned(
        table_policy_sets=3, run_job_policy_sets=1, project_policy_sets=1
    )
    assert receipt["applied"]["failed_at"] is None
    claim = json.loads(_claim(path).read_bytes())
    assert claim == {
        "amendment": "f",
        "delta_sha256": F_PROPOSED_SHA256,
        "grant_sha256": receipt["approval"]["grant_sha256"],
        "planned_payload_sha256": receipt["planned_payload_sha256"],
    }
    assert _claim(path).read_bytes() == _canonical(claim)
    assert _receipt(path).read_bytes() == _canonical(receipt)
    # A spent grant never runs again.
    _refused(cloud, capsys, "grant_consumed", WORD, F_PROPOSED_SHA256, str(path))


def test_a_claim_or_a_receipt_alone_spends_the_grant(cloud, capsys, repo, tmp_path):
    _deploy_funded_job(cloud)
    path = _save(tmp_path, _grant(repo))
    _claims().mkdir()
    for spent in (_claim(path), _receipt(path)):
        spent.write_bytes(b"{}\n")
        _refused(cloud, capsys, "grant_consumed", WORD, F_PROPOSED_SHA256, str(path))
        spent.unlink()


# 3. A malformed grant is refused before any read


def _duplicate(grant: dict) -> bytes:
    text = _canonical(grant).decode("utf-8")
    return text.replace(
        '{\n  "amendment": "f",', '{\n  "amendment": "f",\n  "amendment": "f",', 1
    ).encode("utf-8")


MALFORMED = {
    "missing_key": lambda grant: _canonical({k: v for k, v in grant.items() if k != "purpose"}),
    "extra_key": lambda grant: _canonical({**grant, "note": "x"}),
    "duplicate_key": _duplicate,
    "not_canonical": lambda grant: json.dumps(grant, sort_keys=True).encode("utf-8") + b"\n",
    "no_trailing_newline": lambda grant: _canonical(grant)[:-1],
    "unsorted": lambda grant: (json.dumps(dict(reversed(grant.items())), indent=2) + "\n").encode(
        "utf-8"
    ),
    "not_utf8": lambda grant: b"\xff\xfe" + _canonical(grant),
    "too_large": lambda grant: _canonical(grant) + b" " * 65536,
    "not_an_object": lambda grant: _canonical([grant]),
    "not_a_string": lambda grant: _canonical({**grant, "amendment": 6}),
    "wrong_contract": lambda grant: _canonical({**grant, "contract_version": "42_iam_grant_v1"}),
    "wrong_purpose": lambda grant: _canonical({**grant, "purpose": "iam_delta_apply"}),
    "approved_by_free_text": lambda grant: _canonical(
        {**grant, "approved_by": "Albert Meintjes, go in the session chat"}
    ),
    "approved_by_short_session": lambda grant: _canonical(
        {**grant, "approved_by": f"Albert Meintjes, go in session_abc at {GO}"}
    ),
    "approved_by_trailing_text": lambda grant: _canonical(
        {**grant, "approved_by": f"Albert Meintjes, go in {SESSION} at {GO} and more"}
    ),
    "granted_at_not_the_go": lambda grant: _canonical(
        {**grant, "granted_at": "2026-09-25T09:00:01Z"}
    ),
    "non_z_granted_at": lambda grant: _canonical(
        {**grant, "granted_at": "2026-09-25T09:00:00+00:00"}
    ),
    "non_z_expires_at": lambda grant: _canonical(
        {**grant, "expires_at": "2026-09-25T17:00:00+00:00"}
    ),
    "fractional_expires_at": lambda grant: _canonical(
        {**grant, "expires_at": "2026-09-25T17:00:00.000Z"}
    ),
    "impossible_time": lambda grant: _canonical({**grant, "expires_at": "2026-09-25T25:00:00Z"}),
    "expires_before_granted": lambda grant: _canonical(
        {**grant, "expires_at": "2026-09-25T08:59:59Z"}
    ),
    "expires_at_granted": lambda grant: _canonical({**grant, "expires_at": GO}),
}


@pytest.mark.parametrize("mutate", list(MALFORMED.values()), ids=list(MALFORMED))
def test_a_malformed_grant_is_refused_before_any_read(cloud, capsys, repo, tmp_path, mutate):
    _deploy_funded_job(cloud)
    path = _save(tmp_path, mutate(_grant(repo)))
    _refused(cloud, capsys, "grant_invalid", WORD, F_PROPOSED_SHA256, str(path))
    assert not _claim(path).exists()
    assert not _receipt(path).exists()


def test_a_grant_file_that_does_not_exist_is_refused(cloud, capsys, repo, tmp_path):
    path = tmp_path / "grants" / "absent.json"
    _refused(cloud, capsys, "grant_invalid", WORD, F_PROPOSED_SHA256, str(path))


def test_a_grant_kept_under_ops_deploy_or_the_migrations_is_refused(cloud, capsys, repo):
    for folder in ("ops/deploy", "engine/scripts/migrations"):
        path = repo.root / folder / "f.json"
        path.write_bytes(_canonical(_grant(repo)))
        _refused(cloud, capsys, "grant_invalid", WORD, F_PROPOSED_SHA256, str(path))
        assert not _claim(path).exists()
        path.unlink()


# 4. Only Albert grants


@pytest.mark.parametrize(
    "grantor", ["Albert", "albert meintjes", "Albert Meintjes ", "Someone Else"]
)
def test_a_grant_from_anyone_but_albert_is_refused(cloud, capsys, repo, tmp_path, grantor):
    path = _save(tmp_path, _grant(repo, grantor=grantor))
    _refused(cloud, capsys, "grant_grantor_refused", WORD, F_PROPOSED_SHA256, str(path))


# 5. The window


@pytest.mark.parametrize(
    ("expires_at", "now", "code"),
    [
        ("2026-09-25T17:00:01Z", NOW, "grant_window_too_long"),
        ("2026-09-26T09:00:00Z", NOW, "grant_window_too_long"),
        (EXPIRES, datetime(2026, 9, 25, 8, 59, 59, tzinfo=UTC), "grant_not_yet_valid"),
        (EXPIRES, datetime(2026, 9, 25, 17, 0, 0, tzinfo=UTC), "grant_expired"),
        ("2026-09-25T09:30:00Z", datetime(2026, 9, 25, 9, 30, 0, tzinfo=UTC), "grant_expired"),
    ],
    ids=["one_second_long", "a_day", "before_the_go", "at_expiry", "short_window_expired"],
)
def test_a_grant_applies_only_inside_its_window_of_at_most_eight_hours(
    cloud, capsys, repo, tmp_path, monkeypatch, expires_at, now, code
):
    _deploy_funded_job(cloud)
    monkeypatch.setattr(migration, "_iam_e_now", lambda: now)
    path = _save(tmp_path, _grant(repo, expires_at=expires_at))
    _refused(cloud, capsys, code, WORD, F_PROPOSED_SHA256, str(path))


def test_a_grant_applies_from_the_go_second(cloud, capsys, repo, tmp_path, monkeypatch):
    _deploy_funded_job(cloud)
    monkeypatch.setattr(migration, "_iam_e_now", lambda: datetime(2026, 9, 25, 9, 0, 0, tzinfo=UTC))
    path = _save(tmp_path, _grant(repo))
    code, receipt = _run(capsys, WORD, F_PROPOSED_SHA256, str(path))
    assert code == 0, receipt


# 6. The grant binds the delta bytes


def test_the_grant_binds_the_delta_bytes_and_the_argument_binds_the_checkout(
    cloud, capsys, repo, tmp_path
):
    _deploy_funded_job(cloud)
    path = _save(tmp_path, _grant(repo, delta_sha256=E_STAMPED_SHA256))
    _refused(cloud, capsys, "grant_delta_mismatch", WORD, F_PROPOSED_SHA256, str(path))
    path = _save(tmp_path, _grant(repo), "f2.json")
    for digest in (F_STAMPED_SHA256, E_STAMPED_SHA256, "0" * 64):
        _refused(cloud, capsys, "delta_digest_mismatch", WORD, digest, str(path))
    assert not _claim(path).exists()


# 7. The grant names the pending amendment


@pytest.mark.parametrize("amendment", ["g", "e", "F"])
def test_a_grant_for_another_amendment_is_refused(cloud, capsys, repo, tmp_path, amendment):
    path = _save(tmp_path, _grant(repo, amendment=amendment))
    _refused(cloud, capsys, "grant_amendment_mismatch", WORD, F_PROPOSED_SHA256, str(path))


# 8 and 9. Only a proposed last amendment over approved blocks takes a grant


def test_a_fully_approved_file_needs_no_grant(cloud, capsys, repo, tmp_path):
    for delta, digest in (
        (_f_delta(F_APPROVED), F_STAMPED_SHA256),
        (_e_stamped(), E_STAMPED_SHA256),
    ):
        head = repo.commit(delta)
        path = _save(tmp_path, _grant(repo, head_commit=head, delta_sha256=digest))
        _refused(cloud, capsys, "grant_not_needed", WORD, digest, str(path))


def test_a_grant_over_an_unapproved_earlier_block_is_refused(cloud, capsys, repo, tmp_path):
    root_proposed = _f_delta()
    root_proposed["approval"] = dict(PROPOSED)
    alone = _e_stamped()
    alone["approval"] = dict(PROPOSED)
    for delta in (root_proposed, alone):
        head = repo.commit(delta)
        digest = hashlib.sha256(_canonical(delta)).hexdigest()
        path = _save(tmp_path, _grant(repo, head_commit=head, delta_sha256=digest))
        _refused(cloud, capsys, "grant_prior_unapproved", WORD, digest, str(path))


def test_the_targets_name_the_pending_amendment(repo):
    assert migration.iam_e_targets().pending_amendment == "f"
    repo.commit(_f_delta(F_APPROVED))
    assert migration.iam_e_targets().pending_amendment is None
    delta = _f_delta()
    delta["approval"] = dict(PROPOSED)
    repo.commit(delta)
    assert migration.iam_e_targets().pending_amendment is None


# 10. The checkout


def test_a_grant_for_another_commit_is_refused(cloud, capsys, repo, tmp_path):
    path = _save(tmp_path, _grant(repo, head_commit="0" * 40))
    _refused(cloud, capsys, "grant_head_mismatch", WORD, F_PROPOSED_SHA256, str(path))


def test_a_delta_that_is_not_the_committed_one_is_refused(cloud, capsys, repo, tmp_path):
    head = repo.commit(_e_stamped())
    repo.write(_f_delta())
    path = _save(tmp_path, _grant(repo, head_commit=head))
    _refused(cloud, capsys, "grant_delta_uncommitted", WORD, F_PROPOSED_SHA256, str(path))


@pytest.mark.parametrize(
    "dirty",
    [
        "engine/scripts/migrations/extra.py",
        "ops/deploy/extra.json",
        "engine/src/analysis/open_intelligence/extra.py",
    ],
)
def test_a_dirty_checkout_is_refused(cloud, capsys, repo, tmp_path, dirty):
    (repo.root / dirty).parent.mkdir(parents=True, exist_ok=True)
    path = _save(tmp_path, _grant(repo))
    (repo.root / dirty).write_text("x\n", encoding="utf-8")
    _refused(cloud, capsys, "grant_checkout_dirty", WORD, F_PROPOSED_SHA256, str(path))


def test_a_modified_tracked_file_makes_the_checkout_dirty(cloud, capsys, repo, tmp_path):
    tracked = repo.root / "engine" / "scripts" / "migrations" / "tool.py"
    tracked.write_text("x = 1\n", encoding="utf-8")
    head = repo.commit(_f_delta())
    tracked.write_text("x = 2\n", encoding="utf-8")
    path = _save(tmp_path, _grant(repo, head_commit=head))
    _refused(cloud, capsys, "grant_checkout_dirty", WORD, F_PROPOSED_SHA256, str(path))


def test_a_change_outside_the_guarded_folders_leaves_the_checkout_clean(
    cloud, capsys, repo, tmp_path
):
    _deploy_funded_job(cloud)
    (repo.root / "notes.txt").write_text("x\n", encoding="utf-8")
    path = _save(tmp_path, _grant(repo))
    code, receipt = _run(capsys, WORD, F_PROPOSED_SHA256, str(path))
    assert code == 0, receipt


def test_an_unreadable_head_is_refused(cloud, capsys, repo, tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    subprocess.run(["git", "-C", str(empty), "init", "-q"], check=True)
    monkeypatch.setattr(migration, "IAM_E_REPO", empty)
    path = _save(tmp_path, _grant(repo))
    _refused(cloud, capsys, "grant_head_unreadable", WORD, F_PROPOSED_SHA256, str(path))
    monkeypatch.setattr(migration, "IAM_E_REPO", tmp_path / "nowhere")
    _refused(cloud, capsys, "grant_head_unreadable", WORD, F_PROPOSED_SHA256, str(path))


def _clean_clone(repo: _Repo, tmp_path) -> Path:
    clone = tmp_path / "clean"
    subprocess.run(["git", "clone", "-q", str(repo.root), str(clone)], check=True)
    return clone


@pytest.mark.parametrize(
    "variables",
    [
        ("GIT_DIR", "GIT_WORK_TREE"),
        ("GIT_DIR",),
        ("GIT_WORK_TREE",),
    ],
    ids=["dir_and_work_tree", "dir", "work_tree"],
)
def test_git_variables_naming_a_clean_clone_never_hide_a_dirty_checkout(
    cloud, capsys, repo, tmp_path, monkeypatch, variables
):
    _deploy_funded_job(cloud)
    clone = _clean_clone(repo, tmp_path)
    (repo.root / "engine" / "scripts" / "migrations" / "extra.py").write_text(
        "x\n", encoding="utf-8"
    )
    path = _save(tmp_path, _grant(repo))
    values = {"GIT_DIR": str(clone / ".git"), "GIT_WORK_TREE": str(clone)}
    for name in variables:
        monkeypatch.setenv(name, values[name])
    monkeypatch.setenv("GIT_INDEX_FILE", str(clone / ".git" / "index"))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(repo.root.parent))
    _refused(cloud, capsys, "grant_checkout_dirty", WORD, F_PROPOSED_SHA256, str(path))
    assert not _claim(path).exists()


def test_git_variables_naming_another_commit_never_move_the_head(
    cloud, capsys, repo, tmp_path, monkeypatch
):
    _deploy_funded_job(cloud)
    clone = _clean_clone(repo, tmp_path)
    (repo.root / "notes.txt").write_text("x\n", encoding="utf-8")
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "notes")
    # The grant names the clone's head, which the checkout has moved past.
    path = _save(tmp_path, _grant(repo))
    monkeypatch.setenv("GIT_DIR", str(clone / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(clone))
    _refused(cloud, capsys, "grant_head_mismatch", WORD, F_PROPOSED_SHA256, str(path))


def test_git_variables_do_not_stop_a_clean_checkout_from_applying(
    cloud, capsys, repo, tmp_path, monkeypatch
):
    _deploy_funded_job(cloud)
    clone = _clean_clone(repo, tmp_path)
    # The clone is the dirty one here; the checkout the tool is pointed at is clean.
    (clone / "ops" / "deploy" / "extra.json").write_text("x\n", encoding="utf-8")
    path = _save(tmp_path, _grant(repo))
    monkeypatch.setenv("GIT_DIR", str(clone / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(clone))
    code, receipt = _run(capsys, WORD, F_PROPOSED_SHA256, str(path))
    assert code == 0, receipt


def test_a_folder_inside_a_checkout_is_not_the_checkout(cloud, capsys, repo, tmp_path, monkeypatch):
    path = _save(tmp_path, _grant(repo))
    monkeypatch.setattr(migration, "IAM_E_REPO", repo.root / "ops")
    _refused(cloud, capsys, "grant_head_unreadable", WORD, F_PROPOSED_SHA256, str(path))


def _hook(tmp_path) -> Path:
    """An fsmonitor hook that reports nothing ever changed."""

    hook = tmp_path / "fsmonitor.sh"
    hook.write_text("#!/bin/sh\nprintf 'token\\0'\n", encoding="utf-8")
    hook.chmod(0o755)
    return hook


def _tracked(repo: _Repo, relative: str) -> Path:
    path = repo.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1\n", encoding="utf-8")
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "tracked")
    repo.head = repo.git("rev-parse", "HEAD")
    return path


def test_a_replaced_delta_blob_is_not_taken_for_the_committed_one(cloud, capsys, repo, tmp_path):
    _deploy_funded_job(cloud)
    # HEAD commits amendment e's stamped file; a replace ref and a skip worktree bit
    # make the proposed file look committed and the working file look clean.
    repo.head = repo.commit(_e_stamped())
    committed = repo.git("rev-parse", "HEAD:ops/deploy/iam_delta_v1.json")
    repo.write(_f_delta())
    proposed = repo.git("hash-object", "-w", "ops/deploy/iam_delta_v1.json")
    repo.git("replace", committed, proposed)
    path = _save(tmp_path, _grant(repo))
    _refused(cloud, capsys, "grant_delta_uncommitted", WORD, F_PROPOSED_SHA256, str(path))
    repo.git("update-index", "-q", f"{LONG}skip-worktree", "ops/deploy/iam_delta_v1.json")
    _refused(cloud, capsys, "grant_delta_uncommitted", WORD, F_PROPOSED_SHA256, str(path))
    assert not _claim(path).exists()


@pytest.mark.parametrize("bit", ["skip-worktree", "assume-unchanged"])
@pytest.mark.parametrize(
    "relative",
    [
        "engine/scripts/migrations/tool.py",
        "ops/deploy/other.json",
        "engine/src/analysis/open_intelligence/execution_approval.py",
    ],
)
def test_an_index_bit_that_hides_a_guarded_file_makes_the_checkout_dirty(
    cloud, capsys, repo, tmp_path, bit, relative
):
    _deploy_funded_job(cloud)
    tracked = _tracked(repo, relative)
    repo.git("update-index", LONG + bit, relative)
    path = _save(tmp_path, _grant(repo))
    # The bit alone refuses, before the file changes and after.
    _refused(cloud, capsys, "grant_checkout_dirty", WORD, F_PROPOSED_SHA256, str(path))
    tracked.write_text("2\n", encoding="utf-8")
    _refused(cloud, capsys, "grant_checkout_dirty", WORD, F_PROPOSED_SHA256, str(path))


def test_an_index_bit_outside_the_guarded_folders_is_not_refused(cloud, capsys, repo, tmp_path):
    _deploy_funded_job(cloud)
    _tracked(repo, "notes.txt")
    repo.git("update-index", f"{LONG}skip-worktree", "notes.txt")
    path = _save(tmp_path, _grant(repo))
    code, receipt = _run(capsys, WORD, F_PROPOSED_SHA256, str(path))
    assert code == 0, receipt


@pytest.mark.parametrize("scope", ["global", "local"])
def test_an_fsmonitor_hook_never_hides_a_modified_guarded_file(
    cloud, capsys, repo, tmp_path, monkeypatch, scope
):
    _deploy_funded_job(cloud)
    tracked = _tracked(repo, "ops/deploy/other.json")
    hook = _hook(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    if scope == "global":
        (home / ".gitconfig").write_text(
            f"[core]\n\tfsmonitor = {hook}\n\tfsmonitorHookVersion = 2\n", encoding="utf-8"
        )
    else:
        repo.git("config", "core.fsmonitor", str(hook))
        repo.git("config", "core.fsmonitorHookVersion", "2")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    subprocess.run(
        ["git", "-C", str(repo.root), "status"],
        check=True,
        capture_output=True,
        env={**os.environ, "HOME": str(home)},
    )
    tracked.write_text("2\n", encoding="utf-8")
    path = _save(tmp_path, _grant(repo))
    _refused(cloud, capsys, "grant_checkout_dirty", WORD, F_PROPOSED_SHA256, str(path))


def test_a_global_config_worktree_never_hides_a_dirty_checkout(
    cloud, capsys, repo, tmp_path, monkeypatch
):
    _deploy_funded_job(cloud)
    clone = _clean_clone(repo, tmp_path)
    (repo.root / "ops" / "deploy" / "junk.json").write_text("{}\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(f"[core]\n\tworktree = {clone}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    path = _save(tmp_path, _grant(repo))
    _refused(cloud, capsys, "grant_checkout_dirty", WORD, F_PROPOSED_SHA256, str(path))


# 11. The manifest


def test_a_grant_naming_another_manifest_is_refused(cloud, capsys, repo, tmp_path):
    path = _save(tmp_path, _grant(repo, resource_manifest_sha256="0" * 64))
    _refused(cloud, capsys, "grant_manifest_mismatch", WORD, F_PROPOSED_SHA256, str(path))


# 12. A missing resource leaves the grant unspent


def test_a_missing_resource_leaves_the_grant_unspent_until_the_resource_exists(
    cloud, capsys, repo, tmp_path
):
    path = _save(tmp_path, _grant(repo))
    code, error = _run(capsys, WORD, F_PROPOSED_SHA256, str(path))
    assert code == 1
    assert error["error"] == "execution_approval_iam_e_resource_missing"
    assert error["resource_missing"] == [
        f"//run.googleapis.com/projects/{PROJECT}/locations/us-central1/jobs/{FUNDED_JOB}"
    ]
    assert cloud.writes == []
    assert not _claim(path).exists()
    assert not _receipt(path).exists()
    _deploy_funded_job(cloud)
    code, receipt = _run(capsys, WORD, F_PROPOSED_SHA256, str(path))
    assert code == 0, receipt
    assert _claim(path).exists()
    assert json.loads(_receipt(path).read_bytes()) == receipt


# 13. A refused write spends the grant


def test_a_refused_write_spends_the_grant_and_the_receipt_says_where_it_stopped(
    cloud, capsys, repo, tmp_path
):
    _deploy_funded_job(cloud)
    cloud.refuse_writes.add(PROJECT_URL + ":setIamPolicy")
    path = _save(tmp_path, _grant(repo))
    code, error = _run(capsys, WORD, F_PROPOSED_SHA256, str(path))
    assert code == 1
    assert error["error"] == "execution_approval_iam_e_write_refused"
    assert error["applied"]["failed_at"] == {"kind": "project", "resource": PROJECT}
    assert len(error["applied"]["writes"]) == 4
    assert error["approval"]["amendment"] == "f"
    assert _claim(path).exists()
    assert json.loads(_receipt(path).read_bytes()) == error
    assert _receipt(path).read_bytes() == _canonical(error)
    # A refused write needs a new go.
    cloud.refuse_writes.clear()
    _refused(cloud, capsys, "grant_consumed", WORD, F_PROPOSED_SHA256, str(path))


def test_a_failed_readback_spends_the_grant(cloud, capsys, repo, tmp_path):
    _deploy_funded_job(cloud)
    cloud.drop_writes.add(PROJECT_URL + ":setIamPolicy")
    path = _save(tmp_path, _grant(repo))
    code, error = _run(capsys, WORD, F_PROPOSED_SHA256, str(path))
    assert code == 1
    assert error["error"] == "execution_approval_iam_e_readback_mismatch"
    assert error["applied"]["failed_at"] == "readback"
    assert json.loads(_receipt(path).read_bytes()) == error
    _refused(cloud, capsys, "grant_consumed", WORD, F_PROPOSED_SHA256, str(path))


def test_a_receipt_that_cannot_be_written_is_refused_with_the_receipt(
    cloud, capsys, repo, tmp_path
):
    _deploy_funded_job(cloud)
    path = _save(tmp_path, _grant(repo))
    send = cloud.post

    def post(url, json=None, **kwargs):
        if url == PROJECT_URL + ":setIamPolicy":
            _receipt(path).write_bytes(b"{}\n")
        return send(url, json=json, **kwargs)

    cloud.post = post
    code = migration.main([WORD, F_PROPOSED_SHA256, str(path)])
    captured = capsys.readouterr()
    assert code == 1
    printed = json.loads(captured.out)
    error = json.loads(captured.err)
    assert error == {
        "error": "execution_approval_iam_e_grant_receipt_unwritten",
        "receipt": printed,
    }
    assert printed["applied"]["failed_at"] is None
    assert printed["approval"]["amendment"] == "f"


def _spend_with_a_refused_write(cloud, capsys, repo, tmp_path) -> Path:
    _deploy_funded_job(cloud)
    cloud.refuse_writes.add(PROJECT_URL + ":setIamPolicy")
    path = _save(tmp_path, _grant(repo))
    code, error = _run(capsys, WORD, F_PROPOSED_SHA256, str(path))
    assert code == 1
    assert error["error"] == "execution_approval_iam_e_write_refused"
    cloud.refuse_writes.clear()
    return path


@pytest.mark.parametrize("how", ["copy", "hard_link", "moved"])
def test_the_same_grant_bytes_under_another_path_stay_spent(cloud, capsys, repo, tmp_path, how):
    path = _spend_with_a_refused_write(cloud, capsys, repo, tmp_path)
    other = tmp_path / "elsewhere" / "again.json"
    other.parent.mkdir()
    if how == "copy":
        shutil.copyfile(path, other)
    elif how == "hard_link":
        os.link(path, other)
    else:
        path.rename(other)
    _refused(cloud, capsys, "grant_consumed", WORD, F_PROPOSED_SHA256, str(other))


def test_a_new_grant_for_a_claimed_amendment_and_delta_is_refused(
    cloud, capsys, repo, tmp_path, monkeypatch
):
    _spend_with_a_refused_write(cloud, capsys, repo, tmp_path)
    later = "2026-09-25T09:00:01Z"
    fresh = _grant(
        repo,
        approved_by=f"Albert Meintjes, go in {SESSION} at {later}",
        granted_at=later,
        expires_at="2026-09-25T17:00:01Z",
    )
    path = _save(tmp_path, fresh, "f-again.json")
    _refused(cloud, capsys, "grant_amendment_claimed", WORD, F_PROPOSED_SHA256, str(path))
    assert not _claim(path).exists()


def test_a_new_grant_for_an_applied_amendment_is_refused(cloud, capsys, repo, tmp_path):
    _deploy_funded_job(cloud)
    code, receipt = _run(capsys, WORD, F_PROPOSED_SHA256, str(_save(tmp_path, _grant(repo))))
    assert code == 0, receipt
    later = "2026-09-25T09:30:00Z"
    fresh = _grant(
        repo,
        approved_by=f"Albert Meintjes, go in {SESSION} at {later}",
        granted_at=later,
        expires_at="2026-09-25T17:30:00Z",
    )
    path = _save(tmp_path, fresh, "f-again.json")
    _refused(cloud, capsys, "grant_amendment_claimed", WORD, F_PROPOSED_SHA256, str(path))


def _earlier_claim(receipt: object) -> None:
    folder = _claims()
    folder.mkdir(exist_ok=True)
    claim = {
        "amendment": "f",
        "delta_sha256": F_PROPOSED_SHA256,
        "grant_sha256": "1" * 64,
        "planned_payload_sha256": "2" * 64,
    }
    (folder / ("1" * 64 + ".claim.json")).write_bytes(_canonical(claim))
    if receipt is not None:
        (folder / ("1" * 64 + ".receipt.json")).write_bytes(_canonical(receipt))


def test_an_earlier_claim_whose_receipt_shows_no_write_attempted_does_not_block(
    cloud, capsys, repo, tmp_path
):
    _deploy_funded_job(cloud)
    _earlier_claim({"applied": {"failed_at": None, "writes": []}})
    path = _save(tmp_path, _grant(repo))
    code, receipt = _run(capsys, WORD, F_PROPOSED_SHA256, str(path))
    assert code == 0, receipt


@pytest.mark.parametrize(
    "receipt",
    [
        None,
        {"applied": {"failed_at": {"kind": "table", "resource": "t"}, "writes": []}},
        {"applied": {"failed_at": None, "writes": [{"kind": "table", "resource": "t"}]}},
        {},
    ],
    ids=["no_receipt", "first_write_refused", "writes_landed", "no_applied"],
)
def test_an_earlier_claim_that_may_have_written_blocks_a_new_grant(
    cloud, capsys, repo, tmp_path, receipt
):
    _deploy_funded_job(cloud)
    _earlier_claim(receipt)
    path = _save(tmp_path, _grant(repo))
    _refused(cloud, capsys, "grant_amendment_claimed", WORD, F_PROPOSED_SHA256, str(path))


def test_a_claim_for_another_delta_does_not_block(cloud, capsys, repo, tmp_path):
    _deploy_funded_job(cloud)
    folder = _claims()
    folder.mkdir()
    claim = {
        "amendment": "f",
        "delta_sha256": "3" * 64,
        "grant_sha256": "1" * 64,
        "planned_payload_sha256": "2" * 64,
    }
    (folder / ("1" * 64 + ".claim.json")).write_bytes(_canonical(claim))
    path = _save(tmp_path, _grant(repo))
    code, receipt = _run(capsys, WORD, F_PROPOSED_SHA256, str(path))
    assert code == 0, receipt


def test_the_claims_folder_sits_beside_the_checkout(cloud, capsys, repo, tmp_path):
    _deploy_funded_job(cloud)
    path = _save(tmp_path, _grant(repo))
    code, receipt = _run(capsys, WORD, F_PROPOSED_SHA256, str(path))
    assert code == 0, receipt
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert sorted(item.name for item in _claims().iterdir()) == sorted(
        [
            f"{digest}.claim.json",
            f"{digest}.receipt.json",
            _lock().name,
            f"{digest}.spent.json",
        ]
    )
    assert json.loads(_lock().read_bytes()) == {
        "amendment": "f",
        "delta_sha256": F_PROPOSED_SHA256,
        "grant_sha256": digest,
    }
    assert _claims() == repo.root.parent / "42-iam-grant-claims"
    assert sorted(item.name for item in path.parent.iterdir()) == [path.name]


def _second_grant(repo: _Repo, tmp_path) -> Path:
    """Another valid grant from the same go: only its window differs."""

    return _save(tmp_path, _grant(repo, expires_at="2026-09-25T16:00:00Z"), "f-second.json")


def test_two_grants_from_one_go_run_at_once_write_only_once(
    cloud, capsys, repo, tmp_path, monkeypatch
):
    _deploy_funded_job(cloud)
    first = _save(tmp_path, _grant(repo))
    second = _second_grant(repo, tmp_path)
    loader = migration._load_credentials
    ran = []

    def load():
        # The second run passed every check before this point; the first runs to its
        # end now, with its last write refused, so rows are still missing.
        if not ran:
            ran.append(1)
            cloud.refuse_writes.add(PROJECT_URL + ":setIamPolicy")
            code, error = _run(capsys, WORD, F_PROPOSED_SHA256, str(first))
            assert code == 1
            assert error["error"] == "execution_approval_iam_e_write_refused"
            cloud.refuse_writes.clear()
            cloud.writes.clear()
        return loader()

    monkeypatch.setattr(migration, "_load_credentials", load)
    code, error = _run(capsys, WORD, F_PROPOSED_SHA256, str(second))
    assert code == 1
    assert error["error"] == "execution_approval_iam_e_grant_amendment_claimed"
    assert error["applied"] == {"failed_at": None, "writes": []}
    assert cloud.writes == []
    assert ran == [1]
    # The second grant is spent by its own claim, and its receipt says it wrote nothing.
    assert _claim(second).exists()
    assert json.loads(_receipt(second).read_bytes()) == error
    assert (
        json.loads(_lock().read_bytes())["grant_sha256"]
        == hashlib.sha256(first.read_bytes()).hexdigest()
    )


def _lock_for(grant_sha256: str, receipt: object) -> None:
    folder = _claims()
    folder.mkdir(exist_ok=True)
    _lock().write_bytes(
        _canonical(
            {"amendment": "f", "delta_sha256": F_PROPOSED_SHA256, "grant_sha256": grant_sha256}
        )
    )
    if receipt is not None:
        (folder / f"{grant_sha256}.receipt.json").write_bytes(_canonical(receipt))


@pytest.mark.parametrize(
    "receipt",
    [None, {"applied": {"failed_at": None, "writes": [{"kind": "table", "resource": "t"}]}}],
    ids=["no_receipt", "writes_landed"],
)
def test_a_lock_for_the_amendment_and_delta_blocks_a_new_grant_before_any_read(
    cloud, capsys, repo, tmp_path, receipt
):
    _deploy_funded_job(cloud)
    _lock_for("4" * 64, receipt)
    path = _save(tmp_path, _grant(repo))
    _refused(cloud, capsys, "grant_amendment_claimed", WORD, F_PROPOSED_SHA256, str(path))


def test_a_lock_blocks_whatever_its_grant_s_receipt_says(cloud, capsys, repo, tmp_path):
    _deploy_funded_job(cloud)
    _lock_for("4" * 64, {"applied": {"failed_at": None, "writes": []}})
    path = _save(tmp_path, _grant(repo))
    _refused(cloud, capsys, "grant_amendment_claimed", WORD, F_PROPOSED_SHA256, str(path))
    # The tool never removes a lock.
    assert _lock().exists()


def _new_go(repo: _Repo, tmp_path) -> Path:
    later = "2026-09-25T09:40:00Z"
    fresh = _grant(
        repo,
        approved_by=f"Albert Meintjes, go in {SESSION} at {later}",
        granted_at=later,
        expires_at="2026-09-25T17:40:00Z",
    )
    return _save(tmp_path, fresh, "f-new-go.json")


def _record(repo: _Repo, path: Path, items) -> None:
    """Section 4: move a spent grant's files into the record under the checkout."""

    record = repo.root / "docs" / "operations" / "iam-grants"
    record.mkdir(parents=True, exist_ok=True)
    short = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    for item in items:
        kind = item.name.rsplit(".", 2)[-2]
        item.rename(record / f"f.{short}.{kind}.json")


def test_after_a_refused_write_moving_claim_receipt_and_lock_lets_a_new_go_apply(
    cloud, capsys, repo, tmp_path
):
    path = _spend_with_a_refused_write(cloud, capsys, repo, tmp_path)
    spent = [_claim(path), _receipt(path), _lock()]
    assert all(item.exists() for item in spent)
    # While its claim stands, the spent grant and any copy of it stay consumed.
    copy = tmp_path / "copy.json"
    shutil.copyfile(path, copy)
    for grant in (path, copy):
        _refused(cloud, capsys, "grant_consumed", WORD, F_PROPOSED_SHA256, str(grant))
    fresh = _new_go(repo, tmp_path)
    _refused(cloud, capsys, "grant_amendment_claimed", WORD, F_PROPOSED_SHA256, str(fresh))
    # Section 4: with Albert's new go the executor moves all three into the record.
    _record(repo, path, spent)
    # The spent marker is never moved.
    assert list(_claims().iterdir()) == [_marker(path)]
    # Between the move and the new go's run the old grant is still spent, by its record.
    for grant in (path, copy):
        _refused(cloud, capsys, "grant_consumed", WORD, F_PROPOSED_SHA256, str(grant))
    code, receipt = _run(capsys, WORD, F_PROPOSED_SHA256, str(fresh))
    assert code == 0, receipt
    assert [item for item in cloud.writes if item[1] == PROJECT_URL + ":setIamPolicy"]
    assert receipt["approval"]["grant_sha256"] == hashlib.sha256(fresh.read_bytes()).hexdigest()
    # And after it.
    _refused(cloud, capsys, "grant_consumed", WORD, F_PROPOSED_SHA256, str(path))


@pytest.mark.parametrize("left", ["claim", "receipt", "lock"])
def test_moving_only_part_of_a_spent_grant_s_files_still_blocks_a_new_go(
    cloud, capsys, repo, tmp_path, left
):
    path = _spend_with_a_refused_write(cloud, capsys, repo, tmp_path)
    spent = {"claim": _claim(path), "receipt": _receipt(path), "lock": _lock()}
    _record(repo, path, [item for name, item in spent.items() if name != left])
    fresh = _new_go(repo, tmp_path)
    # A claim, a receipt or a lock left alone blocks before any read.
    _refused(cloud, capsys, "grant_amendment_claimed", WORD, F_PROPOSED_SHA256, str(fresh))
    assert sorted(item.name for item in _claims().iterdir()) == sorted(
        [spent[left].name, _marker(path).name]
    )


@pytest.mark.parametrize("kind", ["claim", "receipt"])
def test_a_grant_whose_claim_or_receipt_is_in_the_record_stays_spent(
    cloud, capsys, repo, tmp_path, kind
):
    _deploy_funded_job(cloud)
    path = _save(tmp_path, _grant(repo))
    record = repo.root / "docs" / "operations" / "iam-grants"
    record.mkdir(parents=True)
    short = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    (record / f"f.{short}.{kind}.json").write_bytes(b"{}\n")
    _refused(cloud, capsys, "grant_consumed", WORD, F_PROPOSED_SHA256, str(path))


def test_a_receipt_that_shows_no_write_does_not_block(cloud, capsys, repo, tmp_path):
    _deploy_funded_job(cloud)
    _claims().mkdir()
    receipt = {
        "applied": {"failed_at": None, "writes": []},
        "approval": {"amendment": "f"},
        "delta_sha256": F_PROPOSED_SHA256,
    }
    (_claims() / ("5" * 64 + ".receipt.json")).write_bytes(_canonical(receipt))
    path = _save(tmp_path, _grant(repo))
    code, done = _run(capsys, WORD, F_PROPOSED_SHA256, str(path))
    assert code == 0, done


@pytest.mark.parametrize("where", ["record_by_full_digest", "no_record", "sibling_checkout"])
def test_a_grant_moved_out_by_section_4_stays_spent_by_its_marker(
    cloud, capsys, repo, tmp_path, monkeypatch, where
):
    path = _spend_with_a_refused_write(cloud, capsys, repo, tmp_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    spent = [_claim(path), _receipt(path), _lock()]
    record = repo.root / "docs" / "operations" / "iam-grants"
    if where == "record_by_full_digest":
        record.mkdir(parents=True)
        for item in spent:
            kind = item.name.rsplit(".", 2)[-2]
            item.rename(record / f"f.{digest}.{kind}.json")
    elif where == "no_record":
        away = tmp_path / "away"
        away.mkdir()
        for item in spent:
            item.rename(away / item.name)
    else:
        _record(repo, path, spent)
        sibling = _clean_clone(repo, tmp_path)
        monkeypatch.setattr(migration, "IAM_E_REPO", sibling)
        monkeypatch.setattr(
            migration, "IAM_E_DELTA_PATH", sibling / "ops" / "deploy" / "iam_delta_v1.json"
        )
        assert not (sibling / "docs").exists()
    assert _marker(path).exists()
    _refused(cloud, capsys, "grant_consumed", WORD, F_PROPOSED_SHA256, str(path))
    # The marker never blocks the delta: a new go applies.
    code, receipt = _run(capsys, WORD, F_PROPOSED_SHA256, str(_new_go(repo, tmp_path)))
    assert code == 0, receipt


def test_the_record_check_matches_the_short_digest_anywhere_in_a_name(
    cloud, capsys, repo, tmp_path
):
    _deploy_funded_job(cloud)
    path = _save(tmp_path, _grant(repo))
    record = repo.root / "docs" / "operations" / "iam-grants"
    record.mkdir(parents=True)
    short = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    (record / f"archived-{short}-claim.json").write_bytes(b"{}\n")
    _refused(cloud, capsys, "grant_consumed", WORD, F_PROPOSED_SHA256, str(path))


@pytest.mark.parametrize(
    "raw", [b"not json\n", b"[]\n", b"\xff\n"], ids=["not_json", "list", "not_utf8"]
)
def test_an_unreadable_lone_receipt_blocks(cloud, capsys, repo, tmp_path, raw):
    _deploy_funded_job(cloud)
    _claims().mkdir()
    (_claims() / ("6" * 64 + ".receipt.json")).write_bytes(raw)
    path = _save(tmp_path, _grant(repo))
    _refused(cloud, capsys, "grant_amendment_claimed", WORD, F_PROPOSED_SHA256, str(path))


def test_a_lone_receipt_for_another_delta_does_not_block(cloud, capsys, repo, tmp_path):
    _deploy_funded_job(cloud)
    _claims().mkdir()
    receipt = {
        "applied": {"failed_at": None, "writes": [{"kind": "table", "resource": "t"}]},
        "approval": {"amendment": "f"},
        "delta_sha256": "7" * 64,
    }
    (_claims() / ("7" * 64 + ".receipt.json")).write_bytes(_canonical(receipt))
    path = _save(tmp_path, _grant(repo))
    code, done = _run(capsys, WORD, F_PROPOSED_SHA256, str(path))
    assert code == 0, done


@pytest.mark.parametrize("which", ["claim", "spent", "lock"])
def test_a_claim_or_lock_that_cannot_be_written_is_its_own_refusal(
    cloud, capsys, repo, tmp_path, monkeypatch, which
):
    _deploy_funded_job(cloud)
    path = _save(tmp_path, _grant(repo))
    write = migration._iam_e_write_exclusive

    def refusing(target, value):
        if target.name.endswith(f".{which}.json"):
            raise PermissionError(target)
        return write(target, value)

    monkeypatch.setattr(migration, "_iam_e_write_exclusive", refusing)
    code, error = _run(capsys, WORD, F_PROPOSED_SHA256, str(path))
    assert code == 1
    assert error["error"] == "execution_approval_iam_e_grant_claim_unwritten"
    assert cloud.writes == []
    # The lock is only ever taken after the grant's own claim.
    assert not _lock().exists()


def test_a_lock_for_another_delta_does_not_block(cloud, capsys, repo, tmp_path):
    _deploy_funded_job(cloud)
    _claims().mkdir()
    _lock(delta="3" * 64).write_bytes(b"{}\n")
    path = _save(tmp_path, _grant(repo))
    code, receipt = _run(capsys, WORD, F_PROPOSED_SHA256, str(path))
    assert code == 0, receipt


# 14. Grant refusals never load credentials: every _refused call above counts the loads.


def test_the_claim_is_written_only_after_every_check_and_the_live_read(
    cloud, capsys, repo, tmp_path, monkeypatch
):
    _deploy_funded_job(cloud)
    path = _save(tmp_path, _grant(repo))
    order = []
    loader = migration._load_credentials

    def load():
        order.append(("credentials", _claim(path).exists()))
        return loader()

    monkeypatch.setattr(migration, "_load_credentials", load)
    code, receipt = _run(capsys, WORD, F_PROPOSED_SHA256, str(path))
    assert code == 0, receipt
    assert order == [("credentials", False)]


# 15. The word takes exactly a digest and a path


@pytest.mark.parametrize(
    "words",
    [
        [WORD],
        [WORD, F_PROPOSED_SHA256],
        [WORD, "grants/f.json"],
        [WORD, F_PROPOSED_SHA256.upper(), "grants/f.json"],
        [WORD, F_PROPOSED_SHA256[:63], "grants/f.json"],
        [WORD, F_PROPOSED_SHA256, "grants/f.txt"],
        [WORD, F_PROPOSED_SHA256, "grants/.json"],
        [WORD, F_PROPOSED_SHA256, "grants/f.json", "extra"],
        [WORD, "grants/f.json", F_PROPOSED_SHA256],
    ],
    ids=[
        "bare",
        "digest_only",
        "path_only",
        "upper",
        "short",
        "not_json",
        "no_stem",
        "extra",
        "swapped",
    ],
)
def test_the_granted_word_takes_exactly_a_digest_and_a_json_path(cloud, capsys, repo, words):
    _refused(cloud, capsys, "grant_required", *words, exit_code=2)
