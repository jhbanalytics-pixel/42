"""deploy_candidate.sh (W8-REL 2.10), run with local command doubles, never a cloud client.

DS-01 to DS-18 and DS-22 of the contract. DS-19 to DS-21 (the ordinary script) are in test_deploy_script.py. The
doubles model the shapes `gcloud run services describe --format=json` returns: spec.traffic and status.traffic
entries, a revision listed by name, and a tag entry that appears only after the service is deployed.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from core.setup.tests.release_prereq import resolve_bash

ROOT = Path(__file__).resolve().parents[3]
A80 = "a80be1dee7f4ee2aa9775d0f353bab930809f057"
PROJECT = "ogilvy-trends-v2"
REPO = f"us-central1-docker.pkg.dev/{PROJECT}/intelligence-42/f42-web"
COMMIT = "d666ef64cd7a0123456789abcdef0123456789ab"
SHORT12 = COMMIT[:12]
TREE = "9aaee874e0e06e54d3354ae58bf6e576685b5303"
RID = "rel-d666ef6-01"
DIGEST = "sha256:" + "47cb8346" + "ab" * 28
CANON = {"f42-agent": "https://f42-agent-fibxg5ynpq-uc.a.run.app", "f42-api": "https://f42-api-fibxg5ynpq-uc.a.run.app"}
TAG_URL = {"f42-agent": f"https://{RID}---f42-agent-fibxg5ynpq-uc.a.run.app",
           "f42-api": f"https://{RID}---f42-api-fibxg5ynpq-uc.a.run.app"}
SERVING = {"f42-agent": "f42-agent-00047-677", "f42-api": "f42-api-00041-lns"}
OLDER = {"f42-agent": "f42-agent-00046-wks", "f42-api": "f42-api-00040-cw5"}
PRIVATE = {"metadata": {"annotations": {}}}
POLICY = {"bindings": [{"role": "roles/run.invoker", "members": [
    "serviceAccount:f42-web@ogilvy-trends-v2.iam.gserviceaccount.com"]}]}


def manifest_hash(manifest):
    body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def make_manifest(**over):
    manifest = {
        "schema_version": 1, "release_id": RID, "mode": "services-only",
        "source": {"commit": COMMIT, "tree": TREE, "short7": COMMIT[:7], "short12": SHORT12},
        "image": {"digest": DIGEST, "reference": f"{REPO}@{DIGEST}"},
        "canonical": {"agent_url": CANON["f42-agent"], "api_url": CANON["f42-api"]},
        "candidates": {name: {"revision": f"{name}-{RID}", "tag": RID, "tag_url": TAG_URL[name]} for name in CANON},
        "allowed_revisions": {name: [SERVING[name], OLDER[name]] for name in CANON},
    }
    for key, value in over.items():
        manifest[key] = value
    manifest["manifest_sha256"] = manifest_hash(manifest)
    return manifest


SYNTHETIC = ("F42_SYNTHETIC_DECLARED_ONE", "F42_SYNTHETIC_DECLARED_TWO")
BASE_ENV = ["APP_MODULE", "F42_DATA", "F42_PROJECT", "F42_VERSION"]


def service(name, *, status_url=None, latest=False, extra=(), candidate=False, candidate_percent=0, tag_to=None, env=None):
    """The description gcloud returns: pinned to the a80 revision by name unless latest. env: the names in the template."""
    first = {"revisionName": SERVING[name], "percent": 100}
    if latest:
        first["latestRevision"] = True
    traffic = [first, *extra]
    if candidate:
        traffic.append({"revisionName": tag_to or f"{name}-{RID}", "percent": candidate_percent, "tag": RID,
                        "url": TAG_URL[name]})
    spec = [{k: v for k, v in t.items() if k != "url"} for t in traffic]
    names = BASE_ENV if env is None else env
    return {"metadata": {"annotations": {}},
            "spec": {"traffic": spec, "template": {"spec": {"containers": [{"env": [{"name": n, "value": "x"} for n in names]}]}}},
            "status": {"url": status_url or CANON[name], "traffic": traffic}}


def declared_module(names):
    """core/setup/release/declared_env_removals.py as the script imports it: the real file, with its digest table replaced by
    the digests of these synthetic names when names is given, so the script's own import path runs on names that are not real."""
    text = (ROOT / "core" / "setup" / "release" / "declared_env_removals.py").read_text(encoding="utf-8")
    if names is None:
        return text
    digests = tuple(hashlib.sha256(n.encode("utf-8")).hexdigest() for n in names)
    table = re.search(r"DECLARED_ENV_REMOVAL_DIGESTS: dict = \{.*?\n\}\n", text, re.S)
    assert table, "the digest table of declared_env_removals.py changed shape"
    return text.replace(table.group(0), "DECLARED_ENV_REMOVAL_DIGESTS: dict = " + repr({"f42-agent": digests}) + "\n")


def run_candidate(tmp_path, *, manifest=None, hash_arg=None, before=None, after=None, revisions=None, head=COMMIT,
                  tree=TREE, short12=SHORT12, policy=POLICY, policy_exit=0, deploy_exit=None, declared=None):
    manifest = make_manifest() if manifest is None else manifest
    # A stop that happens because the script is missing proves nothing, so a missing script fails every test.
    assert (ROOT / "core" / "api" / "deploy_candidate.sh").exists(), "core/api/deploy_candidate.sh does not exist"
    for name in ("deploy_candidate.sh", "deploy_flags.env"):
        target = tmp_path / "core" / "api" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        source = ROOT / "core" / "api" / name
        if source.exists():
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    package = tmp_path / "core" / "setup" / "release"
    package.mkdir(parents=True, exist_ok=True)
    for init in (tmp_path / "core" / "__init__.py", tmp_path / "core" / "setup" / "__init__.py", package / "__init__.py"):
        init.write_text("", encoding="utf-8")
    (package / "declared_env_removals.py").write_text(declared_module(declared), encoding="utf-8", newline="\n")
    (tmp_path / "release").mkdir(exist_ok=True)
    path = tmp_path / "release" / "release-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    before = before or {name: service(name) for name in CANON}
    after = after or {name: service(name, candidate=True) for name in CANON}
    revisions = revisions or {name: [SERVING[name], OLDER[name]] for name in CANON}
    calls = tmp_path / "calls"
    try:
        bash = resolve_bash()
    except FileNotFoundError as error:
        pytest.fail(str(error))
    env = {**{k: v for k, v in os.environ.items() if k != "CLOUDSDK_CORE_DISABLE_FILE_LOGGING"}, "R3_CALLS": calls.as_posix(), "R3_STATE": state.as_posix(), "R3_PYTHON": Path(sys.executable).as_posix(),
           "R3_POLICY": json.dumps(policy), "R3_POLICY_EXIT": str(policy_exit),
           "R3_HEAD": head, "R3_TREE": tree, "R3_SHORT12": short12, "MSYS_NO_PATHCONV": "1",
           **{f"R3_BEFORE_{n.replace('-', '_')}": json.dumps(v) for n, v in before.items()},
           **{f"R3_AFTER_{n.replace('-', '_')}": json.dumps(v) for n, v in after.items()},
           **{f"R3_REVS_{n.replace('-', '_')}": json.dumps(v) for n, v in revisions.items()},
           **{f"R3_DEPLOY_EXIT_{n.replace('-', '_')}": str(c) for n, c in (deploy_exit or {}).items()}}
    shell = r'''
set -euo pipefail
git() {
  case "$*" in
    "rev-parse HEAD") printf '%s\n' "$R3_HEAD" ;;
    "rev-parse HEAD^{tree}") printf '%s\n' "$R3_TREE" ;;
    "rev-parse --short=12 HEAD") printf '%s\n' "$R3_SHORT12" ;;
    *) return 96 ;;
  esac
}
docker() { return 97; }
py() {
  if [ "${2:-}" = -c ]; then command "$R3_PYTHON" -c "$3" "${@:4}"; return; fi
  return 98
}
gcloud() {
  printf '%s\n' "${CLOUDSDK_CORE_DISABLE_FILE_LOGGING-unset}" >> "$R3_STATE/filelog"
  printf '%s\0' "$@" >> "$R3_CALLS"
  printf '\0' >> "$R3_CALLS"
  case "$1 $2 $3" in
    'run services describe')
      name=$4; key=${name//-/_}
      if [[ "$*" == *'--format=json'* ]]; then
        flag="$R3_STATE/deployed-$name"
        if [ -e "$flag" ]; then var="R3_AFTER_$key"; else var="R3_BEFORE_$key"; fi
        printf '%s\n' "${!var}"
      else printf 'https://test.invalid\n'; fi ;;
    'run services get-iam-policy') printf '%s\n' "$R3_POLICY"; return "$R3_POLICY_EXIT" ;;
    'run revisions list')
      name=; for a in "$@"; do if [ "$prev" = --service ]; then name=$a; fi; prev=$a; done
      var="R3_REVS_${name//-/_}"
      "$R3_PYTHON" -c 'import json,sys; print(json.dumps([{"metadata": {"name": n}} for n in json.loads(sys.argv[1])]))' "${!var}" ;;
    'run deploy f42-agent'|'run deploy f42-api')
      name=$3; var="R3_DEPLOY_EXIT_${name//-/_}"; code=${!var:-0}
      if [ "$code" = 0 ]; then : > "$R3_STATE/deployed-$name"; fi
      return "$code" ;;
    *) return 99 ;;
  esac
}
prev=
source core/api/deploy_candidate.sh "$@"
'''
    result = subprocess.run([str(bash), "--noprofile", "--norc", "-c", shell, "candidate-test", path.as_posix(),
                             manifest["manifest_sha256"] if hash_arg is None else hash_arg],
                            cwd=tmp_path, env=env, capture_output=True, encoding="utf-8", timeout=30)
    raw = calls.read_bytes() if calls.exists() else b""
    recorded = [part.decode("utf-8").split("\0") for part in raw.split(b"\0\0") if part]
    return result, recorded


def deploys(calls):
    return [call for call in calls if call[:2] == ["run", "deploy"]]


def flag_value(call, flag):
    return call[call.index(flag) + 1]


def test_ds01_both_deploys_use_the_manifest_digest_and_never_an_image_tag(tmp_path):
    result, calls = run_candidate(tmp_path)
    assert result.returncode == 0, result.stderr
    assert [c[2] for c in deploys(calls)] == ["f42-agent", "f42-api"]
    for call in deploys(calls):
        assert flag_value(call, "--image") == f"{REPO}@{DIGEST}"
        assert not any("f42-web:" in arg for arg in call)


def test_ds02_both_deploys_carry_no_traffic_tag_and_revision_suffix(tmp_path):
    result, calls = run_candidate(tmp_path)
    assert result.returncode == 0, result.stderr
    for call in deploys(calls):
        assert "--no-traffic" in call
        assert flag_value(call, "--tag") == RID and flag_value(call, "--revision-suffix") == RID


def test_ds03_the_script_never_moves_traffic(tmp_path):
    result, calls = run_candidate(tmp_path)
    assert result.returncode == 0, result.stderr
    assert not any("update-traffic" in call for call in calls)
    text = (ROOT / "core" / "api" / "deploy_candidate.sh").read_text(encoding="utf-8")
    assert "--to-latest" not in text and "update-traffic" not in text


def test_ds04_a_failed_agent_deploy_stops_with_its_exit_code_and_never_deploys_the_api(tmp_path):
    result, calls = run_candidate(tmp_path, deploy_exit={"f42-agent": 17})
    assert result.returncode == 17, result.stderr
    assert [c[2] for c in deploys(calls)] == ["f42-agent"]


def test_ds05_an_agent_tag_that_names_another_revision_stops_before_the_api_deploy(tmp_path):
    after = {"f42-agent": service("f42-agent", candidate=True, tag_to="f42-agent-00047-677"),
             "f42-api": service("f42-api", candidate=True)}
    result, calls = run_candidate(tmp_path, after=after)
    assert result.returncode != 0 and "TAG_MAPPING" in result.stderr
    assert [c[2] for c in deploys(calls)] == ["f42-agent"]


@pytest.mark.parametrize("bad", [
    {"percent": 5}, {"url": "https://wrong---f42-agent-fibxg5ynpq-uc.a.run.app"}])
def test_ds05_an_agent_tag_with_traffic_or_the_wrong_url_stops_before_the_api_deploy(tmp_path, bad):
    candidate = {"revisionName": f"f42-agent-{RID}", "percent": bad.get("percent", 0), "tag": RID,
                 "url": bad.get("url", TAG_URL["f42-agent"])}
    broken = service("f42-agent")
    broken["status"]["traffic"].append(candidate)
    broken["spec"]["traffic"].append({k: v for k, v in candidate.items() if k != "url"})
    result, calls = run_candidate(tmp_path, after={"f42-agent": broken, "f42-api": service("f42-api", candidate=True)})
    assert result.returncode != 0 and "TAG_MAPPING" in result.stderr
    assert [c[2] for c in deploys(calls)] == ["f42-agent"]


def test_ds05_an_api_tag_that_is_missing_after_its_deploy_fails_the_script(tmp_path):
    after = {"f42-agent": service("f42-agent", candidate=True), "f42-api": service("f42-api")}
    result, calls = run_candidate(tmp_path, after=after)
    assert result.returncode != 0 and "TAG_MAPPING" in result.stderr
    assert [c[2] for c in deploys(calls)] == ["f42-agent", "f42-api"]


def test_ds06_the_api_gets_the_agent_tag_url_and_the_canonical_audience_from_the_manifest(tmp_path):
    result, calls = run_candidate(tmp_path)
    assert result.returncode == 0, result.stderr
    agent, api = deploys(calls)
    base = (f"APP_MODULE=core.api.app:app,F42_DATA=bigquery,F42_PROJECT={PROJECT},F42_VERSION={SHORT12}")
    assert flag_value(api, "--update-env-vars") == f"{base},AGENT_URL={TAG_URL['f42-agent']},AGENT_AUDIENCE={CANON['f42-agent']}"
    agent_env = flag_value(agent, "--update-env-vars")
    assert "AGENT_URL" not in agent_env and "AGENT_AUDIENCE" not in agent_env and f"F42_VERSION={SHORT12}" in agent_env


def test_ds07_a_live_agent_url_that_differs_from_the_manifest_stops_before_any_deploy(tmp_path):
    before = {"f42-agent": service("f42-agent", status_url="https://f42-agent-590353929363.us-central1.run.app"),
              "f42-api": service("f42-api")}
    result, calls = run_candidate(tmp_path, before=before)
    assert result.returncode != 0 and deploys(calls) == []


def test_ds08_a_head_that_is_not_the_manifest_commit_stops_before_any_write(tmp_path):
    for case in ({"short12": "000000000000"}, {"head": "0" * 40}, {"tree": "0" * 40}):
        result, calls = run_candidate(tmp_path / case_key(case), **case)
        assert result.returncode != 0 and calls == [], case


def case_key(case):
    return "_".join(case)


def test_ds09_a_hash_argument_that_is_not_the_files_hash_stops_before_any_write(tmp_path):
    result, calls = run_candidate(tmp_path, hash_arg="0" * 64)
    assert result.returncode != 0 and calls == []


def test_ds09_a_manifest_edited_without_its_hash_stops_before_any_write(tmp_path):
    manifest = make_manifest()
    edited = {**manifest, "image": {**manifest["image"], "digest": "sha256:" + "0" * 64}}
    result, calls = run_candidate(tmp_path, manifest=edited, hash_arg=manifest["manifest_sha256"])
    assert result.returncode != 0 and calls == []


def test_ds09_a_manifest_whose_stored_hash_field_is_stale_stops_even_with_the_right_argument(tmp_path):
    manifest = make_manifest()
    real = manifest["manifest_sha256"]
    stale = {**manifest, "manifest_sha256": "0" * 64}
    result, calls = run_candidate(tmp_path, manifest=stale, hash_arg=real)
    assert result.returncode != 0 and calls == []


def test_ds10_an_unknown_schema_version_stops_before_any_write(tmp_path):
    result, calls = run_candidate(tmp_path, manifest=make_manifest(schema_version=2))
    assert result.returncode != 0 and calls == []


@pytest.mark.parametrize("tag", ["latest", "rel-d666ef6-1", "REL-d666ef6-01", "rel-d666ef6-01 ", "rel-D666EF6-01", ""])
def test_ds11_a_tag_off_the_grammar_stops_before_any_write(tmp_path, tag):
    manifest = make_manifest(release_id=tag)
    for name, entry in manifest["candidates"].items():
        # Everything else is made consistent with the bad tag, so only the grammar can refuse it.
        entry.update(tag=tag, revision=f"{name}-{tag}", tag_url=f"https://{tag}---f42-agent-fibxg5ynpq-uc.a.run.app"
                     if name == "f42-agent" else f"https://{tag}---f42-api-fibxg5ynpq-uc.a.run.app")
    manifest["source"].update(short7=tag[4:11] if len(tag) >= 11 else manifest["source"]["short7"])
    manifest["manifest_sha256"] = manifest_hash(manifest)
    result, calls = run_candidate(tmp_path, manifest=manifest)
    assert result.returncode != 0 and deploys(calls) == []


@pytest.mark.parametrize("name", ["f42-agent", "f42-api"])
def test_ds12_a_service_that_already_holds_the_tag_stops_before_any_deploy(tmp_path, name):
    before = {n: service(n) for n in CANON}
    before[name] = service(name, candidate=True)
    result, calls = run_candidate(tmp_path, before=before)
    assert result.returncode != 0 and deploys(calls) == []


@pytest.mark.parametrize("name", ["f42-agent", "f42-api"])
def test_ds13_a_revision_outside_the_allowed_set_stops_before_any_deploy(tmp_path, name):
    revisions = {n: [SERVING[n], OLDER[n]] for n in CANON}
    revisions[name] = [*revisions[name], f"{name}-00048-zzz"]
    result, calls = run_candidate(tmp_path, revisions=revisions)
    assert result.returncode != 0 and "UNRELATED_REVISION" in result.stderr and deploys(calls) == []


@pytest.mark.parametrize("name", ["f42-agent", "f42-api"])
def test_ds13_the_candidate_revision_already_existing_stops_before_any_deploy(tmp_path, name):
    revisions = {n: [SERVING[n], OLDER[n]] for n in CANON}
    revisions[name] = [*revisions[name], f"{name}-{RID}"]
    result, calls = run_candidate(tmp_path, revisions=revisions)
    assert result.returncode != 0 and deploys(calls) == []


@pytest.mark.parametrize("name", ["f42-agent", "f42-api"])
def test_ds13_a_candidate_revision_that_is_also_listed_as_allowed_still_stops_when_it_exists(tmp_path, name):
    allowed = {n: [SERVING[n], OLDER[n], f"{n}-{RID}"] for n in CANON}
    revisions = {n: [SERVING[n], OLDER[n]] for n in CANON}
    revisions[name] = [*revisions[name], f"{name}-{RID}"]
    result, calls = run_candidate(tmp_path, manifest=make_manifest(allowed_revisions=allowed), revisions=revisions)
    assert result.returncode != 0 and deploys(calls) == []


def test_ds13_a_prior_attempt_revision_declared_in_the_manifest_is_allowed(tmp_path):
    prior = {n: f"{n}-rel-d666ef6-00" for n in CANON}
    allowed = {n: [SERVING[n], OLDER[n], prior[n]] for n in CANON}
    revisions = {n: [SERVING[n], OLDER[n], prior[n]] for n in CANON}
    result, calls = run_candidate(tmp_path, manifest=make_manifest(allowed_revisions=allowed), revisions=revisions)
    assert result.returncode == 0, result.stderr


def test_ds14_a_public_or_unreadable_agent_stops_before_any_deploy(tmp_path):
    cases = [
        {"policy": {"bindings": [{"role": "roles/run.invoker", "members": ["allUsers"]}]}},
        {"policy": {"bindings": [{"role": "roles/run.invoker", "members": ["allAuthenticatedUsers"]}]}},
        {"agent": {"metadata": {"annotations": {"run.googleapis.com/invoker-iam-disabled": "true"}}}},
        {"policy_exit": 29},
    ]
    for index, case in enumerate(cases):
        before = {n: service(n) for n in CANON}
        if "agent" in case:
            before["f42-agent"]["metadata"] = case["agent"]["metadata"]
            case = {}
        result, calls = run_candidate(tmp_path / str(index), before=before, **case)
        assert result.returncode != 0 and deploys(calls) == [], index


def test_ds15_the_script_has_no_iam_write_and_no_env_removal_flags():
    text = (ROOT / "core" / "api" / "deploy_candidate.sh").read_text(encoding="utf-8")
    for word in ("set-iam-policy", "add-iam-policy-binding", "gcloud iam", "--allow-unauthenticated", "--set-env-vars",
                 "--clear-env-vars"):
        assert word not in text, word
    assert "--update-env-vars" in text
    assert text.count("--remove-env-vars") == 1  # the one declared removal, on the f42-agent deploy only


def test_ds16_every_gcloud_run_line_names_the_project_and_region_and_the_only_literal_is_the_project():
    import re

    text = (ROOT / "core" / "api" / "deploy_candidate.sh").read_text(encoding="utf-8")
    assert set(re.findall(r"ogilvy-[a-z0-9-]+", text)) == {PROJECT}
    commands = re.sub(r"\\\n\s*", " ", text)
    seen = 0
    for line in commands.splitlines():
        if re.search(r"\bgcloud (run|builds) ", line):
            seen += 1
            assert '--project "$PROJECT"' in line and '--region "$REGION"' in line, line
    assert seen >= 5
    for secretish in ("PASSCODE", "API_KEY", "SECRET"):
        assert not re.search(rf"echo[^\n]*{secretish}", text)


def test_ds17_deploy_flags_env_is_byte_identical_to_a80():
    result = subprocess.run(["git", "show", f"{A80}:core/api/deploy_flags.env"], cwd=ROOT, capture_output=True)
    if result.returncode != 0:
        pytest.fail(f"commit {A80} is not available in this checkout; a shallow clone cannot run the release tests")
    assert hashlib.sha256((ROOT / "core" / "api" / "deploy_flags.env").read_bytes().replace(b"\r\n", b"\n")).hexdigest() == \
        hashlib.sha256(result.stdout.replace(b"\r\n", b"\n")).hexdigest()


def test_ds22_a_service_that_still_follows_latest_stops_before_any_deploy(tmp_path):
    for name in CANON:
        before = {n: service(n) for n in CANON}
        before[name] = service(name, latest=True)
        before[name]["status"]["traffic"] = [{"latestRevision": True, "percent": 100, "revisionName": SERVING[name]}]
        before[name]["spec"]["traffic"] = [{"latestRevision": True, "percent": 100}]
        result, calls = run_candidate(tmp_path / name, before=before)
        assert result.returncode != 0 and deploys(calls) == [], name


def test_ds_the_image_reference_must_be_the_repository_at_the_manifest_digest(tmp_path):
    manifest = make_manifest()
    manifest["image"]["reference"] = f"{REPO}:{SHORT12}"
    manifest["manifest_sha256"] = manifest_hash(manifest)
    result, calls = run_candidate(tmp_path, manifest=manifest)
    assert result.returncode != 0 and deploys(calls) == []


def test_ds_a_tag_url_that_is_not_built_from_the_canonical_host_stops_before_any_deploy(tmp_path):
    manifest = make_manifest()
    manifest["candidates"]["f42-agent"]["tag_url"] = "https://other---f42-agent-fibxg5ynpq-uc.a.run.app"
    manifest["manifest_sha256"] = manifest_hash(manifest)
    result, calls = run_candidate(tmp_path, manifest=manifest)
    assert result.returncode != 0 and deploys(calls) == []


def test_ds_an_empty_hash_argument_stops_before_any_write(tmp_path):
    result, calls = run_candidate(tmp_path, hash_arg="")
    assert result.returncode != 0 and calls == []


def test_ds18_every_a80_test_of_the_two_deploy_files_is_still_there_apart_from_the_two_renames_the_contract_asks_for():
    import ast

    def names(source):
        return {n.name for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")}

    renamed = {"core/api/tests/test_workflow_app.py": {"test_deploy_sh_moves_traffic_to_the_new_revisions_only_after_the_health_check",
                                                       "test_deploy_sh_no_traffic_flag_skips_the_move"}}
    for rel in ("core/api/tests/test_deploy_script.py", "core/api/tests/test_workflow_app.py"):
        shown = subprocess.run(["git", "show", f"{A80}:{rel}"], cwd=ROOT, capture_output=True)
        if shown.returncode != 0:
            pytest.fail(f"commit {A80} is not available in this checkout; a shallow clone cannot run the release tests")
        missing = names(shown.stdout.decode("utf-8")) - names((ROOT / rel).read_text(encoding="utf-8"))
        assert missing == renamed.get(rel, set()), rel


# The declared environment removal: exactly the live f42-agent names whose digest is declared, nothing else.

def remove_flag(call):
    return flag_value(call, "--remove-env-vars") if "--remove-env-vars" in call else None


def agent_with(names):
    return {"f42-agent": service("f42-agent", env=[*BASE_ENV, *names]), "f42-api": service("f42-api")}


def test_ds_removal_the_agent_deploy_removes_exactly_the_declared_names_that_are_live_and_keeps_the_update_flag(tmp_path):
    result, calls = run_candidate(tmp_path, before=agent_with(SYNTHETIC), declared=SYNTHETIC)
    assert result.returncode == 0, result.stderr
    agent, api = deploys(calls)
    assert remove_flag(agent) == ",".join(sorted(SYNTHETIC))
    assert "--update-env-vars" in agent and "--no-traffic" in agent
    assert remove_flag(api) is None


@pytest.mark.parametrize("present", [SYNTHETIC[:1], SYNTHETIC[1:]])
def test_ds_removal_only_the_declared_names_that_are_live_are_removed(tmp_path, present):
    result, calls = run_candidate(tmp_path, before=agent_with(present), declared=SYNTHETIC)
    assert result.returncode == 0, result.stderr
    assert remove_flag(deploys(calls)[0]) == present[0]


def test_ds_removal_a_service_with_no_declared_name_gets_no_remove_flag(tmp_path):
    result, calls = run_candidate(tmp_path, declared=SYNTHETIC)
    assert result.returncode == 0, result.stderr
    assert all(remove_flag(call) is None for call in deploys(calls))


def test_ds_removal_undeclared_names_are_never_removed_even_next_to_declared_ones(tmp_path):
    names = [*SYNTHETIC, "F42_OTHER_SETTING", "GEMINI_MODEL"]
    result, calls = run_candidate(tmp_path, before=agent_with(names), declared=SYNTHETIC)
    assert result.returncode == 0, result.stderr
    assert remove_flag(deploys(calls)[0]) == ",".join(sorted(SYNTHETIC))


def test_ds_removal_the_api_is_never_asked_to_remove_anything_even_when_it_carries_a_declared_name(tmp_path):
    before = {"f42-agent": service("f42-agent"), "f42-api": service("f42-api", env=[*BASE_ENV, *SYNTHETIC])}
    result, calls = run_candidate(tmp_path, before=before, declared=SYNTHETIC)
    assert result.returncode == 0, result.stderr
    assert all(remove_flag(call) is None for call in deploys(calls))


def test_ds_removal_the_real_table_removes_nothing_that_it_does_not_declare(tmp_path):
    result, calls = run_candidate(tmp_path, before=agent_with(SYNTHETIC))
    assert result.returncode == 0, result.stderr
    assert all(remove_flag(call) is None for call in deploys(calls))


def test_ds_removal_the_script_says_how_many_it_removes_and_never_prints_a_name(tmp_path):
    result, calls = run_candidate(tmp_path, before=agent_with(SYNTHETIC), declared=SYNTHETIC)
    assert "removing 2 declared environment variable(s)" in result.stderr
    for name in SYNTHETIC:
        assert name not in result.stdout and name not in result.stderr


def test_ds_removal_a_declared_name_that_is_not_a_plain_identifier_stops_before_any_deploy(tmp_path):
    bad = "BAD NAME;touch x"
    result, calls = run_candidate(tmp_path, before=agent_with([bad]), declared=[bad])
    assert result.returncode != 0 and deploys(calls) == []
    assert "STOP" in result.stderr and bad not in result.stderr


def test_ds_removal_the_removal_is_read_before_the_agent_deploy_and_the_api_deploy_still_follows_the_agent_tag(tmp_path):
    result, calls = run_candidate(tmp_path, before=agent_with(SYNTHETIC), declared=SYNTHETIC)
    assert result.returncode == 0, result.stderr
    assert [c[2] for c in deploys(calls)] == ["f42-agent", "f42-api"]
    assert not any(call[:2] == ["run", "deploy"] and "--remove-env-vars" in call and call[2] != "f42-agent" for call in calls)


def test_ds_removal_every_deploy_the_script_makes_is_inside_the_deploy_allowlist_of_the_release_plan(tmp_path):
    from core.setup.release import plan

    for label, before, declared in (("with", agent_with(SYNTHETIC), SYNTHETIC), ("without", None, None)):
        result, calls = run_candidate(tmp_path / label, before=before, declared=declared)
        assert result.returncode == 0, result.stderr
        assert len(deploys(calls)) == 2
        assert all(plan.deploy_call_allowed(call) for call in deploys(calls)), (label, [c[2] for c in deploys(calls)])
    agent = deploys(run_candidate(tmp_path / "again", before=agent_with(SYNTHETIC), declared=SYNTHETIC)[1])[0]
    assert "--remove-env-vars" in agent


def test_ds_removal_every_gcloud_the_script_runs_has_gcloud_file_logging_off_so_the_removed_names_stay_out_of_its_log(tmp_path):
    # gcloud logs every specified argument at DEBUG to its own file unless this is set, and the remove flag carries the names
    result, calls = run_candidate(tmp_path, before=agent_with(SYNTHETIC), declared=SYNTHETIC)
    assert result.returncode == 0, result.stderr
    seen = (tmp_path / "state" / "filelog").read_text(encoding="utf-8").split()
    assert len(seen) == len(calls) and len(seen) > 5 and set(seen) == {"1"}
