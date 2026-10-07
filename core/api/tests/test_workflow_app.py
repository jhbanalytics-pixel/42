"""Checks on the app's staging workflow (task 3.8): f42-agent and f42-api deploy to staging on a push to full-42
through Workload Identity Federation, with the same Cloud Run flags as core/api/deploy.sh because both read
core/api/deploy_flags.env. Reads files only; nothing runs and nothing calls Google Cloud."""
import ast
import fnmatch
import posixpath
import re
import shlex
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "parked-workflows" / "staging-app.yml"
DEPLOY_SH = ROOT / "core" / "api" / "deploy.sh"
FLAGS = ROOT / "core" / "api" / "deploy_flags.env"
BOOTSTRAP = ROOT / "core" / "setup" / "bootstrap.py"
DOCKERFILE = ROOT / "core" / "api" / "Dockerfile"
DOCKERIGNORE = ROOT / "core" / "api" / "Dockerfile.dockerignore"

# What run_ask needs in the f42-agent image after the merge (L3 Needs item 2): L3's agent, L1's SocialCrawl
# client, the caps it checks and the routes file. The client finds the last two from its own location,
# ROOT = parents[2] of core/collect/socialcrawl_client.py, so each must sit at this path relative to core/'s parent.
AGENT_NEEDS = ("core/agent/ask.py", "core/agent/tools/sc_adapter.py", "core/agent/requirements.txt",
               "core/collect/socialcrawl_client.py", "core/collect/stores.py", "core/config/caps.yaml",
               "docs/full-42/reference/sc_routes.json")

STAGING_PROJECT = "ogilvy-trends-v2"
STAGING_NUMBER = "590353929363"
REPO_PATH = f"us-central1-docker.pkg.dev/{STAGING_PROJECT}/intelligence-42/f42-web"
PINNED = re.compile(r"^\s*(-\s+)?uses:\s+[\w.-]+/[\w./-]+@[0-9a-f]{40}\s+#\s+v\d+\.\d+\.\d+\s*$")
SERVICES = ("f42-agent", "f42-api")

# The Cloud Run definitions deploy.sh carried before the flags moved to deploy_flags.env (task 1.14), except
# f42-agent's --min-instances, raised from 0 to 1 so one instance stays warm for Ask, plus F42_T2_READY=1 on
# f42-agent since 3 October 2026 (T2 for the brand lens and the context pack), and f42-api's --min-instances
# raised from 0 to 1 on 4 October 2026 so the first page load in demo week skips a cold start.
EXPECTED = {
    "BUILD_SOURCE_STAGING_DIR": "gs://ogilvy-trends-v2-f42-media-staging/build-source",
    "AGENT_SA": "f42-agent",
    "AGENT_FLAGS": ["--min-instances", "1", "--max-instances", "1",
                    "--no-cpu-throttling", "--timeout", "3600", "--memory", "1Gi"],
    "AGENT_ENV": "APP_MODULE=core.api.agent_app:app,F42_DATA=bigquery,F42_PROJECT=${PROJECT},F42_VERSION=${TAG},"
                 "MODEL_PROVIDER=gemini,GEMINI_MODEL=gemini-3.8-flash,F42_T2_READY=1",
    "AGENT_SECRETS": "SOCIALCRAWL_OGILVY_API_KEY=SOCIALCRAWL_OGILVY_API_KEY:latest",
    "API_SA": "f42-web",
    "API_FLAGS": ["--no-invoker-iam-check", "--min-instances", "1", "--max-instances", "3", "--timeout", "3600"],
    "API_ENV": "APP_MODULE=core.api.app:app,F42_DATA=bigquery,F42_PROJECT=${PROJECT},F42_VERSION=${TAG}",
    "API_SECRETS": "UI_PASSCODE=UI_PASSCODE:latest",
}
SERVICE_VARS = {"f42-agent": "AGENT", "f42-api": "API"}
CLOUD_RUN_FLAGS = ("--min-instances", "--max-instances", "--timeout", "--memory", "--cpu-throttling",
                   "--no-cpu-throttling", "--allow-unauthenticated", "--no-allow-unauthenticated",
                   "--invoker-iam-check", "--no-invoker-iam-check", "--set-env-vars", "--update-env-vars",
                   "--set-secrets")


def load():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def text(path):
    return path.read_text(encoding="utf-8")


def triggers(wf):
    # PyYAML reads the bare key on as the boolean True.
    return wf.get("on", wf.get(True))


def steps(wf):
    return [s for job in wf["jobs"].values() for s in job.get("steps", [])]


def runs(wf):
    return "\n".join(s.get("run", "") for s in steps(wf))


def bootstrap_constants():
    """POOL, PROVIDER and GITHUB_REPO from bootstrap.py, read as source so nothing is imported or run.
    Lane L4 has not merged full-42 yet, so until it does the file comes from origin/full-42."""
    if BOOTSTRAP.exists():
        source = BOOTSTRAP.read_text(encoding="utf-8")
    else:
        source = subprocess.run(["git", "show", "origin/full-42:core/setup/bootstrap.py"], cwd=ROOT,
                                capture_output=True, check=True).stdout.decode("utf-8")
    wanted = {"POOL", "PROVIDER", "GITHUB_REPO"}
    found = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in wanted and isinstance(node.value, ast.Constant):
                found[name] = node.value.value
    assert set(found) == wanted, found
    return found


def flags_file():
    """NAME=value lines of deploy_flags.env, each value read the way bash reads one quoted word."""
    values = {}
    for line in text(FLAGS).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, raw = line.partition("=")
        (value,) = shlex.split(raw, posix=True)
        values[name] = value
    return values


def deploy_commands(body):
    """Every `gcloud run deploy <service> ...` command in a shell body, continuation lines joined."""
    joined = re.sub(r"\\\n\s*", " ", body)
    commands = {}
    for line in joined.splitlines():
        m = re.search(r"gcloud run deploy (\S+) .*", line)
        if m:
            assert m.group(1) not in commands, f"{m.group(1)} is deployed twice"
            commands[m.group(1)] = m.group(0).strip()
    return commands


def normalised(command):
    """A deploy command with its image variable and gcloud's --quiet taken out."""
    command = re.sub(r'--image "\$\{?\w+\}?"', "--image IMAGE", command)
    return re.sub(r"\s+--quiet\b", "", command)


def test_parses_with_one_job():
    wf = load()
    assert list(wf["jobs"]) == ["deploy"]


def test_runs_on_a_push_to_full_42_touching_the_app_or_by_hand():
    on = triggers(load())
    assert set(on) == {"push", "workflow_dispatch"}
    assert on["push"]["branches"] == ["full-42"]
    # The image copies all of core/ (f42-agent runs L3's core.agent), so any core change redeploys.
    assert set(on["push"]["paths"]) == {"core/**", "app/frontend/**", "docs/full-42/reference/**", ".github/workflows/staging-app.yml"}


def test_job_refuses_other_repositories_and_refs():
    repo = bootstrap_constants()["GITHUB_REPO"]
    for job in load()["jobs"].values():
        assert f"github.repository == '{repo}'" in job["if"]
        assert "github.ref == 'refs/heads/full-42'" in job["if"]


def test_token_permissions_are_minimal():
    assert load()["permissions"] == {"contents": "read", "id-token": "write"}


def test_two_deploys_never_overlap():
    concurrency = load()["concurrency"]
    assert concurrency["group"]
    assert concurrency["cancel-in-progress"] is False


def test_every_action_is_pinned_to_a_commit_with_its_version():
    uses = [line for line in text(WORKFLOW).splitlines() if re.match(r"^\s*(-\s+)?uses:", line)]
    assert uses
    for line in uses:
        assert PINNED.match(line), line


def test_authenticates_as_the_deployer_through_the_bootstrap_provider():
    c = bootstrap_constants()
    provider = (f"projects/{STAGING_NUMBER}/locations/global/workloadIdentityPools/{c['POOL']}"
                f"/providers/{c['PROVIDER']}")
    auth = [s for s in steps(load()) if s.get("uses", "").startswith("google-github-actions/auth@")]
    assert len(auth) == 1
    assert auth[0]["with"] == {"workload_identity_provider": provider,
                               "service_account": f"f42-deployer@{STAGING_PROJECT}.iam.gserviceaccount.com"}
    (line,) = [ln for ln in text(WORKFLOW).splitlines() if "google-github-actions/auth@" in ln]
    assert re.search(r"#\s+v3\.\d+\.\d+\s*$", line)


def test_builds_with_docker_and_pushes_through_the_gcloud_helper():
    commands = runs(load())
    assert "gcloud auth configure-docker us-central1-docker.pkg.dev" in commands
    assert "DOCKER_BUILDKIT=1 docker build -f core/api/Dockerfile" in commands
    assert re.search(r"docker build -f core/api/Dockerfile -t \"\$IMAGE\" \.\s*$", commands, re.M)
    assert f'IMAGE="{REPO_PATH}:' in commands
    assert 'docker push "$IMAGE"' in commands
    build = commands.index("docker build")
    assert commands.index("configure-docker") < build < commands.index("docker push")
    assert commands.index("docker push") < commands.index("gcloud run deploy")


def test_deploys_the_image_by_digest_never_by_tag():
    commands = runs(load())
    assert "gcloud artifacts docker images describe" in commands
    assert f"{REPO_PATH}@sha256:[0-9a-f]{{64}}" in commands.replace("\\.", ".")
    for service, command in deploy_commands(commands).items():
        assert '--image "$DIGEST_IMAGE"' in command, service
    assert commands.index("gcloud artifacts docker images describe") < commands.index("gcloud run deploy")


def test_both_deployers_read_the_one_flags_file():
    source = re.compile(r"^\s*(source|\.) core/api/deploy_flags\.env\s*$", re.M)
    assert source.search(text(DEPLOY_SH))
    assert source.search(runs(load()))


def test_flags_file_holds_the_task_1_14_definitions():
    assert flags_file() == {k: " ".join(v) if isinstance(v, list) else v for k, v in EXPECTED.items()}


def test_deploy_sh_and_the_workflow_deploy_both_services_the_same_way():
    sh = deploy_commands(text(DEPLOY_SH))
    wf = deploy_commands(runs(load()))
    assert list(sh) == list(wf) == list(SERVICES)
    for service in SERVICES:
        assert normalised(sh[service]) == normalised(wf[service]), service
        prefix = SERVICE_VARS[service]
        command = wf[service]
        assert f'--service-account "$(SA "${prefix}_SA")"' in command
        assert f"${prefix}_FLAGS" in command
        assert f'--set-secrets "${prefix}_SECRETS"' in command
        assert f'--project "$PROJECT" --region "$REGION"' in command


def test_no_cloud_run_flag_is_set_outside_the_flags_file():
    expected_env = {"f42-agent": "$AGENT_ENV", "f42-api": "${API_ENV},AGENT_URL=${AGENT_URL}"}
    for body in (text(DEPLOY_SH), runs(load())):
        for service, command in deploy_commands(body).items():
            words = command.split()
            for flag in CLOUD_RUN_FLAGS:
                if flag in ("--update-env-vars", "--set-secrets"):
                    continue
                assert flag not in words, (service, flag)
            assert "--set-env-vars" not in command, service
            env = re.findall(r'--update-env-vars "([^"]+)"', command)
            assert env == [expected_env[service]], (service, env)


def test_the_agent_is_deployed_first_and_the_api_gets_its_url():
    commands = runs(load())
    agent = commands.index("gcloud run deploy f42-agent")
    url = commands.index("AGENT_URL=$(gcloud run services describe f42-agent")
    api = commands.index("gcloud run deploy f42-api")
    assert agent < url < api
    assert "AGENT_URL=${AGENT_URL}" in deploy_commands(commands)["f42-api"]


def test_smoke_checks_health_gate_and_page_without_a_passcode():
    commands = runs(load())
    smoke = commands[commands.index("gcloud run deploy f42-api"):]
    assert "API_URL=$(gcloud run services describe f42-api" in smoke
    assert "from core.api import smoke" in smoke
    assert "smoke.check_health(" in smoke and "smoke.check_gate(" in smoke
    assert re.search(r"\.get\(base \+ \"/\"\)", smoke)
    assert "status_code == 200" in smoke
    assert "sys.exit(" in smoke
    assert "F42_SMOKE_PASSCODE" not in text(WORKFLOW)
    assert "check_today" not in smoke and "X-Passcode" not in smoke


def test_staging_project_only_and_no_production():
    body = text(WORKFLOW) + text(FLAGS)
    assert set(re.findall(r"ogilvy-[a-z0-9-]+", body)) == {STAGING_PROJECT, "ogilvy-trends-v2-f42-media-staging"}
    assert "vars." not in text(WORKFLOW)
    assert "production" not in text(WORKFLOW).lower()


def test_no_iam_commands_and_the_agent_stays_private():
    for body in (text(WORKFLOW), text(FLAGS), text(DEPLOY_SH)):
        assert "add-iam-policy-binding" not in body
        assert "set-iam-policy" not in body
        assert "gcloud iam " not in body
        assert not re.search(r"(?<!no-)--allow-unauthenticated", body)
    assert "--no-allow-unauthenticated" not in flags_file()["AGENT_FLAGS"].split()
    assert "--no-invoker-iam-check" not in flags_file()["AGENT_FLAGS"].split()


def test_no_keys_secrets_or_echoed_values():
    body = text(WORKFLOW)
    for word in ("credentials_json", "secrets.", "private_key", "service_account_key", "BEGIN PRIVATE",
                 "set -x", "gcloud secrets", "print-access-token", "UI_PASSCODE", "SOCIALCRAWL"):
        assert word not in body, word
    for line in body.splitlines():
        if re.search(r"\becho\b", line):
            assert not re.search(r"SECRET|PASSCODE|TOKEN|KEY", line), line


def ignored(path):
    """Whether Dockerfile.dockerignore keeps path out of the build context: the last matching line wins, and a
    line matching a directory covers everything under it."""
    parts = path.split("/")
    candidates = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    out = False
    for line in text(DOCKERIGNORE).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        pattern = line.lstrip("!").strip("/")
        if any(fnmatch.fnmatchcase(c, pattern) for c in candidates):
            out = not negated
    return out


def final_stage():
    """(instruction, arguments) of the Dockerfile's last stage, continuation lines joined."""
    body = re.sub(r"\\\r?\n\s*", " ", text(DOCKERFILE))
    lines = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    last = max(i for i, ln in enumerate(lines) if ln.upper().startswith("FROM "))
    return [(ln.split(None, 1)[0].upper(), ln.split(None, 1)[1]) for ln in lines[last + 1:]]


def image_path(path):
    """Where the final stage puts a context file and the index of the COPY that puts it there, or (None, None)."""
    workdir, found = "/", (None, None)
    for i, (op, args) in enumerate(final_stage()):
        if op == "WORKDIR":
            workdir = posixpath.join(workdir, args)
        if op != "COPY" or args.startswith("--from"):
            continue
        src, dest = args.split()
        dest = posixpath.join(workdir, dest)
        if src.endswith("/") and path.startswith(src):
            found = (posixpath.join(dest, path[len(src):]), i)
        elif src == path:
            found = (posixpath.join(dest, posixpath.basename(src)) if dest.endswith("/") else dest, i)
    return found


def test_build_context_holds_what_run_ask_needs():
    for path in AGENT_NEEDS:
        assert not ignored(path), path
    # The rest of docs stays out of the image.
    assert ignored("docs/full-42/SPEC.md")
    assert ignored("docs/full-42/progress/L4.md")


def test_image_puts_run_asks_files_where_the_socialcrawl_client_looks():
    client, _ = image_path("core/collect/socialcrawl_client.py")
    assert client
    root = posixpath.dirname(posixpath.dirname(posixpath.dirname(client)))
    for path in AGENT_NEEDS:
        assert image_path(path)[0] == posixpath.join(root, path), path


def test_image_installs_the_agent_requirements_after_copying_them():
    _, copied = image_path("core/agent/requirements.txt")
    installs = [i for i, (op, args) in enumerate(final_stage())
                if op == "RUN" and "pip install --no-cache-dir -r core/agent/requirements.txt" in args]
    assert installs and copied is not None and min(installs) > copied


def test_agent_keeps_one_instance():
    # Ask's one-live-SocialCrawl-call lock is per process, so a second instance would break it.
    words = flags_file()["AGENT_FLAGS"].split()
    assert words[words.index("--max-instances") + 1] == "1"
    assert words.count("--max-instances") == 1


def test_agent_keeps_one_instance_warm():
    # From cold a live Ask took over 10 minutes; with one warm instance about 4.
    words = flags_file()["AGENT_FLAGS"].split()
    assert words[words.index("--min-instances") + 1] == "1"
    assert words.count("--min-instances") == 1


def test_deploy_sh_moves_traffic_to_the_new_revisions_only_after_the_health_check():
    body = text(DEPLOY_SH)
    health = body.index("core/api/smoke.py")
    skip = body.index('if [ "$TRAFFIC" = no ]')
    assert "smoke.check_health" in body and "smoke.check_gate" in body
    assert health < body.index("smoke.check_health") < skip
    route = body.index("gcloud run services update-traffic")
    assert skip < route
    assert re.search(r'for service in f42-agent f42-api; do\s+gcloud run services update-traffic "\$service" '
                     r'--project "\$PROJECT" --region "\$REGION" --to-latest\s+done', body)
    assert body.count("update-traffic") == 1
    for service in SERVICES:
        assert body.index(f"gcloud run deploy {service}") < health
    assert "set -euo pipefail" in body


def test_deploy_sh_no_traffic_flag_skips_the_move():
    body = text(DEPLOY_SH)
    assert re.search(r"--no-traffic\) TRAFFIC=no ;;", body)
    assert re.search(r'if \[ "\$TRAFFIC" = no \]; then\n(.*\n)*?\s+exit 0\nfi', body)
    # An unknown option stops the script, so a mistyped --no-traffic never moves traffic.
    assert re.search(r'\*\) echo "deploy.sh: unknown option \$arg" >&2; exit 64 ;;', body)


def test_deploy_sh_is_staging_only():
    body = text(DEPLOY_SH)
    assert re.search(r"^PROJECT=ogilvy-trends-v2$", body, re.M)
    assert re.search(r"^REGION=us-central1$", body, re.M)
    assert set(re.findall(r"ogilvy-[a-z0-9-]+", body)) == {STAGING_PROJECT}
    assert "production" not in body.lower()
    commands = re.sub(r"\\\n\s*", " ", body)
    for line in commands.splitlines():
        if re.search(r"\bgcloud (run|builds) ", line):
            assert '--project "$PROJECT"' in line and '--region "$REGION"' in line, line
