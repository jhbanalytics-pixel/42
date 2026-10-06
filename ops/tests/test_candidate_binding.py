"""The candidate binding verifier: one receipt that binds a staging release
candidate to the native readbacks that release.py verify, release.py resume and
the owner's registry reads already wrote, and the guide section rendered from it.

Every scenario runs the real release plan, apply, verify and resume commands
against the fake adapter, so the receipts under test are the ones those
commands write, not hand made copies.
"""

import copy
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
from datetime import UTC, date, datetime, timedelta

import pytest
from ops.certification import bind_candidate
from ops.deploy import release, release_index
from ops.tests import test_foundation_release as foundation
from ops.tests.test_foundation_release import (
    NOW,
    Scenario,
    apply_release,
    install_index,
    plan_release,
    plan_resume,
    release_index_fixture,
    resume_queue,
    sha,
    verify_with_index,
    write_json,
)
from ops.tests.test_release_index import git

# Importable once bind_candidate has put the engine root on the path.
from scripts.staging import collect_42_sources, read_ingest_run  # noqa: I001
from src.analysis.open_intelligence import ingest_receipts

ROOT = bind_candidate._ROOT
DESIGN_VERSION = "2.0.22"
ARCHIVE_NAME = f"ogilvy-intelligence-design-system-{DESIGN_VERSION}"
DIGEST = "ab" * 32


def design_archive(version=DESIGN_VERSION):
    manifest = {
        "package": {"name": "ogilvy-intelligence-design-system", "version": version},
        "contractVersion": "1.2.0",
        "sourceCommit": "61c42d1be2658e1f3dacdbf317683e81a86f4b6a",
    }
    raw = json.dumps(manifest).encode("utf-8")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as bundle:
        info = tarfile.TarInfo("package/dist/manifest.json")
        info.size = len(raw)
        info.mtime = 0
        bundle.addfile(info, io.BytesIO(raw))
    # A fixed gzip header time keeps the archive digest stable between calls.
    return gzip.compress(buffer.getvalue(), mtime=0)


def app_files(*, archive=None, sidecar=None):
    archive = design_archive() if archive is None else archive
    package = {
        "name": "frontend",
        "dependencies": {
            "ogilvy-intelligence-design-system": f"file:vendor/{ARCHIVE_NAME}.tgz"
        },
    }
    return {
        "app/frontend/package.json": json.dumps(package).encode("utf-8"),
        f"app/frontend/vendor/{ARCHIVE_NAME}.tgz": archive,
        f"app/frontend/vendor/{ARCHIVE_NAME}.sha256": (
            sha(archive).upper() if sidecar is None else sidecar
        ).encode("ascii")
        + b"\n",
        "app/main.py": b"print('app')\n",
        "engine/src/a.py": b"x = 1\n",
    }


def checkout(tmp_path, files, name="checkout"):
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "config", "user.name", "scratch")
    git(repo, "config", "user.email", "scratch@example.invalid")
    for path, raw in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    git(repo, "add", "-A")
    # Pinned dates make the commit id a function of the files alone, so two
    # candidates built from the same files share a commit.
    completed = subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "candidate"],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": "2026-09-12T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-09-12T00:00:00Z",
        },
    )
    assert completed.returncode == 0, completed.stderr
    return repo


def app_tree_inventory(files):
    paths = [path for path in files if path.startswith("app/")]
    return release_index.inventory_bytes(files, paths)


def capture_entry(**overrides):
    entry = {
        "profile_id": "daily_za_ng_ke",
        "profile_version": "v3",
        "operation_id": "operation-20260924",
        "result_id": "result-20260924",
        "source_kind": "daily_collection",
        "scope": "staging",
        "source_tables": ["reddit_posts"],
        "snapshot_tables": ["snapshot_reddit_posts"],
        "markets": ["za", "ng"],
        "observation_window_end": "2026-09-12T00:00:00Z",
        "collection_started_at": "2026-09-12T00:05:00Z",
        "collection_completed_at": "2026-09-12T01:05:00Z",
        "snapshot_as_of": "2026-09-12T01:10:00Z",
        "capture_available_at": "2026-09-12T01:20:00Z",
        "content_digest": "01" * 32,
        "schema_digest": "02" * 32,
        "policy_digest": "03" * 32,
        "source_digest": "04" * 32,
        "image_digest": "05" * 32,
        "generation": "1790000000000001",
        "completion_state": "succeeded",
    }
    entry.update(overrides)
    return entry


def release_record(**overrides):
    record = {
        "run_id": "run_20260924_daily",
        "run_receipt_digest": "11" * 32,
        "source_window_digest": "12" * 32,
        "candidate_projection_digest": "13" * 32,
        "packet_digest": "14" * 32,
        "review_receipt_digest": "15" * 32,
        "approval_addendum_sha256": "16" * 32,
        "released_at": "2026-09-12T06:00:00Z",
        "release_contract_version": "open_intelligence_quality_release_v2",
    }
    record.update(overrides)
    return record


INGEST_EXECUTION = "intelligence-42-ingest-staging-a1b2c"
INGEST_RUN = "run-20260912-ingest"
INGEST_BUILD = {
    "source_sha": "7bab3970cd75af4d0f686c339cabe9ecfdd3aad4",
    "image_uri": "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/engine@sha256:"
    + "a" * 64,
}


class _IngestJob:
    def __init__(self, rows, processed):
        self.rows = rows
        self.total_bytes_processed = processed
        self.total_bytes_billed = processed
        self.job_id = "job-ingest"

    def result(self, timeout=None):
        return list(self.rows)


class _IngestClient:
    """The two statements of the ingest readback answered from one stored receipt."""

    def __init__(self, row, raw):
        self.row = row
        self.raw = raw

    def query(self, sql, job_config=None, **kwargs):
        if job_config.dry_run:
            return _IngestJob([], 1000)
        if read_ingest_run.RECEIPT_TABLE_ID in sql:
            digest = hashlib.sha256(
                self.row["receipt_json"].encode("utf-8")
            ).hexdigest()
            return _IngestJob([{**self.row, "computed_sha256": digest}], 1000)
        return _IngestJob(self.raw, 1000)


def ingest_readback(
    *, build=INGEST_BUILD, dry_run=False, raw_total=3, **receipt_changes
):
    """A readback written by the real ingest readback tool over one stored receipt."""
    values = {
        "run_id": INGEST_RUN,
        "execution_id": INGEST_EXECUTION,
        "policy_sha256": read_ingest_run.EXPECTED_POLICY_SHA256,
        "profile_sha256": read_ingest_run.EXPECTED_PROFILE_SHA256,
        "cutoff": "2026-09-11",
        "market_states": {"za": "collected", "ng": "collected", "ke": "collected"},
        "raw_count": 3,
        "enriched_count": 3,
        "authority_kind": collect_42_sources.VERIFIED_AUTHORITY,
        **INGEST_BUILD,
    }
    values.update(receipt_changes)
    row = ingest_receipts.receipt_row(
        collect_42_sources.collection_receipt(**values), trend_date="2026-09-12"
    )
    row["recorded_at"] = datetime(2026, 9, 12, 4, 30, tzinfo=UTC)
    at = datetime(2026, 9, 12, 4, 10, tzinfo=UTC)
    raw = [
        {
            "market": market,
            "source": "gdelt",
            "row_count": count,
            "last_collected_at": at,
        }
        for market, count in (("ke", 1), ("ng", 1), ("za", raw_total - 2))
    ]
    return read_ingest_run.read_ingest_run(
        _IngestClient(row, raw),
        execution_id=INGEST_EXECUTION,
        cutoff=date(2026, 9, 11),
        maximum_bytes_billed=10**9,
        dry_run=dry_run,
        build=build,
    )


COLLECTION_PROFILE = "staging-2026-09-11-" + read_ingest_run.EXPECTED_POLICY_SHA256[:16]
COLLECTION_ATTEMPT = "attempt:collect:20260912"


def ledger_digest(readback, attempt=COLLECTION_ATTEMPT):
    """The source digest the daily capture of this readback's receipt records, built by
    hand as the collect stage and capture runner build it, not through the binder."""
    receipt = dict(readback["receipt_row"]["receipt"])
    receipt.pop("authority_kind")
    receipt["execution_id"] = attempt
    return hashlib.sha256(ingest_receipts.canonical_bytes(receipt)).hexdigest()


def collection_capture(**overrides):
    """The daily capture of the bound collection's cutoff under its source policy."""
    fields = {
        "profile_id": COLLECTION_PROFILE,
        "operation_id": "operation-20260911",
        "result_id": "result-20260911",
        "markets": ["ke", "ng", "za"],
        "policy_digest": read_ingest_run.EXPECTED_POLICY_SHA256,
        "source_digest": ledger_digest(ingest_readback()),
    }
    fields.update(overrides)
    return capture_entry(**fields)


def commit_scenario(tmp_path, commit):
    """The foundation scenario with the new binding at the scratch checkout's
    commit, so the checkout really is the candidate commit."""
    original = foundation.make_binding

    def at_head(policy, **kwargs):
        if "lp_commit" not in kwargs:
            kwargs["lp_commit"] = commit
        return original(policy, **kwargs)

    foundation.make_binding = at_head
    try:
        return Scenario(tmp_path)
    finally:
        foundation.make_binding = original


def at_commit(index, references, commit):
    old = index["commit"]
    index["commit"] = commit
    references["commit"] = commit.encode("ascii")
    for reference, item in index["evidence_objects"].items():
        item["uri"] = item["uri"].replace(
            release.evidence_object_name(old, reference),
            release.evidence_object_name(commit, reference),
        )


class Candidate:
    """A released scenario with a published index whose app tree is the
    scratch checkout's, verified by the real release.py verify."""

    def __init__(self, tmp_path, *, files=None, mutate_index=None, resume=False):
        self.dir = tmp_path
        self.files = app_files() if files is None else files
        self.repo = checkout(tmp_path, self.files)
        self.commit = git(self.repo, "rev-parse", "HEAD")
        self.scenario = commit_scenario(tmp_path, self.commit)
        code, _summary, stderr, self.plan_path = plan_release(self.scenario)
        assert code == 0, stderr
        code, _summary, stderr, self.result_path = apply_release(
            self.scenario, self.plan_path
        )
        assert code == 0, stderr
        index, references = release_index_fixture(self.scenario)
        at_commit(index, references, self.commit)
        inventory = app_tree_inventory(self.files)
        references["app_tree"] = inventory
        index["app_tree_sha256"] = sha(inventory)
        index["evidence_objects"]["app_tree"]["sha256"] = sha(inventory)
        if mutate_index is not None:
            mutate_index(index, references)
        self.index = index
        install_index(self.scenario, index, references)
        self.index_path = write_json(tmp_path / "release-index.json", index)
        self.resume_path = None
        self.resume_plan_path = None
        if resume:
            state = self.scenario.current()
            state["clock"]["now"] = (NOW + timedelta(seconds=200)).isoformat()
            write_json(self.scenario.state_path, state)
            self.scenario.route_path = self.result_path
            code, _summary, stderr, self.resume_plan_path = plan_resume(
                self.scenario, "empty"
            )
            assert code == 0, stderr
            code, _summary, stderr, self.resume_path = resume_queue(
                self.scenario, self.resume_plan_path
            )
            assert code == 0, stderr
        self.verify_path = tmp_path / "verified.json"
        verify_with_index(
            self.scenario,
            self.plan_path,
            self.result_path,
            self.index_path,
            self.verify_path,
        )
        self.verify = json.loads(self.verify_path.read_bytes())
        self.now = release._parse_time(self.verify["checked_at"], "x") + timedelta(
            minutes=5
        )
        self.captures_path = write_json(
            tmp_path / "captures.json", [capture_entry(), collection_capture()]
        )
        self.records_path = write_json(tmp_path / "records.json", [release_record()])
        self.ingest_path = write_json(
            tmp_path / "ingest-readback.json", ingest_readback()
        )

    def bind(self, **overrides):
        arguments = {
            "index_path": self.index_path,
            "plan_path": self.plan_path,
            "verify_path": self.verify_path,
            "resume_path": self.resume_path,
            "resume_plan_path": self.resume_plan_path,
            "repo": self.repo,
            "captures_path": self.captures_path,
            "capture_scope": "staging",
            "capture_profiles": ("daily_za_ng_ke",),
            "release_records_path": self.records_path,
            "ingest_readback_path": self.ingest_path,
            "collection_attempt_id": COLLECTION_ATTEMPT,
            "now": self.now,
        }
        arguments.update(overrides)
        return bind_candidate.bind(**arguments)


def reasons(receipt):
    return {
        name: row.get("reason")
        for name, row in receipt["subjects"].items()
        if row["state"] != "bound"
    }


def test_a_verified_release_with_every_readback_binds_the_whole_candidate(tmp_path):
    candidate = Candidate(tmp_path)
    receipt = candidate.bind()
    assert reasons(receipt) == {}
    assert receipt["state"] == "bound"
    assert list(receipt["subjects"]) == list(bind_candidate.SUBJECTS)
    subjects = receipt["subjects"]
    binding = candidate.scenario.binding
    tag = json.loads(candidate.plan_path.read_bytes())["expected"]["tag"]
    assert subjects["repository_commit"]["value"]["commit"] == binding["lp_commit"]
    assert subjects["images"]["value"]["app_image"] == candidate.index["app_image"]
    assert (
        subjects["service_revision"]["value"]["revision_name"]
        == (binding["revision_name"])
    )
    assert subjects["traffic"]["value"]["url"] == release.asset_origin(
        binding["canonical_service_audience"], tag
    )
    assert subjects["queue"]["value"]["state"] == "PAUSED"
    assert (
        subjects["policy_deployment"]["value"]["deployment_digest"]
        == (binding["deployment_digest"])
    )
    assert (
        subjects["served_assets"]["value"]["assets"] == candidate.index["served_assets"]
    )
    design = subjects["design_package"]["value"]
    assert design["version"] == DESIGN_VERSION
    assert design["archive_sha256"] == sha(design_archive())
    assert subjects["captures"]["value"][0]["result_id"] == "result-20260924"
    assert subjects["released_runs"]["value"][0]["run_id"] == "run_20260924_daily"
    collection = subjects["collection"]["value"]
    assert collection["execution_id"] == INGEST_EXECUTION
    assert collection["run_id"] == INGEST_RUN
    assert collection["cutoff"] == "2026-09-11"
    assert collection["source_sha"] == INGEST_BUILD["source_sha"]
    assert collection["raw_rows"] == 3
    assert collection["markets"] == ["ke", "ng", "za"]
    assert re.fullmatch(r"[0-9a-f]{64}", receipt["candidate_id"])
    assert receipt["inputs"]["release_index_sha256"] == sha(
        candidate.index_path.read_bytes()
    )
    again = candidate.bind(now=candidate.now + timedelta(minutes=1))
    assert again["candidate_id"] == receipt["candidate_id"]


def test_the_verify_receipt_must_be_recent_and_not_from_the_future(tmp_path):
    candidate = Candidate(tmp_path)
    stale = candidate.bind(now=candidate.now + timedelta(hours=2))
    assert stale["state"] == "unproven"
    assert stale["candidate_id"] is None
    assert reasons(stale)["verify_receipt"] == "verify_receipt_stale"
    # Nothing the stale receipt read is carried as bound.
    for name in ("repository_commit", "images", "traffic", "served_assets"):
        assert reasons(stale)[name] == "verify_receipt_unproven"
    early = candidate.bind(now=candidate.now - timedelta(hours=1))
    assert reasons(early)["verify_receipt"] == "verify_receipt_from_future"


def test_a_verify_receipt_of_other_index_bytes_binds_nothing(tmp_path):
    candidate = Candidate(tmp_path)
    edited = copy.deepcopy(candidate.index)
    edited["served_assets"] = dict(edited["served_assets"])
    other = tmp_path / "other-index.json"
    other.write_text(json.dumps(edited, indent=4), encoding="utf-8")
    receipt = candidate.bind(index_path=other)
    assert reasons(receipt)["verify_receipt"] == "verify_receipt_index_mismatch"
    assert receipt["state"] == "unproven"


def test_a_verify_receipt_of_another_plan_binds_nothing(tmp_path):
    candidate = Candidate(tmp_path)
    code, _summary, stderr, other_plan = plan_release(
        candidate.scenario, "release-plan-2.json"
    )
    assert code == 0, stderr
    receipt = candidate.bind(plan_path=other_plan)
    assert reasons(receipt)["verify_receipt"] == "verify_receipt_plan_mismatch"


def test_traffic_drift_in_the_verify_receipt_leaves_traffic_unproven(tmp_path):
    candidate = Candidate(tmp_path)
    drifted = candidate.scenario.current()
    drifted["service"]["trafficStatuses"][0]["percent"] = 50
    write_json(candidate.scenario.state_path, drifted)
    second = tmp_path / "verified-2.json"
    verify_with_index(
        candidate.scenario,
        candidate.plan_path,
        candidate.result_path,
        candidate.index_path,
        second,
    )
    receipt = candidate.bind(verify_path=second)
    assert reasons(receipt)["traffic"] in {"verify_drift", "traffic_unverified"}
    assert receipt["state"] == "unproven"
    assert "traffic" in receipt["unproven"]


def test_an_index_whose_bytes_did_not_resolve_leaves_the_verify_receipt_unproven(
    tmp_path,
):
    candidate = Candidate(tmp_path)
    state = candidate.scenario.current()
    name = candidate.index["evidence_objects"]["engine_tree"]["uri"].split("/", 3)[3]
    del state["objects"][name]
    write_json(candidate.scenario.state_path, state)
    second = tmp_path / "verified-2.json"
    verify_with_index(
        candidate.scenario,
        candidate.plan_path,
        candidate.result_path,
        candidate.index_path,
        second,
    )
    receipt = candidate.bind(verify_path=second)
    assert reasons(receipt)["verify_receipt"] == "verify_receipt_index_unproven"
    assert receipt["candidate_id"] is None


def test_a_running_queue_binds_only_through_a_running_verified_resume(tmp_path):
    candidate = Candidate(tmp_path, resume=True)
    assert candidate.verify["drift"] == [{"item": "queue", "error": "queue_not_paused"}]
    receipt = candidate.bind()
    assert reasons(receipt) == {}
    assert receipt["subjects"]["queue"]["value"]["state"] == "RUNNING"
    without = candidate.bind(resume_path=None)
    assert reasons(without) == {"queue": "resume_receipt_not_supplied"}
    resumed = json.loads(candidate.resume_path.read_bytes())
    resumed["state"] = "resume_unproven"
    unproven = write_json(tmp_path / "resume-unproven.json", resumed)
    assert reasons(candidate.bind(resume_path=unproven)) == {
        "queue": "resume_receipt_not_running_verified"
    }
    resumed = json.loads(candidate.resume_path.read_bytes())
    resumed["binding"] = dict(resumed["binding"], revision_name="other")
    other = write_json(tmp_path / "resume-other.json", resumed)
    assert reasons(candidate.bind(resume_path=other)) == {
        "queue": "resume_receipt_binding_mismatch"
    }


def test_an_unproven_apply_operation_voids_the_verify_receipt(tmp_path):
    candidate = Candidate(tmp_path)
    verify = copy.deepcopy(candidate.verify)
    verify["drift"].append(
        {"item": "operation:route_traffic", "error": "operation_unproven"}
    )
    path = write_json(tmp_path / "verified-op.json", verify)
    receipt = candidate.bind(verify_path=path)
    assert reasons(receipt)["verify_receipt"] == "verify_drift"


def test_the_design_package_is_read_from_a_checkout_of_the_candidate_app_tree(
    tmp_path,
):
    candidate = Candidate(tmp_path)
    assert reasons(candidate.bind(repo=None)) == {
        "design_package": "checkout_not_supplied"
    }
    other_files = dict(candidate.files, **{"app/main.py": b"print('other')\n"})
    other = checkout(tmp_path, other_files, name="other")
    assert reasons(candidate.bind(repo=other)) == {
        "design_package": "checkout_not_at_commit"
    }
    (candidate.repo / "app/main.py").write_bytes(b"dirty\n")
    assert reasons(candidate.bind()) == {"design_package": "checkout_dirty"}


@pytest.mark.parametrize(
    ("files", "reason"),
    [
        (app_files(sidecar="00" * 32), "design_package_archive_digest_mismatch"),
        (
            app_files(archive=design_archive(version="2.0.21")),
            "design_package_version_mismatch",
        ),
        (
            {
                key: value
                for key, value in app_files().items()
                if not key.endswith(".sha256")
            },
            "design_package_archive_missing",
        ),
    ],
    ids=["archive_digest", "archive_version", "sidecar_missing"],
)
def test_a_design_package_that_is_not_its_pinned_archive_stays_unproven(
    tmp_path, files, reason
):
    candidate = Candidate(tmp_path, files=files)
    assert reasons(candidate.bind()) == {"design_package": reason}


def test_captures_and_released_runs_must_be_supplied_and_complete(tmp_path):
    candidate = Candidate(tmp_path)
    missing = candidate.bind(captures_path=None, release_records_path=None)
    assert reasons(missing) == {
        "captures": "captures_not_supplied",
        "released_runs": "release_records_not_supplied",
    }
    empty = write_json(tmp_path / "empty.json", [])
    assert reasons(candidate.bind(captures_path=empty, release_records_path=empty)) == {
        "captures": "captures_empty",
        "released_runs": "release_records_empty",
    }
    for entry, diagnostic in (
        (capture_entry(completion_state="partial"), "not_succeeded:partial"),
        (capture_entry(markets=["za", "gb"]), "capture_field_invalid:markets"),
        (capture_entry(content_digest="x"), "capture_field_invalid:content_digest"),
        (
            capture_entry(capture_available_at="not a time"),
            "capture_field_invalid:capture_available_at",
        ),
    ):
        path = write_json(tmp_path / "bad-capture.json", [entry])
        receipt = candidate.bind(captures_path=path)
        expected = (
            "capture_none_eligible:daily_za_ng_ke"
            if diagnostic.startswith("not_succeeded")
            else "capture_readback_malformed"
        )
        assert reasons(receipt) == {"captures": expected}
        assert [
            item["reason"] for item in receipt["subjects"]["captures"]["value"]
        ] == [diagnostic]
    for record, reason in (
        (
            release_record(
                release_contract_version="open_intelligence_quality_release_v1"
            ),
            "release_record_contract_mismatch",
        ),
        (release_record(packet_digest="short"), "release_record_invalid"),
        ({**release_record(), "extra": 1}, "release_record_invalid"),
    ):
        path = write_json(tmp_path / "bad-record.json", [record])
        assert reasons(candidate.bind(release_records_path=path)) == {
            "released_runs": reason
        }


def test_an_index_with_no_served_assets_leaves_served_assets_unproven(tmp_path):
    def no_assets(index, references):
        index["served_assets"] = {}

    candidate = Candidate(tmp_path, mutate_index=no_assets)
    assert candidate.verify["drift"] == []
    assert reasons(candidate.bind()) == {"served_assets": "served_assets_empty"}


def test_malformed_inputs_refuse_rather_than_bind(tmp_path):
    candidate = Candidate(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="release_index_invalid"):
        candidate.bind(index_path=bad)
    with pytest.raises(ValueError, match="verify_receipt_invalid"):
        candidate.bind(verify_path=bad)
    with pytest.raises(ValueError, match="plan_invalid"):
        candidate.bind(plan_path=bad)
    shape = copy.deepcopy(candidate.index)
    shape["schema_version"] = "42_staging_release_v2"
    with pytest.raises(ValueError, match="release_index_invalid"):
        candidate.bind(index_path=write_json(tmp_path / "v2.json", shape))
    with pytest.raises(ValueError, match="clock_invalid"):
        candidate.bind(now=candidate.now.replace(tzinfo=None))
    with pytest.raises(ValueError, match="ingest_readback_invalid"):
        candidate.bind(ingest_readback_path=bad)
    with pytest.raises(ValueError, match="ingest_readback_invalid"):
        candidate.bind(ingest_readback_path=write_json(tmp_path / "list.json", []))


def test_compare_names_every_identity_subject_a_later_change_touches(tmp_path):
    candidate = Candidate(tmp_path)
    first = candidate.bind()
    assert bind_candidate.changed_subjects(first, first) == []
    later = copy.deepcopy(first)
    later["subjects"]["design_package"]["value"]["version"] = "2.0.23"
    later["subjects"]["captures"] = {"state": "unproven", "reason": "x", "value": None}
    assert bind_candidate.changed_subjects(first, later) == [
        "design_package",
        "captures",
    ]
    with pytest.raises(ValueError, match="binding_receipt_invalid"):
        bind_candidate.changed_subjects({}, first)


def test_the_guide_names_the_markets_no_capture_covers(tmp_path):
    candidate = Candidate(tmp_path)
    path = edited(
        tmp_path,
        "capture-two-markets.json",
        [capture_entry(), collection_capture(markets=["ng", "za"])],
    )
    receipt = candidate.bind(captures_path=path)
    assert reasons(receipt) == {}
    text = bind_candidate.render_guide_section(receipt)
    assert "No capture covers these markets: ke." in text


def test_the_guide_section_takes_every_candidate_value_from_the_receipt(tmp_path):
    candidate = Candidate(tmp_path)
    receipt = candidate.bind()
    text = bind_candidate.render_guide_section(receipt)
    subjects = receipt["subjects"]
    assert text.startswith("<!-- 42_candidate_guide_section_v1 -->\n")
    assert "Ready for you to test." in text
    assert "URL: " + subjects["traffic"]["value"]["url"] in text
    assert subjects["service_revision"]["value"]["revision_name"] in text
    assert subjects["repository_commit"]["value"]["commit"] in text
    assert f"ogilvy-intelligence-design-system {DESIGN_VERSION}" in text
    assert "data up to 2026-09-12T00:00:00.000000Z" in text
    assert "Released run run_20260924_daily at 2026-09-12T06:00:00.000000Z." in text
    assert (
        "Collection for 2026-09-11: run run-20260912-ingest, 3 raw rows in ke, ng, za, "
        "last collected at 2026-09-12T04:10:00.000000Z." in text
    )
    for _, journey in bind_candidate.MANDATORY_JOURNEYS:
        assert journey in text
    for field in bind_candidate.FEEDBACK_FIELDS:
        assert field + ": " in text
    assert "No capture covers these markets" not in text
    # The Cloud Run tag URL carries a triple hyphen by construction.
    prose = text.split("\n", 1)[1].replace(subjects["traffic"]["value"]["url"], "")
    for forbidden in ("TBD", "TODO", "<fill", chr(0x2014), chr(0x2013), "--"):
        assert forbidden not in prose


def test_an_unproven_candidate_renders_as_not_ready_with_its_reasons(tmp_path):
    candidate = Candidate(tmp_path)
    receipt = candidate.bind(repo=None, now=candidate.now + timedelta(hours=2))
    text = bind_candidate.render_guide_section(receipt)
    assert "Not ready for testing." in text
    assert "URL: unproven" in text
    assert "Design package: unproven" in text
    assert "design_package is unproven: checkout_not_supplied." in text
    assert "verify_receipt is unproven: verify_receipt_stale." in text


def test_the_cli_writes_once_and_exits_nonzero_on_an_unproven_candidate(
    tmp_path, capsys
):
    candidate = Candidate(tmp_path)
    output = tmp_path / "binding.json"
    argv = [
        "bind",
        "--release-index",
        str(candidate.index_path),
        "--plan",
        str(candidate.plan_path),
        "--verify",
        str(candidate.verify_path),
        "--repo",
        str(candidate.repo),
        "--captures",
        str(candidate.captures_path),
        "--capture-scope",
        "staging",
        "--capture-profile",
        "daily_za_ng_ke",
        "--release-records",
        str(candidate.records_path),
        "--ingest-readback",
        str(candidate.ingest_path),
        "--collection-attempt-id",
        COLLECTION_ATTEMPT,
        "--output",
        str(output),
    ]
    assert bind_candidate.execute(argv, now=candidate.now) == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["state"] == "bound"
    before = output.read_bytes()
    assert bind_candidate.execute(argv, now=candidate.now) == 1
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["error"] == "output_exists"
    assert output.read_bytes() == before
    guide = tmp_path / "guide.md"
    assert (
        bind_candidate.execute(
            ["guide", "--binding", str(output), "--output", str(guide)]
        )
        == 0
    )
    assert guide.read_text(encoding="utf-8") == bind_candidate.render_guide_section(
        json.loads(before)
    )
    assert (
        bind_candidate.execute(
            ["compare", "--previous", str(output), "--current", str(output)]
        )
        == 0
    )
    stale = tmp_path / "stale.json"
    argv[-1] = str(stale)
    assert bind_candidate.execute(argv, now=candidate.now + timedelta(hours=3)) == 1
    assert json.loads(stale.read_bytes())["state"] == "unproven"


def test_the_release_record_contract_matches_the_release_script():
    assert (
        bind_candidate.release_record_contract_in_script()
        == bind_candidate.RELEASE_RECORD_CONTRACT
    )


def test_documented_commands_only_name_existing_flags():
    text = (ROOT / "docs/operations/42-candidate-binding.md").read_text(
        encoding="utf-8"
    )
    documented = {}
    for match in re.finditer(
        r"bind_candidate\.py (bind|guide|compare)((?:\s+--[a-z-]+(?:\s+[^\s`-][^\s`]*)?)+)",
        text,
    ):
        documented.setdefault(match.group(1), set()).update(
            re.findall(r"--[a-z-]+", match.group(2))
        )
    assert set(documented) == {"bind", "guide", "compare"}
    parser = bind_candidate.build_parser()
    actions = {
        name: {flag for action in sub._actions for flag in action.option_strings}
        for name, sub in parser._subparsers._group_actions[0].choices.items()
    }
    for command, flags in documented.items():
        assert flags <= actions[command], (command, flags - actions[command])


def test_a_verify_receipt_that_read_fewer_references_than_the_index_names_binds_nothing(
    tmp_path,
):
    candidate = Candidate(tmp_path)
    verify = copy.deepcopy(candidate.verify)
    verify["release_index"]["references_read"].pop()
    path = write_json(tmp_path / "verified-short.json", verify)
    receipt = candidate.bind(verify_path=path)
    assert reasons(receipt)["verify_receipt"] == "verify_receipt_references_incomplete"


def test_traffic_is_read_from_the_native_service_not_inferred_from_empty_drift(
    tmp_path,
):
    candidate = Candidate(tmp_path)
    verify = copy.deepcopy(candidate.verify)
    verify["native"]["service"]["trafficStatuses"][0]["tag"] = "question-other"
    path = write_json(tmp_path / "verified-traffic.json", verify)
    assert reasons(candidate.bind(verify_path=path)) == {
        "traffic": "traffic_unverified"
    }


def test_a_wrong_traffic_spec_is_unproven_even_when_its_status_is_right(tmp_path):
    candidate = Candidate(tmp_path)
    verify = copy.deepcopy(candidate.verify)
    verify["native"]["service"]["traffic"][0]["percent"] = 50
    path = write_json(tmp_path / "verified-spec.json", verify)
    assert reasons(candidate.bind(verify_path=path)) == {
        "traffic": "traffic_unverified"
    }


def test_a_queue_readback_of_another_queue_is_unproven(tmp_path):
    candidate = Candidate(tmp_path)
    for name in (None, release.QUEUE_API_NAME + "-other"):
        verify = copy.deepcopy(candidate.verify)
        verify["native"]["queue"]["name"] = name
        path = write_json(tmp_path / "verified-queue-name.json", verify)
        assert reasons(candidate.bind(verify_path=path)) == {
            "queue": "queue_name_mismatch"
        }


def archive_without_manifest():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as bundle:
        raw = b"{}"
        info = tarfile.TarInfo("package/package.json")
        info.size = len(raw)
        bundle.addfile(info, io.BytesIO(raw))
    return gzip.compress(buffer.getvalue(), mtime=0)


def archive_named(name):
    manifest = {"package": {"name": name, "version": DESIGN_VERSION}}
    raw = json.dumps(manifest).encode("utf-8")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as bundle:
        info = tarfile.TarInfo("package/dist/manifest.json")
        info.size = len(raw)
        bundle.addfile(info, io.BytesIO(raw))
    return gzip.compress(buffer.getvalue(), mtime=0)


@pytest.mark.parametrize(
    ("archive", "reason"),
    [
        (archive_without_manifest(), "design_package_manifest_missing"),
        (archive_named("another-design-system"), "design_package_version_mismatch"),
    ],
    ids=["no_manifest", "other_package"],
)
def test_an_archive_that_is_not_the_design_package_stays_unproven(
    tmp_path, archive, reason
):
    candidate = Candidate(tmp_path, files=app_files(archive=archive))
    assert reasons(candidate.bind()) == {"design_package": reason}


def test_a_checkout_at_the_commit_whose_app_tree_differs_from_the_index_is_unproven(
    tmp_path,
):
    def other_app_tree(index, references):
        inventory = app_tree_inventory(
            dict(app_files(), **{"app/main.py": b"print('other')\n"})
        )
        references["app_tree"] = inventory
        index["app_tree_sha256"] = sha(inventory)
        index["evidence_objects"]["app_tree"]["sha256"] = sha(inventory)

    candidate = Candidate(tmp_path, mutate_index=other_app_tree)
    assert candidate.verify["drift"] == []
    assert reasons(candidate.bind()) == {"design_package": "checkout_app_tree_mismatch"}


def test_a_design_dependency_that_is_not_a_string_stays_unproven(tmp_path):
    files = app_files()
    files["app/frontend/package.json"] = json.dumps(
        {"dependencies": {"ogilvy-intelligence-design-system": {"path": "x"}}}
    ).encode("utf-8")
    candidate = Candidate(tmp_path, files=files)
    assert reasons(candidate.bind()) == {
        "design_package": "design_package_dependency_invalid"
    }


def source_binding_of(generation):
    def mutate(index, references):
        raw = b"ledger before, generation " + generation.encode()
        references["source_binding"] = raw
        index["source_binding"]["generation"] = generation
        index["source_binding"]["sha256"] = sha(raw)
        index["evidence_objects"]["source_binding"]["sha256"] = sha(raw)

    return mutate


def test_a_changed_source_binding_changes_the_candidate(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = Candidate(tmp_path / "a", mutate_index=source_binding_of("39")).bind()
    second = Candidate(tmp_path / "b", mutate_index=source_binding_of("38")).bind()
    assert first["state"] == second["state"] == "bound"
    assert first["candidate_id"] != second["candidate_id"]
    assert bind_candidate.changed_subjects(first, second) == ["source_binding"]


def test_an_admitted_question_moving_the_ledger_does_not_change_the_candidate(
    tmp_path,
):
    candidate = Candidate(tmp_path)
    first = candidate.bind()
    verify = copy.deepcopy(candidate.verify)
    verify["native"]["ledger"]["generation"] = str(
        int(verify["native"]["ledger"]["generation"]) + 7
    )
    verify["native"]["ledger"]["request_count"] += 1
    moved = candidate.bind(verify_path=write_json(tmp_path / "moved.json", verify))
    assert moved["state"] == "bound"
    assert moved["candidate_id"] == first["candidate_id"]
    assert bind_candidate.changed_subjects(first, moved) == []
    assert moved["subjects"]["policy_deployment"]["evidence"] == {
        "ledger_generation": verify["native"]["ledger"]["generation"]
    }


def edited(tmp_path, name, value):
    return write_json(tmp_path / name, value)


def test_the_resume_must_chain_to_the_verified_apply_and_run_in_order(tmp_path):
    candidate = Candidate(tmp_path, resume=True)
    assert reasons(candidate.bind()) == {}
    assert reasons(candidate.bind(resume_plan_path=None)) == {
        "queue": "resume_receipt_not_supplied"
    }
    resume = json.loads(candidate.resume_path.read_bytes())
    plan = json.loads(candidate.resume_plan_path.read_bytes())

    other_plan = copy.deepcopy(plan)
    other_plan["route"]["sha256"] = "0" * 64
    other_plan = release._seal(
        {key: value for key, value in other_plan.items() if key != "idempotency_key"}
    )
    other_plan_path = edited(tmp_path, "resume-plan-other.json", other_plan)
    assert reasons(candidate.bind(resume_plan_path=other_plan_path)) == {
        "queue": "resume_receipt_plan_mismatch"
    }
    chained = dict(resume, plan_sha256=sha(other_plan_path.read_bytes()))
    assert reasons(
        candidate.bind(
            resume_plan_path=other_plan_path,
            resume_path=edited(tmp_path, "resume-chained.json", chained),
        )
    ) == {"queue": "resume_receipt_route_mismatch"}

    for field, value in (
        ("started_at", "2026-09-13T07:00:00Z"),
        ("finished_at", "2026-09-13T09:00:00Z"),
    ):
        out_of_order = dict(resume, **{field: value})
        path = edited(tmp_path, f"resume-{field}.json", out_of_order)
        assert reasons(candidate.bind(resume_path=path)) == {
            "queue": "resume_receipt_out_of_order"
        }
    for field, value in (("kind", "release"), ("contract_version", "other")):
        path = edited(tmp_path, f"resume-{field}.json", dict(resume, **{field: value}))
        assert reasons(candidate.bind(resume_path=path)) == {
            "queue": "resume_receipt_not_running_verified"
        }


def test_a_running_queue_with_any_other_drift_is_unproven(tmp_path):
    candidate = Candidate(tmp_path, resume=True)
    verify = copy.deepcopy(candidate.verify)
    verify["drift"].append({"item": "queue", "error": "queue_other"})
    path = edited(tmp_path, "verified-queue.json", verify)
    assert reasons(candidate.bind(verify_path=path))["queue"] == "verify_drift"


def test_a_paused_queue_with_queue_drift_is_unproven(tmp_path):
    candidate = Candidate(tmp_path)
    verify = copy.deepcopy(candidate.verify)
    verify["drift"].append({"item": "queue", "error": "queue_not_paused"})
    path = edited(tmp_path, "verified-paused.json", verify)
    assert reasons(candidate.bind(verify_path=path)) == {
        "queue": "queue_state_unverified"
    }


@pytest.mark.parametrize(
    ("item", "subject", "reason"),
    [
        ("ledger", "policy_deployment", "ledger_not_active"),
        ("revision", "service_revision", "verify_drift"),
        ("service", "traffic", "verify_drift"),
    ],
)
def test_subject_drift_in_the_verify_receipt_leaves_that_subject_unproven(
    tmp_path, item, subject, reason
):
    candidate = Candidate(tmp_path)
    verify = copy.deepcopy(candidate.verify)
    verify["drift"].append({"item": item, "error": "drifted"})
    path = edited(tmp_path, "verified-drift.json", verify)
    assert reasons(candidate.bind(verify_path=path)) == {subject: reason}


def test_a_revision_readback_of_another_revision_is_unproven(tmp_path):
    candidate = Candidate(tmp_path)
    verify = copy.deepcopy(candidate.verify)
    verify["native"]["revision"]["name"] = "listening-post-staging-q-other"
    path = edited(tmp_path, "verified-revision.json", verify)
    assert reasons(candidate.bind(verify_path=path)) == {
        "service_revision": "revision_name_mismatch"
    }


@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        ({"kind": "release"}, "verify_receipt_invalid"),
        ({"contract_version": "other"}, "verify_receipt_invalid"),
    ],
)
def test_only_a_verify_receipt_counts_as_the_verify_readback(tmp_path, edit, reason):
    candidate = Candidate(tmp_path)
    path = edited(tmp_path, "verified-kind.json", dict(candidate.verify, **edit))
    assert reasons(candidate.bind(verify_path=path))["verify_receipt"] == reason


def test_the_index_record_inside_the_verify_receipt_must_name_the_same_bytes(
    tmp_path,
):
    candidate = Candidate(tmp_path)
    verify = copy.deepcopy(candidate.verify)
    verify["release_index"]["sha256"] = "0" * 64
    path = edited(tmp_path, "verified-record.json", verify)
    assert (
        reasons(candidate.bind(verify_path=path))["verify_receipt"]
        == "verify_receipt_index_unproven"
    )


def test_an_unverified_receipt_leaves_the_queue_unproven_too(tmp_path):
    candidate = Candidate(tmp_path)
    receipt = candidate.bind(now=candidate.now + timedelta(hours=2))
    assert reasons(receipt)["queue"] == "verify_receipt_unproven"
    assert reasons(receipt)["source_binding"] == "verify_receipt_unproven"


def capture_diagnostics(receipt):
    row = receipt["subjects"]["captures"]
    diagnostics = (
        row["value"] if row["state"] != "bound" else row["evidence"]["diagnostics"]
    )
    return [item["reason"] for item in diagnostics]


def test_captures_follow_the_registry_selection_rules_at_binding_time(tmp_path):
    candidate = Candidate(tmp_path)
    assert reasons(candidate.bind(capture_scope=None)) == {
        "captures": "capture_scope_not_supplied"
    }
    wrong_scope = candidate.bind(capture_scope="production")
    assert reasons(wrong_scope) == {"captures": "capture_none_eligible:daily_za_ng_ke"}
    assert capture_diagnostics(wrong_scope) == ["scope_mismatch", "scope_mismatch"]
    later = dict(
        observation_window_end="2026-09-13T08:00:00Z",
        collection_started_at="2026-09-13T08:01:00Z",
        collection_completed_at="2026-09-13T08:02:00Z",
        snapshot_as_of="2026-09-13T08:03:00Z",
        capture_available_at="2026-09-13T09:00:00Z",
    )
    for entry, diagnostic in (
        (capture_entry(expires_at="2026-09-13T00:00:00Z"), "expired"),
        (capture_entry(**later), "not_yet_available"),
    ):
        path = edited(tmp_path, "capture-timed.json", [entry, collection_capture()])
        receipt = candidate.bind(captures_path=path)
        assert reasons(receipt) == {"captures": "capture_none_eligible:daily_za_ng_ke"}
        assert capture_diagnostics(receipt) == [diagnostic, "eligible"]
    conflict = edited(
        tmp_path,
        "capture-conflict.json",
        [
            capture_entry(),
            capture_entry(generation="2", source_digest="09" * 32),
            collection_capture(),
        ],
    )
    assert reasons(candidate.bind(captures_path=conflict)) == {
        "captures": "capture_profile_conflict"
    }
    first = candidate.bind()
    offset = edited(
        tmp_path,
        "capture-offset.json",
        [
            capture_entry(observation_window_end="2026-09-12T02:00:00+02:00"),
            collection_capture(),
        ],
    )
    same = candidate.bind(captures_path=offset)
    assert same["candidate_id"] == first["candidate_id"]


def weekly_entry(**overrides):
    return capture_entry(
        profile_id="weekly_za",
        observation_window_end="2026-09-11T00:00:00Z",
        collection_started_at="2026-09-11T00:05:00Z",
        collection_completed_at="2026-09-11T01:05:00Z",
        snapshot_as_of="2026-09-11T01:10:00Z",
        capture_available_at="2026-09-11T01:20:00Z",
        markets=["za"],
        **overrides,
    )


def test_the_unedited_registry_readback_binds_the_latest_eligible_capture(tmp_path):
    candidate = Candidate(tmp_path)
    first = candidate.bind()
    readback = edited(
        tmp_path,
        "capture-readback.json",
        [
            capture_entry(completion_state="failed", generation="1790000000000000"),
            capture_entry(),
            capture_entry(generation="1790000000000002", result_id="result-later"),
            capture_entry(expires_at="2026-09-13T00:00:00Z", profile_id="old_day"),
            capture_entry(profile_id="other_scope", scope="production"),
            weekly_entry(),
            collection_capture(),
        ],
    )
    receipt = candidate.bind(captures_path=readback)
    assert reasons(receipt) == {}
    selected = receipt["subjects"]["captures"]["value"]
    assert [(item["profile_id"], item["result_id"]) for item in selected] == [
        ("daily_za_ng_ke", "result-later"),
        (COLLECTION_PROFILE, "result-20260911"),
    ]
    assert capture_diagnostics(receipt) == [
        "not_succeeded:failed",
        "eligible",
        "eligible",
        "expired",
        "scope_mismatch",
        "eligible",
        "eligible",
    ]
    assert receipt["candidate_id"] != first["candidate_id"]
    both = candidate.bind(
        captures_path=readback, capture_profiles=("weekly_za", "daily_za_ng_ke")
    )
    assert [item["profile_id"] for item in both["subjects"]["captures"]["value"]] == [
        "weekly_za",
        "daily_za_ng_ke",
        COLLECTION_PROFILE,
    ]
    only = candidate.bind(captures_path=readback, capture_profiles=())
    assert reasons(only) == {}
    assert [item["profile_id"] for item in only["subjects"]["captures"]["value"]] == [
        COLLECTION_PROFILE
    ]
    assert reasons(
        candidate.bind(captures_path=readback, capture_profiles=("old_day",))
    ) == {"captures": "capture_none_eligible:old_day"}


@pytest.mark.parametrize(
    "bad",
    [
        {"junk": 1},
        "not an entry",
        None,
        "newer_unparsable",
    ],
)
def test_a_malformed_entry_anywhere_in_the_readback_leaves_captures_unproven(
    tmp_path, bad
):
    candidate = Candidate(tmp_path)
    if bad == "newer_unparsable":
        bad = capture_entry(
            generation="1790000000000009", capture_available_at="yesterday"
        )
    path = edited(tmp_path, "capture-malformed.json", [capture_entry(), bad])
    receipt = candidate.bind(captures_path=path)
    assert reasons(receipt) == {"captures": "capture_readback_malformed"}
    assert capture_diagnostics(receipt)[0] == "eligible"


def test_a_profile_conflict_is_unproven_and_shows_the_entries(tmp_path):
    candidate = Candidate(tmp_path)
    path = edited(
        tmp_path,
        "capture-conflict-2.json",
        [capture_entry(), capture_entry(generation="2", source_digest="09" * 32)],
    )
    receipt = candidate.bind(captures_path=path)
    assert reasons(receipt) == {"captures": "capture_profile_conflict"}
    assert [
        (item["profile_id"], item["generation"])
        for item in receipt["subjects"]["captures"]["value"]
    ] == [("daily_za_ng_ke", "1790000000000001"), ("daily_za_ng_ke", "2")]


def test_released_runs_refuse_future_duplicate_and_malformed_rows(tmp_path):
    candidate = Candidate(tmp_path)
    for rows, reason in (
        (
            [release_record(released_at="2026-09-14T00:00:00Z")],
            "release_record_from_future",
        ),
        ([release_record(released_at="yesterday")], "release_record_invalid"),
        ([release_record(run_id="Run With Spaces")], "release_record_invalid"),
        ([release_record(), release_record()], "release_record_duplicate"),
    ):
        path = edited(tmp_path, "records-bad.json", rows)
        assert reasons(candidate.bind(release_records_path=path)) == {
            "released_runs": reason
        }


@pytest.mark.parametrize(
    "profiles",
    [(None,), (None, "daily_za_ng_ke"), "daily_za_ng_ke", ("",), (" daily_za_ng_ke",)],
    ids=["none", "mixed", "bare_string", "empty", "padded"],
)
def test_required_capture_profiles_must_be_named_ids(tmp_path, profiles):
    candidate = Candidate(tmp_path)
    receipt = candidate.bind(capture_profiles=profiles)
    assert reasons(receipt) == {"captures": "capture_profiles_invalid"}


def test_the_required_profiles_are_recorded_with_the_captures(tmp_path):
    receipt = Candidate(tmp_path).bind()
    assert receipt["subjects"]["captures"]["evidence"]["required_profiles"] == [
        "daily_za_ng_ke",
        COLLECTION_PROFILE,
    ]


# Collection


def collection_reason(candidate, tmp_path, readback, name="readback.json"):
    path = write_json(tmp_path / name, readback)
    return reasons(candidate.bind(ingest_readback_path=path)).get("collection")


def test_the_readback_contract_is_the_ingest_readback_tool_contract():
    assert bind_candidate.INGEST_READBACK_CONTRACT == read_ingest_run.RECEIPT_VERSION


def test_collection_must_be_a_supplied_verified_readback_of_a_real_read(tmp_path):
    candidate = Candidate(tmp_path)
    assert reasons(candidate.bind(ingest_readback_path=None)) == {
        "collection": "ingest_readback_not_supplied"
    }
    assert collection_reason(
        candidate, tmp_path, ingest_readback(dry_run=True), "d.json"
    ) == ("ingest_readback_not_read")
    refused = ingest_readback(policy_sha256="b" * 64)
    assert refused["state"] == "refused"
    assert collection_reason(candidate, tmp_path, refused, "r.json") == (
        "ingest_readback_not_verified"
    )
    short = ingest_readback(raw_total=2)
    assert short["reasons"] == ["raw_count_mismatch"]
    assert collection_reason(candidate, tmp_path, short, "s.json") == (
        "ingest_readback_not_verified"
    )


def test_collection_needs_the_build_bound_as_the_daily_stage_binds_it(tmp_path):
    candidate = Candidate(tmp_path)
    readback = ingest_readback(build=None)
    assert readback["state"] == "verified"
    assert collection_reason(candidate, tmp_path, readback) == "ingest_build_not_bound"


def test_a_readback_of_another_contract_binds_nothing(tmp_path):
    candidate = Candidate(tmp_path)
    readback = {**ingest_readback(), "contract_version": "ingest_run_readback_v0"}
    assert collection_reason(candidate, tmp_path, readback) == (
        "ingest_readback_contract_mismatch"
    )


@pytest.mark.parametrize(
    "edit",
    [
        lambda r: r["receipt_row"].update(computed_sha256="0" * 64),
        lambda r: r["receipt_row"].update(row_count=2),
        lambda r: r["receipt_row"].update(admitted_by_daily_stage=False),
        lambda r: r["receipt_row"]["receipt"].update(
            execution_id="intelligence-42-ingest-staging-zzzzz"
        ),
        lambda r: r["receipt_row"]["receipt"].update(cutoff="2026-09-10"),
        lambda r: r["receipt_row"]["receipt"].update(policy_sha256="c" * 64),
        lambda r: r["receipt_row"]["receipt"].update(source_sha="d" * 40),
        lambda r: r["raw_content"].update(row_count_total=4),
        lambda r: r["raw_content"].update(pipeline_run_id="another-run"),
        lambda r: r.update(receipt_row=None),
        lambda r: r.update(raw_content=None),
    ],
)
def test_an_edited_verified_readback_is_inconsistent(tmp_path, edit):
    candidate = Candidate(tmp_path)
    readback = ingest_readback()
    edit(readback)
    assert readback["state"] == "verified"
    assert (
        collection_reason(candidate, tmp_path, readback)
        == "ingest_readback_inconsistent"
    )


def test_a_readback_recorded_after_the_binding_time_binds_nothing(tmp_path):
    candidate = Candidate(tmp_path)
    readback = ingest_readback()
    readback["receipt_row"]["recorded_at"] = "2027-01-01T00:00:00+00:00"
    assert (
        collection_reason(candidate, tmp_path, readback)
        == "ingest_readback_from_future"
    )


def test_another_collection_changes_the_candidate(tmp_path):
    candidate = Candidate(tmp_path)
    first = candidate.bind()
    readback = ingest_readback(run_id="run-20260912-again")
    other = write_json(tmp_path / "other.json", readback)
    # The other collection's own capture closed the other receipt.
    captures = write_json(
        tmp_path / "other-captures.json",
        [
            capture_entry(),
            collection_capture(source_digest=ledger_digest(readback)),
        ],
    )
    second = candidate.bind(ingest_readback_path=other, captures_path=captures)
    assert second["state"] == "bound"
    assert bind_candidate.changed_subjects(first, second) == ["collection", "captures"]


def resign(readback):
    """Give an edited receipt the digests of its own canonical text, as a readback
    that edited the receipt consistently would carry."""
    row = readback["receipt_row"]
    text = ingest_receipts.canonical_bytes(row["receipt"])
    row["receipt_sha256"] = row["computed_sha256"] = hashlib.sha256(text).hexdigest()
    return readback


def _set(path, value):
    def edit(readback):
        target = readback
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return edit


def _receipt_and_raw(**fields):
    """Edit the receipt and the raw summary together so the two still agree, keeping
    the digests the warehouse computed for the original text."""

    def edit(readback):
        receipt = readback["receipt_row"]["receipt"]
        raw = readback["raw_content"]
        if "run_id" in fields:
            receipt["run_id"] = raw["pipeline_run_id"] = fields["run_id"]
        if "raw" in fields:
            receipt["raw_rows_persisted"] = fields["raw"]
            raw["receipt_raw_rows_persisted"] = fields["raw"]
            raw["row_count_total"] = fields["raw"]
            raw["groups"][-1]["row_count"] = fields["raw"] - 2

    return edit


@pytest.mark.parametrize(
    "edit",
    [
        # Shapes that must refuse rather than raise.
        _set(("receipt_row", "receipt", "market_states"), ["za", "ng", "ke"]),
        _set(("receipt_row", "receipt", "market_states"), None),
        _set(("raw_content", "last_collected_by_market"), ["za"]),
        _set(("raw_content", "last_collected_by_market"), None),
        _set(("raw_content", "groups"), {"za": 1}),
        _set(("raw_content", "groups"), [["za", "gdelt", 1]]),
        _set(("expected",), None),
        _set(("cutoff",), 20260911),
        # Counts that are not integers.
        _set(("receipt_row", "row_count"), True),
        _set(("receipt_row", "row_count"), 1.0),
        _set(("raw_content", "row_count_total"), True),
        lambda r: r["raw_content"]["groups"][0].update(row_count=True),
        lambda r: r["raw_content"]["groups"][0].update(row_count=0),
        # A receipt edited after the read, with or without its digest redone.
        lambda r: r["receipt_row"]["receipt"].update(raw_rows_persisted=True),
        _receipt_and_raw(run_id="run-edited"),
        _receipt_and_raw(raw=40),
        lambda r: (
            resign(r)
            if r["receipt_row"]["receipt"].update(complete=False) is None
            else r
        ),
        lambda r: (
            resign(r)
            if r["receipt_row"]["receipt"].update(authority_kind="unverified") is None
            else r
        ),
        lambda r: (
            resign(r)
            if r["receipt_row"]["receipt"].update(market_states={"za": "collected"})
            is None
            else r
        ),
        # The expectations the readback carried, edited.
        lambda r: (
            resign(r)
            if (
                r["receipt_row"]["receipt"].update(policy_sha256="c" * 64),
                r["expected"].update(policy_sha256="c" * 64),
            )
            else r
        ),
        lambda r: r["expected"].update(image_uri=r["expected"]["image_uri"][:-1] + "b"),
        lambda r: r["expected"].update(trend_date="2026-09-13"),
        _set(("receipt_row", "trend_date"), "2026-09-13"),
        _set(("execution_id",), "intelligence-42-ingest-staging-zzzzz"),
        _set(("receipt_row", "receipt_sha256"), "AB" * 32),
        # Raw rows that do not agree with the receipt or the window.
        lambda r: r["raw_content"]["groups"].pop(),
        lambda r: r["raw_content"]["groups"][0].update(
            last_collected_at="2026-09-20T00:00:00+00:00"
        ),
        lambda r: r["raw_content"]["last_collected_by_market"].pop("ke"),
    ],
)
def test_a_verified_readback_that_the_tool_would_not_have_written_is_inconsistent(
    tmp_path, edit
):
    candidate = Candidate(tmp_path)
    readback = ingest_readback()
    edit(readback)
    assert readback["state"] == "verified"
    assert (
        collection_reason(candidate, tmp_path, readback)
        == "ingest_readback_inconsistent"
    )


def test_a_verified_state_with_reasons_is_not_verified(tmp_path):
    candidate = Candidate(tmp_path)
    readback = {**ingest_readback(), "reasons": ["raw_count_mismatch"]}
    assert (
        collection_reason(candidate, tmp_path, readback)
        == "ingest_readback_not_verified"
    )


def test_raw_rows_collected_after_the_binding_time_bind_nothing(tmp_path):
    candidate = Candidate(tmp_path)
    readback = ingest_readback()
    for group in readback["raw_content"]["groups"]:
        group["last_collected_at"] = "2026-09-13T23:00:00+00:00"
    readback["raw_content"]["last_collected_by_market"] = dict.fromkeys(
        ("ke", "ng", "za"), "2026-09-13T23:00:00+00:00"
    )
    readback["receipt_row"]["recorded_at"] = "2026-09-12T05:00:00+00:00"
    assert candidate.now < datetime(2026, 9, 13, 23, tzinfo=UTC)
    assert (
        collection_reason(candidate, tmp_path, readback)
        == "ingest_readback_from_future"
    )


def _whole_raw_summary_at(stamp):
    def edit(readback):
        raw = readback["raw_content"]
        for group in raw["groups"]:
            group["last_collected_at"] = stamp
        raw["last_collected_by_market"] = dict.fromkeys(
            raw["last_collected_by_market"], stamp
        )

    return edit


def _extra_group(**group):
    def edit(readback):
        raw = readback["raw_content"]
        raw["groups"].append(group)
        raw["last_collected_by_market"][group["market"]] = group["last_collected_at"]
        raw["last_collected_by_market"] = dict(
            sorted(raw["last_collected_by_market"].items())
        )

    return edit


def _partial_but_consistent(readback):
    """A partial collection the tool refused, recorded as verified: every digest and
    raw figure still agrees, so only the tool's reasons tell it apart."""
    receipt = readback["receipt_row"]["receipt"]
    receipt["market_states"] = {"za": "collected", "ng": "partial", "ke": "collected"}
    receipt["complete"] = False
    resign(readback)


@pytest.mark.parametrize(
    "edit",
    [
        _whole_raw_summary_at("2026-09-11T10:00:00+00:00"),
        _extra_group(
            market="zz",
            source="gdelt",
            row_count=0,
            last_collected_at="2026-09-12T04:10:00+00:00",
        ),
        lambda r: r["raw_content"]["groups"][0].update(source=5),
        _partial_but_consistent,
        lambda r: r["expected"].update(image_uri="x"),
        lambda r: r["expected"].update(source_sha="not-a-sha"),
        _set(("cutoff",), "20260911"),
        _set(
            ("tables", "receipts"),
            "other-project.intelligence_42_sources_staging.collection_receipts",
        ),
    ],
)
def test_edits_that_survive_the_later_checks_are_still_inconsistent(tmp_path, edit):
    candidate = Candidate(tmp_path)
    readback = ingest_readback()
    edit(readback)
    assert readback["state"] == "verified"
    assert (
        collection_reason(candidate, tmp_path, readback)
        == "ingest_readback_inconsistent"
    )


def test_values_no_digest_covers_stay_out_of_the_identity(tmp_path):
    candidate = Candidate(tmp_path)
    first = candidate.bind()
    readback = ingest_readback()
    readback["receipt_row"]["recorded_at"] = "2026-09-12T04:45:00+00:00"
    _whole_raw_summary_at("2026-09-12T04:20:00+00:00")(readback)
    second = candidate.bind(
        ingest_readback_path=write_json(tmp_path / "moved.json", readback)
    )
    assert second["state"] == "bound"
    assert second["candidate_id"] == first["candidate_id"]
    evidence = second["subjects"]["collection"]["evidence"]
    assert evidence["recorded_at"] == "2026-09-12T04:45:00.000000Z"
    assert evidence["last_collected_at"] == "2026-09-12T04:20:00.000000Z"
    assert "recorded_at" not in second["subjects"]["collection"]["value"]


def test_a_raw_count_the_tool_refused_recorded_as_verified_is_inconsistent(tmp_path):
    candidate = Candidate(tmp_path)
    readback = ingest_readback()
    readback["receipt_row"]["receipt"]["raw_rows_persisted"] = 4
    readback["raw_content"]["receipt_raw_rows_persisted"] = 4
    resign(readback)
    assert (
        collection_reason(candidate, tmp_path, readback)
        == "ingest_readback_inconsistent"
    )


def test_raw_rows_after_the_run_window_are_inconsistent_long_after_the_run(tmp_path):
    candidate = Candidate(tmp_path)
    readback = ingest_readback()
    _whole_raw_summary_at("2026-09-14T01:00:00+00:00")(readback)
    later = candidate.now + timedelta(days=5)
    path = write_json(tmp_path / "late.json", readback)
    row = candidate.bind(ingest_readback_path=path, now=later)["subjects"]["collection"]
    assert row["reason"] == "ingest_readback_inconsistent"


@pytest.mark.parametrize("recorded_at", [None, "not a time"])
def test_a_missing_or_unreadable_record_time_is_inconsistent(tmp_path, recorded_at):
    candidate = Candidate(tmp_path)
    readback = ingest_readback()
    readback["receipt_row"]["recorded_at"] = recorded_at
    assert (
        collection_reason(candidate, tmp_path, readback)
        == "ingest_readback_inconsistent"
    )


def test_a_refused_state_with_no_reasons_is_not_verified(tmp_path):
    candidate = Candidate(tmp_path)
    readback = {**ingest_readback(), "state": "refused"}
    assert (
        collection_reason(candidate, tmp_path, readback)
        == "ingest_readback_not_verified"
    )


def test_the_readback_fields_are_the_ones_the_tool_writes():
    assert set(ingest_readback()) == bind_candidate.INGEST_READBACK_FIELDS


@pytest.mark.parametrize(
    "edit",
    [
        lambda r: r["receipt_row"].update(admitted_by_daily_stage=1),
        lambda r: r["raw_content"].update(row_count_total=3.0),
        lambda r: r["raw_content"].update(receipt_raw_rows_persisted=3.0),
        lambda r: r["raw_content"]["groups"].reverse(),
        lambda r: r.update(note="added by hand"),
        lambda r: r["receipt_row"].update(recorded_at="2026-09-12T04:30:00Z"),
        lambda r: r["receipt_row"].update(recorded_at="2026-09-12T06:30:00+02:00"),
        _whole_raw_summary_at("2026-09-12T04:10:00Z"),
        _whole_raw_summary_at("2026-09-14T00:00:00+00:00"),
    ],
)
def test_spellings_the_tool_never_writes_are_inconsistent(tmp_path, edit):
    candidate = Candidate(tmp_path)
    readback = ingest_readback()
    edit(readback)
    assert (
        collection_reason(candidate, tmp_path, readback)
        == "ingest_readback_inconsistent"
    )


def test_a_group_at_the_window_start_binds(tmp_path):
    candidate = Candidate(tmp_path)
    readback = ingest_readback()
    _whole_raw_summary_at("2026-09-12T00:00:00+00:00")(readback)
    assert collection_reason(candidate, tmp_path, readback) is None


def test_the_latest_collection_time_across_groups_is_the_evidence(tmp_path):
    candidate = Candidate(tmp_path)
    readback = ingest_readback()
    raw = readback["raw_content"]
    times = [
        "2026-09-12T04:10:00+00:00",
        "2026-09-12T04:40:00+00:00",
        "2026-09-12T04:20:00+00:00",
    ]
    for group, stamp in zip(raw["groups"], times, strict=True):
        group["last_collected_at"] = stamp
        raw["last_collected_by_market"][group["market"]] = stamp
    receipt = candidate.bind(
        ingest_readback_path=write_json(tmp_path / "t.json", readback)
    )
    evidence = receipt["subjects"]["collection"]["evidence"]
    assert evidence["last_collected_at"] == "2026-09-12T04:40:00.000000Z"


def test_a_different_receipt_under_the_same_run_changes_the_candidate(tmp_path):
    candidate = Candidate(tmp_path)
    first = candidate.bind()
    other = ingest_readback(enriched_count=2)
    captures = write_json(
        tmp_path / "e-captures.json",
        [
            capture_entry(),
            collection_capture(source_digest=ledger_digest(other)),
        ],
    )
    second = candidate.bind(
        ingest_readback_path=write_json(tmp_path / "e.json", other),
        captures_path=captures,
    )
    assert second["state"] == "bound"
    assert (
        second["subjects"]["collection"]["value"]["receipt_sha256"]
        != first["subjects"]["collection"]["value"]["receipt_sha256"]
    )
    assert second["candidate_id"] != first["candidate_id"]


# Captures required by the bound collection


def test_the_collection_names_the_capture_it_answers_from(tmp_path):
    candidate = Candidate(tmp_path)
    receipt = candidate.bind()
    captures = receipt["subjects"]["captures"]
    assert receipt["state"] == "bound"
    assert [item["profile_id"] for item in captures["value"]] == [
        "daily_za_ng_ke",
        COLLECTION_PROFILE,
    ]
    assert captures["evidence"]["collection_profile"] == COLLECTION_PROFILE
    assert captures["evidence"]["required_profiles"] == sorted(
        ["daily_za_ng_ke", COLLECTION_PROFILE]
    )


def test_the_collection_capture_is_required_without_any_named_profile(tmp_path):
    candidate = Candidate(tmp_path)
    receipt = candidate.bind(capture_profiles=())
    assert reasons(receipt) == {}
    assert [
        item["profile_id"] for item in receipt["subjects"]["captures"]["value"]
    ] == [COLLECTION_PROFILE]


def test_without_a_bound_collection_captures_need_named_profiles(tmp_path):
    candidate = Candidate(tmp_path)
    receipt = candidate.bind(capture_profiles=(), ingest_readback_path=None)
    assert reasons(receipt) == {
        "collection": "ingest_readback_not_supplied",
        "captures": "capture_profiles_not_supplied",
    }


def test_a_missing_collection_capture_leaves_captures_unproven(tmp_path):
    candidate = Candidate(tmp_path)
    path = write_json(tmp_path / "only-named.json", [capture_entry()])
    assert reasons(candidate.bind(captures_path=path)) == {
        "captures": "capture_none_eligible:" + COLLECTION_PROFILE
    }


def test_a_collection_capture_under_another_full_policy_is_unproven(tmp_path):
    candidate = Candidate(tmp_path)
    other = read_ingest_run.EXPECTED_POLICY_SHA256[:16] + "0" * 48
    path = write_json(
        tmp_path / "other-policy.json",
        [capture_entry(), collection_capture(policy_digest=other)],
    )
    assert reasons(candidate.bind(captures_path=path)) == {
        "captures": "capture_collection_policy_mismatch"
    }


def test_the_capture_scope_is_never_defaulted(tmp_path):
    candidate = Candidate(tmp_path)
    path = write_json(
        tmp_path / "default-scope.json", [collection_capture(scope="ogilvy_default")]
    )
    receipt = candidate.bind(
        captures_path=path, capture_scope=None, capture_profiles=()
    )
    assert reasons(receipt) == {"captures": "capture_scope_not_supplied"}


@pytest.mark.parametrize(
    "collection",
    [
        {},
        {"cutoff": "2026-09-11"},
        {"cutoff": "11 September", "policy_sha256": "a" * 64},
        {"cutoff": "2026-09-11", "policy_sha256": "short"},
        {"cutoff": None, "policy_sha256": "a" * 64},
    ],
    ids=["empty", "no_policy", "bad_cutoff", "bad_policy", "none_cutoff"],
)
def test_a_malformed_collection_value_leaves_captures_unproven(collection):
    row = bind_candidate._captures_row(
        [collection_capture()],
        scope="staging",
        profiles=(),
        now=datetime(2026, 9, 12, 6, tzinfo=UTC),
        collection=collection,
    )
    assert row["state"] == "unproven"
    assert row["reason"] == "capture_collection_profile_invalid"


def test_a_bound_collection_needs_its_daily_attempt_id(tmp_path):
    candidate = Candidate(tmp_path)
    assert reasons(candidate.bind(collection_attempt_id=None)) == {
        "captures": "collection_attempt_not_supplied"
    }


@pytest.mark.parametrize(
    "attempt",
    ["", " ", " attempt:collect:20260912", 7],
    ids=["empty", "blank", "padded", "int"],
)
def test_the_collection_attempt_id_must_be_a_named_id(tmp_path, attempt):
    candidate = Candidate(tmp_path)
    assert reasons(candidate.bind(collection_attempt_id=attempt)) == {
        "captures": "collection_attempt_invalid"
    }


def test_a_capture_that_closed_another_attempt_is_unproven(tmp_path):
    candidate = Candidate(tmp_path)
    assert reasons(candidate.bind(collection_attempt_id="attempt:collect:other")) == {
        "captures": "capture_collection_receipt_mismatch"
    }


def test_the_capture_of_a_retried_execution_is_unproven(tmp_path):
    # A retried execution of the same cutoff under the same policy registers the same
    # profile id; only the source digest names which receipt the capture closed.
    candidate = Candidate(tmp_path)
    retry = ingest_readback(run_id="run-20260912-retry")
    receipt = candidate.bind(
        ingest_readback_path=write_json(tmp_path / "retry.json", retry)
    )
    assert reasons(receipt) == {"captures": "capture_collection_receipt_mismatch"}


def test_the_bound_capture_names_the_receipt_and_attempt_it_closed(tmp_path):
    receipt = Candidate(tmp_path).bind()
    captures = receipt["subjects"]["captures"]
    closing = [c for c in captures["value"] if c["profile_id"] == COLLECTION_PROFILE]
    assert [c["source_digest"] for c in closing] == [ledger_digest(ingest_readback())]
    assert captures["evidence"]["collection_attempt_id"] == COLLECTION_ATTEMPT


def test_a_collection_without_its_receipt_leaves_captures_unproven():
    row = bind_candidate._captures_row(
        [collection_capture()],
        scope="staging",
        profiles=(),
        now=datetime(2026, 9, 12, 6, tzinfo=UTC),
        collection={
            "cutoff": "2026-09-11",
            "policy_sha256": read_ingest_run.EXPECTED_POLICY_SHA256,
        },
        collection_receipt=None,
        collection_attempt=COLLECTION_ATTEMPT,
    )
    assert row["reason"] == "capture_collection_profile_invalid"
