"""The environment read scan (W8-REL-B v2.1 section 3.5, JB-06): B's job code reads no setting the live job definitions lack.
Offline: the trees are lists of (path, text) as durable_effects_check.git_tree_files returns them."""
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
    found = scan([("core/setup/stamp.py", STAMP_LIKE)])
    assert sorted((f.name, f.line) for f in found) == [("F42_IMAGE_DIGEST", 6), ("F42_VERSION", 6)]
    assert scan([("core/setup/stamp.py", STAMP_LIKE)], names={"F42_VERSION", "F42_IMAGE_DIGEST"}) == []


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
