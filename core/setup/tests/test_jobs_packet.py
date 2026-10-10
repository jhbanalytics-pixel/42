"""The packet command line in mode jobs (core/setup/release/packet.py): capture-baseline-j, bind, lock, review and receipt for Release B,
the command that gave Release A its packet files and that the jobs release had only as functions (packet flag F12). Offline: the gcloud
binary is a stand-in, the repository is a throwaway one, and nothing here starts pwsh."""
import hashlib
import json
import re
from pathlib import Path

import pytest

from core.setup import durable_effects_check as dec
from core.setup.release import bound_readback as helper
from core.setup.release import chain_evidence as ce
from core.setup.release import jobs_baseline as jb
from core.setup.release import jobs_only as jo
from core.setup.release import lock as locklib
from core.setup.release import packet
from core.setup.release import services_only as so
from core.setup.tests import jobs_packet_world as jpw
from core.setup.tests import jobs_world as jw
from core.setup.tests import packet_world as pw
from core.setup.tests import release_world as rw

ROOT = pw.ROOT
JOBS_PASTE = ROOT / "core/setup/release/JOBS-PASTE.ps1"
ROLLBACK = jw.ROLLBACK_DIGEST


@pytest.fixture
def bench(tmp_path, monkeypatch):
    return jpw.JobsBench(tmp_path, monkeypatch)


def run(argv, capsys):
    code = packet.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def captured(bench, capsys):
    bench.write_a_readback()
    code, out, err = run(bench.capture_argv(), capsys)
    assert code == 0, err
    return bench


def with_producer(bench, capsys):
    captured(bench, capsys)
    code, out, err = run(bench.producer_argv(), capsys)
    assert code == 0, err
    return bench


def prepared(bench, capsys):
    with_producer(bench, capsys)
    bench.write_chain()
    bench.write_durable()
    bench.write_dry_run()
    return bench


def bound_bench(bench, capsys):
    prepared(bench, capsys)
    code, out, err = run(bench.bind_argv(), capsys)
    assert code == 0, err
    return bench


def locked_bench(bench, capsys):
    bound_bench(bench, capsys)
    code, out, err = run(bench.lock_argv(), capsys)
    assert code == 0, err
    return bench


def reviewed_bench(bench, capsys):
    locked_bench(bench, capsys)
    code, out, err = run(bench.review_argv(), capsys)
    assert code == 0, err
    return bench


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# capture-baseline-j

def test_capture_baseline_j_writes_the_receipt_the_library_function_returns_and_prints_its_hash(bench, capsys):
    captured(bench, capsys)
    value = bench.baseline_value()
    expected = jb.capture_baseline_j(rw.FakeReader(bench.world), a_terminal={"kind": "AfterPromotion", "at_utc": jw.A_TERMINAL_AT, "a_release_id": jw.A_RID},
                                     a80_serving=None, rollback_digest=ROLLBACK, a_retire="waits_for_b", captured_utc=value["captured_utc"])
    assert value == expected
    assert value["kind"] == "baseline-J" and value["captured_utc"] == jpw.NOW.replace(microsecond=0).isoformat()


def test_capture_baseline_j_prints_the_path_and_the_hash_and_nothing_else_of_the_world(bench, capsys):
    bench.write_a_readback()
    code, out, err = run(bench.capture_argv(), capsys)
    assert code == 0 and str(bench.baseline) in out and sha(bench.baseline) in out
    for name in rw.SYNTHETIC_DECLARED:
        assert name not in out + err


def test_capture_baseline_j_asks_gcloud_only_the_read_commands_it_needs(bench, capsys):
    captured(bench, capsys)
    asked = pw.calls(bench.log)
    assert asked
    for args in asked:
        assert any(tuple(args[:len(prefix)]) == prefix for prefix in helper.READ_PREFIXES), args
        assert tuple(args[:3]) in {("run", "services", "describe"), ("run", "revisions", "describe"), ("run", "revisions", "list"),
                                   ("run", "jobs", "describe")}, args
    assert len([a for a in asked if a[:3] == ["run", "jobs", "describe"]]) == len(so.JOB_NAMES)


def test_capture_baseline_j_never_overwrites_an_existing_file_and_finds_it_before_any_read(bench, capsys):
    bench.write_a_readback()
    bench.baseline.parent.mkdir(parents=True)
    bench.baseline.write_text("kept" + chr(10), encoding="utf-8")
    code, out, err = run(bench.capture_argv(), capsys)
    assert code == 1 and "REFUSED" in err and bench.baseline.read_text(encoding="utf-8") == "kept" + chr(10)
    assert pw.calls(bench.log) == []


@pytest.mark.parametrize("change", [
    {"phase": "BeforePromotion"}, {"phase": "AfterSmoke"}, {"mode": "jobs"}, {"schema_version": 2}, {"release_id": "rel-nonsense"},
    {"at_utc": "2026-10-09T10:00:00"}, {"at_utc": "yesterday"}, {"at_utc": "2999-01-01T00:00:00+00:00"},
], ids=["before_promotion", "after_smoke", "wrong_mode", "version", "release_id", "naive_time", "not_a_time", "after_the_capture"])
def test_capture_baseline_j_refuses_a_readback_that_is_not_a_terminal_readback_of_release_a_and_writes_nothing(bench, capsys, change):
    bench.write_a_readback(**change)
    code, out, err = run(bench.capture_argv(), capsys)
    assert code == 1 and "REFUSED" in err and not bench.baseline.exists()


def test_capture_baseline_j_refuses_a_missing_readback_before_any_read(bench, capsys):
    code, out, err = run(bench.capture_argv(), capsys)
    assert code == 1 and "REFUSED" in err and pw.calls(bench.log) == [] and not bench.baseline.exists()


def test_capture_baseline_j_refuses_jobs_that_are_not_on_the_pinned_rollback_digest_and_writes_nothing(tmp_path, monkeypatch, capsys):
    bench = jpw.JobsBench(tmp_path, monkeypatch)
    jw.set_job_image(bench.world, "f42-probe", "sha256:" + "ab" * 32)
    pw.fake_gcloud(bench.tmp / "fake", bench.world)
    bench.write_a_readback()
    code, out, err = run(bench.capture_argv(), capsys)
    assert code == 1 and "REFUSED" in err and not bench.baseline.exists()


def test_capture_baseline_j_refuses_a_retire_declaration_the_tag_set_contradicts(bench, capsys):
    bench.write_a_readback()
    code, out, err = run(bench.capture_argv(**{"--a-retire": "retired"}), capsys)
    assert code == 1 and "Retire" in err and not bench.baseline.exists()


def test_capture_baseline_j_refuses_an_a_that_was_rolled_back_while_the_candidates_still_serve(bench, capsys):
    bench.write_a_readback(phase="AfterRollback")
    code, out, err = run(bench.capture_argv(), capsys)
    assert code == 1 and "REFUSED" in err and not bench.baseline.exists()


def test_capture_baseline_j_a_read_that_cannot_complete_exits_3_and_writes_nothing(bench, capsys):
    bench.argv, bench.log = pw.fake_gcloud(bench.tmp / "fake2", bench.world, fail_on="f42-gdelt")
    bench.monkeypatch.setattr(helper, "gcloud_command", lambda: bench.argv)
    bench.write_a_readback()
    code, out, err = run(bench.capture_argv(), capsys)
    assert code == 3 and "PROBE" in err and not bench.baseline.exists()


def test_capture_baseline_j_needs_a_timeout_a_destination_a_readback_and_a_retire_declaration(bench, capsys):
    bench.write_a_readback()
    good = bench.capture_argv()
    for drop in ("--out", "--gcloud-timeout-seconds", "--a-readback", "--a-retire"):
        argv = list(good)
        index = argv.index(drop)
        del argv[index:index + 2]
        with pytest.raises(SystemExit) as stopped:
            packet.main(argv)
        assert stopped.value.code == 2, drop
    with pytest.raises(SystemExit):
        packet.main(bench.capture_argv(**{"--a-retire": "maybe"}))


# bind: the key set, and where each value comes from

def paste_bound_properties():
    text = JOBS_PASTE.read_text(encoding="utf-8")
    named = set(re.findall(r"\$(?:Script:)?[Bb]ound\.([A-Za-z_]+)", text)) - {"PSObject"}
    listed = re.search(r"foreach \(\$key in @\(([^)]*)\)\)", text)
    return named | set(re.findall(r"'([A-Za-z]+)'", listed.group(1)))


def library_bound_keys():
    """Every key the python half reads out of the bindings, found in the source of the modules that read them."""
    keys = set()
    for name in ("jobs_only", "jobs_run", "jobs_freeze", "chain_evidence", "bound_readback", "jobs_source"):
        text = (ROOT / "core/setup/release" / f"{name}.py").read_text(encoding="utf-8")
        keys |= set(re.findall(r"(?:bound|self\.bound|rel\.bound)\[\"([A-Za-z0-9_]+)\"\]", text))
        keys |= set(re.findall(r"(?:bound|self\.bound|rel\.bound)\.get\(\"([A-Za-z0-9_]+)\"", text))
    return keys


def test_bind_writes_exactly_the_key_set_the_paste_and_the_python_half_read_and_the_validator_requires(bench, capsys):
    bound_bench(bench, capsys)
    value = bench.bindings_value()
    required = set(jo.RELEASE_BINDINGS) | {"schema_version", "mode", "status"}
    assert set(value) == required
    assert paste_bound_properties() <= required, paste_bound_properties() - required
    assert library_bound_keys() <= required, library_bound_keys() - required
    assert {"release_id", "target", "tree", "releaseDir", "baselinePath", "baselineChainPath", "durableManifestPath", "dryRunReceiptPath",
            "schemaReadbackReceiptPath", "callerAccount"} <= paste_bound_properties()
    jo.validate_jobs_bindings(value)


def test_bind_marks_the_bindings_jobs_mode_and_ready_for_independent_review(bench, capsys):
    bound_bench(bench, capsys)
    value = bench.bindings_value()
    assert value["mode"] == "jobs" and value["status"] == "BOUND_FOR_INDEPENDENT_REVIEW" and value["schema_version"] == 1
    assert value["window"] == jo.PINNED_WINDOW and value["priorAttempts"] == [] and value["priorLedgers"] == []


def test_bind_takes_each_value_from_its_source_not_from_the_caller(bench, capsys):
    bound_bench(bench, capsys)
    value = bench.bindings_value()
    assert value["target"] == bench.target == pw.head(bench.repo) and value["tree"] == bench.tree == pw.git(bench.repo, "rev-parse", "HEAD^{tree}")
    assert value["release_id"] == f"rel-{value['target'][:7]}-01"
    assert value["releaseDir"] == str(bench.packet.resolve())
    assert value["baselinePath"] == str(bench.baseline.resolve()) and value["baselineSha256"] == sha(bench.baseline)
    assert value["baselineChainPath"] == str(bench.chain_path.resolve()) and value["baselineChainSha256"] == sha(bench.chain_path)
    assert value["durableManifestPath"] == str(bench.durable.resolve()) and value["durableManifestSha256"] == sha(bench.durable)
    assert value["dryRunReceiptPath"] == str(bench.dry_run.resolve()) and value["dryRunReceiptSha256"] == sha(bench.dry_run)
    assert value["schemaReadbackReceiptPath"] == str((bench.packet / "schema-readback-receipt.json").resolve())
    assert value["rollbackJobsDigest"] == ROLLBACK == packet.ROLLBACK_JOBS_DIGEST
    assert value["templateHashes"] == ce.template_hashes() and value["quietTemplateHashes"] == jo.quiet_template_hashes()
    assert value["callerAccount"] == jpw.CALLER and value["readTimeoutSeconds"] == {"gcloud": 120, "http": 30}
    assert value["chainEvidenceBytesCap"] == 67108864 and value["collectStartToleranceMinutes"] == 10
    assert value["maxBaselineAgeDays"] == 2 and value["maxCandidateAgeHours"] == 12
    from core.setup import deploy_jobs as dj

    assert value["buildServiceAccount"] == dj.DEPLOYER
    assert value["buildConfigSha256"] == packet.blob_sha256(bench.repo, bench.target, "core/setup/cloudbuild.jobs.yaml")
    assert value["dockerfileSha256"] == packet.blob_sha256(bench.repo, bench.target, "core/setup/jobs.Dockerfile")


def test_bind_derives_the_schema_receipt_hash_from_the_durable_manifest_and_not_from_the_caller(bench, capsys):
    bound_bench(bench, capsys)
    value = bench.bindings_value()
    expected = jo.schema_receipt_body(value, json.loads(bench.durable.read_text(encoding="utf-8")))
    assert value["schemaReadbackReceiptSha256"] == so.fingerprint(expected)
    assert expected["effects"], "B's durable manifest carries schema effects, so the receipt must too"
    assert not (bench.packet / "schema-readback-receipt.json").exists(), "the receipt is JobsCandidate's to write"


def test_bind_pins_the_rollback_digest_instead_of_reading_it_from_the_baseline(bench, capsys):
    assert packet.ROLLBACK_JOBS_DIGEST == "sha256:e77c3819cc688d7690d4370a5fd575026e4d3e62c0ab322d54b6eabb515c75de"
    captured(bench, capsys)
    value = json.loads(bench.baseline.read_text(encoding="utf-8"))
    value["rollbackJobsDigest"] = "sha256:" + "ab" * 32
    for name in value["jobs"]:
        value["jobs"][name]["image"] = f"{jo.JOBS_REPO}@sha256:" + "ab" * 32
    bench.baseline.write_text(json.dumps(value), encoding="utf-8")
    code, out, err = run(bench.producer_argv(), capsys)
    assert code == 1 and "REFUSED" in err and "rollback" in err.lower()


def test_bind_output_is_accepted_by_the_release_readback_before_any_write(bench, capsys):
    bound_bench(bench, capsys)
    value = bench.bindings_value()
    release = jo.JobsRelease(value, rw.FakeReader(bench.world), bench.tmp / "evidence", now=lambda: jpw.NOW)
    assert release.rid == value["release_id"] and release.baseline["kind"] == "baseline-J"


def test_bind_runs_the_baseline_chain_checks_of_the_acting_path_on_the_manifest(bench, capsys):
    bound_bench(bench, capsys)
    value = bench.bindings_value()
    release = jo.JobsRelease(value, rw.FakeReader(bench.world), bench.tmp / "evidence", now=lambda: jpw.NOW)
    jo.check_baseline_chain(release)
    assert release.observations["baseline_chain"]["sha256"] == value["baselineChainSha256"]


def refuse(bench, capsys, argv=None):
    code, out, err = run(argv or bench.bind_argv(), capsys)
    assert code == 1 and "REFUSED" in err, err
    assert not bench.bindings.exists()
    return err


@pytest.mark.parametrize("name,change", [
    ("a commit that is not HEAD", {"--release-commit": "0" * 40}),
    ("a short commit", {"--release-commit": "a" * 12}),
    ("an attempt other than the first", {"--attempt": "02"}),
    ("an attempt that is not two digits", {"--attempt": "1"}),
    ("a zero timeout", {"--gcloud-timeout-seconds": "0"}),
    ("a zero bytes cap", {"--chain-evidence-bytes-cap": "0"}),
    ("a tolerance above the cap", {"--collect-start-tolerance-minutes": "31"}),
    ("a negative tolerance", {"--collect-start-tolerance-minutes": "-1"}),
    ("a zero baseline age", {"--max-baseline-age-days": "0"}),
    ("a zero candidate age", {"--max-candidate-age-hours": "0"}),
])
def test_bind_refuses_an_unusable_value_and_writes_no_bindings(bench, capsys, name, change):
    prepared(bench, capsys)
    try:
        code, out, err = run(bench.bind_argv(**change), capsys)
    except SystemExit as stopped:
        code = stopped.code
    assert code in (1, 2), name
    assert not bench.bindings.exists(), name


def test_bind_refuses_a_dirty_checkout(bench, capsys):
    prepared(bench, capsys)
    (bench.repo / "core/setup/release/plan.py").write_text("changed" + chr(10), encoding="utf-8")
    assert "clean" in refuse(bench, capsys)


def test_bind_refuses_a_file_that_is_not_baseline_j(bench, capsys):
    prepared(bench, capsys)
    value = bench.baseline_value()
    value["kind"] = "baseline-A"
    bench.baseline.write_text(json.dumps(value), encoding="utf-8")
    assert "baseline" in refuse(bench, capsys).lower()


def test_bind_refuses_a_baseline_whose_jobs_are_not_all_on_the_pinned_digest(bench, capsys):
    prepared(bench, capsys)
    value = bench.baseline_value()
    value["jobs"]["f42-digest"]["image"] = f"{jo.JOBS_REPO}@sha256:" + "cd" * 32
    bench.baseline.write_text(json.dumps(value), encoding="utf-8")
    assert "rollback digest" in refuse(bench, capsys)


def test_bind_refuses_a_baseline_chain_manifest_that_does_not_belong_to_this_baseline_and_digest(bench, capsys):
    prepared(bench, capsys)
    value = json.loads(bench.chain_path.read_text(encoding="utf-8"))
    value["role"] = "first-b"
    bench.chain_path.write_text(json.dumps(value), encoding="utf-8")
    assert "baseline" in refuse(bench, capsys).lower()


def test_bind_refuses_a_baseline_chain_manifest_that_does_not_qualify(bench, capsys):
    with_producer(bench, capsys)
    bench.fixture.set_counts("understand", partial=True)
    bench.write_chain()
    bench.write_durable()
    bench.write_dry_run()
    assert "baseline" in refuse(bench, capsys).lower()


def test_bind_refuses_a_baseline_chain_manifest_that_is_too_old(bench, capsys, monkeypatch):
    prepared(bench, capsys)
    monkeypatch.setattr(packet, "utc_now", lambda: jpw.NOW.replace(day=20))
    assert "age" in refuse(bench, capsys).lower()


def test_bind_refuses_a_durable_manifest_the_checker_does_not_accept(bench, capsys):
    prepared(bench, capsys)
    value = json.loads(bench.durable.read_text(encoding="utf-8"))
    value["effects"][0]["destructive"] = True
    value["manifest_sha256"] = dec.manifest_hash(value)
    bench.durable.write_text(json.dumps(value), encoding="utf-8")
    assert "durable" in refuse(bench, capsys)


def test_bind_refuses_a_durable_manifest_made_for_another_release(bench, capsys):
    prepared(bench, capsys)
    value = json.loads(bench.durable.read_text(encoding="utf-8"))
    value["release_id"] = "rel-0badf00-01"
    value["manifest_sha256"] = dec.manifest_hash(value)
    bench.durable.write_text(json.dumps(value), encoding="utf-8")
    assert "another release" in refuse(bench, capsys)


@pytest.mark.parametrize("how", ["drift", "failed", "skipped", "no_tail", "empty"])
def test_bind_refuses_a_dry_run_receipt_that_is_not_a_clean_dry_run_of_the_views_of_this_commit(bench, capsys, how):
    prepared(bench, capsys)
    from core.schema import apply

    view = next(apply.statement_name(s) for s in apply.load_statements() if apply.kind(s) == "view")
    text = {"drift": jw.dry_run_receipt_text(drift=[view]), "failed": jw.dry_run_receipt_text(failed=view), "skipped": jw.dry_run_receipt_text(skipped=[view]),
            "no_tail": jw.dry_run_receipt_text(tail=""), "empty": ""}[how]
    bench.write_dry_run(text)
    assert "dry run" in refuse(bench, capsys).lower()


@pytest.mark.parametrize("which", ["durable", "dry_run", "chain"])
def test_bind_refuses_a_bound_file_outside_the_release_directory(bench, capsys, which):
    prepared(bench, capsys)
    elsewhere = bench.tmp / "elsewhere" / "file.json"
    elsewhere.parent.mkdir(parents=True)
    source = {"durable": bench.durable, "dry_run": bench.dry_run, "chain": bench.chain_path}[which]
    elsewhere.write_bytes(source.read_bytes())
    flag = {"durable": "--durable-manifest", "dry_run": "--dry-run-receipt", "chain": "--baseline-chain"}[which]
    assert "release directory" in refuse(bench, capsys, bench.bind_argv(**{flag: elsewhere}))


def test_bind_never_overwrites_existing_bindings(bench, capsys):
    bound_bench(bench, capsys)
    before = bench.bindings.read_bytes()
    code, out, err = run(bench.bind_argv(), capsys)
    assert code == 1 and "REFUSED" in err and bench.bindings.read_bytes() == before


def test_bind_prints_only_paths_and_digests(bench, capsys):
    prepared(bench, capsys)
    code, out, err = run(bench.bind_argv(), capsys)
    assert code == 0 and sha(bench.bindings) in out and str(bench.bindings) in out
    for name in rw.SYNTHETIC_DECLARED:
        assert name not in out + err


def test_bind_without_a_mode_is_still_the_services_bind(bench, capsys):
    argv = ["bind"] + [a for a in bench.bind_argv()[1:] if a not in ("--mode", "jobs")]
    with pytest.raises(SystemExit) as stopped:
        packet.main(argv)
    assert stopped.value.code == 2


# bind --producer-only: the file the chain evidence producer reads (the lead wrote it by hand before)

def test_the_producer_bindings_hold_exactly_the_keys_the_producer_reads_and_nothing_the_chain_manifest_has_yet_to_supply(bench, capsys):
    with_producer(bench, capsys)
    value = json.loads(bench.producer.read_text(encoding="utf-8"))
    assert set(value) == set(jo.PRODUCER_BINDINGS) | {"schema_version", "mode"}
    jo.validate_jobs_bindings(value, producer=True)
    assert "baselineChainPath" not in value and value["baselineSha256"] == sha(bench.baseline)


def test_the_producer_bindings_drive_the_producer_command_to_a_manifest_that_qualifies(bench, capsys):
    with_producer(bench, capsys)
    code = ce.main(["--date", jw.RUN_DATE.isoformat(), "--bindings", str(bench.producer), "--evidence", str(bench.tmp / "run"), "--expect", "baseline"],
                   reader_factory=lambda timeouts: bench.fixture.reader(), bq_factory=lambda b: bench.fixture.bq(), now=lambda: jpw.NOW)
    assert code == 0
    manifest = json.loads((bench.packet / f"chain-evidence-{jw.RUN_DATE.isoformat()}.json").read_text(encoding="utf-8"))
    assert manifest["role"] == "baseline" and manifest["verdict"]["qualifies"] is True


def test_the_producer_bindings_are_never_overwritten(bench, capsys):
    with_producer(bench, capsys)
    code, out, err = run(bench.producer_argv(), capsys)
    assert code == 1 and "REFUSED" in err


# lock

def test_lock_is_the_library_lock_of_the_five_files_the_jobs_paste_names(bench, capsys):
    locked_bench(bench, capsys)
    value = bench.bindings_value()
    lock = json.loads(bench.lock_file().read_text(encoding="utf-8"))
    bound = {"bindings": bench.bindings, "baseline": bench.baseline, "durable_manifest": bench.durable, "baseline_chain": bench.chain_path,
             "dry_run_receipt": bench.dry_run}
    assert lock == locklib.build_lock(bench.repo, value["release_id"], bound, names=locklib.JOBS_BOUND_NAMES)
    assert set(lock["bound_files"]) == set(locklib.JOBS_BOUND_NAMES)
    assert locklib.lock_problems(bench.repo, lock) == []
    assert "core/setup/release/JOBS-PASTE.ps1" in lock["repo_files"]


def test_lock_prints_its_path_and_the_hash_the_review_will_bind(bench, capsys):
    bound_bench(bench, capsys)
    code, out, err = run(bench.lock_argv(), capsys)
    assert code == 0 and str(bench.lock_file()) in out and sha(bench.lock_file()) in out


@pytest.mark.parametrize("victim", ["baseline", "durable", "chain", "dry_run"])
def test_lock_refuses_a_bound_file_that_changed_after_it_was_bound_and_writes_no_lock(bench, capsys, victim):
    bound_bench(bench, capsys)
    path = {"baseline": bench.baseline, "durable": bench.durable, "chain": bench.chain_path, "dry_run": bench.dry_run}[victim]
    path.write_bytes(path.read_bytes() + b" ")
    code, out, err = run(bench.lock_argv(), capsys)
    assert code == 1 and "differs from the bindings" in err and not bench.lock_file().exists()


def test_lock_refuses_when_the_checkout_is_not_at_the_bound_commit_or_is_dirty(bench, capsys):
    bound_bench(bench, capsys)
    (bench.repo / "core/setup/release/lock.py").write_text("changed" + chr(10), encoding="utf-8")
    code, out, err = run(bench.lock_argv(), capsys)
    assert code == 1 and "clean" in err and not bench.lock_file().exists()


def test_lock_never_overwrites_an_existing_lock(bench, capsys):
    locked_bench(bench, capsys)
    before = bench.lock_file().read_bytes()
    code, out, err = run(bench.lock_argv(), capsys)
    assert code == 1 and "REFUSED" in err and bench.lock_file().read_bytes() == before


def test_lock_in_the_default_mode_still_asks_for_the_services_bindings(bench, capsys):
    bound_bench(bench, capsys)
    argv = ["lock", "--repo", str(bench.repo), "--packet-dir", str(bench.packet), "--lock-dir", str(bench.lock_dir)]
    code, out, err = run(argv, capsys)
    assert code == 1 and "REFUSED" in err and not bench.lock_file().exists()


# review

def test_review_records_the_verdict_the_reviewer_names_and_binds_the_lock_and_target(bench, capsys):
    reviewed_bench(bench, capsys)
    review = json.loads(bench.review.read_text(encoding="utf-8"))
    assert review == locklib.review_for(bench.lock_file(), bench.target) and review["lock_sha256"] == sha(bench.lock_file())
    assert review["verdict"] == "ACCEPT" and review["target"] == bench.target


def test_review_has_no_default_verdict(bench, capsys):
    locked_bench(bench, capsys)
    argv = [a for a in bench.review_argv() if a not in ("--verdict", "ACCEPT")]
    with pytest.raises(SystemExit) as stopped:
        packet.main(argv)
    assert stopped.value.code == 2 and not bench.review.exists()


def test_review_refuses_a_lock_that_binds_other_bindings(bench, capsys):
    locked_bench(bench, capsys)
    lock = json.loads(bench.lock_file().read_text(encoding="utf-8"))
    lock["bound_files"]["bindings"] = "0" * 64
    bench.lock_file().write_text(json.dumps(lock), encoding="utf-8")
    code, out, err = run(bench.review_argv(), capsys)
    assert code == 1 and "other bindings" in err and not bench.review.exists()


def test_review_refuses_a_lock_that_no_longer_matches_the_checkout(bench, capsys):
    locked_bench(bench, capsys)
    (bench.repo / "core/setup/release/JOBS-PASTE.ps1").write_text("changed" + chr(10), encoding="utf-8")
    code, out, err = run(bench.review_argv(), capsys)
    assert code == 1 and "does not match the checkout" in err and not bench.review.exists()


def test_review_reject_is_recorded_as_reject(bench, capsys):
    locked_bench(bench, capsys)
    assert run(bench.review_argv("REJECT"), capsys)[0] == 0
    assert json.loads(bench.review.read_text(encoding="utf-8"))["verdict"] == "REJECT"


# receipt

def receipt_world(bench, capsys, text=None, quiet=None):
    reviewed_bench(bench, capsys)
    bench.line.write_text((bench.line_text() if text is None else text) + chr(10), encoding="utf-8")
    bench.quiet_line_file.write_text((bench.quiet_text() if quiet is None else quiet) + chr(10), encoding="utf-8")


def test_receipt_is_written_from_the_line_albert_typed_and_has_the_shape_the_jobs_paste_reads(bench, capsys):
    receipt_world(bench, capsys)
    code, out, err = run(bench.receipt_argv(), capsys)
    assert code == 0, err
    value = json.loads(bench.receipt.read_text(encoding="utf-8"))
    assert value == {"schema_version": 1, "release_id": bench.release_id, "target": bench.target, "mode": "jobs",
                     "declared_steps": ["JobsCandidate", "JobsUpdate", "JobsRollback"], "later_explicit_user_instruction": True,
                     "quiet_window_confirmed": True, "no_new_manual_starts_until_execution_ends": True, "jobs_only": True}
    assert "single_T1_smoke_authorized" not in value and "max_live_asks" not in value


def test_receipt_steps_can_be_narrowed_and_are_kept_in_the_order_of_the_release(bench, capsys):
    receipt_world(bench, capsys)
    assert run(bench.receipt_argv("--steps", "JobsRollback", "JobsCandidate"), capsys)[0] == 0
    assert json.loads(bench.receipt.read_text(encoding="utf-8"))["declared_steps"] == ["JobsCandidate", "JobsRollback"]


def test_receipt_refuses_a_services_step_in_mode_jobs(bench, capsys):
    receipt_world(bench, capsys)
    code, out, err = run(bench.receipt_argv("--steps", "Candidate"), capsys)
    assert code == 1 and "REFUSED" in err and not bench.receipt.exists()


def test_the_jobs_authorisation_line_is_exactly_the_line_albert_approved_with_the_release_id_and_no_other_sentence():
    line = packet.JOBS_AUTHORISATION_TEMPLATE.format(release_id="rel-abcdef0-01", n_jobs=len(so.JOB_NAMES))
    assert line == jpw.APPROVED_LINE.format(release_id="rel-abcdef0-01")
    assert line == ("RELEASE B rel-abcdef0-01: I authorise the jobs only release of this release id and nothing else. Only the 14 Cloud Run jobs "
                    "change, to one image built from the release commit; no service revision, traffic entry, tag, scheduler entry, secret, "
                    "environment variable or paid Ask changes.")
    assert packet.jobs_authorisation("rel-abcdef0-01") == line
    assert line.count(". ") == 1 and line.endswith("paid Ask changes.")
    for word in ("passcode", "T1", "Ask,", "USD", "credits", "SAST", "JobsUpdate", "DEPLOY", "IDLE", "quiet"):
        assert word not in line, word
    assert not any(ch in line for ch in (chr(0x2013), chr(0x2014))) and chr(45) * 2 not in line


@pytest.mark.parametrize("change", [
    lambda t, rid: t.replace(rid, "rel-0badf00-01"),
    lambda t, rid: t.replace("RELEASE B", "RELEASE A"),
    lambda t, rid: t.replace("Only the 14 Cloud Run jobs change", "Cloud Run jobs and services change"),
    lambda t, rid: t.replace("nothing else", "nothing else, with a refusal"),
    lambda t, rid: t.replace("paid Ask changes.", "paid Ask changes. I will type DEPLOY, IDLE and the passcode myself."),
    lambda t, rid: t + " JobsUpdate runs on the day of JobsCandidate between 08:05 and 21:00 SAST.",
    lambda t, rid: t + " The window is quiet and I will start no manual job until the execution ends.",
    lambda t, rid: t + " I will type DEPLOY and IDLE myself.",
    lambda t, rid: t.replace(" no service revision,", ""),
    lambda t, rid: "",
], ids=["other_release", "release_a", "services_change", "extra_clause", "passcode", "window_sentence", "quiet_sentence", "typed_words_sentence",
        "missing_clause", "empty"])
def test_receipt_refuses_a_line_that_is_not_the_one_the_packet_asks_albert_to_type(bench, capsys, change):
    reviewed_bench(bench, capsys)
    bench.line.write_text(change(bench.line_text(), bench.release_id) + chr(10), encoding="utf-8")
    code, out, err = run(bench.receipt_argv(), capsys)
    assert code == 1 and "authorisation line" in err and not bench.receipt.exists()


def test_receipt_accepts_the_line_albert_approved_word_for_word(bench, capsys):
    reviewed_bench(bench, capsys)
    bench.line.write_text(jpw.APPROVED_LINE.format(release_id=bench.release_id) + chr(10), encoding="utf-8")
    bench.quiet_line_file.write_text(jpw.QUIET_LINE.format(release_id=bench.release_id) + chr(10), encoding="utf-8")
    code, out, err = run(bench.receipt_argv(), capsys)
    assert code == 0, err
    assert json.loads(bench.receipt.read_text(encoding="utf-8"))["release_id"] == bench.release_id


def test_receipt_refuses_the_release_a_line_in_mode_jobs_and_the_jobs_line_in_mode_services(bench, capsys):
    receipt_world(bench, capsys, text=pw.authorisation_line(bench.release_id))
    code, out, err = run(bench.receipt_argv(), capsys)
    assert code == 1 and "authorisation line" in err and not bench.receipt.exists()
    with pytest.raises(packet.Refused):
        packet.check_authorisation(jpw.authorisation_line(bench.release_id), bench.release_id)


def test_the_jobs_quiet_line_is_release_a_s_own_sentence_with_the_release_b_id_in_front_and_nothing_else():
    line = packet.JOBS_QUIET_TEMPLATE.format(release_id="rel-abcdef0-01")
    assert line == jpw.QUIET_LINE.format(release_id="rel-abcdef0-01")
    assert line == "RELEASE B rel-abcdef0-01 QUIET: The window is quiet and I will start no manual job until the execution ends."
    assert packet.jobs_quiet("rel-abcdef0-01") == line
    assert packet.AUTHORISATION_TEMPLATE.split("RELEASE A {release_id}: ")[1].split(" I will type")[0].endswith(line.split("QUIET: ")[1])
    assert not any(ch in line for ch in (chr(0x2013), chr(0x2014))) and chr(45) * 2 not in line


@pytest.mark.parametrize("change", [
    lambda t, rid: t.replace(rid, "rel-0badf00-01"),
    lambda t, rid: t.replace("RELEASE B", "RELEASE A"),
    lambda t, rid: t.replace(" QUIET:", ":"),
    lambda t, rid: t.replace("I will start no manual job", "I will start few manual jobs"),
    lambda t, rid: t + " I will type DEPLOY and IDLE myself.",
    lambda t, rid: t.replace("The window is quiet and ", ""),
    lambda t, rid: "",
    lambda t, rid: jpw.authorisation_line(rid),
], ids=["other_release", "release_a", "no_marker", "weaker_promise", "extra_sentence", "missing_clause", "empty", "the_other_line"])
def test_receipt_refuses_a_quiet_line_that_is_not_the_one_the_packet_asks_albert_to_type(bench, capsys, change):
    receipt_world(bench, capsys, quiet=change(bench.quiet_text(), bench.release_id))
    code, out, err = run(bench.receipt_argv(), capsys)
    assert code == 1 and "quiet" in err.lower() and not bench.receipt.exists()


def test_receipt_refuses_without_the_second_line_file_and_without_the_file_it_names(bench, capsys):
    receipt_world(bench, capsys)
    code, out, err = run(bench.receipt_argv(quiet=False), capsys)
    assert code == 1 and "quiet" in err.lower() and not bench.receipt.exists()
    bench.quiet_line_file.unlink()
    code, out, err = run(bench.receipt_argv(), capsys)
    assert code == 1 and "quiet" in err.lower() and not bench.receipt.exists()


def test_receipt_accepts_the_quiet_line_word_for_word_and_normalises_whitespace(bench, capsys):
    receipt_world(bench, capsys, quiet="  " + bench.quiet_text().replace(" ", "  ", 4) + "  ")
    code, out, err = run(bench.receipt_argv(), capsys)
    assert code == 0, err
    value = json.loads(bench.receipt.read_text(encoding="utf-8"))
    assert value["quiet_window_confirmed"] is True and value["no_new_manual_starts_until_execution_ends"] is True


def test_the_services_receipt_takes_no_second_line_and_refuses_one(bench, capsys):
    receipt_world(bench, capsys)
    code, out, err = run(["receipt", "--repo", str(bench.repo), "--packet-dir", str(bench.packet), "--mode", "services-only",
                          "--authorisation-line-file", str(bench.line), "--quiet-line-file", str(bench.quiet_line_file)], capsys)
    assert code == 1 and "quiet" in err.lower() and not bench.receipt.exists()


def test_receipt_normalises_whitespace_like_release_a_does(bench, capsys):
    receipt_world(bench, capsys, text="  " + bench.line_text().replace(". ", ".  ").replace(" ", "  ", 3) + "  ")
    assert run(bench.receipt_argv(), capsys)[0] == 0


def test_receipt_refuses_without_an_accepting_review_and_never_overwrites(bench, capsys):
    locked_bench(bench, capsys)
    bench.line.write_text(bench.line_text() + chr(10), encoding="utf-8")
    code, out, err = run(bench.receipt_argv(), capsys)
    assert code == 1 and "review" in err.lower() and not bench.receipt.exists()
    assert run(bench.review_argv("REJECT"), capsys)[0] == 0
    code, out, err = run(bench.receipt_argv(), capsys)
    assert code == 1 and "ACCEPT" in err and not bench.receipt.exists()


def test_the_packet_files_are_created_exclusively_in_mode_jobs_too(tmp_path):
    target = tmp_path / "once.json"
    packet.write_new(target, "first" + chr(10))
    with pytest.raises(packet.Refused):
        packet.write_new(target, "second" + chr(10))


# the lock lists the files of the jobs release

def test_the_lock_a_jobs_packet_builds_lists_the_tooling_and_job_code_files_the_jobs_paste_runs(bench, capsys):
    locked_bench(bench, capsys)
    lock = json.loads(bench.lock_file().read_text(encoding="utf-8"))
    for rel in ("core/setup/release/packet.py", "core/setup/release/jobs_freeze.py", "core/setup/release/jobs_baseline.py",
                "core/setup/release/jobs_source.py", "core/setup/release/jobs_effects.py", "core/setup/release/env_scan.py",
                "core/setup/deploy_jobs.py", "core/setup/release/jobs_run.py", "core/collect/chain.py", "core/setup/stamp.py"):
        assert rel in lock["repo_files"], rel


def test_bind_says_why_a_second_attempt_is_not_supported_instead_of_failing_somewhere_else(bench, capsys):
    prepared(bench, capsys)
    code, out, err = run(bench.bind_argv(**{"--attempt": "02"}), capsys)
    assert code == 1 and "supports attempt 01 only" in err and not bench.bindings.exists()


@pytest.mark.parametrize("minutes", ["0", "30"])
def test_the_producer_bindings_accept_the_two_ends_of_the_collect_start_tolerance(bench, capsys, minutes):
    captured(bench, capsys)
    code, out, err = run(bench.producer_argv(**{"--collect-start-tolerance-minutes": minutes}), capsys)
    assert code == 0, err
    assert json.loads(bench.producer.read_text(encoding="utf-8"))["collectStartToleranceMinutes"] == int(minutes)


def test_bind_accepts_the_upper_end_of_the_collect_start_tolerance_when_the_chain_was_produced_under_it(bench, capsys):
    captured(bench, capsys)
    flag = {"--collect-start-tolerance-minutes": "30"}
    assert run(bench.producer_argv(**flag), capsys)[0] == 0
    bench.write_chain()
    bench.write_durable()
    bench.write_dry_run()
    code, out, err = run(bench.bind_argv(**flag), capsys)
    assert code == 0, err
    assert bench.bindings_value()["collectStartToleranceMinutes"] == 30


# the baseline chain manifest is judged only under the bindings the producer read

def test_bind_refuses_when_the_producer_bindings_are_missing_because_the_manifest_then_has_no_bindings_to_be_tied_to(bench, capsys):
    prepared(bench, capsys)
    bench.producer.unlink()
    code, out, err = run(bench.bind_argv(), capsys)
    assert code == 1 and "producer bindings" in err and not bench.bindings.exists()


@pytest.mark.parametrize("flag,value,key", [
    ("--collect-start-tolerance-minutes", "20", "collectStartToleranceMinutes"),
    ("--chain-evidence-bytes-cap", "1048576", "chainEvidenceBytesCap"),
    ("--caller-account", "someone.else@example.invalid", "callerAccount"),
    ("--http-timeout-seconds", "31", "readTimeoutSeconds"),
])
def test_bind_refuses_values_that_differ_from_the_producer_bindings_the_baseline_chain_was_produced_under(bench, capsys, flag, value, key):
    prepared(bench, capsys)
    code, out, err = run(bench.bind_argv(**{flag: value}), capsys)
    assert code == 1 and "produced under other bindings" in err and key in err and not bench.bindings.exists()


def test_bind_refuses_a_baseline_chain_manifest_generated_from_another_commit(bench, capsys):
    prepared(bench, capsys)
    value = json.loads(bench.chain_path.read_text(encoding="utf-8"))
    value["generated_from"]["head"] = "0" * 40
    bench.chain_path.write_text(json.dumps(value), encoding="utf-8")
    code, out, err = run(bench.bind_argv(), capsys)
    assert code == 1 and "another commit" in err and not bench.bindings.exists()


def test_capture_baseline_j_after_a_rollback_holds_the_services_to_the_pinned_a80_revisions_and_records_them(tmp_path, monkeypatch, capsys):
    bench = jpw.JobsBench(tmp_path, monkeypatch)
    for name in so.SERVICES:
        bench.world.restore(name, keep_tag=True)
    pw.fake_gcloud(bench.tmp / "fake", bench.world)
    bench.write_a_readback(phase="AfterRollback")
    code, out, err = run(bench.capture_argv(), capsys)
    assert code == 0, err
    value = bench.baseline_value()
    assert value["aTerminal"]["kind"] == "AfterRollback" and value["a80Serving"] == packet.A80_SERVING == rw.A80_REV
    assert {name: value["services"][name]["serving"]["name"] for name in so.SERVICES} == rw.A80_REV
