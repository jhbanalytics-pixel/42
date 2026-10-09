"""tests_support/ci_skip_report.py: the job summary that names the skipped native opt-in tests."""
import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "tests_support" / "ci_skip_report.py"

SAMPLE = textwrap.dedent('''
    import os
    import pytest

    def test_plain():
        assert True

    def test_fails():
        assert False

    @pytest.mark.skipif(os.environ.get("F42_BQ") != "1", reason="set F42_BQ=1 to dry-run on BigQuery staging")
    def test_bigquery_opt_in():
        raise AssertionError("must not run")

    @pytest.mark.skipif(os.environ.get("F42_BQ") != "1", reason="set F42_BQ=1 to dry-run on BigQuery staging")
    def test_bigquery_opt_in_again():
        raise AssertionError("must not run")

    def test_native_opt_in():
        pytest.skip("Actual packaged browser gate runs in the final Linux image")

    def test_other_skip():
        pytest.skip("bash is required for the preparation boundary")
''')


@pytest.fixture(scope="module")
def junit(tmp_path_factory):
    folder = tmp_path_factory.mktemp("sample")
    (folder / "test_sample.py").write_text(SAMPLE, encoding="utf-8")
    xml = folder / "junit.xml"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", "F42_BQ")}
    subprocess.run([sys.executable, "-m", "pytest", str(folder / "test_sample.py"), "-q", "-p", "no:cacheprovider",
                    f"--junitxml={xml}"], cwd=folder, env=env, capture_output=True, text=True, encoding="utf-8")
    return xml


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True,
                          encoding="utf-8")


def test_the_summary_counts_outcomes_and_names_each_opt_in_group_apart(junit):
    done = run(junit)
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert "6 tests: 1 passed, 1 failed, 4 skipped." in out
    assert "- 2 skipped: live BigQuery dry runs (F42_BQ=1)" in out
    assert "- 1 skipped: packaged Linux browser and boundary image (PDF_RENDERER_NATIVE=1)" in out
    assert "- 1: bash is required for the preparation boundary" in out


def test_a_run_with_no_opt_in_skip_says_so(tmp_path):
    xml = tmp_path / "j.xml"
    xml.write_text('<testsuites><testsuite><testcase classname="a" name="b"/></testsuite></testsuites>',
                   encoding="utf-8")
    done = run(xml)
    assert done.returncode == 0
    assert "none were skipped for an opt-in reason" in done.stdout


@pytest.mark.parametrize("content", [None, "", "<testsuites/>", "<testsuites><testsuite/></testsuites>", "not xml"])
def test_a_missing_empty_or_unreadable_file_fails_the_job(tmp_path, content):
    path = tmp_path / "j.xml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    done = run(path)
    assert done.returncode == 1
    assert "tests:" not in done.stdout


def test_no_argument_fails_with_the_usage():
    done = run()
    assert done.returncode == 1 and "usage" in done.stdout


def test_the_module_can_be_imported_without_running_anything():
    spec = importlib.util.spec_from_file_location("ci_skip_report_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.classify("set F42_BQ=1 to dry-run on BigQuery staging").startswith("live BigQuery")
    assert module.classify("no browser on this host") is None


def test_a_run_with_at_least_the_floor_of_tests_passes(junit):
    done = run(junit, "--min-total", "6")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "6 tests:" in done.stdout


def test_a_run_below_the_floor_fails_the_job_after_printing_the_summary(junit):
    done = run(junit, "--min-total", "7")
    assert done.returncode == 1
    assert "6 tests:" in done.stdout
    assert "fewer tests than the floor of 7" in done.stdout


@pytest.mark.parametrize("args", [["--min-total"], ["--min-total", "many"], ["--min-total", "0"],
                                  ["--min-total", "-3"], ["--min-total", "5", "extra"], ["--other", "5"]])
def test_a_floor_that_is_not_a_positive_whole_number_is_a_usage_error(junit, args):
    done = run(junit, *args)
    assert done.returncode == 1
    assert "usage" in done.stdout and "tests:" not in done.stdout
