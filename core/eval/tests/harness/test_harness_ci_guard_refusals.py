"""tests_support/ci_guard_refusals.py: the workflow step that fails the job on a refusal the tests did not ask for."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "tests_support" / "ci_guard_refusals.py"
OWN = "core/eval/tests/harness/test_harness_offline_guard.py"


def row(test, event="socket.getaddrinfo", detail="name lookup off this machine"):
    return json.dumps({"at": 1.0, "event": event, "detail": detail, "test": test})


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True,
                          encoding="utf-8")


def log(tmp_path, *lines):
    path = tmp_path / "guard.jsonl"
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def test_no_log_means_nothing_was_refused(tmp_path):
    done = run(tmp_path / "absent.jsonl")
    assert done.returncode == 0 and "refused nothing" in done.stdout


def test_an_empty_log_means_nothing_was_refused(tmp_path):
    assert run(log(tmp_path)).returncode == 0


def test_a_refusal_made_by_a_product_test_fails_the_job_and_names_the_test(tmp_path):
    path = log(tmp_path, row("core/collect/tests/test_gdelt.py::test_fetch (call)"))
    done = run(path)
    assert done.returncode == 1
    assert "core/collect/tests/test_gdelt.py::test_fetch (call)" in done.stdout
    assert "socket.getaddrinfo" in done.stdout and "name lookup off this machine" in done.stdout


def test_a_refusal_during_collection_fails_the_job(tmp_path):
    assert run(log(tmp_path, row("collection"))).returncode == 1


def test_a_refusal_the_guards_own_tests_made_is_listed_but_does_not_fail(tmp_path):
    for test in (f"{OWN}::test_x (call)", OWN.replace("/", "\\") + "::test_x (call)"):
        done = run(log(tmp_path, row(test)))
        assert done.returncode == 0, done.stdout
        assert "1 from the guard's own tests" in done.stdout


def test_one_foreign_refusal_among_the_guards_own_still_fails(tmp_path):
    done = run(log(tmp_path, row(f"{OWN}::test_x (call)"), row("core/api/tests/test_a.py::test_b (call)"),
                   row(f"{OWN}::test_y (call)")))
    assert done.returncode == 1
    assert "core/api/tests/test_a.py::test_b" in done.stdout


@pytest.mark.parametrize("line", ["not json", "[1, 2]", "{}", '{"test": 5}'])
def test_a_log_line_that_cannot_be_read_as_a_refusal_fails_the_job(tmp_path, line):
    assert run(log(tmp_path, line)).returncode == 1


def test_a_test_whose_path_only_ends_like_the_guards_own_file_is_not_exempt(tmp_path):
    done = run(log(tmp_path, row("core/other/tests/test_harness_offline_guard.py::test_x (call)")))
    assert done.returncode == 1


def test_no_argument_fails_with_the_usage():
    done = run()
    assert done.returncode == 1 and "usage" in done.stdout
