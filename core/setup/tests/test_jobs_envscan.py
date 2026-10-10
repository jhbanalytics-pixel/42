"""The environment read scan (W8-REL-B v2.1 section 3.5, JB-06): B's job code reads no setting the live job definitions lack.
Offline: the trees are lists of (path, text) as durable_effects_check.git_tree_files returns them."""
import copy
import re
import subprocess
from pathlib import Path

import pytest

from core.setup import deploy_jobs as dj
from core.setup import durable_effects_check as de
from core.setup.release import env_scan

ROOT = Path(__file__).resolve().parents[3]
A80 = "a80be1dee7f4ee2aa9775d0f353bab930809f057"


def tree(**files):
    return [(path.replace("__", "/"), text) for path, text in files.items()]


def scan(head, base=(), names=()):
    return env_scan.scan(list(base), list(head), set(names))


def one(head, base=(), names=()):
    found = scan(head, base, names)
    assert len(found) == 1, found
    return found[0]


def test_jb06_a_new_read_of_an_unclassified_name_fails_with_its_file_and_line():
    head = [("core/collect/job.py", 'import os\n\n\ndef f():\n    return os.environ.get("NEW_SETTING", "")\n')]
    finding = one(head)
    assert (finding.path, finding.line, finding.name) == ("core/collect/job.py", 5, "NEW_SETTING")
    assert "core/collect/job.py:5" in str(finding) and "NEW_SETTING" in str(finding)


@pytest.mark.parametrize("source", [
    'import os\nx = os.getenv("NEW_SETTING")\n',
    'import os\nx = os.environ["NEW_SETTING"]\n',
    'from os import environ\nx = environ.get("NEW_SETTING")\n',
    'import os\nx = os.environ.get("NEW_SETTING")\n',
    'import os\nx = os.environ.pop("NEW_SETTING", None)\n',
    'import os\nx = "NEW_SETTING" in os.environ\n',
])
def test_jb06_every_form_of_environment_read_is_seen(source):
    assert one([("core/a.py", source)]).name == "NEW_SETTING"


def test_jb06_a_write_to_the_environment_is_not_a_read():
    assert scan([("core/a.py", 'import os\nos.environ["NEW_SETTING"] = "1"\n')]) == []


def test_jb06_platform_injected_names_pass():
    head = [("core/collect/chain.py", 'import os\na = os.environ.get("CLOUD_RUN_JOB")\nb = os.environ.get("CLOUD_RUN_TASK_ATTEMPT")\n')]
    assert scan(head) == []
    assert one([("core/a.py", 'import os\nx = os.environ.get("CLOUD_RUNNER")\n')]).name == "CLOUD_RUNNER"


def test_jb06_the_image_baked_name_passes_and_is_baked_by_the_dockerfile():
    assert scan([("core/setup/stamp.py", 'import os\nx = os.environ.get("F42_GIT_SHA")\n')]) == []
    dockerfile = (ROOT / "core/setup/jobs.Dockerfile").read_text(encoding="utf-8")
    for name in env_scan.IMAGE_BAKED:
        assert re.search(rf"^ENV {name}=", dockerfile, re.M), name


def test_jb06_a_process_local_setdefault_passes_and_a_plain_read_of_the_same_name_does_not():
    assert scan([("core/understand/cluster.py", 'import os\nos.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/x")\n')]) == []
    assert one([("core/understand/cluster.py", 'import os\nx = os.environ.get("NUMBA_CACHE_DIR")\n')]).name == "NUMBA_CACHE_DIR"


def test_jb06_a_setdefault_passes_whatever_its_name_because_it_only_fills_the_process_own_environment():
    source = 'import os\n\n\ndef f(name):\n    os.environ.setdefault(name, "x")\n'
    assert scan([("core/a.py", source)]) == []


def test_jb06_a_name_in_the_baseline_env_list_passes_and_one_absent_from_it_fails():
    head = [("core/a.py", 'import os\nx = os.environ.get("MODEL_PROVIDER")\n')]
    assert scan(head, names={"MODEL_PROVIDER"}) == []
    assert one(head, names={"F42_DATA"}).name == "MODEL_PROVIDER"


def test_jb06_a_read_the_a80_file_already_made_is_not_new_and_a_read_in_another_file_is():
    base = [("core/a.py", 'import os\nx = os.environ.get("OLD_SETTING")\n')]
    assert scan(base, base) == []
    moved = base + [("core/b.py", 'import os\nx = os.environ.get("OLD_SETTING")\n')]
    assert one(moved, base).path == "core/b.py"


def test_jb06_tests_and_the_release_tooling_are_outside_the_scan():
    head = [("core/collect/tests/test_a.py", 'import os\nx = os.environ.get("NEW_SETTING")\n'),
            ("core/setup/release/jobs_only.py", 'import os\nx = os.environ.get("NEW_SETTING")\n'),
            ("core/setup/tests/conftest.py", 'import os\nx = os.environ.get("NEW_SETTING")\n')]
    assert scan(head) == []
    assert one([("core/setup/release_extra.py", 'import os\nx = os.environ.get("NEW_SETTING")\n')]).name == "NEW_SETTING"


def test_jb06_a_name_that_is_not_a_literal_cannot_be_classified_and_fails():
    finding = one([("core/a.py", 'import os\n\n\ndef f(name):\n    return os.environ.get(name)\n')])
    assert finding.name == "<dynamic>" and finding.line == 5


STAMP_LIKE = '''import os


def build(env=None):
    env = os.environ if env is None else env
    return {"git_sha": env.get("F42_GIT_SHA") or env.get("F42_VERSION"), "digest": env.get("F42_IMAGE_DIGEST")}
'''


def test_jb06_a_whole_mapping_taken_from_the_environment_is_judged_by_the_names_the_function_reads_from_it():
    found = scan([("core/setup/other_stamp.py", STAMP_LIKE)])
    assert sorted((f.name, f.line) for f in found) == [("F42_IMAGE_DIGEST", 6), ("F42_VERSION", 6)]
    assert scan([("core/setup/other_stamp.py", STAMP_LIKE)], names={"F42_VERSION", "F42_IMAGE_DIGEST"}) == []


def test_jb06_a_whole_mapping_with_no_literal_name_to_judge_fails_as_a_mapping():
    finding = one([("core/a.py", 'import os\n\n\ndef f():\n    return dict(os.environ)\n')])
    assert finding.name == "<mapping>" and finding.line == 5


def test_jb06_the_scan_reads_the_added_reads_of_this_checkout_and_classifies_the_chain_and_cluster_ones():
    a80 = de.git_tree_files(A80, "core", ROOT)
    head = env_scan.tree_from_disk(ROOT / "core", ROOT)
    inventory = env_scan.added_reads(a80, head)
    names = {r.name: r for r in inventory}
    for name in ("CLOUD_RUN_JOB", "CLOUD_RUN_TASK_ATTEMPT", "NUMBA_CACHE_DIR", "F42_FIXTURE_STATE"):
        assert name in names, f"{name} is not among the added environment reads"
    unclassified = {f.name for f in env_scan.scan(a80, head, set())}
    assert not {"CLOUD_RUN_JOB", "CLOUD_RUN_TASK_ATTEMPT", "NUMBA_CACHE_DIR", "F42_GIT_SHA"} & unclassified
    for finding in env_scan.scan(a80, head, set()):
        assert finding.path.startswith("core/") and finding.line > 0


def test_jb06_the_cli_exits_1_naming_the_file_and_line_when_a_read_is_unclassified(tmp_path, capsys):
    base = tmp_path / "base"
    head = tmp_path / "head"
    for folder, text in ((base, "x = 1\n"), (head, 'import os\nx = os.environ.get("NEW_SETTING")\n')):
        (folder / "core").mkdir(parents=True)
        (folder / "core" / "a.py").write_text(text, encoding="utf-8")
    code = env_scan.main(["--base-dir", str(base), "--head-dir", str(head)])
    assert code == 1
    assert "core/a.py:2" in capsys.readouterr().out
    (head / "core" / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert env_scan.main(["--base-dir", str(base), "--head-dir", str(head)]) == 0


# F42_FIXTURE_STATE: no job definition sets it, so unset must take the normal path.

def test_jb06_no_job_definition_sets_f42_fixture_state():
    for job in dj.JOBS:
        assert not any(entry.startswith("F42_FIXTURE_STATE") for entry in job.env), job.name
    assert "F42_FIXTURE_STATE" not in (ROOT / "core/setup/deploy_jobs.py").read_text(encoding="utf-8")


def test_jb06_with_f42_fixture_state_unset_the_fixture_agent_takes_the_normal_path(monkeypatch):
    from core.api import agent_app, fixture_states

    monkeypatch.delenv("F42_FIXTURE_STATE", raising=False)
    monkeypatch.setenv("F42_FIXTURE_DELAY", "0")

    def forbidden(*args, **kwargs):
        raise AssertionError("fixture_states.build ran with F42_FIXTURE_STATE unset")

    monkeypatch.setattr(fixture_states, "build", forbidden)
    request = {"ask_id": "a_0123456789ab", "question": "What is behind #fixture in South Africa this week?", "market": "ZA"}
    got = agent_app.fixture_agent(request, lambda event: None, lambda: False)
    assert set(got) == {"answer", "run", "answer_meta"}
    assert got["answer"]["status"] == "complete"


# RB-C7: an optional tuning read, and two names classified explicitly

LEARN = ("core/detect/learn.py", "LEARN_OUTCOME_DEADLINE_SECONDS")
TUNING_SOURCE = 'import os\n\n\ndef f():\n    return float(os.environ.get("LEARN_OUTCOME_DEADLINE_SECONDS", ""))\n'


def test_rbc7_the_optional_tuning_read_passes_in_its_own_file_and_nowhere_else():
    assert scan([(LEARN[0], TUNING_SOURCE)]) == []
    assert one([("core/detect/other.py", TUNING_SOURCE)]).name == LEARN[1]
    assert one([(LEARN[0], TUNING_SOURCE.replace("LEARN_OUTCOME_DEADLINE_SECONDS", "LEARN_OTHER_SECONDS"))]).name == "LEARN_OTHER_SECONDS"


def test_rbc7_the_pinned_default_is_what_the_code_uses_when_the_variable_is_unset_empty_or_not_a_number(monkeypatch):
    from core.detect import learn

    assert env_scan.OPTIONAL_UNSET_DEFAULT[LEARN] == 480
    for value in (None, "", "soon", "-5", "0", "nan"):
        if value is None:
            monkeypatch.delenv("LEARN_OUTCOME_DEADLINE_SECONDS", raising=False)
        else:
            monkeypatch.setenv("LEARN_OUTCOME_DEADLINE_SECONDS", value)
        assert learn.outcome_deadline_setting() == (env_scan.OPTIONAL_UNSET_DEFAULT[LEARN], None), value
    assert learn.OUTCOME_DEADLINE_SECONDS == env_scan.OPTIONAL_UNSET_DEFAULT[LEARN]
    monkeypatch.setenv("LEARN_OUTCOME_DEADLINE_SECONDS", "60")
    assert learn.outcome_deadline_setting() == (60.0, None)
    monkeypatch.setenv("LEARN_OUTCOME_DEADLINE_SECONDS", "9999")      # never above the default: a longer wait costs the run its row
    assert learn.outcome_deadline_setting() == (480.0, 9999.0)


def test_rbc7_no_job_definition_sets_the_tuning_variable_so_every_run_takes_the_default():
    for job in dj.JOBS:
        assert not any(entry.startswith("LEARN_OUTCOME_DEADLINE_SECONDS") for entry in job.env), job.name


def test_rbc7_agent_audience_is_classified_as_service_only_in_its_file_and_that_file_is_not_job_code():
    pair = ("core/api/app.py", "AGENT_AUDIENCE")
    assert pair in env_scan.SERVICE_ONLY
    source = 'import os\nx = os.environ.get("AGENT_AUDIENCE", "")\n'
    assert scan([(pair[0], source)]) == []
    assert one([("core/api/other.py", source)]).name == "AGENT_AUDIENCE"
    files = env_scan.tree_from_disk(ROOT / "core", ROOT)
    entries = sorted({job.module for job in dj.JOBS if job.module} | {job.module for job in dj.SMOKE_JOBS})
    reachable = env_scan.import_closure(files, entries)
    assert "core.api.digest" in reachable and "core.api.scheduled" in reachable      # the closure does reach the api package
    assert "core.api.app" not in reachable, "a job now imports core.api.app, which AGENT_AUDIENCE was classified as not reaching"


def test_rbc7_the_closure_follows_plain_from_and_lazy_imports():
    files = [("core/a.py", "import core.b\n"), ("core/b.py", "def f():\n    from core import c\n"), ("core/c.py", "x = 1\n"),
             ("core/d.py", "x = 1\n"), ("core/e.py", "x = 1\n"), ("core/__init__.py", "import core.e\n")]
    # importing core.a runs core/__init__.py first, which imports core.e; core.d is imported by nothing
    assert env_scan.import_closure(files, ["core.a"]) == {"core", "core.a", "core.b", "core.c", "core.e"}
    nested = [("core/__init__.py", ""), ("core/pkg/__init__.py", "import core.e\n"), ("core/pkg/m.py", "x = 1\n"), ("core/e.py", "x = 1\n")]
    assert env_scan.import_closure(nested, ["core.pkg.m"]) == {"core", "core.pkg", "core.pkg.m", "core.e"}


def test_rbc7_the_test_harness_variable_is_classified_in_conftest_and_that_file_is_outside_the_scan():
    pair = ("core/conftest.py", "F42_TEST_LOCALITY_AUTHORITY")
    assert pair in env_scan.TEST_HARNESS
    assert not env_scan.in_scope(pair[0])
    read = env_scan.Read(pair[0], 61, pair[1], "get")
    assert env_scan.classified(read, set())
    assert not env_scan.classified(env_scan.Read("core/other.py", 1, pair[1], "get"), set())
    readers = {p for p, t in env_scan.tree_from_disk(ROOT / "core", ROOT) if "F42_TEST_LOCALITY_AUTHORITY" in t}
    assert readers == {"core/conftest.py", "core/detect/tests/test_detect_scorecard.py", "core/setup/release/env_scan.py",
                       "core/setup/tests/test_jobs_envscan.py"}


def test_rbc7_the_scan_of_this_checkout_no_longer_reports_the_three_names():
    a80 = de.git_tree_files(A80, "core", ROOT)
    head = env_scan.tree_from_disk(ROOT / "core", ROOT)
    names = {f.name for f in env_scan.scan(a80, head, set())}
    assert not names & {"LEARN_OUTCOME_DEADLINE_SECONDS", "AGENT_AUDIENCE", "F42_TEST_LOCALITY_AUTHORITY"}


# RJ-1: the class of an optional read whose unset value is the pinned default, and the real tree

FIXTURE = ("core/api/agent_app.py", "F42_FIXTURE_STATE")
VERSION = ("core/setup/stamp.py", "F42_VERSION")
DIGEST = ("core/setup/stamp.py", "F42_IMAGE_DIGEST")


def test_rj1_the_class_names_what_it_is_and_pins_each_read_by_file_and_name_with_its_unset_value():
    assert not hasattr(env_scan, "OPTIONAL_TUNING")
    assert env_scan.OPTIONAL_UNSET_DEFAULT == {LEARN: 480, FIXTURE: None, VERSION: None, DIGEST: None}


@pytest.mark.parametrize("pair", [FIXTURE, VERSION, DIGEST])
def test_rj1_each_pinned_read_passes_in_its_own_file_and_fails_in_any_other(pair):
    path, name = pair
    source = f'import os\nx = os.environ.get("{name}")\n'
    assert scan([(path, source)]) == []
    assert one([("core/api/elsewhere.py", source)]).name == name
    assert one([(path, source.replace(name, name + "_X"))]).name == name + "_X"


def test_rj1_the_scan_of_this_checkout_at_the_release_commit_finds_nothing_outside_the_classes():
    a80 = de.git_tree_files(A80, "core", ROOT)
    head = env_scan.tree_from_disk(ROOT / "core", ROOT)
    assert [str(f) for f in env_scan.scan(a80, head, set())] == []


@pytest.mark.parametrize("value", [None, "", "no-such-fixture", "F05", " F09 "])
def test_rj1_f42_fixture_state_unset_or_empty_takes_the_normal_path_and_a_malformed_id_is_refused_never_served(monkeypatch, value):
    from core.api import agent_app, fixture_states

    monkeypatch.setenv("F42_FIXTURE_DELAY", "0")
    if value is None:
        monkeypatch.delenv("F42_FIXTURE_STATE", raising=False)
    else:
        monkeypatch.setenv("F42_FIXTURE_STATE", value)
    seen = []
    real = fixture_states.build
    monkeypatch.setattr(fixture_states, "build", lambda *a, **k: (seen.append(a[0]), real(*a, **k))[1])
    request = {"ask_id": "a_0123456789ab", "question": "What is behind #fixture in South Africa this week?", "market": "ZA"}
    if value in (None, ""):
        got = agent_app.fixture_agent(request, lambda event: None, lambda: False)
        assert seen == [] and got["answer"]["status"] == "complete" and set(got) == {"answer", "run", "answer_meta"}
        assert env_scan.OPTIONAL_UNSET_DEFAULT[FIXTURE] is None
    else:
        with pytest.raises(ValueError):
            agent_app.fixture_agent(request, lambda event: None, lambda: False)
        assert seen == [value]


@pytest.mark.parametrize("name,field", [("F42_VERSION", "git_sha"), ("F42_IMAGE_DIGEST", "image_digest")])
@pytest.mark.parametrize("value", [None, "", "   ", "not-a-value", "sha256:zz", "A80BE1D"])
def test_rj1_f42_version_and_f42_image_digest_unset_empty_or_malformed_stamp_the_pinned_default_from_the_process_environment(
        monkeypatch, tmp_path, name, field, value):
    from core.setup import stamp

    for other in ("F42_GIT_SHA", "F42_VERSION", "F42_IMAGE_DIGEST"):
        monkeypatch.delenv(other, raising=False)
    if value is not None:
        monkeypatch.setenv(name, value)
    assert env_scan.OPTIONAL_UNSET_DEFAULT[("core/setup/stamp.py", name)] is None
    assert stamp.build(None, tmp_path)[field] is None


# RJ-2: the baseline-J class reads a bound baseline-J file and takes its names from the job env keys, nowhere else

def written_baseline(tmp_path, mutate=None):
    """A baseline-J captured from the fake world and written once; (path, sha256 of its bytes, the baseline)."""
    from core.setup.release import jobs_baseline as jb
    from core.setup.tests import release_world as rw
    from core.setup.tests.test_jobs_baseline import capture, promoted

    _, baseline = capture(promoted())
    if mutate:
        mutate(baseline)
    path = tmp_path / "baseline-J.json"
    return path, jb.write_once(path, baseline), baseline


def job_env_names(baseline):
    return {name for view in baseline["jobs"].values() for name in view["env"]}


def cli(tmp_path, source, *extra):
    base, head = tmp_path / "base", tmp_path / "head"
    for folder, text in ((base, "x = 1\n"), (head, source)):
        (folder / "core").mkdir(parents=True, exist_ok=True)
        (folder / "core" / "a.py").write_text(text, encoding="utf-8")
    return env_scan.main(["--base-dir", str(base), "--head-dir", str(head), *extra])


def test_rj2_the_names_are_the_env_keys_of_the_fourteen_job_views_of_the_bound_file(tmp_path):
    path, sha, baseline = written_baseline(tmp_path)
    names = env_scan.baseline_j_names(path, sha)
    assert names == job_env_names(baseline) and len(names) >= 2


def test_rj2_a_name_held_anywhere_else_in_the_baseline_is_not_a_job_env_name(tmp_path):
    def add(baseline):
        baseline["aRetire"] = "NOT_A_JOB_ENV_NAME"
        baseline["extraNames"] = ["ALSO_NOT_ONE"]
        baseline["services"]["f42-api"]["template"]["x"] = {"TEMPLATE_ONLY_NAME": "1"}

    path, sha, baseline = written_baseline(tmp_path, add)
    names = env_scan.baseline_j_names(path, sha)
    assert names == job_env_names(baseline)
    assert not names & {"NOT_A_JOB_ENV_NAME", "ALSO_NOT_ONE", "TEMPLATE_ONLY_NAME"}


@pytest.mark.parametrize("how", ["wrong_hash", "one_byte_changed", "uppercase_hash", "short_hash", "empty_hash"])
def test_rj2_a_hash_that_does_not_equal_the_recomputation_of_the_file_refuses(tmp_path, how):
    from core.setup.release import services_only as so

    path, sha, _ = written_baseline(tmp_path)
    if how == "wrong_hash":
        sha = "0" * 64
    elif how == "one_byte_changed":
        data = path.read_bytes()
        path.write_bytes(data.replace(b'"kind": "baseline-J"', b'"kind": "baseline-J" '))
    elif how == "uppercase_hash":
        sha = sha.upper()
    elif how == "short_hash":
        sha = sha[:40]
    else:
        sha = ""
    with pytest.raises(so.Stop) as stop:
        env_scan.baseline_j_names(path, sha)
    assert stop.value.code == "BINDINGS"


def test_rj2_a_hand_typed_names_file_is_refused_even_with_its_own_correct_hash(tmp_path):
    from core.setup.release import services_only as so

    typed = tmp_path / "names.txt"
    typed.write_text("NEW_SETTING\nOTHER_SETTING\n", encoding="utf-8")
    sha = so.sha_bytes(typed.read_bytes())
    with pytest.raises(Exception):
        env_scan.baseline_j_names(typed, sha)
    assert cli(tmp_path, 'import os\nx = os.environ.get("NEW_SETTING")\n', "--baseline-j", str(typed), "--baseline-j-sha256", sha) != 0


@pytest.mark.parametrize("shape", ["wrong_kind", "three_jobs", "job_without_env", "env_not_a_mapping", "extra_job"])
def test_rj2_a_file_that_is_not_a_baseline_j_of_the_fourteen_jobs_is_refused_even_with_its_own_correct_hash(tmp_path, shape):
    from core.setup.release import services_only as so

    def shape_it(baseline):
        if shape == "wrong_kind":
            baseline["kind"] = "baseline-A"
        elif shape == "three_jobs":
            for name in list(baseline["jobs"])[3:]:
                del baseline["jobs"][name]
        elif shape == "job_without_env":
            del baseline["jobs"]["f42-collect"]["env"]
        elif shape == "env_not_a_mapping":
            baseline["jobs"]["f42-collect"]["env"] = ["NEW_SETTING"]
        else:
            baseline["jobs"]["f42-extra"] = copy.deepcopy(baseline["jobs"]["f42-collect"])

    path, sha, _ = written_baseline(tmp_path, shape_it)
    with pytest.raises(so.Stop):
        env_scan.baseline_j_names(path, sha)


def test_rj2_the_free_names_file_option_is_gone_and_the_two_options_come_together(tmp_path, capsys):
    with pytest.raises(SystemExit) as gone:
        cli(tmp_path, "x = 1\n", "--baseline-env-names", str(tmp_path / "names.txt"))
    assert gone.value.code == 2
    path, sha, _ = written_baseline(tmp_path)
    for only in (["--baseline-j", str(path)], ["--baseline-j-sha256", sha]):
        with pytest.raises(SystemExit) as stop:
            cli(tmp_path, "x = 1\n", *only)
        assert stop.value.code == 2
    assert not hasattr(env_scan, "ENV_NAMES_FILE")
    assert "baseline-env-names" not in Path(env_scan.__file__).read_text(encoding="utf-8")


def test_rj2_the_cli_classifies_a_name_the_bound_baseline_holds_and_refuses_a_file_whose_hash_is_wrong(tmp_path, capsys):
    path, sha, baseline = written_baseline(tmp_path)
    held = sorted(job_env_names(baseline))[0]
    source = f'import os\nx = os.environ.get("{held}")\n'
    assert cli(tmp_path, source) == 1
    assert cli(tmp_path, source, "--baseline-j", str(path), "--baseline-j-sha256", sha) == 0
    capsys.readouterr()
    assert cli(tmp_path, source, "--baseline-j", str(path), "--baseline-j-sha256", "1" * 64) == 2
    assert "BINDINGS" in capsys.readouterr().out
    other = 'import os\nx = os.environ.get("NOT_HELD_BY_ANY_JOB")\n'
    assert cli(tmp_path, other, "--baseline-j", str(path), "--baseline-j-sha256", sha) == 1


# RJ-6: a whole mapping is judged by its subscript reads too, and only by reads

SUBSCRIPT_LIKE = '''import os


def build(env=None):
    env = os.environ if env is None else env
    env["WRITTEN_NAME"] = "1"
    return env["SUBSCRIPTED_NAME"]
'''


def test_rj6_a_whole_mapping_is_judged_by_the_names_it_reads_with_a_subscript_and_not_by_a_name_it_writes():
    found = scan([("core/setup/other_stamp.py", SUBSCRIPT_LIKE)])
    assert [(f.name, f.line) for f in found] == [("SUBSCRIPTED_NAME", 7)]
    assert scan([("core/setup/other_stamp.py", SUBSCRIPT_LIKE)], names={"SUBSCRIPTED_NAME"}) == []
