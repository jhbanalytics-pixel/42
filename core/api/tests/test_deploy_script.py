"""Execute the deploy script with local command doubles, never a cloud client."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SOURCE = "gs://ogilvy-trends-v2-f42-media-staging/build-source"
PRIVATE = {"metadata": {"annotations": {}}}
POLICY = {"bindings": [{"role": "roles/run.invoker", "members": [
    "serviceAccount:f42-web@ogilvy-trends-v2.iam.gserviceaccount.com"]}]}


def run_deploy(tmp_path, *, service=PRIVATE, policy=POLICY, build_exit=0, policy_exit=0, args=()):
    for name in ("deploy.sh", "deploy_flags.env"):
        target = tmp_path / "core" / "api" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ROOT / "core" / "api" / name).read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    calls = tmp_path / "calls"
    git = Path(shutil.which("git"))
    bash = git.parent.parent / "bin" / "bash.exe" if os.name == "nt" else Path(shutil.which("bash"))
    env = {**os.environ, "R3_CALLS": calls.as_posix(), "R3_PYTHON": Path(sys.executable).as_posix(),
           "R3_SERVICE": json.dumps(service), "R3_POLICY": json.dumps(policy),
           "R3_BUILD_EXIT": str(build_exit), "R3_POLICY_EXIT": str(policy_exit),
           "F42_SMOKE_PASSCODE": "test-only", "MSYS_NO_PATHCONV": "1"}
    shell = r'''
set -euo pipefail
git() { printf 'd666ef64cd7a\n'; }
docker() { return 97; }
py() {
  if [ "${2:-}" = core/api/smoke.py ]; then return 0; fi
  if [ "${2:-}" = -c ]; then command "$R3_PYTHON" -c "$3"; return; fi
  return 98
}
gcloud() {
  printf '%s\0' "$@" >> "$R3_CALLS"
  printf '\0' >> "$R3_CALLS"
  case "$1 $2 $3" in
    'run services describe')
      if [[ "$*" == *'--format=json'* ]]; then printf '%s\n' "$R3_SERVICE"
      else printf 'https://test.invalid\n'; fi ;;
    'run services get-iam-policy') printf '%s\n' "$R3_POLICY"; return "$R3_POLICY_EXIT" ;;
    'builds submit --project') return "$R3_BUILD_EXIT" ;;
    'run deploy f42-agent'|'run deploy f42-api') return 0 ;;
    *) return 99 ;;
  esac
}
source core/api/deploy.sh --no-traffic "$@"
'''
    result = subprocess.run([str(bash), "--noprofile", "--norc", "-c", shell, "deploy-test", *args],
                            cwd=tmp_path, env=env, capture_output=True, encoding="utf-8", timeout=15)
    recorded = [part.decode("utf-8").split("\0") for part in calls.read_bytes().split(b"\0\0") if part]
    return result, recorded


def test_service_build_uses_the_authorised_source_bucket_project_and_builder(tmp_path):
    result, calls = run_deploy(tmp_path)
    assert result.returncode == 0, result.stderr
    [build] = [call for call in calls if call[:2] == ["builds", "submit"]]
    assert build[build.index("--gcs-source-staging-dir") + 1] == SOURCE
    assert build[build.index("--project") + 1] == "ogilvy-trends-v2"
    assert build[build.index("--region") + 1] == "us-central1"
    assert build[build.index("--service-account") + 1] == (
        "projects/ogilvy-trends-v2/serviceAccounts/f42-deployer@ogilvy-trends-v2.iam.gserviceaccount.com")
    assert build[-1] == "."


def test_private_redeploy_reads_auth_and_never_requests_an_iam_change(tmp_path):
    result, calls = run_deploy(tmp_path, args=("--no-build",))
    assert result.returncode == 0, result.stderr
    assert any(call[:3] == ["run", "services", "get-iam-policy"] for call in calls)
    deploys = [call for call in calls if call[:2] == ["run", "deploy"]]
    assert [call[2] for call in deploys] == ["f42-agent", "f42-api"]
    assert all(not any("allow-unauthenticated" in arg or "set-iam-policy" in arg
                       or "add-iam-policy-binding" in arg for arg in call) for call in calls)
    assert "--no-invoker-iam-check" not in deploys[0]
    assert deploys[0][deploys[0].index("--service-account") + 1] == (
        "f42-agent@ogilvy-trends-v2.iam.gserviceaccount.com")
    assert "--no-invoker-iam-check" in deploys[1]


def test_redeploy_preserves_environment_settings_outside_the_release(tmp_path):
    result, calls = run_deploy(tmp_path, args=("--no-build",))
    assert result.returncode == 0, result.stderr
    deploys = [call for call in calls if call[:2] == ["run", "deploy"]]
    assert [call[2] for call in deploys] == ["f42-agent", "f42-api"]
    for call in deploys:
        assert "--set-env-vars" not in call
        assert "--clear-env-vars" not in call
        assert "--remove-env-vars" not in call
        settings = call[call.index("--update-env-vars") + 1]
        assert "F42_PROJECT=ogilvy-trends-v2" in settings.split(",")
        assert "F42_VERSION=d666ef64cd7a" in settings.split(",")


@pytest.mark.parametrize("member", ["allUsers", "allAuthenticatedUsers"])
def test_public_agent_stops_before_build_or_deploy(tmp_path, member):
    policy = {"bindings": [{"role": "roles/run.invoker", "members": [member]}]}
    result, calls = run_deploy(tmp_path, policy=policy)
    assert result.returncode != 0
    assert not any(call[:2] in (["builds", "submit"], ["run", "deploy"]) for call in calls)


def test_agent_with_invoker_check_disabled_stops_before_build_or_deploy(tmp_path):
    service = {"metadata": {"annotations": {"run.googleapis.com/invoker-iam-disabled": "true"}}}
    result, calls = run_deploy(tmp_path, service=service)
    assert result.returncode != 0
    assert not any(call[:2] in (["builds", "submit"], ["run", "deploy"]) for call in calls)


def test_unreadable_agent_policy_stops_before_build_or_deploy(tmp_path):
    result, calls = run_deploy(tmp_path, policy_exit=29)
    assert result.returncode != 0
    assert not any(call[:2] in (["builds", "submit"], ["run", "deploy"]) for call in calls)


def test_failed_service_build_stops_before_deploy(tmp_path):
    result, calls = run_deploy(tmp_path, build_exit=23)
    assert result.returncode == 23
    assert not any(call[:2] == ["run", "deploy"] for call in calls)
