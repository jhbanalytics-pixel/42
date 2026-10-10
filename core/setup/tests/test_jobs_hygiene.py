"""The lock, the forbidden verbs, the SQL text, the mutation table and the commit hygiene of Release B (W8-REL-B v2.1 JP-01 to JP-05).
These nodes collect in the core partition. A missing prerequisite fails them with a message; nothing here skips."""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from core.setup.release import bound_readback as helper
from core.setup.release import chain_evidence as ce
from core.setup.release import jobs_only as jo
from core.setup.release import lock as locklib
from core.setup.release import plan
from core.setup.tests import cloud_world as cw
from core.setup.tests import jobs_world as jw

ROOT = Path(__file__).resolve().parents[3]
RELEASE = ROOT / "core/setup/release"
# The commit Release B's branch starts from. The hygiene range is BASE..HEAD; a clone that lacks it fails JP-05, it never skips.
BASE = "8541030"


# JP-01

NEW_REPO_FILES = ("core/setup/release/chain_evidence.py", "core/setup/cloudbuild.jobs.yaml", "core/setup/jobs.Dockerfile",
                  "core/setup/deploy_jobs.py", "core/setup/release/JOBS-PASTE.ps1", "core/setup/release/jobs_only.py", "core/setup/release/jobs_run.py")
NEW_TEST_FILES = ("core/setup/tests/test_chain_evidence.py", "core/setup/tests/test_jobs_readback.py", "core/setup/tests/test_jobs_plan.py",
                  "core/setup/tests/test_jobs_update.py", "core/setup/tests/test_jobs_paste.py", "core/setup/tests/test_jobs_hygiene.py",
                  "core/setup/tests/test_jobs_checks.py", "core/setup/tests/test_jobs_prefix.py", "core/setup/tests/update_support.py",
                  "core/setup/tests/jobs_world.py", "core/setup/tests/cloud_world.py", "core/setup/tests/jobs_paste_world.py",
                  "core/setup/tests/range_hygiene.py")


def test_jp01_the_lock_lists_the_producer_the_build_files_the_deploy_script_and_the_jobs_paste_and_the_new_test_sources():
    assert set(NEW_REPO_FILES) <= set(locklib.REPO_FILES)
    assert set(NEW_TEST_FILES) <= set(locklib.TEST_FILES)
    for rel in NEW_REPO_FILES + NEW_TEST_FILES:
        assert (ROOT / rel).is_file(), rel
    assert "core/setup/release/SERVICES-PASTE.ps1" in locklib.REPO_FILES


def test_jp01_a_lock_built_from_the_repository_holds_every_new_file_and_a_changed_one_is_found(tmp_path):
    bound = {n: tmp_path / "x" for n in locklib.JOBS_BOUND_NAMES}
    lock = locklib.build_lock(ROOT, "rel-d666ef6-01", bound, names=locklib.JOBS_BOUND_NAMES)
    assert set(NEW_REPO_FILES) <= set(lock["repo_files"]) and set(NEW_TEST_FILES) <= set(lock["tests"])
    assert set(lock["bound_files"]) == set(locklib.JOBS_BOUND_NAMES)
    assert locklib.lock_problems(ROOT, lock) == []
    for rel in NEW_REPO_FILES:
        broken = {**lock, "repo_files": {**lock["repo_files"], rel: "0" * 64}}
        assert any(rel in problem for problem in locklib.lock_problems(ROOT, broken)), rel
    for rel in NEW_TEST_FILES:
        broken = {**lock, "tests": {**lock["tests"], rel: "0" * 64}}
        assert any(rel in problem for problem in locklib.lock_problems(ROOT, broken)), rel


def test_jp01_a_new_file_left_out_of_the_lock_is_found_and_a_lock_missing_a_bound_name_is_refused(tmp_path):
    lock = locklib.build_lock(ROOT, "rel-d666ef6-01", {n: tmp_path / "x" for n in locklib.JOBS_BOUND_NAMES}, names=locklib.JOBS_BOUND_NAMES)
    for rel in NEW_REPO_FILES:
        left_out = {**lock, "repo_files": {k: v for k, v in lock["repo_files"].items() if k != rel}}
        assert any(f"{rel} is not in the lock" in p for p in locklib.lock_problems(ROOT, left_out)), rel
    with pytest.raises(ValueError):
        locklib.build_lock(ROOT, "rel-d666ef6-01", {"bindings": tmp_path / "x"}, names=locklib.JOBS_BOUND_NAMES)
    with pytest.raises(ValueError):
        locklib.build_lock(ROOT, "rel-d666ef6-01", {n: tmp_path / "x" for n in locklib.JOBS_BOUND_NAMES})


def test_jp01_every_python_source_of_the_release_folder_that_a_jobs_action_runs_is_in_the_lock():
    run_by_the_paste = {"bound_readback.py", "jobs_only.py", "jobs_run.py", "plan.py", "services_only.py", "chain_evidence.py"}
    for name in run_by_the_paste:
        assert f"core/setup/release/{name}" in locklib.REPO_FILES, name


# JP-02: forbidden verbs, scanned over argv and never over file text

FORBIDDEN = (("update-traffic",), ("run", "deploy"), ("run", "services", "update"), ("scheduler",), ("set-iam-policy",),
             ("add-iam-policy-binding",), ("iam",), ("jobs", "execute"), ("jobs", "delete"), ("secrets",), ("--set-secrets",),
             ("--update-env-vars",), ("--remove-env-vars",))


def contains(argv, sequence):
    tokens = [str(t) for t in argv]
    n = len(sequence)
    return any(tuple(tokens[i:i + n]) == sequence for i in range(len(tokens) - n + 1))


def forbidden_in(argvs):
    return [(list(a), s) for a in argvs for s in FORBIDDEN if contains(a, s)]


def renders():
    ctx = plan.JobsCtx(release_id=jw.B_RID, commit=jw.B_COMMIT, helper="core/setup/release/bound_readback.py", runner="core/setup/release/jobs_run.py",
                       bindings="b", evidence="e", digest=jw.NEW_DIGEST, rollback_digest=jw.ROLLBACK_DIGEST, image_tag="t", schema_effects=True)
    return [list(s.argv) for action in plan.JOBS_ACTIONS.values() for s in action(ctx)]


def test_jp02_the_scan_finds_each_forbidden_verb_in_an_argv():
    probes = [["gcloud", "run", "services", "update-traffic", "f42-api"], ["gcloud", "run", "deploy", "x"], ["gcloud", "run", "services", "update", "x"],
              ["gcloud", "scheduler", "jobs", "pause", "x"], ["gcloud", "run", "services", "set-iam-policy", "x"],
              ["gcloud", "run", "services", "add-iam-policy-binding", "x"], ["gcloud", "iam", "service-accounts", "list"],
              ["gcloud", "run", "jobs", "execute", "x"], ["gcloud", "run", "jobs", "delete", "x"], ["gcloud", "secrets", "list"],
              ["gcloud", "run", "jobs", "update", "x", "--set-secrets", "a=b"], ["gcloud", "run", "jobs", "update", "x", "--update-env-vars", "a=1"],
              ["gcloud", "run", "jobs", "update", "x", "--remove-env-vars", "a"]]
    assert len(forbidden_in(probes)) == len(probes)
    assert forbidden_in([["gcloud", "run", "jobs", "update", "f42-collect", "--image", "x@sha256:" + "a" * 64]]) == []


def test_jp02_the_services_entries_plan_py_also_holds_do_not_trip_a_scan_over_argv():
    text = (RELEASE / "plan.py").read_text(encoding="utf-8")
    assert "update-traffic" in text and "--remove-tags" in text  # the file text would fail a text scan
    assert forbidden_in(renders()) == []


def test_jp02_the_plan_render_of_the_three_actions_carries_no_forbidden_verb():
    argvs = renders()
    assert len(argvs) > 60 and forbidden_in(argvs) == []


def test_jp02_the_orchestrator_call_log_carries_no_forbidden_verb_in_any_branch(tmp_path):
    w = jw.ReleaseWorld(tmp_path)
    w.prepare()
    clock = cw.Clock(w.now)
    cloud = cw.FakeCloudRun(w.world, clock)
    w.fake_reader = cloud
    w.run("BeforeAnyWrite", reader=cloud)
    w.built()
    w.run("BeforeJobsUpdate", reader=cloud)
    from core.setup.release import jobs_run as jr

    cloud.fail["f42-detect"] = 1
    jr.run_update(w.bound, cloud, w.bq_client(), w.evidence, now=clock.now)
    cloud.fail.clear()
    jr.run_rollback(w.bound, cloud, w.evidence, now=clock.now)
    jr.run_snapshot(w.bound, cloud, w.bq_client(), w.evidence, now=clock.now)
    assert len(cloud.argv_calls) > 60 and forbidden_in(cloud.argv_calls) == []


def test_jp02_the_producer_command_set_carries_no_forbidden_verb(tmp_path):
    fix = jw.ChainFixture(tmp_path)
    fix.write_bindings()
    reader = fix.reader()
    ce.build_manifest(fix.bound, reader, fix.bq(), fix.day, "baseline", now=lambda: jw.utc("2026-10-11T09:00:00+00:00"))
    assert len(reader.argv_log) > 20 and forbidden_in(reader.argv_log) == []
    assert forbidden_in([list(prefix) for prefix in helper.READ_PREFIXES]) == []


def test_jp02_the_paste_world_call_logs_carry_no_forbidden_verb(tmp_path):
    if shutil.which("pwsh") is None:
        pytest.fail("pwsh is not installed; the paste world cannot run (install PowerShell 7)")
    from core.setup.tests.jobs_paste_world import JobsPasteWorld

    for action in ("JobsCandidate", "JobsUpdate", "JobsRollback"):
        result = JobsPasteWorld(tmp_path / action, action).run()
        assert result.returncode == 0, result.stderr
        argvs = [c["argv"] for c in result.external]
        assert len(argvs) >= 5 and forbidden_in(argvs) == [], action


# JP-03: no statement that removes or replaces data in a file B adds under core/setup/release

B_FILES = ("chain_evidence.py", "jobs_only.py", "jobs_run.py", "JOBS-PASTE.ps1")
SERVICES_FILES = {"bound_readback.py", "declared_env_removals.py", "lock.py", "packet.py", "plan.py", "services_only.py", "SERVICES-PASTE.ps1",
                  "durable-effects-manifest.template.json", "__init__.py"}
DESTRUCTIVE = re.compile(r"create\s+or\s+replace|\bdrop\s+(table|view|schema|function|index)\b|\bdelete\s+from\b|\btruncate\s+table\b|\bdrop\b|\btruncate\b", re.I)


def test_jp03_no_create_or_replace_drop_delete_or_truncate_text_in_any_file_b_adds():
    for name in B_FILES:
        text = (RELEASE / name).read_text(encoding="utf-8")
        assert DESTRUCTIVE.search(text) is None, (name, DESTRUCTIVE.search(text))
        assert not re.search(r"\bdelete\b", text, re.I), name


def test_jp03_every_file_in_the_release_folder_is_either_a_services_file_or_scanned_here():
    present = {p.name for p in RELEASE.iterdir() if p.is_file()}
    assert present - SERVICES_FILES <= set(B_FILES), sorted(present - SERVICES_FILES - set(B_FILES))


def test_jp03_the_scan_finds_the_statements_it_is_there_for():
    for text in ("CREATE OR REPLACE TABLE x", "drop table x", "DELETE FROM x WHERE 1", "TRUNCATE TABLE x", "create  or\treplace view v"):
        assert DESTRUCTIVE.search(text), text


# JP-04: the mutation table. Each row changes one line of a copy of the tooling and names the test that must then fail.

MUTATIONS = (
    ("drop the window check before an update", "core/setup/release/jobs_run.py",
     '            if not jo.window_open(bound, now()):\n                stop(log, job, "WINDOW"', '            if False:\n                stop(log, job, "WINDOW"',
     "core/setup/tests/test_jobs_update.py::test_ju06_once_the_clock_passes_2100_mid_run_the_next_update_is_refused_outside_the_chain_group"),
    ("drop the WINDOW rule in the producer", "core/setup/release/chain_evidence.py",
     'not jo.aware(collect_exec["start"]) > a_at', "False",
     "core/setup/tests/test_chain_evidence.py::test_jm02_window_when_collect_started_before_the_terminal_readback_of_release_a"),
    ("reverse the chain group order", "core/setup/release/jobs_only.py",
     '"f42-calendar", "f42-digest", "f42-scheduled-asks", *CHAIN_GROUP)', '"f42-calendar", "f42-digest", "f42-scheduled-asks", *reversed(CHAIN_GROUP))',
     "core/setup/tests/test_jobs_plan.py::test_ju01_jobs_update_renders_exactly_14_update_steps_in_the_order_of_3_6_each_followed_by_its_readback"),
    ("accept a list-only digest", "core/setup/release/jobs_only.py",
     '    if match is None:\n        raise Probe("An execution description carries no image digest")\n',
     '    if match is None:\n        match = re.search(r"(sha256:[0-9a-f]{64})", json.dumps(raw)) or re.search(r"(x)", "x")\n',
     "core/setup/tests/test_chain_evidence.py::test_jm06_a_describe_with_no_digest_form_is_a_probe_and_exit_3_even_when_list_has_one"),
    ("accept a cluster error", "core/setup/release/chain_evidence.py",
     '                or any(isinstance(v, dict) and v.get("error") for v in cluster.values()))', '                or False)',
     "core/setup/tests/test_chain_evidence.py::test_jm02_degraded_when_a_market_carries_a_cluster_error"),
    ("accept a partial run", "core/setup/release/chain_evidence.py",
     'counts.get("embed_error") or counts.get("partial")\n                or any', 'counts.get("embed_error")\n                or any',
     "core/setup/tests/test_chain_evidence.py::test_jm02_degraded_when_understand_shows_an_error_or_any_partial"),
    ("accept an index failure as disqualifying", "core/setup/release/chain_evidence.py",
     'return bool(counts.get("enrich_error") or counts.get("embed_error") or counts.get("partial")',
     'return bool(counts.get("enrich_error") or counts.get("embed_error") or counts.get("partial") or counts.get("index") == "failed"',
     "core/setup/tests/test_chain_evidence.py::test_jm03_an_index_failure_alone_on_the_bound_image_after_release_a_qualifies"),
    ("skip the second snapshot read", "core/setup/release/jobs_run.py",
     '    second = jo.quiet_snapshot(adapter, runner_for(bound, bq_client), started)\n', '    second = first\n',
     "core/setup/tests/test_jobs_update.py::test_ju07_the_second_read_comes_before_the_first_update_call"),
    ("allow a 15th update", "core/setup/release/jobs_only.py",
     '"f42-calendar", "f42-digest", "f42-scheduled-asks", *CHAIN_GROUP)', '"f42-calendar", "f42-digest", "f42-scheduled-asks", *CHAIN_GROUP, "f42-watchdog")',
     "core/setup/tests/test_jobs_plan.py::test_ju01_jobs_update_renders_exactly_14_update_steps_in_the_order_of_3_6_each_followed_by_its_readback"),
    ("allow mode full on a pinned service", "core/setup/release/bound_readback.py",
     '        so.require(args.mode in ("services-only", "jobs"), "MODE", "Only services-only and jobs modes are carried; full mode stays refused")\n'
     '        so.require(isinstance(bound, dict) and bound.get("mode") == args.mode, "MODE", f"The bindings are not for {args.mode} mode")\n',
     '        pass\n',
     "core/setup/tests/test_jobs_readback.py::test_jr01_mode_full_with_services_only_bindings_is_refused_before_any_read"),
    ("accept an execution of another job", "core/setup/release/chain_evidence.py",
     'execution["job"] != jo.STAGE_JOB[stage] or not counted', 'not counted',
     "core/setup/tests/test_chain_evidence.py::test_jm02_bound_three_ways"),
    ("accept the a80 service state with A never run", "core/setup/release/jobs_only.py",
     '    if a_state:\n        check_a_state(rel.baseline, live)\n', '    if a_state:\n        pass\n',
     "core/setup/tests/test_jobs_checks.py::test_ju10_the_a80_pair_with_release_a_never_run_stops_even_when_baseline_j_was_captured_from_it"),
    ("accept an expired baseline", "core/setup/release/jobs_only.py",
     'require(0 <= (today - day).days <= rel.bound["maxBaselineAgeDays"], "BASELINE_AGE",', 'require(True, "BASELINE_AGE",',
     "core/setup/tests/test_jobs_checks.py::test_ju09_an_expired_baseline_is_refused_with_baseline_age_naming_the_manifest_date"),
    ("skip the automatic restore on the 21:00 stop", "core/setup/release/jobs_run.py",
     '            if job in jo.CHAIN_GROUP:\n                restore_chain_group(', '            if job in jo.CHAIN_GROUP and log["stopped"]["code"] != "WINDOW":\n                restore_chain_group(',
     "core/setup/tests/test_jobs_prefix.py::test_jx04_inside_the_chain_group_the_abort_restores_the_prefix_in_reverse_order_then_stops"),
)


def test_jp04_every_mutation_applies_exactly_once_to_the_source_and_names_a_test_that_exists():
    assert len(MUTATIONS) == 14
    assert len({name for name, *_ in MUTATIONS}) == 14
    for name, rel, old, new, node in MUTATIONS:
        text = (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")
        assert text.count(old) == 1, (name, text.count(old))
        assert old != new
        path, _, test = node.partition("::")
        assert re.search(rf"^def {re.escape(test)}\b", (ROOT / path).read_text(encoding="utf-8"), re.M), (name, node)


def pytest_run(copy_root, nodes):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_CURRENT_TEST")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    base = copy_root / "pt"
    base.mkdir(exist_ok=True)
    stamp = str(len(list(base.iterdir())))
    proc = subprocess.run([sys.executable, "-B", "-m", "pytest", *nodes, "-q", "-x", "-p", "no:cacheprovider", f"--basetemp={base / stamp}"],
                          cwd=copy_root, env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=600)
    return proc.returncode, proc.stdout


def test_jp04_each_mutation_is_caught_by_its_named_test_in_a_copy_and_the_copy_is_clean_first(tmp_path):
    copy_root = tmp_path / "copy"
    shutil.copytree(ROOT / "core/setup", copy_root / "core/setup", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "locks"))
    shutil.copyfile(ROOT / "core/__init__.py", copy_root / "core/__init__.py")
    nodes = sorted({node for *_, node in MUTATIONS})
    code, out = pytest_run(copy_root, nodes)
    assert code == 0, "the copy is not clean before a mutation:\n" + out[-1500:]
    for name, rel, old, new, node in MUTATIONS:
        target = copy_root / rel
        original = target.read_bytes()
        text = original.decode("utf-8")
        newline = "\r\n" if "\r\n" in text else "\n"
        body = text.replace("\r\n", "\n")
        assert body.count(old) == 1, name
        target.write_bytes(body.replace(old, new).replace("\n", newline).encode("utf-8"))
        try:
            code, out = pytest_run(copy_root, [node])
        finally:
            target.write_bytes(original)
        assert code == 1 and re.search(r"\b[1-9][0-9]* failed\b", out), (name, code, out[-800:])
        assert target.read_bytes() == original


# JP-05: hygiene on the branch range, scoped to the commits that touch this lane's files (RB-T6). A branch that has other lanes merged
# into it carries their commits too, and their messages follow their own review; the whole range is a one-shot step for a reviewer
# (py -3.13 -m core.setup.tests.range_hygiene <base> <head>), not a node of the core suite.

from core.setup.tests import range_hygiene as rh  # noqa: E402

EM, EN = rh.EM, rh.EN


def lane_commits():
    try:
        shas = rh.commits(ROOT, BASE, "HEAD", rh.LANE_PATHS)
    except RuntimeError as error:
        pytest.fail(f"{error} (the hygiene range is {BASE}..HEAD)")
    assert shas, "no commit of the range touches the lane's files; the base or the lane list is wrong"
    return shas


def test_jp05_the_lane_paths_are_files_the_lock_lists_and_the_scan_finds_the_commits_that_touch_them():
    assert set(NEW_REPO_FILES) | set(NEW_TEST_FILES) <= set(rh.LANE_PATHS)
    assert all((ROOT / path).is_file() for path in rh.LANE_PATHS)
    assert len(lane_commits()) >= 4


def test_jp05_author_and_committer_of_every_lane_commit_are_albert_meintjes():
    assert rh.identity_problems(ROOT, lane_commits()) == []


def test_jp05_no_lane_commit_carries_a_trailer_or_a_dash_or_a_prose_double_hyphen_in_its_message():
    assert rh.message_problems(ROOT, lane_commits()) == []


def test_jp05_a_case_insensitive_search_of_the_lane_commits_for_tool_and_vendor_names_returns_nothing():
    assert rh.name_problems(ROOT, BASE, "HEAD", lane_commits(), rh.LANE_PATHS) == []


def test_jp05_no_em_or_en_dash_is_added_to_a_lane_file():
    assert rh.dash_problems(ROOT, BASE, "HEAD", rh.LANE_PATHS) == []


def test_jp05_the_lane_adds_no_agent_or_instruction_file_and_no_scratch_output_in_its_folders():
    assert rh.file_problems(ROOT, BASE, "HEAD", rh.LANE_FOLDERS) == []


# the scan itself, on a small repository made for the purpose: it must ignore other lanes' commits and find the lane's own faults

ALBERT = ("Albert Meintjes", "albert.meintjes@ogilvy.co.za")
OTHER = ("Someone Else", "someone.else@example.com")
LANE_FILE = "core/setup/release/jobs_run.py"


def scratch_repo(tmp_path):
    root = Path(tmp_path) / "scan"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    return root


def commit(root, who, files, message):
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        subprocess.run(["git", "add", "--", name], cwd=root, check=True, capture_output=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": who[0], "GIT_AUTHOR_EMAIL": who[1], "GIT_COMMITTER_NAME": who[0], "GIT_COMMITTER_EMAIL": who[1]}
    subprocess.run(["git", "-c", "core.autocrlf=false", "commit", "-q", "--allow-empty", "-m", message], cwd=root, check=True, capture_output=True, env=env)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, encoding="utf-8").stdout.strip()


def lane_scan(root, base, paths=(LANE_FILE,), folders=("core/setup/release",)):
    return rh.scan(root, base, "HEAD", list(paths), list(folders))


def test_jp05_scan_ignores_a_commit_that_does_not_touch_the_lane_files_whatever_its_author_or_message_holds(tmp_path):
    root = scratch_repo(tmp_path)
    base = commit(root, ALBERT, {"README": "x\n"}, "Start the repository")
    commit(root, ALBERT, {LANE_FILE: "one\n"}, "Add the update verb, which takes --bindings and --evidence")
    commit(root, OTHER, {"app/other.py": "two\n"}, "Other lane -- fix\n\nOwner: someone")
    assert lane_scan(root, base) == []
    whole = rh.scan(root, base, "HEAD")
    assert any("author or committer" in line for line in whole) and any("trailer" in line for line in whole)
    assert any("double hyphen" in line for line in whole)


def test_jp05_scan_finds_a_lane_commit_by_someone_else_with_a_trailer_a_dash_or_a_prose_double_hyphen(tmp_path):
    root = scratch_repo(tmp_path)
    base = commit(root, ALBERT, {"README": "x\n"}, "Start the repository")
    commit(root, OTHER, {LANE_FILE: "one\n"}, "Plain message")
    assert any("author or committer" in line for line in lane_scan(root, base))
    cases = (("Plain message\n\nOwner: someone", "trailer"), ("Plain " + EM + " message", "dash"), ("Plain " + EN + " message", "dash"),
             ("a -- b", "double hyphen"), ("word--word", "double hyphen"), ("ends with --", "double hyphen"))
    for index, (message, expected) in enumerate(cases):
        sub = scratch_repo(Path(tmp_path) / f"case{index}")
        start = commit(sub, ALBERT, {"README": "x\n"}, "Start")
        commit(sub, ALBERT, {LANE_FILE: "one\n"}, message)
        assert any(expected in line for line in lane_scan(sub, start)), (message, lane_scan(sub, start))


def test_jp05_a_command_line_flag_in_a_message_is_allowed_and_a_message_without_one_passes(tmp_path):
    root = scratch_repo(tmp_path)
    base = commit(root, ALBERT, {"README": "x\n"}, "Start the repository")
    commit(root, ALBERT, {LANE_FILE: "one\n"}, "Run it with --apply and --as-of, or pass -n\n\nThe flag --dry-run changes nothing.")
    assert lane_scan(root, base) == []
    assert rh.prose_double_hyphen("a -- b") and rh.prose_double_hyphen("x--y") and not rh.prose_double_hyphen("use --apply here")


def test_jp05_scan_finds_a_tool_name_an_added_dash_and_an_agent_file_in_the_lane(tmp_path):
    root = scratch_repo(tmp_path)
    base = commit(root, ALBERT, {"README": "x\n"}, "Start the repository")
    commit(root, ALBERT, {LANE_FILE: "# made with " + rh.TOOL_NAMES[0] + "\n"}, "Add a comment")
    assert any("tool or vendor name" in line for line in lane_scan(root, base))
    root2 = scratch_repo(Path(tmp_path) / "dash")
    base2 = commit(root2, ALBERT, {"README": "x\n"}, "Start")
    commit(root2, ALBERT, {LANE_FILE: "a " + EM + " b\n"}, "Add a line")
    assert any("em or en dash" in line for line in lane_scan(root2, base2))
    root3 = scratch_repo(Path(tmp_path) / "agent")
    base3 = commit(root3, ALBERT, {"README": "x\n"}, "Start")
    commit(root3, ALBERT, {LANE_FILE: "one\n", "core/setup/release/" + "AGE" + "NTS.md": "notes\n"}, "Add a file")
    assert any("must not be committed" in line for line in lane_scan(root3, base3))


def test_jp05_scan_says_so_when_no_commit_touches_the_lane_files(tmp_path):
    root = scratch_repo(tmp_path)
    base = commit(root, ALBERT, {"README": "x\n"}, "Start the repository")
    commit(root, ALBERT, {"app/x.py": "x\n"}, "Another file")
    assert any("no commit of the range touches" in line for line in lane_scan(root, base))


def test_jp05_a_merge_commit_by_another_committer_is_not_a_lane_commit(tmp_path):
    root = scratch_repo(tmp_path)
    base = commit(root, ALBERT, {"README": "x\n"}, "Start the repository")
    commit(root, ALBERT, {LANE_FILE: "one\n"}, "Lane work")
    subprocess.run(["git", "checkout", "-q", "-b", "side", base], cwd=root, check=True, capture_output=True)
    commit(root, OTHER, {"app/side.py": "s\n"}, "Side work")
    subprocess.run(["git", "checkout", "-q", "-"], cwd=root, check=True, capture_output=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": OTHER[0], "GIT_AUTHOR_EMAIL": OTHER[1], "GIT_COMMITTER_NAME": OTHER[0], "GIT_COMMITTER_EMAIL": OTHER[1]}
    subprocess.run(["git", "merge", "--no-ff", "-q", "-m", "Merge side", "side"], cwd=root, check=True, capture_output=True, env=env)
    assert lane_scan(root, base) == []


def test_jp05_a_merge_commit_that_resolves_a_conflict_in_a_lane_file_is_not_a_lane_commit_either(tmp_path):
    root = scratch_repo(tmp_path)
    base = commit(root, ALBERT, {LANE_FILE: "a\nb\nc\n"}, "Start with the file")
    subprocess.run(["git", "checkout", "-q", "-b", "side"], cwd=root, check=True, capture_output=True)
    commit(root, ALBERT, {LANE_FILE: "a\nside\nc\n"}, "Side edit")
    subprocess.run(["git", "checkout", "-q", "-"], cwd=root, check=True, capture_output=True)
    commit(root, ALBERT, {LANE_FILE: "a\nmain\nc\n"}, "Main edit")
    env = {**os.environ, "GIT_AUTHOR_NAME": OTHER[0], "GIT_AUTHOR_EMAIL": OTHER[1], "GIT_COMMITTER_NAME": OTHER[0], "GIT_COMMITTER_EMAIL": OTHER[1]}
    merged = subprocess.run(["git", "merge", "--no-ff", "-q", "-m", "Merge side", "side"], cwd=root, capture_output=True, env=env)
    assert merged.returncode != 0  # the same line changed on both sides
    (root / LANE_FILE).write_text("a\nboth\nc\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "--", LANE_FILE], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "--no-edit"], cwd=root, check=True, capture_output=True, env=env)
    assert len(subprocess.run(["git", "log", "--merges", "--format=%H"], cwd=root, capture_output=True, encoding="utf-8").stdout.split()) == 1
    assert lane_scan(root, base) == []


def test_jp05_the_base_must_exist_or_the_nodes_fail_and_never_skip():
    with pytest.raises(RuntimeError):
        rh.commits(ROOT, "0" * 40, "HEAD", rh.LANE_PATHS)


# JP-06: the release test map knows the seven prefixes

SEVEN = {"JM": 11, "JB": 8, "JS": 11, "JU": 10, "JR": 6, "JX": 8, "JP": 6}
SEVEN_PARTITIONS = {"JM": "core", "JB": "core", "JS": "core", "JR": "core", "JX": "core", "JU": "deploy", "JP": "deploy"}


def test_jp06_the_map_counts_partitions_pattern_and_collection_roots_name_the_seven_prefixes():
    from core.setup.tests import test_release_map as tm

    assert {p: tm.COUNTS[p] for p in SEVEN} == SEVEN and sum(SEVEN.values()) == 60
    assert {p: tm.PARTITION_OF_PREFIX[p] for p in SEVEN} == SEVEN_PARTITIONS
    for prefix in SEVEN:
        assert tm.NODE.match(f"test_{prefix.lower()}01_something") and tm.NODE.match(f"test_{prefix.lower()}07"), prefix
    assert tm.NODE.match("test_jz01_x") is None
    assert {"core/collect/tests", "core/detect/tests"} <= set(tm.COLLECTION_ROOTS)
    data = tm.load_map()
    for prefix, count in SEVEN.items():
        for n in range(1, count + 1):
            entry = data["ids"][f"{prefix}-{n:02d}"]
            assert entry["partition"] == SEVEN_PARTITIONS[prefix] and entry["status"] in ("built", "pending")
            assert entry["status"] == "built" or entry["reason"]


def test_jp06_every_built_id_of_the_seven_prefixes_has_a_node_in_a_file_of_its_partition():
    from core.setup.tests import test_release_map as tm

    data = tm.load_map()
    found = tm.collected()
    files_of = {name: set(files) for name, files in data["partitions"].items()}
    for prefix in SEVEN:
        for n in range(1, SEVEN[prefix] + 1):
            ident, entry = f"{prefix}-{n:02d}", data["ids"][f"{prefix}-{n:02d}"]
            if entry["status"] == "built":
                assert ident in found and found[ident] <= files_of[entry["partition"]], (ident, found.get(ident))
            else:
                assert ident not in found, ident


def test_jp06_a_missing_prerequisite_fails_the_map_nodes_and_never_skips_them(tmp_path):
    import ast

    text = (ROOT / "core/setup/tests/test_release_map.py").read_text(encoding="utf-8")
    used = {node.attr for node in ast.walk(ast.parse(text)) if isinstance(node, ast.Attribute)}
    assert "fail" in used and not used & {"skip", "skipif", "xfail", "importorskip"}
    from core.setup.tests import release_prereq

    missing = release_prereq.prerequisites(tmp_path, env={"F42_BASH": str(ROOT / "nope")}, which=lambda name: None)
    assert missing[0] == "pwsh" and missing[1] == "bash"
