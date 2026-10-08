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


def run_deploy(tmp_path, *, service=PRIVATE, policy=POLICY, build_exit=0, policy_exit=0, args=(), status="",
               dockerignore=None, services=None):
    for name in ("deploy.sh", "deploy_flags.env", "Dockerfile.dockerignore"):
        target = tmp_path / "core" / "api" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        text = (ROOT / "core" / "api" / name).read_text(encoding="utf-8")
        if name == "Dockerfile.dockerignore" and dockerignore is not None:
            text = dockerignore
        target.write_text(text, encoding="utf-8", newline="\n")
    calls = tmp_path / "calls"
    git = Path(shutil.which("git"))
    bash = git.parent.parent / "bin" / "bash.exe" if os.name == "nt" else Path(shutil.which("bash"))
    env = {**os.environ, "R3_CALLS": calls.as_posix(), "R3_PYTHON": Path(sys.executable).as_posix(),
           "R3_SERVICE": json.dumps(service), "R3_POLICY": json.dumps(policy),
           **{"R3_SERVICE_" + name.replace("-", "_"): json.dumps(value) for name, value in (services or {}).items()},
           "R3_BUILD_EXIT": str(build_exit), "R3_POLICY_EXIT": str(policy_exit),
           "F42_SMOKE_PASSCODE": "test-only", "MSYS_NO_PATHCONV": "1", "R3_STATUS": status}
    shell = r'''
set -euo pipefail
git() {
  case "$1" in
    rev-parse) printf 'd666ef64cd7a\n' ;;
    status)
      # Models git: untracked files show only with --untracked-files=all (a repo can set
      # status.showUntrackedFiles=no), ignored files only with --ignored, and a pathspec keeps its directory.
      shift
      paths=(); after=0; ignored=0; untracked=0
      for a in "$@"; do
        if [ "$after" = 1 ]; then paths+=("$a"); fi
        case "$a" in
          --) after=1 ;;
          --ignored) ignored=1 ;;
          --untracked-files=all) untracked=1 ;;
        esac
      done
      while IFS= read -r line; do
        [ -n "$line" ] || continue
        code=${line:0:2}; path=${line:3}
        if [ "$code" = '??' ] && [ "$untracked" = 0 ]; then continue; fi
        if [ "$code" = '!!' ] && [ "$ignored" = 0 ]; then continue; fi
        if [ "${#paths[@]}" -gt 0 ]; then
          match=0
          for p in "${paths[@]}"; do
            if [ "$path" = "$p" ] || [[ "$path" == "$p"/* ]]; then match=1; fi
          done
          if [ "$match" = 0 ]; then continue; fi
        fi
        printf '%s\n' "$line"
      done <<< "$R3_STATUS" ;;
    *) return 96 ;;
  esac
}
docker() { return 97; }
py() {
  if [ "${2:-}" = core/api/smoke.py ]; then return 0; fi
  if [ "${2:-}" = -c ]; then command "$R3_PYTHON" -c "$3" "${@:4}"; return; fi
  return 98
}
gcloud() {
  printf '%s\0' "$@" >> "$R3_CALLS"
  printf '\0' >> "$R3_CALLS"
  case "$1 $2 $3" in
    'run services describe')
      if [[ "$*" == *'--format=json'* ]]; then var="R3_SERVICE_${4//-/_}"; printf '%s\n' "${!var:-$R3_SERVICE}"
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
    raw = calls.read_bytes() if calls.exists() else b""
    recorded = [part.decode("utf-8").split("\0") for part in raw.split(b"\0\0") if part]
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


DIRTY = " M core/agent/ask.py\n"


@pytest.mark.parametrize("args", [(), ("--local-build",)])
@pytest.mark.parametrize("status", [DIRTY, "?? core/api/new_module.py\n"])
def test_a_dirty_tree_is_refused_before_any_cloud_call_when_the_script_builds(tmp_path, args, status):
    result, calls = run_deploy(tmp_path, args=args, status=status)
    assert result.returncode != 0
    assert calls == []
    assert "uncommitted" in result.stderr and "d666ef64cd7a" in result.stderr


def test_a_clean_tree_still_builds_and_deploys(tmp_path):
    result, calls = run_deploy(tmp_path, status="")
    assert result.returncode == 0, result.stderr
    assert any(call[:2] == ["builds", "submit"] for call in calls)
    assert [call[2] for call in calls if call[:2] == ["run", "deploy"]] == ["f42-agent", "f42-api"]


def test_no_build_redeploys_the_image_of_head_even_with_edits_outside_core(tmp_path):
    result, calls = run_deploy(tmp_path, args=("--no-build",), status=" M docs/operations/notes.md\n")
    assert result.returncode == 0, result.stderr
    assert not any(call[:2] == ["builds", "submit"] for call in calls)
    assert [call[2] for call in calls if call[:2] == ["run", "deploy"]] == ["f42-agent", "f42-api"]


@pytest.mark.parametrize("path", ["core/api/deploy_flags.env", "core/api/deploy.sh"])
def test_no_build_is_refused_when_the_flags_it_deploys_with_are_not_those_of_head(tmp_path, path):
    result, calls = run_deploy(tmp_path, args=("--no-build",), status=f" M {path}\n")
    assert result.returncode != 0
    assert calls == []
    assert "uncommitted" in result.stderr and path in result.stderr


@pytest.mark.parametrize("status", [
    " M core/api/smoke.py\n",  # the release smoke runs from the working tree
    " M core/agent/ask.py\n",  # smoke imports core.api, which imports the rest of core
    "?? core/api/helper.py\n",
])
def test_no_build_is_refused_when_any_file_under_core_differs_from_head(tmp_path, status):
    result, calls = run_deploy(tmp_path, args=("--no-build",), status=status)
    assert result.returncode != 0
    assert calls == []
    assert "uncommitted" in result.stderr and status.split()[-1] in result.stderr


@pytest.mark.parametrize("status", [
    "?? httpx.py\n",  # smoke.py puts the repo root first on sys.path, so a root module shadows what it imports
    "?? sitecustomize.py\n",
    "?? yaml.py\n",
    " M conftest.py\n",
    "?? google/auth/__init__.py\n",  # a package that shadows an installed one
    "?? ops/helper.py\n",
])
def test_no_build_is_refused_for_a_root_module_or_untracked_python_outside_core(tmp_path, status):
    result, calls = run_deploy(tmp_path, args=("--no-build",), status=status)
    assert result.returncode != 0
    assert calls == []
    assert "uncommitted" in result.stderr and status.split()[-1] in result.stderr


@pytest.mark.parametrize("status", [
    " M docs/operations/notes.md\n", "?? docs/operations/new.md\n", "?? notes.txt\n", " M README.md\n",
    " M app/frontend/src/App.jsx\n",
])
def test_no_build_allows_files_the_release_never_imports(tmp_path, status):
    result, calls = run_deploy(tmp_path, args=("--no-build",), status=status)
    assert result.returncode == 0, result.stderr
    assert [call[2] for call in calls if call[:2] == ["run", "deploy"]] == ["f42-agent", "f42-api"]


# What the build copies is what core/api/Dockerfile.dockerignore lets through: core, app/frontend and
# docs/full-42/reference/sc_routes.json, less node_modules, test-results, playwright-report, __pycache__, .pytest_cache.
@pytest.mark.parametrize("status", [
    "!! core/.env\n", "!! core/api/.env.local\n", "!! core/data/cache.db\n", "!! core/new_dir/\n",
    "!! app/frontend/dist/\n", "!! app/frontend/.env\n",
    "!! docs/full-42/reference/sc_routes.json\n",
])
def test_local_build_is_refused_for_ignored_files_the_dockerignore_lets_into_the_image(tmp_path, status):
    result, calls = run_deploy(tmp_path, args=("--local-build",), status=status)
    assert result.returncode != 0
    assert calls == []
    assert "ignored" in result.stderr and status.split()[-1] in result.stderr


@pytest.mark.parametrize("status", [
    "!! app/frontend/node_modules/\n", "!! app/frontend/node_modules/react/index.js\n",
    "!! app/frontend/test-results/\n", "!! app/frontend/playwright-report/index.html\n",
    "!! core/api/__pycache__/\n", "!! core/setup/tests/__pycache__/\n", "!! core/api/.pytest_cache/\n",
    "!! docs/full-42/reference/local.json\n", "!! docs/scratch.md\n", "!! scratch/notes.txt\n", "!! .env\n",
])
def test_local_build_allows_ignored_files_the_dockerignore_keeps_out_of_the_image(tmp_path, status):
    result, calls = run_deploy(tmp_path, args=("--local-build",), status=status)
    assert result.returncode == 97, result.stderr  # past the guard, stopped by the docker double


def test_the_local_build_guard_follows_the_dockerignore_it_is_given(tmp_path):
    # Read from the file at run time, not copied into the script: change the file and the guard changes with it.
    lets_node_modules_in = "*\n!core/\n!app/frontend/\n"
    result, calls = run_deploy(tmp_path / "a", args=("--local-build",), status="!! app/frontend/node_modules/\n",
                               dockerignore=lets_node_modules_in)
    assert result.returncode != 0 and "app/frontend/node_modules" in result.stderr
    keeps_env_out = "*\n!core/\n**/.env\n"
    result, calls = run_deploy(tmp_path / "b", args=("--local-build",), status="!! core/.env\n",
                               dockerignore=keeps_env_out)
    assert result.returncode == 97, result.stderr


def test_the_deploy_script_does_not_claim_there_is_no_dockerignore():
    text = (ROOT / "core" / "api" / "deploy.sh").read_text(encoding="utf-8")
    assert "there is no .dockerignore" not in text and "Dockerfile.dockerignore" in text


def test_the_cloud_build_does_not_refuse_ignored_files(tmp_path):
    # gcloud builds the upload from the .gitignore rules, so an ignored file is not sent. Reasoned, not run.
    result, calls = run_deploy(tmp_path, status="!! core/.env\n")
    assert result.returncode == 0, result.stderr


# W8-REL 2.8: once Release A has pinned the services by name, the ordinary script must not deploy onto them. A new
# revision of a pinned service takes no traffic, so the script would test the old revision and then --to-latest would
# move traffic to a revision nothing checked.
def with_traffic(*entries, spec=False):
    return {"metadata": {"annotations": {}}, "spec" if spec else "status": {"traffic": list(entries)}}


LATEST = {"latestRevision": True, "percent": 100, "revisionName": "f42-api-00041-lns"}
PINNED = {"percent": 100, "revisionName": "f42-api-00041-lns"}
TAGGED = {"percent": 0, "revisionName": "f42-api-00042-abc", "tag": "rel-0000000-01"}
TAGGED_LATEST = {"percent": 0, "latestRevision": True, "revisionName": "f42-api-00041-lns", "tag": "rel-0000000-01"}


def no_build_or_deploy(calls):
    return not any(call[:2] in (["builds", "submit"], ["run", "deploy"]) for call in calls)


@pytest.mark.parametrize("args", [(), ("--no-build",), ("--local-build",)])
@pytest.mark.parametrize("name", ["f42-agent", "f42-api"])
def test_ds19_a_pinned_service_refuses_the_ordinary_deploy_before_any_build(tmp_path, args, name):
    result, calls = run_deploy(tmp_path, args=args, services={name: with_traffic(PINNED)})
    assert result.returncode == 65, result.stderr
    assert no_build_or_deploy(calls)
    assert name in result.stderr and "pinned" in result.stderr


@pytest.mark.parametrize("name", ["f42-agent", "f42-api"])
@pytest.mark.parametrize("entry", [PINNED, TAGGED, TAGGED_LATEST])
def test_ds19_a_pinned_or_tagged_entry_is_found_in_the_spec_as_well_as_the_status(tmp_path, name, entry):
    result, calls = run_deploy(tmp_path, services={name: with_traffic(LATEST, entry, spec=True)})
    assert result.returncode == 65, result.stderr
    assert no_build_or_deploy(calls)


@pytest.mark.parametrize("entry", [TAGGED, TAGGED_LATEST])
@pytest.mark.parametrize("name", ["f42-agent", "f42-api"])
def test_ds20_a_tagged_service_refuses_the_ordinary_deploy_before_any_build(tmp_path, name, entry):
    result, calls = run_deploy(tmp_path, services={name: with_traffic(LATEST, entry)})
    assert result.returncode == 65, result.stderr
    assert no_build_or_deploy(calls)
    assert name in result.stderr and "tag" in result.stderr


def test_ds19_services_that_follow_latest_still_deploy_in_the_ordinary_script(tmp_path):
    both = {name: with_traffic(LATEST, spec=False) for name in ("f42-agent", "f42-api")}
    result, calls = run_deploy(tmp_path, services=both)
    assert result.returncode == 0, result.stderr
    assert [call[2] for call in calls if call[:2] == ["run", "deploy"]] == ["f42-agent", "f42-api"]


def test_ds21_the_script_and_its_tests_no_longer_claim_traffic_is_untouched_until_the_move():
    body = (ROOT / "core" / "api" / "deploy.sh").read_text(encoding="utf-8")
    assert "to leave traffic where it is" not in body and "leave traffic where it is" not in body
    assert "before any traffic moves" not in body
    assert "follows LATEST" in body
    assert not any("run deploy" in line and "--no-traffic" in line for line in body.splitlines())
    workflow = (ROOT / "core" / "api" / "tests" / "test_workflow_app.py").read_text(encoding="utf-8")
    assert "def test_deploy_sh_moves_traffic_to_the_new_revisions_only_after_the_health_check" not in workflow
    assert "def test_deploy_sh_no_traffic_flag_skips_the_move" not in workflow
    assert "def test_deploy_sh_issues_its_to_latest_step_after_the_health_check_and_both_deploys" in workflow
    assert "def test_deploy_sh_no_traffic_flag_skips_only_its_own_to_latest_step" in workflow
