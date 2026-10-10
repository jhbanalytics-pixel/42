"""The packet command line (core/setup/release/packet.py): the one entry point that produces the baseline, the bindings, the lock,
the review and the receipt the release paste reads. Offline: the gcloud binary is a stand-in that records what it was asked, the
repository is a throwaway one, and nothing here starts pwsh (the paste itself is driven in test_release_packet_paste.py)."""
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from core.setup import durable_effects_check as dec
from core.setup.release import bound_readback as helper
from core.setup.release import lock as locklib
from core.setup.release import packet
from core.setup.release import services_only as so
from core.setup.tests import packet_world as pw
from core.setup.tests import release_world as rw

ROOT = pw.ROOT
PASTE = ROOT / "core/setup/release/SERVICES-PASTE.ps1"


@pytest.fixture
def bench(tmp_path, monkeypatch):
    return pw.Bench(tmp_path, monkeypatch)


def run(argv, capsys):
    code = packet.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def bound_bench(bench, capsys):
    """A bench with the baseline captured and the bindings written."""
    bench.prepare_inputs()
    assert run(bench.capture_argv(), capsys)[0] == 0
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


# capture-baseline

def test_capture_baseline_writes_the_receipt_the_library_function_returns_and_prints_its_hash(bench, capsys):
    code, out, err = run(bench.capture_argv(), capsys)
    assert code == 0, err
    expected = so.capture_baseline(rw.FakeReader(bench.world), json.loads(bench.baseline.read_text(encoding="utf-8"))["captured_utc"])
    assert json.loads(bench.baseline.read_text(encoding="utf-8")) == expected
    assert hashlib.sha256(bench.baseline.read_bytes()).hexdigest() in out
    assert str(bench.baseline) in out


def test_capture_baseline_asks_gcloud_only_read_commands_and_only_the_ones_the_baseline_needs(bench, capsys):
    assert run(bench.capture_argv(), capsys)[0] == 0
    asked = pw.calls(bench.log)
    assert asked, "the stand-in was never asked"
    for args in asked:
        assert any(tuple(args[:len(prefix)]) == prefix for prefix in helper.READ_PREFIXES), args
        assert tuple(args[:3]) in {("run", "services", "describe"), ("run", "revisions", "describe"), ("run", "revisions", "list"),
                                   ("run", "jobs", "describe"), ("run", "services", "get-iam-policy")}, args
    verbs = {tuple(a[:3]) for a in asked}
    assert ("run", "jobs", "describe") in verbs and len([a for a in asked if a[:3] == ["run", "jobs", "describe"]]) == len(so.JOB_NAMES)


def test_capture_baseline_reader_refuses_every_write_before_gcloud_is_started(bench):
    reader = packet.baseline_reader(30)
    for args in (["run", "services", "update-traffic", "f42-api", "--to-revisions=x=100"], ["run", "deploy", "f42-api"],
                 ["run", "services", "add-iam-policy-binding", "f42-agent"], ["builds", "submit"], ["scheduler", "jobs", "run", "x"],
                 ["run", "services", "update", "f42-api", "--remove-env-vars=X"]):
        with pytest.raises(so.Stop) as stopped:
            reader._gcloud(args)
        assert stopped.value.code == "WRITE_REFUSED"
    assert pw.calls(bench.log) == []


def test_capture_baseline_never_overwrites_an_existing_file(bench, capsys):
    bench.baseline.parent.mkdir(parents=True)
    bench.baseline.write_text("kept\n", encoding="utf-8")
    code, out, err = run(bench.capture_argv(), capsys)
    assert code == 1 and "REFUSED" in err
    assert bench.baseline.read_text(encoding="utf-8") == "kept\n"
    assert pw.calls(bench.log) == [], "a refused output path must be found before any read"


def test_capture_baseline_refuses_live_that_is_not_the_a80_pair_and_writes_nothing(tmp_path, monkeypatch, capsys):
    world = pw.a80_world(serving=rw.OLDER_REV)
    bench = pw.Bench(tmp_path, monkeypatch, world=world)
    code, out, err = run(bench.capture_argv(), capsys)
    assert code == 1 and "REFUSED" in err and "a80" in err
    assert not bench.baseline.exists()


def test_capture_baseline_refuses_jobs_that_are_not_on_the_e77c3819_image_and_writes_nothing(tmp_path, monkeypatch, capsys):
    bench = pw.Bench(tmp_path, monkeypatch, world=pw.a80_world(jobs_digest=rw.OTHER_DIGEST))
    code, out, err = run(bench.capture_argv(), capsys)
    assert code == 1 and "REFUSED" in err and "e77c3819" in err
    assert not bench.baseline.exists()


def test_capture_baseline_a_read_that_cannot_complete_exits_3_and_writes_nothing(tmp_path, monkeypatch, capsys):
    bench = pw.Bench(tmp_path, monkeypatch)
    bench.argv, bench.log = pw.fake_gcloud(tmp_path / "fake2", bench.world, fail_on="f42-gdelt")
    monkeypatch.setattr(helper, "gcloud_command", lambda: bench.argv)
    code, out, err = run(bench.capture_argv(), capsys)
    assert code == 3 and "PROBE" in err
    assert not bench.baseline.exists()


def test_capture_baseline_needs_a_timeout_and_a_destination(bench, capsys):
    for argv in (["capture-baseline", "--out", str(bench.baseline)], ["capture-baseline", "--gcloud-timeout-seconds", "120"],
                 ["capture-baseline", "--out", str(bench.baseline), "--gcloud-timeout-seconds", "0"]):
        with pytest.raises(SystemExit) as stopped:
            packet.main(argv)
        assert stopped.value.code == 2


def test_capture_baseline_prints_no_environment_name_or_value(bench, capsys):
    code, out, err = run(bench.capture_argv(), capsys)
    for name in rw.SYNTHETIC_DECLARED:
        assert name not in out + err


# bind: the key set, and where each value comes from

def paste_bound_properties():
    return set(re.findall(r"\$(?:Script:)?[Bb]ound\.([A-Za-z_]+)", PASTE.read_text(encoding="utf-8"))) - {"PSObject"}


def checker_binding_keys():
    return set(re.findall(r'bindings\.get\("([A-Za-z0-9_]+)"\)', Path(dec.__file__).read_text(encoding="utf-8")))


def test_bind_writes_exactly_the_key_set_the_library_the_paste_and_the_checker_read(bench, capsys):
    bound_bench(bench, capsys)
    expected = set(so.REQUIRED_BINDINGS) | paste_bound_properties() | checker_binding_keys() | {"schema_version"}
    assert {"status", "mode", "durableManifestPath", "inflightAsksSqlSha256", "inflightAsksBytesCap"} <= expected
    assert set(bench.bindings_value()) == expected
    assert so.validate_bindings(bench.bindings_value())


def test_bind_resolves_the_difference_by_carrying_the_union_never_a_rollback_key(bench, capsys):
    bound_bench(bench, capsys)
    text = bench.bindings.read_text(encoding="utf-8")
    assert not re.search(r'"rollback(Revision|ImageDigest)"', text)
    assert so.stale_target(bench.bindings_value()) is None
    value = bench.bindings_value()
    assert value["status"] == "BOUND_FOR_INDEPENDENT_REVIEW" and value["mode"] == "services-only" and value["schema_version"] == 1


def test_bind_takes_each_value_from_its_source_not_from_the_caller(bench, capsys, monkeypatch):
    bound_bench(bench, capsys)
    value = bench.bindings_value()
    assert value["target"] == pw.head(bench.repo) and value["tree"] == pw.git(bench.repo, "rev-parse", "HEAD^{tree}")
    assert value["release_id"] == f"rel-{value['target'][:7]}-01"
    assert value["baselinePath"] == str(bench.baseline.resolve()) and value["baselineSha256"] == pw.sha(bench.baseline)
    assert value["durableManifestPath"] == str(bench.durable.resolve()) and value["durableManifestSha256"] == pw.sha(bench.durable)
    assert value["compatReceiptSha256"] == pw.sha(bench.packet / "compat-receipt.json")
    assert value["oldReaderReceiptSha256"] == pw.sha(bench.packet / "old-reader-receipt.json")
    assert value["releaseDir"] == str(bench.packet.resolve())
    assert value["expectedSmokeChecks"] == locklib.smoke_check_count(bench.repo / "core/api/smoke.py")
    assert value["inflightAsksSqlSha256"] == dec.INFLIGHT_ASKS_SQL_SHA256 and value["inflightAsksBytesCap"] == 10485760
    assert value["callerAccount"] == pw.CALLER and value["readTimeoutSeconds"] == {"gcloud": 120, "http": 30}
    assert value["maxSmokeAgeMinutes"] == 360
    monkeypatch.chdir(bench.repo)
    freeze_check = helper.GcloudReader({"gcloud": 30, "http": 30}).source_file_sha256(value["target"], "core/api/cloudbuild.yaml")
    assert value["buildConfigSha256"] == freeze_check
    assert value["buildServiceAccount"] == "f42-deployer@ogilvy-trends-v2.iam.gserviceaccount.com"
    assert value["buildServiceAccount"] in PASTE.read_text(encoding="utf-8")


def test_bind_derives_the_environment_deltas_from_the_baseline_and_the_target(bench, capsys):
    bound_bench(bench, capsys)
    value, baseline = bench.bindings_value(), bench.baseline_value()
    agent = baseline["canonical"]["f42-agent"]
    short = value["target"][:12]
    assert value["envDeltas"] == {"f42-agent": {"F42_VERSION": short},
                                  "f42-api": {"F42_VERSION": short, "AGENT_URL": so.tag_url(value["release_id"], agent), "AGENT_AUDIENCE": agent}}
    assert so.stage1(value, baseline)["release_id"] == value["release_id"]


def test_bind_output_is_accepted_by_the_release_readback_before_any_write(bench, capsys):
    bound_bench(bench, capsys)
    value = bench.bindings_value()
    world = rw.World(declared=rw.SYNTHETIC_DECLARED)
    for name in world.jobs:
        world.jobs[name]["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["image"] = (
            f"{so.REGION}-docker.pkg.dev/{so.PROJECT}/intelligence-42/jobs@{pw.A80_JOBS_DIGEST}")
    release = so.Release(value, rw.FakeReader(world), bench.tmp / "evidence")
    assert release.rid == value["release_id"] and release.stage1["source"]["commit"] == value["target"]


@pytest.mark.parametrize("name,change", [
    ("a commit that is not HEAD", {"--release-commit": "0" * 40}),
    ("a short commit", {"--release-commit": "a" * 12}),
    ("an attempt that is not two digits", {"--attempt": "1"}),
    ("a zero timeout", {"--gcloud-timeout-seconds": "0"}),
    ("a negative smoke age", {"--max-smoke-age-minutes": "-5"}),
    ("a zero bytes cap", {"--inflight-asks-bytes-cap": "0"}),
])
def test_bind_refuses_an_unusable_value_and_writes_no_bindings(bench, capsys, name, change):
    bench.prepare_inputs()
    assert run(bench.capture_argv(), capsys)[0] == 0
    try:
        code, out, err = run(bench.bind_argv(**change), capsys)
    except SystemExit as stopped:
        code = stopped.code
    assert code in (1, 2), name
    assert not bench.bindings.exists(), name


def test_bind_refuses_a_dirty_checkout(bench, capsys):
    bench.prepare_inputs()
    assert run(bench.capture_argv(), capsys)[0] == 0
    (bench.repo / "core/setup/release/plan.py").write_text("changed\n", encoding="utf-8")
    code, out, err = run(bench.bind_argv(), capsys)
    assert code == 1 and "clean" in err
    assert not bench.bindings.exists()


def test_bind_refuses_a_receipt_that_does_not_say_pass_or_is_missing(bench, capsys):
    bench.prepare_inputs()
    assert run(bench.capture_argv(), capsys)[0] == 0
    pw.write_receipts(bench.packet, verdict="fail")
    code, out, err = run(bench.bind_argv(), capsys)
    assert code == 1 and "compat" in err and not bench.bindings.exists()
    (bench.packet / "compat-receipt.json").unlink()
    code, out, err = run(bench.bind_argv(), capsys)
    assert code == 1 and "compat" in err and not bench.bindings.exists()


def test_bind_refuses_a_durable_manifest_the_checker_does_not_accept(bench, capsys):
    bench.prepare_inputs()
    assert run(bench.capture_argv(), capsys)[0] == 0
    template = ROOT / "core/setup/release/durable-effects-manifest.template.json"
    bench.durable.write_bytes(template.read_bytes())
    code, out, err = run(bench.bind_argv(), capsys)
    assert code == 1 and "durable" in err and not bench.bindings.exists()


def test_bind_refuses_a_file_that_is_not_a_baseline(bench, capsys):
    bench.prepare_inputs()
    assert run(bench.capture_argv(), capsys)[0] == 0
    value = bench.baseline_value()
    value["kind"] = "something-else"
    bench.baseline.write_text(json.dumps(value), encoding="utf-8")
    code, out, err = run(bench.bind_argv(), capsys)
    assert code == 1 and "baseline" in err and not bench.bindings.exists()


def test_bind_never_overwrites_existing_bindings(bench, capsys):
    bound_bench(bench, capsys)
    before = bench.bindings.read_bytes()
    code, out, err = run(bench.bind_argv(), capsys)
    assert code == 1 and "REFUSED" in err and bench.bindings.read_bytes() == before


def test_bind_carries_the_smoke_check_count_the_library_counts_not_a_typed_one(bench, capsys, monkeypatch):
    monkeypatch.setattr(locklib, "smoke_check_count", lambda path: 17)
    bound_bench(bench, capsys)
    assert bench.bindings_value()["expectedSmokeChecks"] == 17


def test_every_file_is_created_exclusively_so_a_race_cannot_replace_one(tmp_path):
    target = tmp_path / "once.json"
    packet.write_new(target, "first\n")
    with pytest.raises(packet.Refused):
        packet.write_new(target, "second\n")
    assert target.read_text(encoding="utf-8") == "first\n"


def test_bind_prints_only_paths_and_digests(bench, capsys):
    bench.prepare_inputs()
    run(bench.capture_argv(), capsys)
    code, out, err = run(bench.bind_argv(), capsys)
    assert code == 0 and pw.sha(bench.bindings) in out and str(bench.bindings) in out
    for name in rw.SYNTHETIC_DECLARED:
        assert name not in out + err


# lock

def test_lock_is_the_library_lock_of_the_files_the_paste_names(bench, capsys):
    locked_bench(bench, capsys)
    value = bench.bindings_value()
    path = bench.lock_file()
    assert path.is_file() and path.parent == bench.repo / "core/setup/release/locks"
    lock = json.loads(path.read_text(encoding="utf-8"))
    bound = {"bindings": bench.bindings, "baseline": bench.baseline, "durable_manifest": bench.durable,
             "compat_receipt": bench.packet / "compat-receipt.json", "old_reader_receipt": bench.packet / "old-reader-receipt.json"}
    assert lock == locklib.build_lock(bench.repo, value["release_id"], bound)
    assert set(lock["bound_files"]) == set(locklib.BOUND_NAMES)
    assert locklib.lock_problems(bench.repo, lock) == []


def test_lock_prints_its_path_and_the_hash_the_review_will_bind(bench, capsys):
    bound_bench(bench, capsys)
    code, out, err = run(bench.lock_argv(), capsys)
    assert code == 0 and str(bench.lock_file()) in out and hashlib.sha256(bench.lock_file().read_bytes()).hexdigest() in out


@pytest.mark.parametrize("victim", ["baseline", "compat", "durable"])
def test_lock_refuses_a_bound_file_that_changed_after_it_was_bound_and_writes_no_lock(bench, capsys, victim):
    bound_bench(bench, capsys)
    path = {"baseline": bench.baseline, "compat": bench.packet / "compat-receipt.json", "durable": bench.durable}[victim]
    path.write_bytes(path.read_bytes() + b" ")
    code, out, err = run(bench.lock_argv(), capsys)
    assert code == 1 and "differs from the bindings" in err
    assert not bench.lock_file().exists()


def test_lock_refuses_when_the_checkout_is_not_at_the_bound_commit_or_is_dirty(bench, capsys):
    bound_bench(bench, capsys)
    (bench.repo / "core/setup/release/lock.py").write_text("changed\n", encoding="utf-8")
    code, out, err = run(bench.lock_argv(), capsys)
    assert code == 1 and "clean" in err and not bench.lock_file().exists()
    pw.git(bench.repo, "commit", "-q", "-am", "moves head")
    code, out, err = run(bench.lock_argv(), capsys)
    assert code == 1 and "bound commit" in err and not bench.lock_file().exists()


def checkout_is_what_the_paste_demands(bench):
    """The two source checks of the paste before any command: HEAD is the bound target, and git status is empty."""
    return pw.head(bench.repo) == bench.target and pw.git(bench.repo, "status", "--porcelain=v1") == ""


def test_a_lock_written_into_the_checkout_leaves_it_unclean_for_the_paste_source_check(bench, capsys):
    locked_bench(bench, capsys)
    assert bench.lock_file().parent == bench.repo / "core/setup/release/locks"
    assert not checkout_is_what_the_paste_demands(bench)


def test_a_lock_can_be_written_outside_the_checkout_so_the_checkout_stays_what_the_paste_demands(bench, capsys):
    bench.lock_dir = bench.tmp / "locks"
    reviewed_bench(bench, capsys)
    assert run(bench.receipt_argv(), capsys)[0] == 0
    assert bench.lock_file().parent == bench.lock_dir and bench.lock_file().is_file()
    assert not (bench.repo / "core/setup/release/locks").exists()
    assert checkout_is_what_the_paste_demands(bench)
    value = bench.bindings_value()
    bound = {"bindings": bench.bindings, "baseline": bench.baseline, "durable_manifest": bench.durable,
             "compat_receipt": bench.packet / "compat-receipt.json", "old_reader_receipt": bench.packet / "old-reader-receipt.json"}
    assert json.loads(bench.lock_file().read_text(encoding="utf-8")) == locklib.build_lock(bench.repo, value["release_id"], bound)
    assert json.loads(bench.review.read_text(encoding="utf-8")) == locklib.review_for(bench.lock_file(), value["target"])


def test_review_and_receipt_look_for_the_lock_where_lock_dir_says_and_nowhere_else(bench, capsys):
    bench.lock_dir = bench.tmp / "locks"
    locked_bench(bench, capsys)
    bench.lock_dir = bench.tmp / "elsewhere"
    code, out, err = run(bench.review_argv(), capsys)
    assert code == 1 and "lock" in err and not bench.review.exists()


def test_lock_never_replaces_a_lock(bench, capsys):
    locked_bench(bench, capsys)
    before = bench.lock_file().read_bytes()
    code, out, err = run(bench.lock_argv(), capsys)
    assert code == 1 and "REFUSED" in err and bench.lock_file().read_bytes() == before


def test_lock_refuses_bindings_the_library_does_not_accept(bench, capsys):
    bound_bench(bench, capsys)
    value = bench.bindings_value()
    value["rollbackRevision"] = "f42-agent-00047-677"
    bench.bindings.write_text(json.dumps(value), encoding="utf-8")
    code, out, err = run(bench.lock_argv(), capsys)
    assert code == 1 and not bench.lock_file().exists()


# review

def test_review_has_the_library_shape_with_a_hash_recomputed_from_the_lock_file(bench, capsys):
    reviewed_bench(bench, capsys)
    review = json.loads(bench.review.read_text(encoding="utf-8"))
    assert review == locklib.review_for(bench.lock_file(), bench.bindings_value()["target"])
    assert set(review) == {"schema_version", "verdict", "target", "lock_sha256"}
    assert review["lock_sha256"] == hashlib.sha256(bench.lock_file().read_bytes()).hexdigest()


def test_review_has_no_default_verdict(bench, capsys):
    locked_bench(bench, capsys)
    with pytest.raises(SystemExit) as stopped:
        packet.main(["review", "--repo", str(bench.repo), "--packet-dir", str(bench.packet)])
    assert stopped.value.code == 2 and not bench.review.exists()


def test_review_can_record_a_rejection_and_the_receipt_then_refuses_it(bench, capsys):
    locked_bench(bench, capsys)
    assert run(bench.review_argv("REJECT"), capsys)[0] == 0
    assert json.loads(bench.review.read_text(encoding="utf-8"))["verdict"] == "REJECT"
    code, out, err = run(bench.receipt_argv(), capsys)
    assert code == 1 and "ACCEPT" in err and not bench.receipt.exists()


def test_review_refuses_a_lock_that_no_longer_matches_the_repository(bench, capsys):
    locked_bench(bench, capsys)
    lock = json.loads(bench.lock_file().read_text(encoding="utf-8"))
    lock["repo_files"]["core/setup/release/plan.py"] = "0" * 64
    bench.lock_file().write_text(json.dumps(lock), encoding="utf-8")
    code, out, err = run(bench.review_argv(), capsys)
    assert code == 1 and "lock" in err and not bench.review.exists()


def test_review_refuses_a_lock_that_binds_other_bindings(bench, capsys):
    locked_bench(bench, capsys)
    value = bench.bindings_value()
    value["maxSmokeAgeMinutes"] = 999
    bench.bindings.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    code, out, err = run(bench.review_argv(), capsys)
    assert code == 1 and "bindings" in err and not bench.review.exists()


def test_review_without_a_lock_is_refused_and_never_overwrites(bench, capsys):
    bound_bench(bench, capsys)
    code, out, err = run(bench.review_argv(), capsys)
    assert code == 1 and "lock" in err and not bench.review.exists()
    assert run(bench.lock_argv(), capsys)[0] == 0
    assert run(bench.review_argv(), capsys)[0] == 0
    before = bench.review.read_bytes()
    code, out, err = run(bench.review_argv("REJECT"), capsys)
    assert code == 1 and bench.review.read_bytes() == before


# receipt

def test_receipt_has_exactly_the_keys_and_types_the_paste_requires(bench, capsys):
    reviewed_bench(bench, capsys)
    code, out, err = run(bench.receipt_argv(), capsys)
    assert code == 0, err
    receipt = json.loads(bench.receipt.read_text(encoding="utf-8"))
    value = bench.bindings_value()
    assert set(receipt) == {"schema_version", "release_id", "target", "declared_steps", "later_explicit_user_instruction",
                            "quiet_window_confirmed", "no_new_manual_starts_until_execution_ends", "single_T1_smoke_authorized",
                            "max_live_asks"}
    assert receipt["schema_version"] == 1 and receipt["release_id"] == value["release_id"] and receipt["target"] == value["target"]
    assert receipt["declared_steps"] == list(pw.STEPS)
    for key in ("later_explicit_user_instruction", "quiet_window_confirmed", "no_new_manual_starts_until_execution_ends",
                "single_T1_smoke_authorized"):
        assert receipt[key] is True, key
    assert receipt["max_live_asks"] == 1 and type(receipt["max_live_asks"]) is int


def test_receipt_can_declare_fewer_steps_and_refuses_an_unknown_one(bench, capsys):
    reviewed_bench(bench, capsys)
    code, out, err = run(bench.receipt_argv("--steps", "Rollback", "Retire"), capsys)
    assert code == 0, err
    assert json.loads(bench.receipt.read_text(encoding="utf-8"))["declared_steps"] == ["Rollback", "Retire"]
    bench.receipt.unlink()
    with pytest.raises(SystemExit) as stopped:
        packet.main(bench.receipt_argv("--steps", "Deploy"))
    assert stopped.value.code == 2 and not bench.receipt.exists()


@pytest.mark.parametrize("name,replace", [
    ("another release", {"RELEASE A rel-": "RELEASE A xrel-"}),
    ("a second Ask allowed", {"no second Ask and no retry": "a second Ask if needed"}),
    ("not live", {"one live T1 Ask": "one T1 Ask"}),
    ("not the candidate url", {"against the candidate API tag URL only": "against the public URL"}),
    ("a window that is not quiet", {"The window is quiet and I will start no manual job until the execution ends.": ""}),
    ("someone else types", {"I will type DEPLOY, IDLE and the passcode myself.": "The lead will type them."}),
])
def test_receipt_refuses_a_line_that_is_not_the_authorisation(bench, capsys, name, replace):
    reviewed_bench(bench, capsys)
    bench.line.write_text(pw.authorisation_line(bench.release_id, **replace) + "\n", encoding="utf-8")
    code, out, err = run(bench.receipt_argv(), capsys)
    assert code == 1 and "authorisation" in err, name
    assert not bench.receipt.exists(), name


def test_receipt_refuses_an_empty_or_missing_line_and_a_line_for_another_release_id(bench, capsys):
    reviewed_bench(bench, capsys)
    bench.line.write_text("", encoding="utf-8")
    assert run(bench.receipt_argv(), capsys)[0] == 1
    bench.line.write_text(pw.authorisation_line("rel-0000000-01") + "\n", encoding="utf-8")
    assert run(bench.receipt_argv(), capsys)[0] == 1
    bench.line.unlink()
    assert run(bench.receipt_argv(), capsys)[0] == 1
    assert not bench.receipt.exists()


def test_receipt_needs_an_accepting_review_bound_to_this_lock(bench, capsys):
    locked_bench(bench, capsys)
    code, out, err = run(bench.receipt_argv(), capsys)
    assert code == 1 and "review" in err and not bench.receipt.exists()
    bench.review.write_text(json.dumps(locklib.review_for(bench.lock_file(), bench.bindings_value()["target"])), encoding="utf-8")
    review = json.loads(bench.review.read_text(encoding="utf-8"))
    review["lock_sha256"] = "0" * 64
    bench.review.write_text(json.dumps(review), encoding="utf-8")
    code, out, err = run(bench.receipt_argv(), capsys)
    assert code == 1 and "review" in err and not bench.receipt.exists()


def test_receipt_never_overwrites(bench, capsys):
    reviewed_bench(bench, capsys)
    assert run(bench.receipt_argv(), capsys)[0] == 0
    before = bench.receipt.read_bytes()
    code, out, err = run(bench.receipt_argv("--steps", "Rollback"), capsys)
    assert code == 1 and bench.receipt.read_bytes() == before


# the entry point, as a process

def test_the_module_runs_as_a_command_and_lists_its_five_subcommands(tmp_path):
    done = subprocess.run([sys.executable, "-B", "-m", "core.setup.release.packet", "--help"], cwd=ROOT, capture_output=True,
                          encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stderr
    for name in ("capture-baseline", "bind", "lock", "review", "receipt"):
        assert name in done.stdout
    bare = subprocess.run([sys.executable, "-B", "-m", "core.setup.release.packet"], cwd=ROOT, capture_output=True, encoding="utf-8", timeout=120)
    assert bare.returncode == 2


def test_capture_baseline_as_a_process_exits_0_on_success_and_1_on_a_refusal(bench):
    lines = ["import sys", "from core.setup.release import bound_readback as h", "from core.setup.release import packet",
             f"h.gcloud_command = lambda: {bench.argv!r}", "raise SystemExit(packet.main(sys.argv[1:]))"]
    driver = chr(10).join(lines)
    command = [sys.executable, "-B", "-c", driver, *bench.capture_argv()]
    done = subprocess.run(command, cwd=ROOT, capture_output=True, encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stderr
    assert bench.baseline.is_file() and pw.sha(bench.baseline) in done.stdout
    again = subprocess.run(command, cwd=ROOT, capture_output=True, encoding="utf-8", timeout=120)
    assert again.returncode == 1 and "REFUSED" in again.stderr


# bind: the durable manifest must belong to the release being bound (flag F10)

def rewrite_durable(path, change):
    """Change the manifest, then recompute its hash so the checker still accepts it and only the binding to the release can refuse it."""
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    change(manifest)
    manifest["manifest_sha256"] = dec.manifest_hash(manifest)
    assert dec.validate_manifest(manifest) == []
    Path(path).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


OTHER_COMMIT = "b" * 40


@pytest.mark.parametrize("field,change", [
    ("release_id", lambda m: m.update(release_id="rel-1234567-01")),
    ("release_id", lambda m: m.update(release_id=m["release_id"][:-2] + "02")),
    ("release_id", lambda m: m.update(release_id="")),
    ("release_id", lambda m: m.update(release_id=None)),
    ("source.commit", lambda m: m["source"].update(commit=OTHER_COMMIT)),
    ("source.tree", lambda m: m["source"].update(tree=OTHER_COMMIT)),
    ("source", lambda m: m.update(source="not an object")),
    ("generated_from.head", lambda m: m["generated_from"].update(head=OTHER_COMMIT)),
    ("generated_from", lambda m: m.update(generated_from=[])),
])
def test_bind_refuses_a_durable_manifest_that_belongs_to_another_release_and_writes_nothing(bench, capsys, field, change):
    bench.prepare_inputs()
    assert run(bench.capture_argv(), capsys)[0] == 0
    rewrite_durable(bench.durable, change)
    code, out, err = run(bench.bind_argv(), capsys)
    assert code == 1 and "durable" in err and field in err and not bench.bindings.exists()
    assert OTHER_COMMIT not in err + out


def test_bind_names_the_field_but_never_the_value_of_a_mismatched_durable_manifest(bench, capsys):
    bench.prepare_inputs()
    assert run(bench.capture_argv(), capsys)[0] == 0
    rewrite_durable(bench.durable, lambda m: m.update(release_id="rel-abcdef0-77"))
    code, out, err = run(bench.bind_argv(), capsys)
    assert code == 1 and "release_id" in err and "rel-abcdef0-77" not in err + out and not bench.bindings.exists()


def test_bind_still_accepts_a_durable_manifest_written_for_this_release_and_attempt(bench, capsys):
    bench.prepare_inputs()
    assert run(bench.capture_argv(), capsys)[0] == 0
    code, out, err = run(bench.bind_argv(), capsys)
    assert code == 0, err
    assert bench.bindings_value()["release_id"] == json.loads(bench.durable.read_text(encoding="utf-8"))["release_id"]
