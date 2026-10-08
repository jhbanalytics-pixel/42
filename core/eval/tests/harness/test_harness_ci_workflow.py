"""The test-only workflow under .github/workflows.

GitHub runs only what is in .github/workflows. The three workflows that sign in to Google Cloud, build and deploy
are parked in .github/parked-workflows and must stay out of it. These checks read every workflow file that is
live and fail if one asks for a credential, a secret, a cloud tool, a build or a deploy, if an action is not
pinned to a commit, if the offline guard is not on PYTHONPATH for the core run, or if a native opt-in run could
be switched on. They read the parsed YAML, so a comment that says "no deploy" is not mistaken for a deploy step.
"""
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
LIVE = ROOT / ".github" / "workflows"
PARKED = ROOT / ".github" / "parked-workflows"
SHA = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
FORBIDDEN_TEXT = (
    "secrets.", "id-token", "google-github-actions", "workload_identity", "workload-identity", "gcloud", "gsutil",
    "docker build", "docker push", "docker login", "docker run", "buildx", "cloud build", "cloudbuild", "deploy",
    "kubectl", "terraform", "--apply", "setup-gcloud", "service_account", "credentials_json", "aws-actions",
    "azure/login", "bigquery.googleapis", "run.googleapis", "oauth2", "npm publish", "twine", "gh release",
)
ALLOWED_TRIGGERS = {"pull_request", "push", "workflow_dispatch"}


def workflow_files():
    return sorted(p for p in LIVE.iterdir() if p.is_file()) if LIVE.is_dir() else []


def load(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    triggers = data.get(True, data.get("on"))  # YAML 1.1 reads the key `on` as the boolean True
    return data, triggers


def strings(node):
    """Every string in a parsed YAML tree: keys and values."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from strings(key)
            yield from strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from strings(item)


def steps(data):
    return [(name, step) for name, job in data["jobs"].items() for step in job.get("steps", [])]


def effective_env(data, job_name, step):
    return {**(data.get("env") or {}), **(data["jobs"][job_name].get("env") or {}), **(step.get("env") or {})}


def test_there_is_at_least_one_live_workflow_and_it_is_the_test_workflow():
    names = [p.name for p in workflow_files()]
    assert names, "no live workflow: the core suite has no CI gate"
    assert "tests.yml" in names


def test_the_parked_workflows_are_still_parked_and_not_copied_into_the_live_directory():
    parked = {p.name for p in PARKED.iterdir()}
    assert {"production.yml", "staging.yml", "staging-app.yml"} <= parked
    assert not parked & {p.name for p in workflow_files()}


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
class TestEveryLiveWorkflow:
    def test_it_asks_for_read_access_to_the_contents_and_nothing_else(self, path):
        data, _ = load(path)
        assert data.get("permissions") == {"contents": "read"}
        for name, job in data["jobs"].items():
            assert "permissions" not in job or job["permissions"] == {"contents": "read"}, name

    def test_it_runs_only_on_the_three_plain_triggers(self, path):
        _, triggers = load(path)
        names = set(triggers if isinstance(triggers, (dict, list)) else [triggers])
        assert names and names <= ALLOWED_TRIGGERS, names

    def test_it_holds_no_credential_secret_cloud_tool_build_or_deploy(self, path):
        data, _ = load(path)
        text = "\n".join(strings(data)).lower()
        found = sorted(word for word in FORBIDDEN_TEXT if word in text)
        assert not found, found

    def test_it_names_no_environment_and_sets_no_credential_variable(self, path):
        data, _ = load(path)
        for name, job in data["jobs"].items():
            assert "environment" not in job, name
        for job_name, step in steps(data):
            env = effective_env(data, job_name, step)
            for key, value in env.items():
                assert not re.search(r"(TOKEN|SECRET|PASSWORD|PASSCODE|API_KEY|CREDENTIAL)", key.upper()), key
                if key == "GOOGLE_APPLICATION_CREDENTIALS":
                    assert value in ("", None)

    def test_every_action_is_pinned_to_a_full_commit(self, path):
        data, _ = load(path)
        used = [step["uses"] for _, step in steps(data) if "uses" in step]
        assert used
        assert all(SHA.match(u) for u in used), used

    def test_checkout_keeps_no_credential_in_the_git_config(self, path):
        data, _ = load(path)
        checkouts = [step for _, step in steps(data) if step.get("uses", "").startswith("actions/checkout@")]
        assert checkouts
        assert all(step.get("with", {}).get("persist-credentials") is False for step in checkouts)

    def test_no_failure_can_be_hidden(self, path):
        data, _ = load(path)
        for name, job in data["jobs"].items():
            assert not job.get("continue-on-error"), name
            assert isinstance(job.get("timeout-minutes"), int), name
        for job_name, step in steps(data):
            assert not step.get("continue-on-error"), step
            run = step.get("run", "")
            assert "|| true" not in run and "set +e" not in run and "exit 0" not in run, run

    def test_it_runs_on_a_pinned_linux_image(self, path):
        data, _ = load(path)
        for name, job in data["jobs"].items():
            assert re.fullmatch(r"ubuntu-\d\d\.\d\d", job["runs-on"]), (name, job["runs-on"])


def core_workflow():
    return load(LIVE / "tests.yml")[0]


def test_native_opt_in_tests_are_switched_off_and_the_run_refuses_to_start_if_they_are_on():
    data = core_workflow()
    assert str(data["env"]["F42_BQ"]) == "0"
    for job_name, step in steps(data):
        env = effective_env(data, job_name, step)
        assert str(env.get("F42_BQ", "0")) == "0"
        assert not env.get("PDF_RENDERER_NATIVE")
    guard_steps = [step for _, step in steps(data) if "PDF_RENDERER_NATIVE" in step.get("run", "")]
    assert guard_steps and "F42_BQ" in guard_steps[0]["run"]
    names = [step.get("name") for _, step in steps(data)]
    refuse = next(i for i, step in enumerate(s for _, s in steps(data)) if "PDF_RENDERER_NATIVE" in step.get("run", ""))
    pytest_at = next(i for i, step in enumerate(s for _, s in steps(data)) if "-m pytest" in step.get("run", ""))
    assert refuse < pytest_at, names


def test_the_core_run_has_the_offline_guard_on_pythonpath_and_requires_it():
    data = core_workflow()
    core_steps = [(j, s) for j, s in steps(data) if "-m pytest" in s.get("run", "")]
    assert len(core_steps) == 1
    job_name, step = core_steps[0]
    env = effective_env(data, job_name, step)
    assert "tests_support/offline_guard" in env["PYTHONPATH"]
    assert env["PYTHONPATH"].split(":")[0].endswith("tests_support/offline_guard")
    assert str(env["F42_REQUIRE_OFFLINE_GUARD"]) == "1"
    assert (ROOT / "tests_support" / "offline_guard" / "sitecustomize.py").is_file()


def test_the_core_run_collects_the_whole_core_tree_from_the_repository_root():
    data = core_workflow()
    [(job_name, step)] = [(j, s) for j, s in steps(data) if "-m pytest" in s.get("run", "")]
    command = " ".join(step["run"].split())
    assert re.search(r"-m pytest core( |$)", command), command
    assert "working-directory" not in step
    assert "-p no:cacheprovider" in command and "--junitxml" in command
    assert not re.search(r"--(deselect|ignore|continue-on-collection-errors)|-k |-m \"?not", command), command


def test_the_core_job_installs_from_the_one_core_requirements_file_and_that_file_exists():
    data = core_workflow()
    runs = "\n".join(step.get("run", "") for _, step in steps(data))
    assert "-r core/requirements-test.txt" in runs
    assert (ROOT / "core" / "requirements-test.txt").is_file()
    assert "bertopic==0.17.4" in runs and "--no-deps" in runs


def test_the_skip_report_runs_even_when_the_suite_fails_and_the_script_exists():
    data = core_workflow()
    reports = [s for _, s in steps(data) if "ci_skip_report.py" in s.get("run", "")]
    assert len(reports) == 1
    assert reports[0].get("if") == "always()"
    assert (ROOT / "tests_support" / "ci_skip_report.py").is_file()


def test_the_frontend_job_installs_from_the_lock_file_and_runs_the_bun_tests():
    data = core_workflow()
    frontend = [s for name, job in data["jobs"].items() if name == "frontend" for s in job["steps"]]
    assert frontend
    runs = [s for s in frontend if s.get("run")]
    install = next(s for s in runs if "bun install" in s["run"])
    assert "--frozen-lockfile" in install["run"] and install.get("working-directory") == "app/frontend"
    test = next(s for s in runs if re.search(r"\bbun test\b", s["run"]))
    assert test.get("working-directory") == "app/frontend"
    assert (ROOT / "app" / "frontend" / "bun.lock").is_file()


def test_the_bun_version_comes_from_one_pin_in_the_workflow():
    data = core_workflow()
    text = json.dumps(data)
    pins = set(re.findall(r"bun@(\d+\.\d+\.\d+)", text))
    assert len(pins) == 1, pins


def frontend_runs(data):
    return [s for s in data["jobs"]["frontend"]["steps"] if s.get("run")]


def test_the_frontend_job_builds_the_bundle_before_bun_test_reads_it():
    """neutral-contract.test.jsx reads app/web/dist/index.html, which is gitignored, so a fresh checkout has none.
    The job must run the local vite build in app/frontend after the install and before the tests."""
    data = core_workflow()
    runs = frontend_runs(data)
    test_at = next(i for i, s in enumerate(runs) if re.search(r"\bbun test\b", s["run"]))
    install_at = next(i for i, s in enumerate(runs) if "bun install" in s["run"])
    builds = [i for i, s in enumerate(runs) if re.search(r"\bbun run build\b", s["run"])]
    assert builds, "the frontend job never builds, so app/web/dist/index.html is missing for neutral-contract.test.jsx"
    assert install_at < builds[0] < test_at, (install_at, builds, test_at)
    assert runs[builds[0]].get("working-directory") == "app/frontend"


def test_the_frontend_test_that_needs_the_built_bundle_still_names_that_path_and_the_build_writes_there():
    contract = (ROOT / "app" / "frontend" / "src" / "ui" / "__tests__" / "neutral-contract.test.jsx").read_text(
        encoding="utf-8")
    assert "web/dist/index.html" in contract
    package = json.loads((ROOT / "app" / "frontend" / "package.json").read_text(encoding="utf-8"))
    assert package["scripts"]["build"] == "vite build"
    config = (ROOT / "app" / "frontend" / "vite.config.mjs").read_text(encoding="utf-8")
    assert "web/dist" in config.replace("\\", "/")
