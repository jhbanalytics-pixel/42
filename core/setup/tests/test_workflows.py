"""Checks on the two GitHub Actions workflows (task 3.8): staging deploys on a push to full-42 through Workload
Identity Federation, production only on Albert's own start. Reads the YAML files only; nothing runs."""
import re
from pathlib import Path

import pytest
import yaml

from core.setup import bootstrap, deploy_jobs

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "parked-workflows"
STAGING = WORKFLOWS / "staging.yml"
PRODUCTION = WORKFLOWS / "production.yml"
STAGING_PROJECT = "ogilvy-trends-v2"
STAGING_NUMBER = "590353929363"
STAGING_PROVIDER = (f"projects/{STAGING_NUMBER}/locations/global/workloadIdentityPools/{bootstrap.POOL}"
                    f"/providers/{bootstrap.PROVIDER}")
DEPLOYER = f"f42-deployer@{STAGING_PROJECT}.iam.gserviceaccount.com"
PINNED = re.compile(r"^\s*(-\s+)?uses:\s+[\w.-]+/[\w./-]+@[0-9a-f]{40}\s+#\s+v\d+\.\d+\.\d+\s*$")
PROD_VARS = ("PROD_PROJECT", "PROD_WIF_PROVIDER", "PROD_DEPLOYER", "PROD_ACTOR_ID")


def load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def text(path):
    return path.read_text(encoding="utf-8")


def triggers(wf):
    # PyYAML reads the bare key on as the boolean True.
    return wf.get("on", wf.get(True))


def steps(wf):
    return [s for job in wf["jobs"].values() for s in job.get("steps", [])]


def auth_steps(wf):
    return [s for s in steps(wf) if s.get("uses", "").startswith("google-github-actions/auth@")]


def runs(wf):
    return "\n".join(s.get("run", "") for s in steps(wf))


@pytest.mark.parametrize("path", [STAGING, PRODUCTION])
def test_parses_with_jobs(path):
    wf = load(path)
    assert wf["jobs"]


def test_staging_runs_only_on_a_push_to_full_42():
    assert triggers(load(STAGING)) == {"push": {"branches": ["full-42"]}}


def test_staging_job_refuses_other_repositories_and_refs():
    for job in load(STAGING)["jobs"].values():
        assert f"github.repository == '{bootstrap.GITHUB_REPO}'" in job["if"]
        assert "github.ref == 'refs/heads/full-42'" in job["if"]


def test_production_starts_only_by_hand():
    assert set(triggers(load(PRODUCTION))) == {"workflow_dispatch"}


@pytest.mark.parametrize("path", [STAGING, PRODUCTION])
def test_every_action_is_pinned_to_a_commit_with_its_version(path):
    uses = [line for line in text(path).splitlines() if re.match(r"^\s*(-\s+)?uses:", line)]
    assert uses
    for line in uses:
        assert PINNED.match(line), line


@pytest.mark.parametrize("path", [STAGING, PRODUCTION])
def test_no_keys_or_secrets(path):
    body = text(path)
    for word in ("credentials_json", "secrets.", "private_key", "service_account_key", "BEGIN PRIVATE"):
        assert word not in body, word


@pytest.mark.parametrize("path", [STAGING, PRODUCTION])
def test_token_permissions_are_minimal(path):
    assert load(path)["permissions"] == {"contents": "read", "id-token": "write"}


@pytest.mark.parametrize("path", [STAGING, PRODUCTION])
def test_two_runs_never_deploy_at_once(path):
    concurrency = load(path)["concurrency"]
    assert concurrency["group"]
    assert concurrency["cancel-in-progress"] is False


def test_staging_authenticates_as_the_deployer_through_the_staging_provider():
    (step,) = auth_steps(load(STAGING))
    assert step["with"] == {"workload_identity_provider": STAGING_PROVIDER, "service_account": DEPLOYER}


def test_staging_deploys_only_to_the_staging_project():
    assert deploy_jobs.PROJECT == STAGING_PROJECT
    body = text(STAGING)
    assert set(re.findall(r"ogilvy-[a-z0-9-]+", body)) == {STAGING_PROJECT}
    assert "vars." not in body


def test_staging_runs_the_existing_entry_points():
    commands = runs(load(STAGING))
    assert "python core/setup/deploy_jobs.py --build --apply" in commands
    assert "python core/setup/deploy_jobs.py --smoke --apply" in commands
    assert "dj.SMOKE_JOBS" in commands and "dj.execute_argv" in commands
    order = [commands.index(c) for c in ("--build --apply", "--smoke --apply", "dj.SMOKE_JOBS")]
    assert order == sorted(order)


def test_production_is_bound_to_the_production_environment_on_main():
    for job in load(PRODUCTION)["jobs"].values():
        assert job["environment"] == {"name": "production"}
        assert "github.ref == 'refs/heads/main'" in job["if"]
        assert f"github.repository == '{bootstrap.GITHUB_REPO}'" in job["if"]


def test_production_refuses_before_authenticating_when_a_setting_is_missing():
    wf = load(PRODUCTION)
    all_steps = steps(wf)
    auth = all_steps.index(auth_steps(wf)[0])
    gate = next(i for i, s in enumerate(all_steps) if "PROD_PROJECT" in s.get("env", {}))
    assert gate < auth
    gate_step = all_steps[gate]
    loop = next(line for line in gate_step["run"].splitlines() if line.startswith("for name in "))
    assert loop.removeprefix("for name in ").removesuffix("; do").split() == list(PROD_VARS)
    assert '[ -n "${!name}" ] || missing=' in gate_step["run"]
    for name in PROD_VARS:
        assert gate_step["env"][name] == "${{ vars.%s }}" % name
    assert "exit 1" in gate_step["run"]
    assert STAGING_PROJECT in gate_step["run"]
    assert "github.actor_id" in str(gate_step["env"]) and "github.run_attempt" in str(gate_step["env"])


def test_production_names_no_project_and_authenticates_from_environment_variables():
    body = text(PRODUCTION)
    assert set(re.findall(r"ogilvy-[a-z0-9-]+", body)) <= {STAGING_PROJECT}
    (step,) = auth_steps(load(PRODUCTION))
    assert step["with"] == {"workload_identity_provider": "${{ vars.PROD_WIF_PROVIDER }}",
                            "service_account": "${{ vars.PROD_DEPLOYER }}"}


def test_production_deploys_nothing_until_deploy_jobs_takes_a_project():
    wf = load(PRODUCTION)
    assert "deploy_jobs.py" not in runs(wf).replace("core/setup/deploy_jobs.py deploys", "")
    assert steps(wf)[-1]["run"].rstrip().endswith("exit 1")
