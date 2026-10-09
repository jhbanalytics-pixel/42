"""Unit tests for core/setup/deploy_jobs.py and the jobs image files. No cloud access: fake gcloud, git and
Cloud Build session record every call, and module presence is faked, so nothing touches disk or Google Cloud."""
import re
from pathlib import Path

import pytest
import yaml

from core.collect import chain
from core.setup import deploy_jobs as dj

SETUP = Path(dj.__file__).resolve().parent
FILES = [SETUP / n for n in ("jobs.Dockerfile", "requirements-jobs.txt", "cloudbuild.jobs.yaml", "deploy_jobs.py",
                             "tests/test_deploy_jobs.py")]
SHA = "abc1234"
DIGEST = "sha256:" + "d" * 64
IMAGE = "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/jobs"
DEPLOYER = "projects/ogilvy-trends-v2/serviceAccounts/f42-deployer@ogilvy-trends-v2.iam.gserviceaccount.com"
BUCKET = "ogilvy-trends-v2-f42-media-staging"
OBJECT = f"build-source/jobs-{SHA}.tar.gz"
BUILDS = "https://cloudbuild.googleapis.com/v1/projects/ogilvy-trends-v2/locations/us-central1/builds"
BUILD_ID = "b1d-0001"
LOG_URL = "https://console.cloud.google.com/cloud-build/builds;region=us-central1/b1d-0001?project=1"
TOKEN = "ya29.fake-access-token-never-printed"
SECRET_FLAG = "--set-secrets=SOCIALCRAWL_OGILVY_API_KEY=SOCIALCRAWL_OGILVY_API_KEY:latest"
WITH_SECRET = {"f42-probe", "f42-collect", "f42-brief", "f42-reconcile", "f42-pulse"}
PRESENT = {"core.collect.probe", "core.collect.gdelt", "core.collect.job", "core.detect.job", "core.brief.job",
           "core.collect.reconcile", "core.setup.watchdog", "core.collect.drift"}
DEPLOYED = ["f42-probe", "f42-gdelt", "f42-collect", "f42-detect", "f42-brief", "f42-reconcile", "f42-watchdog",
            "f42-drift"]
UNDERSTAND = "core.understand.job"
CHAIN = ["f42-collect", "f42-understand", "f42-detect", "f42-brief"]
ENV_FLAG = "--update-env-vars=CHAIN_UNDERSTAND=1"
SCHEDULED = "f42-scheduled-asks"
SCHEDULED_MODULE = "core.api.scheduled"
SCHEDULED_SECRETS = "--set-secrets=SOCIALCRAWL_OGILVY_API_KEY=SOCIALCRAWL_OGILVY_API_KEY:latest"
SCHEDULED_ENV = "--update-env-vars=F42_DATA=bigquery,F42_PROJECT=ogilvy-trends-v2"


class FakeGcloud:
    def __init__(self, digest=DIGEST):
        self.digest = digest
        self.calls = []

    def run(self, args):
        self.calls.append(list(args))
        if args[:4] == ["artifacts", "docker", "images", "describe"]:
            return self.digest + "\n"
        return ""

    def deploys(self):
        return [c for c in self.calls if c[:3] == ["run", "jobs", "deploy"]]


class FakeResponse:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code
        self.text = str(body)

    def json(self):
        return self.body


class FakeCredentials:
    token = TOKEN


class FakeSession:
    """Cloud Build REST API: the create returns an operation, each get returns the next scripted status."""

    def __init__(self, statuses=("WORKING", "SUCCESS"), results=None, create_status=200):
        self.credentials = FakeCredentials()
        self.statuses = list(statuses)
        self.results = {"images": [{"name": f"{IMAGE}:{SHA}", "digest": DIGEST}]} if results is None else results
        self.create_status = create_status
        self.posts, self.gets = [], []

    def build(self, status):
        build = {"id": BUILD_ID, "status": status, "logUrl": LOG_URL}
        if status == "SUCCESS":
            build["results"] = self.results
        return build

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        if self.create_status != 200:
            return FakeResponse({"error": {"status": "PERMISSION_DENIED"}}, self.create_status)
        return FakeResponse({"name": "projects/p/locations/us-central1/operations/op1",
                             "metadata": {"build": self.build("QUEUED")}})

    def get(self, url, timeout=None):
        self.gets.append(url)
        return FakeResponse(self.build(self.statuses.pop(0)))


class FakeGit:
    def __init__(self, dirty=""):
        self.dirty = dirty
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:1] == ["rev-parse"]:
            return SHA + "\n"
        if args[:1] == ["status"]:
            return self.dirty
        return ""


def run(argv, present=PRESENT, dirty="", session=None):
    gcloud, git = FakeGcloud(), FakeGit(dirty)
    session = session or FakeSession()
    code = dj.main(argv, gcloud=gcloud, git=git, exists=lambda module: module in present, workdir="unused",
                   session=session, sleep=lambda seconds: None)
    return code, gcloud, git


def job(name):
    return next(j for j in dj.JOBS if j.name == name)


def flag(argv, name):
    hits = [a.split("=", 1)[1] for a in argv if a.startswith(name + "=")]
    assert len(hits) == 1, (name, argv)
    return hits[0]


def test_dry_run_calls_no_gcloud_and_prints_every_command(capsys):
    session = FakeSession()
    code, gcloud, _ = run([], session=session)
    out = capsys.readouterr().out
    assert code == 0
    assert gcloud.calls == []
    assert session.posts == [] and session.gets == []
    assert "gcloud builds submit" not in out
    assert f"gcloud storage cp {Path('unused') / 'source.tar.gz'} gs://{BUCKET}/{OBJECT} --no-clobber" in out
    assert f"POST {BUILDS}" in out
    assert f"{IMAGE}:{SHA}" in out
    assert "CLOUD_LOGGING_ONLY" in out and "f42-deployer" in out
    assert "every 15 s" in out
    assert "gcloud artifacts docker images describe" in out
    for name in DEPLOYED:
        assert f"gcloud run jobs deploy {name} " in out
    assert "Dry run" in out


def test_deploy_argv_pins_digest_identity_timeout_retries_parallelism_and_command():
    argv = dj.deploy_argv(job("f42-probe"), f"{IMAGE}@{DIGEST}")
    assert argv[:4] == ["run", "jobs", "deploy", "f42-probe"]
    assert flag(argv, "--image") == f"{IMAGE}@{DIGEST}"
    assert flag(argv, "--service-account") == "f42-collector@ogilvy-trends-v2.iam.gserviceaccount.com"
    assert flag(argv, "--task-timeout") == "1800s"
    assert flag(argv, "--max-retries") == "0"
    assert flag(argv, "--parallelism") == "1"
    assert flag(argv, "--tasks") == "1"
    assert flag(argv, "--command") == "python"
    assert flag(argv, "--args") == "-m,core.collect.probe"
    assert flag(argv, "--project") == "ogilvy-trends-v2"
    assert flag(argv, "--region") == "us-central1"


@pytest.mark.parametrize("name, identity, module, seconds", [
    ("f42-collect", "f42-collector", "core.collect.job", 3 * 3600),
    ("f42-detect", "f42-brief", "core.detect.job", 3600),
    ("f42-brief", "f42-brief", "core.brief.job", 3600),
])
def test_each_chain_job_carries_its_identity_module_and_chain_timeout(name, identity, module, seconds):
    argv = dj.deploy_argv(job(name), f"{IMAGE}@{DIGEST}")
    assert flag(argv, "--service-account") == f"{identity}@ogilvy-trends-v2.iam.gserviceaccount.com"
    assert flag(argv, "--args") == f"-m,{module}"
    assert flag(argv, "--task-timeout") == f"{seconds}s"


def test_reconcile_runs_as_the_collector_for_15_minutes_with_one_retry_and_the_key():
    argv = dj.deploy_argv(job("f42-reconcile"), f"{IMAGE}@{DIGEST}")
    assert flag(argv, "--service-account") == "f42-collector@ogilvy-trends-v2.iam.gserviceaccount.com"
    assert flag(argv, "--args") == "-m,core.collect.reconcile"
    assert flag(argv, "--task-timeout") == "900s"
    assert flag(argv, "--max-retries") == "1"
    assert SECRET_FLAG in argv


def test_chain_jobs_match_chain_names_and_timeouts():
    for stage, name in chain.JOBS.items():
        assert job(name).timeout == chain.TIMEOUTS[stage]


def test_secret_is_mounted_only_on_probe_collect_brief_reconcile_and_pulse():
    for j in dj.JOBS:
        argv = dj.deploy_argv(j, f"{IMAGE}@{DIGEST}")
        secrets = [a for a in argv if a.startswith("--set-secrets")]
        expected = ([SCHEDULED_SECRETS] if j.name == SCHEDULED else [DIGEST_SECRETS] if j.name == DIGEST_JOB
                    else [SECRET_FLAG] if j.name in WITH_SECRET else [])
        assert secrets == expected, j.name


def test_understand_is_skipped_while_its_module_is_missing(capsys):
    assert job("f42-understand").module == UNDERSTAND
    _, gcloud, _ = run(["--apply"])
    assert "f42-understand" not in {c[3] for c in gcloud.deploys()}
    assert "f42-understand: skipped, core/understand/job.py is not in the working tree" in capsys.readouterr().out


def test_web_and_agent_never_appear(capsys):
    _, gcloud, _ = run(["--build", "--apply"])
    out = capsys.readouterr().out
    names = {j.name for j in dj.JOBS}
    assert names == {"f42-probe", "f42-gdelt", "f42-collect", "f42-understand", "f42-detect", "f42-brief",
                     "f42-reconcile", "f42-watchdog", "f42-drift", SCHEDULED, "f42-pulse", "f42-learn",
                     "f42-breaking", DIGEST_JOB, "f42-calendar", "f42-gdelt-daily"}
    for word in ("f42-web", "f42-agent"):
        assert word not in names
        assert not any(word in a for c in gcloud.calls for a in c)
    assert "f42-web" not in out
    # f42-agent shows up only as the identity f42-scheduled-asks and f42-digest run as, never as a job or service.
    assert all(SCHEDULED in ln or DIGEST_JOB in ln for ln in out.splitlines() if "f42-agent" in ln)


def test_a_job_whose_module_is_missing_is_skipped_with_a_note(capsys):
    _, gcloud, _ = run(["--apply"], present={"core.collect.probe", "core.collect.job"})
    out = capsys.readouterr().out
    assert [c[3] for c in gcloud.deploys()] == ["f42-probe", "f42-collect"]
    assert "f42-detect: skipped, core/detect/job.py is not in the working tree" in out
    assert "f42-brief: skipped, core/brief/job.py is not in the working tree" in out
    assert "f42-reconcile: skipped, core/collect/reconcile.py is not in the working tree" in out


def test_dry_run_with_no_modules_yet_still_prints_every_deploy_command(capsys):
    _, gcloud, _ = run([], present=set())
    out = capsys.readouterr().out
    assert gcloud.calls == []
    for name in DEPLOYED + ["f42-understand"]:
        assert f"{name}: skipped" in out
        assert f"gcloud run jobs deploy {name} " in out


def test_module_path_is_the_file_python_m_runs():
    assert dj.module_file("core.collect.probe") == dj.ROOT / "core" / "collect" / "probe.py"


def test_no_argv_ever_deletes_executes_or_grants():
    _, gcloud, git = run(["--build", "--apply"])
    assert gcloud.calls
    for call in gcloud.calls + git.calls:
        for arg in call:
            low = arg.lower()
            for word in ("delete", "execute", "roles/", "iam-policy", "update-traffic"):
                assert word not in low, call
    for call in gcloud.calls:
        assert flag(call, "--project") == "ogilvy-trends-v2"
        if call[0] == "run":
            assert flag(call, "--region") == "us-central1"
        elif call[0] == "storage":
            assert call[3] == f"gs://{BUCKET}/{OBJECT}"
        else:
            assert call[4].startswith("us-central1-docker.pkg.dev/")


def test_upload_argv_copies_the_archive_into_the_media_bucket_without_overwriting():
    argv = dj.upload_argv("C:/tmp/source.tar.gz", SHA)
    assert argv[:4] == ["storage", "cp", "C:/tmp/source.tar.gz", f"gs://{BUCKET}/{OBJECT}"]
    assert "--no-clobber" in argv
    assert flag(argv, "--project") == "ogilvy-trends-v2"
    assert not any(a.startswith("--region") for a in argv)


def test_jobs_build_and_upload_read_the_shared_source_directory(tmp_path, monkeypatch):
    flags = tmp_path / "deploy_flags.env"
    flags.write_text('BUILD_SOURCE_STAGING_DIR="gs://fixture-bucket/release-source"\n', encoding="utf-8")
    monkeypatch.setattr(dj, "AGENT_FLAGS", flags)
    argv = dj.upload_argv("source.tar.gz", SHA)
    assert argv[3] == f"gs://fixture-bucket/release-source/jobs-{SHA}.tar.gz"
    assert dj.build_request(SHA)["source"] == {
        "storageSource": {"bucket": "fixture-bucket", "object": f"release-source/jobs-{SHA}.tar.gz"}}


def test_build_request_reads_the_config_and_substitutes_image_and_tag():
    body = dj.build_request(SHA)
    assert body["source"] == {"storageSource": {"bucket": BUCKET, "object": OBJECT}}
    assert body["serviceAccount"] == DEPLOYER
    assert body["options"]["logging"] == "CLOUD_LOGGING_ONLY"
    assert body["timeout"] == "1200s"
    assert body["images"] == [f"{IMAGE}:{SHA}"]
    args = body["steps"][0]["args"]
    assert body["steps"][0]["name"] == "gcr.io/cloud-builders/docker"
    assert args[args.index("-t") + 1] == f"{IMAGE}:{SHA}"
    assert args[args.index("-f") + 1] == "core/setup/jobs.Dockerfile"
    assert "substitutions" not in body
    assert "${" not in str(body)


def test_build_creates_the_build_through_the_api_and_polls_every_15_seconds_to_success(capsys):
    session, sleeps = FakeSession(statuses=("QUEUED", "WORKING", "WORKING", "SUCCESS")), []
    gcloud, git = FakeGcloud(), FakeGit()
    code = dj.main(["--build"], gcloud=gcloud, git=git, exists=lambda m: True, workdir="unused",
                   session=session, sleep=sleeps.append)
    out = capsys.readouterr().out
    assert code == 0
    assert [c[:2] for c in gcloud.calls] == [["storage", "cp"]]
    assert gcloud.calls[0][2] == str(Path("unused") / "source.tar.gz")
    assert [url for url, _ in session.posts] == [BUILDS]
    assert session.posts[0][1] == dj.build_request(SHA)
    assert session.gets == [f"{BUILDS}/{BUILD_ID}"] * 4
    assert sleeps == [15] * 4
    assert out.count("WORKING") == 1 and "SUCCESS" in out
    assert TOKEN not in out


@pytest.mark.parametrize("status", ["FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED", "EXPIRED"])
def test_a_failed_build_exits_non_zero_with_its_id_and_logs_and_deploys_nothing(status, capsys):
    session = FakeSession(statuses=("WORKING", status))
    gcloud = FakeGcloud()
    with pytest.raises(SystemExit) as stop:
        dj.main(["--build", "--apply"], gcloud=gcloud, git=FakeGit(), exists=lambda m: True, workdir="unused",
                session=session, sleep=lambda seconds: None)
    captured = capsys.readouterr()
    message = str(stop.value.code)
    assert stop.value.code not in (0, None)
    assert BUILD_ID in message and LOG_URL in message and status in message
    assert gcloud.deploys() == []
    assert TOKEN not in captured.out + captured.err + message


def test_a_refused_create_exits_non_zero_with_the_api_error_and_no_poll():
    session = FakeSession(create_status=403)
    with pytest.raises(SystemExit) as stop:
        dj.main(["--build"], gcloud=FakeGcloud(), git=FakeGit(), exists=lambda m: True, workdir="unused",
                session=session, sleep=lambda seconds: None)
    assert "403" in str(stop.value.code) and "PERMISSION_DENIED" in str(stop.value.code)
    assert session.gets == []


def test_build_then_apply_deploys_each_enabled_job_by_the_digest_from_the_build_results(capsys):
    code, gcloud, git = run(["--build", "--apply"])
    assert code == 0
    assert gcloud.calls[0][:2] == ["storage", "cp"]
    assert not any(c[:4] == ["artifacts", "docker", "images", "describe"] for c in gcloud.calls)
    assert DIGEST in capsys.readouterr().out
    assert [c[3] for c in gcloud.deploys()] == DEPLOYED
    for c in gcloud.deploys():
        assert flag(c, "--image") == f"{IMAGE}@{DIGEST}"
    archive = [c for c in git.calls if c[:1] == ["archive"]]
    assert archive == [["archive", "--format=tar.gz", f"--output={Path('unused') / 'source.tar.gz'}", SHA,
                        "core", "docs/full-42/reference/sc_routes.json"]]


def test_build_falls_back_to_describe_when_results_carry_no_digest():
    _, gcloud, _ = run(["--build", "--apply"], session=FakeSession(results={}))
    describes = [c for c in gcloud.calls if c[:4] == ["artifacts", "docker", "images", "describe"]]
    assert len(describes) == 1
    assert describes[0][4] == f"{IMAGE}:{SHA}"
    assert flag(describes[0], "--format") == "value(image_summary.digest)"
    for c in gcloud.deploys():
        assert flag(c, "--image") == f"{IMAGE}@{DIGEST}"


def test_apply_alone_deploys_the_image_already_built_for_head():
    session = FakeSession()
    _, gcloud, git = run(["--apply"], session=session)
    assert session.posts == []
    assert not any(c[:2] == ["builds", "submit"] for c in gcloud.calls)
    assert not any(c[:1] == ["archive"] for c in git.calls)
    assert gcloud.calls[0][4] == f"{IMAGE}:{SHA}"
    assert len(gcloud.deploys()) == len(DEPLOYED)


def test_apply_stops_when_no_digest_comes_back():
    gcloud, git = FakeGcloud(digest=""), FakeGit()
    with pytest.raises(SystemExit):
        dj.main(["--apply"], gcloud=gcloud, git=git, exists=lambda m: True, workdir="unused")
    assert gcloud.deploys() == []


def test_only_limits_to_one_job(capsys):
    _, gcloud, _ = run(["--apply", "--only", "f42-collect"])
    assert [c[3] for c in gcloud.deploys()] == ["f42-collect"]
    assert "f42-probe" not in capsys.readouterr().out


def test_only_rejects_an_unknown_job():
    with pytest.raises(SystemExit):
        run(["--only", "f42-web"])


@pytest.mark.parametrize("argv", [["--build"], ["--apply"], ["--build", "--apply"]])
def test_uncommitted_changes_under_core_stop_build_and_apply_before_any_gcloud_call(argv):
    gcloud, git, session = FakeGcloud(), FakeGit(dirty="?? core/collect/probe.py\n"), FakeSession()
    with pytest.raises(SystemExit):
        dj.main(argv, gcloud=gcloud, git=git, exists=lambda m: True, workdir="unused", session=session)
    assert gcloud.calls == []
    assert session.posts == []
    assert ["status", "--porcelain", "--", "core", "docs/full-42/reference/sc_routes.json"] in git.calls


def test_dry_run_on_a_dirty_tree_still_prints_the_plan_with_a_note(capsys):
    code, gcloud, _ = run([], dirty=" M core/collect/chain.py\n")
    assert code == 0 and gcloud.calls == []
    assert "uncommitted changes" in capsys.readouterr().out


def dockerfile_lines():
    """The Dockerfile's instructions, comments dropped and backslash-continued lines joined with single spaces."""
    text = (SETUP / "jobs.Dockerfile").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    joined = []
    for ln in lines:
        if joined and joined[-1].endswith("\\"):
            joined[-1] = joined[-1][:-1].rstrip() + " " + ln
        else:
            joined.append(ln)
    return [" ".join(ln.split()) for ln in joined]


def test_dockerfile_builds_one_non_root_image_with_the_routes_file_and_no_secrets():
    lines = dockerfile_lines()
    assert lines[0] == "FROM python:3.13-slim"
    assert "COPY core/ /app/core/" in lines
    assert "COPY docs/full-42/reference/sc_routes.json /app/docs/full-42/reference/sc_routes.json" in lines
    assert any(ln.startswith("RUN pip install") and "requirements-jobs.txt" in ln for ln in lines)
    users = [ln for ln in lines if ln.startswith("USER ")]
    assert users and users[-1] not in ("USER root", "USER 0")
    assert not re.search(r"SOCIALCRAWL|API_KEY|TOKEN|PASSWORD|SECRET", "\n".join(lines), re.I | re.M)
    assert [ln for ln in lines if ln.startswith("ARG ")] == ["ARG GIT_SHA=unknown"]  # the commit sha is the one build argument


def test_dockerfile_installs_the_understand_cluster_requirements_and_bertopic_with_no_dependencies():
    # f42-understand runs on this image and its cluster step imports BERTopic, UMAP, HDBSCAN and scikit-learn
    # (core/understand/cluster.py fit_topics). Its requirements file names them and says BERTopic goes in on its own
    # with --no-deps after that file, at the version the comment pins.
    lines = dockerfile_lines()
    understand = (SETUP.parent / "understand" / "requirements.txt").read_text(encoding="utf-8")
    bertopic = re.search(r"pip install --no-deps (bertopic==[0-9][0-9A-Za-z.]*)", understand).group(1)
    assert {"umap-learn", "hdbscan", "scikit-learn"} <= set(pins(SETUP.parent / "understand" / "requirements.txt"))
    copied = [ln.split()[2] for ln in lines if ln.startswith("COPY core/understand/requirements.txt ")]
    assert copied, "jobs.Dockerfile does not copy core/understand/requirements.txt"
    commands = [c.strip() for ln in lines if ln.startswith("RUN ") for c in ln[4:].split("&&")]
    pip = [c.split() for c in commands if c.startswith("pip install")]
    reqs = [i for i, argv in enumerate(pip) if copied[0] in argv and "/tmp/requirements-jobs.txt" in argv]
    assert reqs, "jobs.Dockerfile does not install the understand requirements with the jobs requirements"
    after = [argv for argv in pip[reqs[0] + 1:] if bertopic in argv]
    assert after and "--no-deps" in after[0], f"jobs.Dockerfile does not pip install --no-deps {bertopic} after them"


def test_requirements_are_pinned():
    reqs = [ln.strip() for ln in (SETUP / "requirements-jobs.txt").read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]
    names = {r.split("==")[0].lower() for r in reqs}
    assert {"google-cloud-bigquery", "google-auth", "requests", "pyyaml"} <= names
    assert all(re.fullmatch(r"[A-Za-z0-9_.\-]+==[0-9][0-9A-Za-z.]*", r) for r in reqs), reqs


def pins(path):
    return {ln.split("==")[0].strip().lower(): ln.split("==")[1].strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if "==" in ln and not ln.strip().startswith("#")}


def test_requirements_carry_what_scheduled_asks_imports_at_the_agent_image_pins():
    # f42-scheduled-asks runs core.api.scheduled on this one jobs image: it imports core.api.agent_app (fastapi,
    # starlette) and Ask (google-genai, sqlglot), so the jobs image carries them at the agent image's pins.
    jobs = pins(SETUP / "requirements-jobs.txt")
    agent = pins(SETUP.parent / "agent" / "requirements.txt")
    assert {"fastapi", "starlette", "google-genai", "sqlglot"} <= set(jobs)
    for name in ("google-genai", "sqlglot"):
        assert jobs[name] == agent[name], name
    shared = set(jobs) & set(agent)
    assert {name: jobs[name] for name in shared} == {name: agent[name] for name in shared}


def test_every_extra_secret_a_job_mounts_is_declared_in_bootstrap_for_that_job_identity_only():
    from core.setup import bootstrap as bs
    # GMAIL_APP_PASSWORD is Albert's to create, and bootstrap plans its grant only once it exists, so the state has it.
    state = bs.State(account="a", number="1", apis=set(), accounts=set(), datasets={}, tables=set(), bucket=True,
                     secrets={bs.GMAIL_APP_PASSWORD}, connection=None, pool=False, provider=False)
    bindings = bs.desired_bindings(state, None)
    mounted = {(name, job.identity) for job in dj.JOBS for name in job.secrets}
    # Deliberate change on 3 October 2026: f42-scheduled-asks no longer mounts CHANNEL_WEBHOOK_URL (the channel post
    # is not wired), so no job mounts it.
    assert ("GMAIL_APP_PASSWORD", "f42-agent") in mounted
    assert "CHANNEL_WEBHOOK_URL" not in {name for name, _ in mounted}
    for name, identity in mounted:
        on_secret = [(b.member, b.role) for b in bindings if b.kind == "secret" and b.name == name]
        assert on_secret == [("serviceAccount:" + dj.sa_email(identity), "roles/secretmanager.secretAccessor")], name
        assert name in (bs.UI_PASSCODE, bs.CHANNEL_WEBHOOK_URL, bs.GMAIL_APP_PASSWORD), name


def test_requirements_carry_google_genai_for_gemini_on_vertex_with_a_google_auth_it_resolves_with():
    reqs = {ln.split("==")[0].lower(): ln.split("==")[1] for ln in
            (SETUP / "requirements-jobs.txt").read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")}
    assert reqs["google-genai"] == "2.25.0" and reqs["google-auth"] == "2.59.0"
    assert reqs["jsonschema"] == "4.25.1"


def test_cloudbuild_config_builds_the_dockerfile_logs_to_cloud_logging_and_pushes_the_tag():
    cfg = yaml.safe_load((SETUP / "cloudbuild.jobs.yaml").read_text(encoding="utf-8"))
    args = cfg["steps"][0]["args"]
    assert args[args.index("-f") + 1] == "core/setup/jobs.Dockerfile"
    assert "${_IMAGE}:${_TAG}" in args
    assert cfg["images"] == ["${_IMAGE}:${_TAG}"]
    assert cfg["options"]["logging"] == "CLOUD_LOGGING_ONLY"
    assert "_TAG" not in cfg.get("substitutions", {})


SMOKE_ROLES = ("collector", "enricher", "agent", "brief", "web")
SMOKE_WITH_SECRET = {"collector", "enricher", "agent", "brief"}


def smoke_job(role):
    return next(j for j in dj.SMOKE_JOBS if j.name == f"f42-smoke-{role}")


def test_one_smoke_job_per_runtime_identity_except_scheduler_which_the_builder_proves():
    assert [j.name for j in dj.SMOKE_JOBS] == [f"f42-smoke-{r}" for r in SMOKE_ROLES]
    assert not any("scheduler" in j.name or j.identity == "f42-scheduler" for j in dj.SMOKE_JOBS)


@pytest.mark.parametrize("role", SMOKE_ROLES)
def test_smoke_deploy_argv_runs_the_smoke_module_as_its_identity_once_for_ten_minutes(role):
    argv = dj.deploy_argv(smoke_job(role), f"{IMAGE}@{DIGEST}")
    assert argv[:4] == ["run", "jobs", "deploy", f"f42-smoke-{role}"]
    assert flag(argv, "--image") == f"{IMAGE}@{DIGEST}"
    assert flag(argv, "--service-account") == f"f42-{role}@ogilvy-trends-v2.iam.gserviceaccount.com"
    assert flag(argv, "--command") == "python"
    assert flag(argv, "--args") == f"-m,core.setup.smoke,--role,{role}"
    assert flag(argv, "--task-timeout") == "600s"
    assert flag(argv, "--max-retries") == "0"
    assert flag(argv, "--parallelism") == "1"
    assert flag(argv, "--tasks") == "1"
    assert flag(argv, "--project") == "ogilvy-trends-v2"
    assert flag(argv, "--region") == "us-central1"
    secrets = [a for a in argv if a.startswith("--set-secrets")]
    assert secrets == ([SECRET_FLAG] if role in SMOKE_WITH_SECRET else [])


def test_probe_watchdog_and_pulse_have_no_retry_and_other_jobs_keep_one():
    for j in dj.JOBS:
        assert j.retries == (0 if j.name in ("f42-probe", "f42-understand", "f42-watchdog", "f42-pulse") else 1), j.name


@pytest.mark.parametrize("name, identity, module, seconds, retries", [
    ("f42-gdelt", "f42-collector", "core.collect.gdelt", 900, 1),
    ("f42-understand", "f42-enricher", "core.understand.job", 7200, 0),
    ("f42-watchdog", "f42-brief", "core.setup.watchdog", 300, 0),
    ("f42-drift", "f42-collector", "core.collect.drift", 600, 1),
    ("f42-breaking", "f42-brief", "core.detect.breaking", 900, 1),
])
def test_each_new_job_carries_its_identity_module_timeout_retries_and_no_key(name, identity, module, seconds, retries):
    argv = dj.deploy_argv(job(name), f"{IMAGE}@{DIGEST}")
    assert argv[:4] == ["run", "jobs", "deploy", name]
    assert flag(argv, "--service-account") == f"{identity}@ogilvy-trends-v2.iam.gserviceaccount.com"
    assert flag(argv, "--args") == f"-m,{module}"
    assert flag(argv, "--task-timeout") == f"{seconds}s"
    assert flag(argv, "--max-retries") == str(retries)
    assert flag(argv, "--tasks") == "1" and flag(argv, "--parallelism") == "1"
    assert not any(a.startswith("--set-secrets") for a in argv)
    assert not any(a.startswith(("--set-env-vars", "--update-env-vars")) for a in argv)


def test_watchdog_job_is_the_one_watchdog_py_declares():
    from core.setup import watchdog
    j = job("f42-watchdog")
    assert (j.name, j.module, j.identity, j.timeout, j.secret, j.retries) == tuple(
        watchdog.JOB[k] for k in ("name", "module", "identity", "timeout", "secret", "retries"))


@pytest.mark.parametrize("understand", [True, False])
def test_drift_is_outside_the_chain_and_never_carries_or_drops_chain_understand(understand):
    j = job("f42-drift")
    assert j.name not in chain.JOBS.values()
    assert (j.identity, j.secret, j.retries, j.timeout.total_seconds()) == ("f42-collector", False, 1, 600)
    argv = dj.deploy_argv(j, f"{IMAGE}@{DIGEST}", understand=understand)
    assert not any("CHAIN_UNDERSTAND" in a for a in argv)
    assert not any(a.startswith(("--set-secrets", "--set-env-vars", "--update-env-vars", "--remove-env-vars"))
                   for a in argv)


def test_drift_with_understand_in_the_chain_deploys_without_the_switch():
    _, gcloud, _ = run(["--apply"], present=PRESENT | {UNDERSTAND})
    assert envs(gcloud)["f42-drift"] == [] and removes(gcloud)["f42-drift"] == []


LEARN = "f42-learn"


def test_learn_runs_the_learn_module_as_the_detect_identity_for_thirty_minutes_with_no_key():
    j = job(LEARN)
    assert j.module == "core.detect.learn"
    assert j.identity == job(chain.JOBS["detect"]).identity
    assert (j.secret, j.secrets, j.env, j.args, j.retries, j.timeout.total_seconds()) == (False, (), (), (), 1, 1800)
    argv = dj.deploy_argv(j, f"{IMAGE}@{DIGEST}")
    assert argv[:4] == ["run", "jobs", "deploy", LEARN]
    assert flag(argv, "--service-account") == f"{j.identity}@ogilvy-trends-v2.iam.gserviceaccount.com"
    assert flag(argv, "--args") == "-m,core.detect.learn"
    assert flag(argv, "--task-timeout") == "1800s"


@pytest.mark.parametrize("understand", [True, False])
@pytest.mark.parametrize("provider", [None, "gemini"])
def test_learn_is_outside_the_chain_and_the_model_jobs(understand, provider):
    assert LEARN not in chain.JOBS.values()
    assert LEARN not in dj.MODEL_JOBS
    argv = dj.deploy_argv(job(LEARN), f"{IMAGE}@{DIGEST}", understand=understand, provider=provider)
    assert not any("CHAIN_UNDERSTAND" in a or "MODEL_PROVIDER" in a for a in argv)
    assert not any(a.startswith(("--set-secrets", "--set-env-vars", "--update-env-vars", "--remove-env-vars"))
                   for a in argv)


def test_dry_run_shows_learn(capsys):
    code, gcloud, _ = run([], present=PRESENT | {"core.detect.learn"})
    out = capsys.readouterr().out
    assert code == 0 and gcloud.calls == []
    assert "f42-learn: python -m core.detect.learn as f42-brief, timeout 0:30:00, 1 task, max retries 1\n" in out
    assert "gcloud run jobs deploy f42-learn " in out


CALENDAR = "f42-calendar"


def test_calendar_refresh_runs_the_loader_with_apply_as_the_drift_identity_for_ten_minutes_with_no_key():
    j = job(CALENDAR)
    assert j.module == "core.collect.calendar"
    assert j.identity == job("f42-drift").identity == "f42-collector"
    assert (j.secret, j.secrets, j.env, j.args, j.retries, j.timeout.total_seconds()) == (False, (), (), ("--apply",),
                                                                                         1, 600)
    argv = dj.deploy_argv(j, f"{IMAGE}@{DIGEST}")
    assert argv[:4] == ["run", "jobs", "deploy", CALENDAR]
    assert flag(argv, "--service-account") == "f42-collector@ogilvy-trends-v2.iam.gserviceaccount.com"
    assert flag(argv, "--args") == "-m,core.collect.calendar,--apply"
    assert flag(argv, "--task-timeout") == "600s"
    assert flag(argv, "--max-retries") == "1"
    assert not any(a.startswith(("--set-secrets", "--set-env-vars", "--update-env-vars", "--remove-env-vars"))
                   for a in argv)


@pytest.mark.parametrize("understand", [True, False])
@pytest.mark.parametrize("provider", [None, "gemini"])
def test_calendar_refresh_is_outside_the_chain_and_the_model_jobs(understand, provider):
    assert CALENDAR not in chain.JOBS.values() and CALENDAR not in dj.MODEL_JOBS
    argv = dj.deploy_argv(job(CALENDAR), f"{IMAGE}@{DIGEST}", understand=understand, provider=provider)
    assert not any("CHAIN_UNDERSTAND" in a or "MODEL_PROVIDER" in a for a in argv)


def test_calendar_refresh_deploys_before_the_jobs_that_mount_extra_secrets():
    # A missing GMAIL_APP_PASSWORD version fails only the jobs after the calendar refresh.
    names = [j.name for j in dj.JOBS]
    extra = [j.name for j in dj.JOBS if j.secrets]
    assert extra == [DIGEST_JOB]
    assert all(names.index(CALENDAR) < names.index(name) for name in extra)


def test_dry_run_shows_the_calendar_refresh(capsys):
    code, gcloud, _ = run([], present=PRESENT | {"core.collect.calendar"})
    out = capsys.readouterr().out
    assert code == 0 and gcloud.calls == []
    assert ("f42-calendar: python -m core.collect.calendar --apply as f42-collector, timeout 0:10:00, 1 task, "
            "max retries 1\n") in out
    assert "gcloud run jobs deploy f42-calendar " in out


def test_gdelt_deploys_before_collect():
    names = [j.name for j in dj.JOBS]
    assert names.index("f42-gdelt") < names.index("f42-collect")


GDELT_DAILY = "f42-gdelt-daily"


def test_gdelt_daily_runs_the_daily_aggregate_with_backfill_as_the_collector_for_thirty_minutes_with_no_key():
    from core.collect import gdelt_daily
    j = job(GDELT_DAILY)
    assert j.module == gdelt_daily.__name__ == "core.collect.gdelt_daily"
    assert j.identity == job("f42-gdelt").identity == "f42-collector"
    assert (j.secret, j.secrets, j.env, j.args, j.retries, j.timeout.total_seconds()) == (
        False, (), (), ("--apply", "--backfill"), 1, 1800)
    assert j.name not in chain.JOBS.values()
    argv = dj.deploy_argv(j, f"{IMAGE}@{DIGEST}")
    assert argv[:4] == ["run", "jobs", "deploy", GDELT_DAILY]
    assert flag(argv, "--image") == f"{IMAGE}@{DIGEST}"
    assert flag(argv, "--service-account") == "f42-collector@ogilvy-trends-v2.iam.gserviceaccount.com"
    assert flag(argv, "--command") == "python"
    assert flag(argv, "--args") == "-m,core.collect.gdelt_daily,--apply,--backfill"
    assert flag(argv, "--task-timeout") == "1800s"
    assert flag(argv, "--max-retries") == "1"
    assert flag(argv, "--tasks") == "1" and flag(argv, "--parallelism") == "1"
    assert flag(argv, "--project") == "ogilvy-trends-v2" and flag(argv, "--region") == "us-central1"
    assert not any(a.startswith(("--set-secrets", "--set-env-vars", "--update-env-vars", "--remove-env-vars",
                                 "--memory", "--cpu")) for a in argv)


@pytest.mark.parametrize("understand", [True, False])
def test_gdelt_daily_never_carries_or_drops_chain_understand(understand):
    argv = dj.deploy_argv(job(GDELT_DAILY), f"{IMAGE}@{DIGEST}", understand=understand)
    assert not any("CHAIN_UNDERSTAND" in a for a in argv)


def test_gdelt_daily_deploys_on_apply_once_its_module_is_in_the_tree(capsys):
    _, gcloud, _ = run(["--apply"], present=PRESENT | {"core.collect.gdelt_daily"})
    assert GDELT_DAILY in [c[3] for c in gcloud.deploys()]
    out = capsys.readouterr().out
    assert ("f42-gdelt-daily: python -m core.collect.gdelt_daily --apply --backfill as f42-collector, "
            "timeout 0:30:00, 1 task, max retries 1\n") in out


def test_gdelt_daily_alone_with_only(capsys):
    _, gcloud, _ = run(["--apply", "--only", GDELT_DAILY], present=PRESENT | {"core.collect.gdelt_daily"})
    assert [c[3] for c in gcloud.deploys()] == [GDELT_DAILY]


def test_gdelt_daily_module_takes_the_deployed_args_and_defaults_to_yesterday_utc():
    from core.collect import gdelt_daily
    assert dj.module_present("core.collect.gdelt_daily")
    src = dj.module_file("core.collect.gdelt_daily").read_text(encoding="utf-8")
    for arg in job(GDELT_DAILY).args:
        assert f'"{arg}"' in src
    assert gdelt_daily.SETTLED.hour == 1  # the UTC hour the news day settles: Scheduler must start it after this


def envs(gcloud):
    return {c[3]: [a for a in c if a.startswith("--update-env-vars")] for c in gcloud.deploys()}


def test_with_the_understand_module_present_all_four_chain_jobs_carry_chain_understand_together(capsys):
    _, gcloud, _ = run(["--apply"], present=PRESENT | {UNDERSTAND})
    deployed = envs(gcloud)
    assert set(CHAIN) <= set(deployed)
    for name, env in deployed.items():
        assert env == ([ENV_FLAG] if name in CHAIN else []), name
    out = capsys.readouterr().out
    assert "CHAIN_UNDERSTAND=1" in out and "f42-understand: python -m core.understand.job as f42-enricher" in out


def test_without_the_understand_module_no_job_carries_chain_understand(capsys):
    _, gcloud, _ = run(["--apply"])
    assert all(env == [] for env in envs(gcloud).values())
    assert "CHAIN_UNDERSTAND=1" not in capsys.readouterr().out


def test_the_switch_off_keeps_understand_out_of_the_chain_even_with_its_module(monkeypatch):
    monkeypatch.setattr(dj, "CHAIN_UNDERSTAND", False)
    _, gcloud, _ = run(["--apply"], present=PRESENT | {UNDERSTAND})
    assert all(env == [] for env in envs(gcloud).values())


REMOVE_FLAG = "--remove-env-vars=CHAIN_UNDERSTAND"


def removes(gcloud):
    return {c[3]: [a for a in c if a.startswith("--remove-env-vars")] for c in gcloud.deploys()}


def test_with_understand_out_of_the_chain_the_chain_jobs_drop_the_switch_and_no_other_job_is_touched():
    for j in dj.JOBS:
        argv = dj.deploy_argv(j, f"{IMAGE}@{DIGEST}", understand=False)
        removed = [a for a in argv if a.startswith("--remove-env-vars")]
        assert removed == ([REMOVE_FLAG] if j.name in CHAIN else []), j.name
        assert not any(a.startswith("--set-env-vars") for a in argv), j.name
        updated = [a for a in argv if a.startswith("--update-env-vars")]
        assert updated == ([SCHEDULED_ENV] if j.name == SCHEDULED else [DIGEST_ENV] if j.name == DIGEST_JOB
                           else []), j.name


def test_with_understand_in_the_chain_no_job_removes_the_switch():
    for j in dj.JOBS:
        argv = dj.deploy_argv(j, f"{IMAGE}@{DIGEST}", understand=True)
        assert not any(a.startswith("--remove-env-vars") for a in argv), j.name


def test_switch_off_apply_removes_chain_understand_from_every_deployed_chain_job(monkeypatch):
    monkeypatch.setattr(dj, "CHAIN_UNDERSTAND", False)
    _, gcloud, _ = run(["--apply"], present=PRESENT | {UNDERSTAND})
    deployed = removes(gcloud)
    assert set(CHAIN) <= set(deployed)
    for name, flags in deployed.items():
        assert flags == ([REMOVE_FLAG] if name in CHAIN else []), name


def test_module_missing_apply_removes_chain_understand_from_the_deployed_chain_jobs():
    _, gcloud, _ = run(["--apply"])
    deployed = removes(gcloud)
    for name, flags in deployed.items():
        assert flags == ([REMOVE_FLAG] if name in CHAIN else []), name


@pytest.mark.parametrize("name", CHAIN)
@pytest.mark.parametrize("extra", [[], ["--apply"], ["--build", "--apply"]])
def test_only_one_chain_job_with_understand_in_the_chain_refuses_in_one_line_before_any_call(name, extra):
    gcloud, git, session = FakeGcloud(), FakeGit(), FakeSession()
    present = PRESENT | {UNDERSTAND}
    with pytest.raises(SystemExit) as stop:
        dj.main([*extra, "--only", name], gcloud=gcloud, git=git, exists=lambda m: m in present, workdir="unused",
                session=session, sleep=lambda seconds: None)
    message = str(stop.value.code)
    assert stop.value.code not in (0, None)
    assert "\n" not in message and name in message
    assert all(n in message for n in CHAIN)
    assert gcloud.calls == [] and session.posts == []


def test_only_one_chain_job_is_allowed_while_understand_is_out_of_the_chain():
    _, gcloud, _ = run(["--apply", "--only", "f42-detect"])
    assert removes(gcloud) == {"f42-detect": [REMOVE_FLAG]}


def test_only_a_non_chain_job_is_allowed_with_understand_in_the_chain():
    code, gcloud, _ = run(["--apply", "--only", "f42-probe"], present=PRESENT | {UNDERSTAND})
    assert code == 0
    assert [c[3] for c in gcloud.deploys()] == ["f42-probe"]
    assert envs(gcloud) == {"f42-probe": []} and removes(gcloud) == {"f42-probe": []}


MODEL_JOBS = {"f42-brief", "f42-understand"}


def env_flags(argv):
    return [a for a in argv if a.startswith(("--set-env-vars", "--update-env-vars", "--remove-env-vars"))]


def model_run(argv, understand, monkeypatch):
    # Every module present, so f42-understand deploys too; the switch decides whether understand is in the chain.
    monkeypatch.setattr(dj, "CHAIN_UNDERSTAND", understand)
    _, gcloud, _ = run(["--apply", *argv], present=PRESENT | {UNDERSTAND})
    return {c[3]: env_flags(c) for c in gcloud.deploys()}


@pytest.mark.parametrize("understand", [True, False])
def test_without_model_provider_no_job_carries_or_drops_model_provider_and_the_argv_is_unchanged(understand):
    for j in dj.JOBS + dj.SMOKE_JOBS:
        argv = dj.deploy_argv(j, f"{IMAGE}@{DIGEST}", understand)
        assert argv == dj.deploy_argv(j, f"{IMAGE}@{DIGEST}", understand, provider=None), j.name
        assert not any("MODEL_PROVIDER" in a for a in argv), j.name
        assert all(a == (SCHEDULED_ENV if j.name == SCHEDULED else DIGEST_ENV if j.name == DIGEST_JOB else ENV_FLAG)
                   for a in argv if a.startswith("--update-env-vars")), j.name
    brief = dj.deploy_argv(job("f42-brief"), f"{IMAGE}@{DIGEST}", understand)
    assert brief[-2:] == [SECRET_FLAG, ENV_FLAG if understand else REMOVE_FLAG]


@pytest.mark.parametrize("understand", [True, False])
def test_without_model_provider_apply_touches_no_model_provider(understand, monkeypatch):
    for name, flags in model_run([], understand, monkeypatch).items():
        assert not any("MODEL_PROVIDER" in f for f in flags), name


@pytest.mark.parametrize("provider", ["gemini"])
def test_model_provider_with_understand_in_the_chain_joins_the_one_update_list_on_brief_and_understand_only(
        provider, monkeypatch):
    deployed = model_run(["--model-provider", provider], True, monkeypatch)
    assert set(CHAIN) <= set(deployed)
    for name, flags in deployed.items():
        if name in MODEL_JOBS:
            assert flags == [f"--update-env-vars=CHAIN_UNDERSTAND=1,MODEL_PROVIDER={provider}"], name
        elif name in CHAIN:
            assert flags == [ENV_FLAG], name
        else:
            assert flags == [], name


@pytest.mark.parametrize("provider", ["gemini"])
def test_model_provider_with_understand_out_of_the_chain_updates_brief_and_understand_and_still_drops_the_switch(
        provider, monkeypatch):
    deployed = model_run(["--model-provider", provider], False, monkeypatch)
    assert set(CHAIN) <= set(deployed)
    for name, flags in deployed.items():
        if name in MODEL_JOBS:
            assert flags == [REMOVE_FLAG, f"--update-env-vars=MODEL_PROVIDER={provider}"], name
        elif name in CHAIN:
            assert flags == [REMOVE_FLAG], name
        else:
            assert flags == [], name


@pytest.mark.parametrize("understand", [True, False])
@pytest.mark.parametrize("provider", [None, "gemini"])
def test_no_argv_ever_sets_or_clears_env_vars_so_a_deploy_keeps_every_variable_it_does_not_name(understand, provider):
    # --set-env-vars and --clear-env-vars drop every variable not listed (MODEL_PROVIDER, GEMINI_*, cap overrides).
    for j in dj.JOBS + dj.SMOKE_JOBS:
        argv = dj.deploy_argv(j, f"{IMAGE}@{DIGEST}", understand, provider)
        assert not any(a.startswith(("--set-env-vars", "--clear-env-vars", "--env-vars-file")) for a in argv), j.name
        kinds = [f.split("=", 1)[0] for f in env_flags(argv)]
        assert len(kinds) == len(set(kinds)), j.name


@pytest.mark.parametrize("understand", [True, False])
@pytest.mark.parametrize("provider", [None, "gemini"])
def test_no_apply_ever_sends_set_env_vars(understand, provider, monkeypatch):
    extra = ["--model-provider", provider] if provider else []
    for name, flags in model_run(extra, understand, monkeypatch).items():
        assert not any(f.startswith("--set-env-vars") for f in flags), name


def test_model_provider_help_names_update_env_vars_and_the_gemini_smoke_execute(capsys):
    with pytest.raises(SystemExit):
        run(["-h"])
    out = capsys.readouterr().out
    assert "--update-env-vars=MODEL_PROVIDER=gemini" in out and "smoke" in out


def test_model_provider_never_reaches_the_smoke_jobs(monkeypatch):
    _, gcloud, _ = run(["--smoke", "--apply", "--model-provider", "gemini"], present=PRESENT | {"core.setup.smoke"})
    assert gcloud.deploys() and not any("MODEL_PROVIDER" in a for c in gcloud.deploys() for a in c)


def test_model_provider_dry_run_names_the_env_on_brief_understand_and_scheduled_asks_only(capsys):
    code, gcloud, _ = run(["--model-provider", "gemini"], present=PRESENT | {UNDERSTAND})
    out = capsys.readouterr().out
    assert code == 0 and gcloud.calls == []
    named = [ln.split(":")[0].strip() for ln in out.splitlines() if "MODEL_PROVIDER=gemini" in ln
             and not ln.lstrip().startswith("gcloud")]
    deploys = [ln.split()[4] for ln in out.splitlines() if "MODEL_PROVIDER=gemini" in ln
               and ln.lstrip().startswith("gcloud run jobs deploy")]
    # f42-scheduled-asks is skipped here (its module is not present), so only its deploy line names the env.
    assert sorted(named) == sorted(MODEL_JOBS) and sorted(deploys) == sorted(MODEL_JOBS | {SCHEDULED})


def test_model_provider_accepts_only_gemini():
    with pytest.raises(SystemExit):
        run(["--model-provider", "openai"])


def test_the_understand_default_follows_the_module_file_in_the_tree():
    assert dj.understand_in_chain(lambda m: m == UNDERSTAND) is True
    assert dj.understand_in_chain(lambda m: False) is False


def test_default_dry_run_lists_the_new_jobs(capsys):
    code, gcloud, _ = run([])
    out = capsys.readouterr().out
    assert code == 0 and gcloud.calls == []
    assert "f42-gdelt: python -m core.collect.gdelt as f42-collector, timeout 0:15:00, 1 task, max retries 1\n" in out
    assert "f42-watchdog: python -m core.setup.watchdog as f42-brief, timeout 0:05:00, 1 task, max retries 0\n" in out
    assert "f42-drift: python -m core.collect.drift as f42-collector, timeout 0:10:00, 1 task, max retries 1\n" in out
    assert "f42-understand: skipped, core/understand/job.py is not in the working tree" in out
    assert "gcloud run jobs deploy f42-understand " in out


def test_scheduled_asks_runs_l4s_module_live_as_f42_agent_for_an_hour_with_one_retry():
    j = job(SCHEDULED)
    assert (j.module, j.identity, j.args, j.retries, j.timeout.total_seconds()) == (
        SCHEDULED_MODULE, "f42-agent", ("--live",), 1, 3600)
    argv = dj.deploy_argv(j, f"{IMAGE}@{DIGEST}")
    assert argv[:4] == ["run", "jobs", "deploy", SCHEDULED]
    assert flag(argv, "--service-account") == "f42-agent@ogilvy-trends-v2.iam.gserviceaccount.com"
    assert flag(argv, "--args") == "-m,core.api.scheduled,--live"
    assert flag(argv, "--task-timeout") == "3600s"
    assert flag(argv, "--max-retries") == "1"
    assert flag(argv, "--tasks") == "1" and flag(argv, "--parallelism") == "1"


def test_scheduled_asks_mounts_only_the_socialcrawl_key_and_no_channel_webhook():
    # Deliberate change on 3 October 2026: CHANNEL_WEBHOOK_URL was mounted beside the key, but core/api/scheduled.py
    # never reads it and the channel post (contract.md 14.3) is not wired.
    assert job(SCHEDULED).secret is True and job(SCHEDULED).secrets == ()
    argv = dj.deploy_argv(job(SCHEDULED), f"{IMAGE}@{DIGEST}")
    assert [a for a in argv if a.startswith("--set-secrets")] == [SCHEDULED_SECRETS]
    assert not any("CHANNEL_WEBHOOK_URL" in a or "webhook" in a.lower() for a in argv)
    assert not any("http" in a.lower() and "googleapis" not in a for a in argv)


@pytest.mark.parametrize("understand", [True, False])
@pytest.mark.parametrize("provider", [None, "gemini"])
def test_scheduled_asks_is_outside_the_chain_and_carries_model_provider_only_from_the_flag(understand, provider):
    # Deliberate change on 3 October 2026: it never carried MODEL_PROVIDER; it now asks on the Ask service's model.
    assert SCHEDULED not in chain.JOBS.values() and SCHEDULED not in dj.MODEL_JOBS and SCHEDULED in dj.ASK_JOBS
    argv = dj.deploy_argv(job(SCHEDULED), f"{IMAGE}@{DIGEST}", understand, provider)
    assert not any("CHAIN_UNDERSTAND" in a for a in argv)
    model = {None: "", "gemini": ",MODEL_PROVIDER=gemini,GEMINI_MODEL=gemini-3.8-flash"}[provider]
    assert env_flags(argv) == [SCHEDULED_ENV + model]


def test_scheduled_asks_takes_gemini_model_from_the_agent_service_flags():
    flags = (SETUP.parent / "api" / "deploy_flags.env").read_text(encoding="utf-8")
    [agent_env] = [ln for ln in flags.splitlines() if ln.startswith("AGENT_ENV=")]
    assert "MODEL_PROVIDER=gemini" in agent_env and "GEMINI_MODEL=gemini-3.8-flash" in agent_env
    assert dj.ask_service_gemini_model() == "gemini-3.8-flash"


def test_scheduled_asks_without_gemini_model_in_the_flags_gets_model_provider_only(tmp_path, monkeypatch):
    flags = tmp_path / "deploy_flags.env"
    flags.write_text('AGENT_ENV="APP_MODULE=x,MODEL_PROVIDER=gemini"\n', encoding="utf-8")
    monkeypatch.setattr(dj, "AGENT_FLAGS", flags)
    assert dj.ask_service_gemini_model() is None
    assert dj.model_env(job(SCHEDULED), "gemini") == ["MODEL_PROVIDER=gemini"]


def test_scheduled_asks_stays_the_last_job_deployed():
    # It used to be last so a missing webhook version failed only it; it mounts no webhook now, the order is kept.
    assert [j.name for j in dj.JOBS][-1] == SCHEDULED


def test_scheduled_asks_without_model_provider_print_that_their_model_is_left_as_it_is(capsys):
    run([], present=PRESENT | {SCHEDULED_MODULE})
    out = capsys.readouterr().out
    assert f"no --model-provider: MODEL_PROVIDER on {SCHEDULED} is left as it is" in out
    run(["--model-provider", "gemini"], present=PRESENT | {SCHEDULED_MODULE})
    assert "no --model-provider" not in capsys.readouterr().out


def ready(monkeypatch):
    from core.setup import schedule
    monkeypatch.setattr(schedule, "SCHEDULED_ASKS_READY", True)


def not_ready(monkeypatch):
    from core.setup import schedule
    monkeypatch.setattr(schedule, "SCHEDULED_ASKS_READY", False)


def test_scheduled_asks_ship_switched_on_so_apply_deploys_them_once_the_module_is_present():
    # Deliberate change on 3 October 2026: SCHEDULED_ASKS_READY was False until Albert said go.
    from core.setup import schedule
    assert schedule.SCHEDULED_ASKS_READY is True
    _, gcloud, _ = run(["--apply"], present=PRESENT | {UNDERSTAND, SCHEDULED_MODULE})
    [deploy] = [c for c in gcloud.deploys() if c[3] == SCHEDULED]
    assert deploy == dj.deploy_argv(job(SCHEDULED), f"{IMAGE}@{DIGEST}")
    assert not any(c[:3] == ["run", "jobs", "execute"] for c in gcloud.calls)


@pytest.mark.parametrize("present", [PRESENT, PRESENT | {SCHEDULED_MODULE}])
def test_scheduled_asks_while_not_ready_print_one_skip_line_in_the_dry_run_and_no_deploy(present, capsys,
                                                                                          monkeypatch):
    from core.setup import schedule
    not_ready(monkeypatch)
    code, gcloud, _ = run([], present=present)
    out = capsys.readouterr().out
    assert code == 0 and gcloud.calls == []
    [line] = [ln for ln in out.splitlines() if SCHEDULED in ln]
    assert line == f"  {SCHEDULED}: skipped, {schedule.SCHEDULED_ASKS_OFF}"
    assert "f42-agent" in line and "sqlglot" in line and "CHANNEL_WEBHOOK_URL" not in line


@pytest.mark.parametrize("argv", [["--apply"], ["--build", "--apply"], ["--apply", "--only", SCHEDULED]])
def test_scheduled_asks_while_not_ready_deploy_nothing_on_apply(argv, monkeypatch):
    not_ready(monkeypatch)
    code, gcloud, _ = run(argv, present=PRESENT | {UNDERSTAND, SCHEDULED_MODULE})
    assert code == 0
    assert SCHEDULED not in [c[3] for c in gcloud.deploys()]
    assert not any("CHANNEL_WEBHOOK_URL" in a or "f42-agent" in a for c in gcloud.calls for a in c)


def test_apply_when_ready_with_l4s_module_present_deploys_scheduled_asks_as_it_is_and_starts_nothing(monkeypatch,
                                                                                                     capsys):
    ready(monkeypatch)
    present = PRESENT | {UNDERSTAND, SCHEDULED_MODULE}
    _, gcloud, _ = run(["--apply", "--model-provider", "gemini"], present=present)
    [deploy] = [c for c in gcloud.deploys() if c[3] == SCHEDULED]
    assert deploy == dj.deploy_argv(job(SCHEDULED), f"{IMAGE}@{DIGEST}", provider="gemini")
    assert env_flags(deploy) == [SCHEDULED_ENV + ",MODEL_PROVIDER=gemini,GEMINI_MODEL=gemini-3.8-flash"]
    assert not any(c[:3] == ["run", "jobs", "execute"] for c in gcloud.calls)
    out = capsys.readouterr().out
    assert (f"{SCHEDULED}: python -m core.api.scheduled --live as f42-agent, timeout 1:00:00, 1 task, max retries 1, "
            "SocialCrawl key mounted, env F42_DATA=bigquery,F42_PROJECT=ogilvy-trends-v2, "
            "env MODEL_PROVIDER=gemini,GEMINI_MODEL=gemini-3.8-flash\n") in out


def test_only_scheduled_asks_when_ready_is_allowed_with_understand_in_the_chain(monkeypatch):
    ready(monkeypatch)
    code, gcloud, _ = run(["--apply", "--only", SCHEDULED], present=PRESENT | {UNDERSTAND, SCHEDULED_MODULE})
    assert code == 0
    assert [c[3] for c in gcloud.deploys()] == [SCHEDULED]


def test_dry_run_when_ready_without_l4s_module_lists_scheduled_asks_as_skipped_with_its_deploy(monkeypatch, capsys):
    ready(monkeypatch)
    code, gcloud, _ = run([])
    out = capsys.readouterr().out
    assert code == 0 and gcloud.calls == []
    assert f"{SCHEDULED}: skipped, core/api/scheduled.py is not in the working tree" in out
    assert f"gcloud run jobs deploy {SCHEDULED} " in out


def test_smoke_dry_run_prints_every_smoke_job_its_deploy_and_the_command_that_runs_it(capsys):
    code, gcloud, _ = run(["--smoke"], present=PRESENT | {"core.setup.smoke"})
    out = capsys.readouterr().out
    assert code == 0 and gcloud.calls == []
    for role in SMOKE_ROLES:
        assert f"gcloud run jobs deploy f42-smoke-{role} " in out
        assert (f"gcloud run jobs execute f42-smoke-{role} --wait --project=ogilvy-trends-v2 "
                "--region=us-central1") in out
    assert "py -3.13 core/setup/smoke.py --role builder" in out
    for name in DEPLOYED:
        assert f"gcloud run jobs deploy {name} " not in out


def test_smoke_apply_deploys_only_the_smoke_jobs_and_starts_none():
    _, gcloud, _ = run(["--smoke", "--apply"], present=PRESENT | {"core.setup.smoke"})
    assert [c[3] for c in gcloud.deploys()] == [f"f42-smoke-{r}" for r in SMOKE_ROLES]
    assert not any("execute" in a for c in gcloud.calls for a in c)


def test_smoke_only_limits_to_one_smoke_job():
    _, gcloud, _ = run(["--smoke", "--apply", "--only", "f42-smoke-web"], present={"core.setup.smoke"})
    assert [c[3] for c in gcloud.deploys()] == ["f42-smoke-web"]


def test_run_smoke_prints_the_builder_step_then_each_execute_and_calls_nothing(capsys):
    gcloud, git, session = FakeGcloud(), FakeGit(), FakeSession()
    code = dj.main(["--run-smoke"], gcloud=gcloud, git=git, exists=lambda m: True, workdir="unused",
                   session=session)
    out = capsys.readouterr().out
    assert code == 0
    assert gcloud.calls == [] and git.calls == [] and session.posts == []
    builder = out.index("py -3.13 core/setup/smoke.py --role builder")
    runs = [out.index(f"gcloud run jobs execute f42-smoke-{r} ") for r in SMOKE_ROLES]
    assert builder < runs[0] and runs == sorted(runs)
    assert "f42-smoke-scheduler" not in out
    assert "f42-scheduler" in out and "f42-smoke-noop" in out


@pytest.mark.parametrize("argv", [["--only", "f42-smoke-web"], ["--apply", "--only", "f42-smoke-collector"]])
def test_only_with_a_smoke_name_but_without_smoke_refuses_in_one_line_before_any_call(argv):
    gcloud, git, session = FakeGcloud(), FakeGit(), FakeSession()
    with pytest.raises(SystemExit) as stop:
        dj.main(argv, gcloud=gcloud, git=git, exists=lambda m: True, workdir="unused", session=session)
    message = str(stop.value.code)
    assert "--smoke" in message and "\n" not in message
    assert gcloud.calls == [] and git.calls == [] and session.posts == []


@pytest.mark.parametrize("extra", [["--apply"], ["--build"]])
def test_run_smoke_refuses_to_combine_with_build_or_apply(extra):
    with pytest.raises(SystemExit):
        run(["--run-smoke", *extra])


def test_no_banned_literals_or_prose_dashes_in_the_five_files():
    # Built with join so no banned literal survives compile time constant folding into the bytecode.
    banned = re.compile("|".join(["".join(["gen", "z"]), "".join(["gen", " z"]), "".join(["google", "_trends"])]), re.I)
    dashes = re.compile("[" + chr(0x2013) + chr(0x2014) + "]|\\s" + "-" * 2 + "\\s")
    for path in FILES:
        text = path.read_text(encoding="utf-8")
        assert not banned.search(text), path
        assert not dashes.search(text), path


PULSE = "f42-pulse"
PULSE_MODULE = "core.collect.pulse_job"


def pulse_ready(monkeypatch):
    from core.setup import schedule
    monkeypatch.setattr(schedule, "PULSE_READY", True)


def test_the_pulse_runs_its_job_module_as_the_collector_for_15_minutes_with_no_retry_and_the_key():
    j = job(PULSE)
    assert (j.module, j.identity, j.args, j.retries, j.timeout.total_seconds()) == (
        PULSE_MODULE, "f42-collector", (), 0, 900)
    argv = dj.deploy_argv(j, f"{IMAGE}@{DIGEST}")
    assert argv[:4] == ["run", "jobs", "deploy", PULSE]
    assert flag(argv, "--service-account") == "f42-collector@ogilvy-trends-v2.iam.gserviceaccount.com"
    assert flag(argv, "--args") == "-m,core.collect.pulse_job"
    assert flag(argv, "--task-timeout") == "900s"
    assert flag(argv, "--max-retries") == "0"
    assert [a for a in argv if a.startswith("--set-secrets")] == [SECRET_FLAG]


@pytest.mark.parametrize("understand", [True, False])
@pytest.mark.parametrize("provider", [None, "gemini"])
def test_the_pulse_is_outside_the_chain_and_carries_no_env(understand, provider):
    assert PULSE not in chain.JOBS.values() and PULSE not in dj.MODEL_JOBS
    argv = dj.deploy_argv(job(PULSE), f"{IMAGE}@{DIGEST}", understand, provider)
    assert env_flags(argv) == []


@pytest.mark.parametrize("present", [PRESENT, PRESENT | {PULSE_MODULE}])
def test_the_pulse_is_off_by_default_so_the_dry_run_prints_one_skip_line_and_no_deploy(present, capsys):
    from core.setup import schedule
    assert schedule.PULSE_READY is False
    code, gcloud, _ = run([], present=present)
    out = capsys.readouterr().out
    assert code == 0 and gcloud.calls == []
    [line] = [ln for ln in out.splitlines() if PULSE in ln]
    assert line == f"  {PULSE}: skipped, {schedule.PULSE_OFF}"
    assert "Albert" in line and "PULSE_READY" in line


@pytest.mark.parametrize("argv", [["--apply"], ["--build", "--apply"], ["--apply", "--only", PULSE]])
def test_the_pulse_is_off_by_default_so_apply_deploys_nothing_for_it(argv):
    code, gcloud, _ = run(argv, present=PRESENT | {UNDERSTAND, PULSE_MODULE})
    assert code == 0
    assert PULSE not in [c[3] for c in gcloud.deploys()]
    assert not any("pulse" in a for c in gcloud.calls for a in c)


def test_apply_when_the_pulse_is_ready_deploys_it_as_it_is_and_starts_nothing(monkeypatch, capsys):
    pulse_ready(monkeypatch)
    _, gcloud, _ = run(["--apply", "--model-provider", "gemini"], present=PRESENT | {UNDERSTAND, PULSE_MODULE})
    [deploy] = [c for c in gcloud.deploys() if c[3] == PULSE]
    assert deploy == dj.deploy_argv(job(PULSE), f"{IMAGE}@{DIGEST}")
    assert not any(c[:3] == ["run", "jobs", "execute"] for c in gcloud.calls)
    out = capsys.readouterr().out
    assert (f"{PULSE}: python -m core.collect.pulse_job as f42-collector, timeout 0:15:00, 1 task, max retries 0, "
            "SocialCrawl key mounted\n") in out


def test_only_the_pulse_when_ready_is_allowed_with_understand_in_the_chain(monkeypatch):
    pulse_ready(monkeypatch)
    code, gcloud, _ = run(["--apply", "--only", PULSE], present=PRESENT | {UNDERSTAND, PULSE_MODULE})
    assert code == 0
    assert [c[3] for c in gcloud.deploys()] == [PULSE]


BREAKING = "f42-breaking"
BREAKING_MODULE = "core.detect.breaking"


def test_breaking_is_outside_the_chain_runs_as_the_detect_identity_and_carries_no_key_or_env():
    j = job(BREAKING)
    assert BREAKING not in chain.JOBS.values() and BREAKING not in dj.MODEL_JOBS
    assert (j.module, j.identity, j.secret, j.args, j.secrets, j.env) == (
        BREAKING_MODULE, job("f42-detect").identity, False, (), (), ())
    assert env_flags(dj.deploy_argv(j, f"{IMAGE}@{DIGEST}", True, "gemini")) == []


@pytest.mark.parametrize("present", [PRESENT, PRESENT | {BREAKING_MODULE}])
def test_breaking_is_off_with_the_pulse_so_the_dry_run_prints_one_skip_line_and_no_deploy(present, capsys):
    from core.setup import schedule
    assert schedule.PULSE_READY is False
    code, gcloud, _ = run([], present=present)
    assert code == 0 and gcloud.calls == []
    [line] = [ln for ln in capsys.readouterr().out.splitlines() if BREAKING in ln]
    assert line == f"  {BREAKING}: skipped, {schedule.PULSE_OFF}"


@pytest.mark.parametrize("argv", [["--apply"], ["--apply", "--only", BREAKING]])
def test_breaking_is_off_with_the_pulse_so_apply_deploys_nothing_for_it(argv):
    code, gcloud, _ = run(argv, present=PRESENT | {UNDERSTAND, BREAKING_MODULE})
    assert code == 0
    assert BREAKING not in [c[3] for c in gcloud.deploys()]
    assert not any(BREAKING in a for c in gcloud.calls for a in c)


def test_apply_when_the_pulse_is_ready_deploys_breaking_too_and_starts_nothing(monkeypatch):
    pulse_ready(monkeypatch)
    _, gcloud, _ = run(["--apply"], present=PRESENT | {UNDERSTAND, PULSE_MODULE, BREAKING_MODULE})
    [deploy] = [c for c in gcloud.deploys() if c[3] == BREAKING]
    assert deploy == dj.deploy_argv(job(BREAKING), f"{IMAGE}@{DIGEST}")
    assert flag(deploy, "--args") == f"-m,{BREAKING_MODULE}"
    assert not any(c[:3] == ["run", "jobs", "execute"] for c in gcloud.calls)


CHAIN_SIZES = {"f42-collect": ("2Gi", "1"), "f42-understand": ("4Gi", "2"), "f42-detect": ("4Gi", "2"),
               "f42-brief": ("2Gi", "1")}


def test_every_morning_chain_job_records_its_memory_and_cpu_never_the_512mi_default():
    """Detect died out of memory on 512Mi on 5 Oct 2026 and blocked the brief: no chain job deploys without a size."""
    assert {j.name for j in dj.JOBS if j.name in chain.JOBS.values()} == set(CHAIN_SIZES)
    for name, (memory, cpu) in CHAIN_SIZES.items():
        j = job(name)
        assert (j.memory, j.cpu) == (memory, cpu), name
        for understand in (False, True):
            argv = dj.deploy_argv(j, f"{IMAGE}@{DIGEST}", understand)
            assert flag(argv, "--memory") == memory, name
            assert flag(argv, "--cpu") == cpu, name


def test_understand_and_detect_keep_at_least_4gi_and_2_cpu():
    for name in ("f42-understand", "f42-detect"):
        assert (job(name).memory, job(name).cpu) == ("4Gi", "2"), name


def test_jobs_without_resources_send_no_memory_or_cpu_flag_so_their_live_settings_stay():
    for j in dj.JOBS + dj.SMOKE_JOBS:
        if j.name in CHAIN_SIZES:
            continue
        assert (j.memory, j.cpu) == (None, None), j.name
        argv = dj.deploy_argv(j, f"{IMAGE}@{DIGEST}")
        assert not [a for a in argv if a.startswith(("--memory", "--cpu"))], j.name


def test_apply_deploys_each_chain_job_with_its_memory_and_cpu():
    _, gcloud, _ = run(["--apply"], present=PRESENT | {UNDERSTAND})
    for name, (memory, cpu) in CHAIN_SIZES.items():
        deploy = next(c for c in gcloud.deploys() if c[3] == name)
        assert flag(deploy, "--memory") == memory and flag(deploy, "--cpu") == cpu, name


def test_dry_run_names_each_chain_job_memory_and_cpu(capsys):
    run([], present=PRESENT | {UNDERSTAND})
    out = capsys.readouterr().out.splitlines()
    for name, (memory, cpu) in CHAIN_SIZES.items():
        line = next(ln for ln in out if ln.startswith(f"  {name}:"))
        assert f"memory {memory}, cpu {cpu}" in line, name


DIGEST_JOB = "f42-digest"
DIGEST_MODULE = "core.api.digest"
DIGEST_SECRETS = "--set-secrets=GMAIL_APP_PASSWORD=GMAIL_APP_PASSWORD:latest"
DIGEST_ENV = ("--update-env-vars=F42_DATA=bigquery,F42_PROJECT=ogilvy-trends-v2,"
              "GMAIL_USER=jhb.analytics@gmail.com")


def digest_ready(monkeypatch):
    from core.setup import schedule
    monkeypatch.setattr(schedule, "DIGEST_READY", True)


def test_the_digest_sends_as_f42_agent_for_ten_minutes_with_one_retry():
    j = job(DIGEST_JOB)
    assert (j.module, j.identity, j.args, j.retries, j.timeout.total_seconds(), j.secret) == (
        DIGEST_MODULE, "f42-agent", ("--send",), 1, 600, False)
    argv = dj.deploy_argv(j, f"{IMAGE}@{DIGEST}")
    assert argv[:4] == ["run", "jobs", "deploy", DIGEST_JOB]
    assert flag(argv, "--service-account") == "f42-agent@ogilvy-trends-v2.iam.gserviceaccount.com"
    assert flag(argv, "--args") == "-m,core.api.digest,--send"
    assert flag(argv, "--task-timeout") == "600s"
    assert flag(argv, "--max-retries") == "1"


def test_the_digest_mounts_only_the_gmail_app_password_and_sets_the_sender_but_never_switches_sending_on():
    argv = dj.deploy_argv(job(DIGEST_JOB), f"{IMAGE}@{DIGEST}")
    assert [a for a in argv if a.startswith("--set-secrets")] == [DIGEST_SECRETS]
    assert [a for a in argv if "GMAIL_APP_PASSWORD" in a] == [DIGEST_SECRETS]
    assert env_flags(argv) == [DIGEST_ENV]
    # Sending and the recipient list are set by hand on the deployed job; --update-env-vars keeps them on redeploy.
    assert not any("DIGEST_SEND_ENABLED" in a or "DIGEST_RECIPIENTS" in a for a in argv)
    assert not any("SOCIALCRAWL" in a for a in argv)


@pytest.mark.parametrize("understand", [True, False])
@pytest.mark.parametrize("provider", [None, "gemini"])
def test_the_digest_is_outside_the_chain_and_never_carries_chain_understand_or_model_provider(understand, provider):
    assert DIGEST_JOB not in chain.JOBS.values() and DIGEST_JOB not in dj.MODEL_JOBS
    argv = dj.deploy_argv(job(DIGEST_JOB), f"{IMAGE}@{DIGEST}", understand, provider)
    assert env_flags(argv) == [DIGEST_ENV]


@pytest.mark.parametrize("present", [PRESENT, PRESENT | {DIGEST_MODULE}])
def test_the_digest_while_not_ready_is_one_skip_line_in_the_dry_run_and_no_deploy(present, capsys, monkeypatch):
    from core.setup import schedule
    monkeypatch.setattr(schedule, "DIGEST_READY", False)
    code, gcloud, _ = run([], present=present)
    out = capsys.readouterr().out
    assert code == 0 and gcloud.calls == []
    [line] = [ln for ln in out.splitlines() if DIGEST_JOB in ln]
    assert line == f"  {DIGEST_JOB}: skipped, {schedule.DIGEST_OFF}"
    assert "GMAIL_APP_PASSWORD" in line and "f42-agent" in line and "DIGEST_READY" in line


@pytest.mark.parametrize("argv", [["--apply"], ["--build", "--apply"], ["--apply", "--only", DIGEST_JOB]])
def test_the_digest_while_not_ready_is_not_deployed_by_apply(argv, monkeypatch):
    from core.setup import schedule
    monkeypatch.setattr(schedule, "DIGEST_READY", False)
    code, gcloud, _ = run(argv, present=PRESENT | {UNDERSTAND, DIGEST_MODULE})
    assert code == 0
    assert DIGEST_JOB not in [c[3] for c in gcloud.deploys()]
    assert not any("GMAIL" in a or DIGEST_JOB in a for c in gcloud.calls for a in c)


def test_apply_when_the_digest_is_ready_deploys_it_as_it_is_and_starts_nothing(monkeypatch, capsys):
    digest_ready(monkeypatch)
    _, gcloud, _ = run(["--apply"], present=PRESENT | {UNDERSTAND, DIGEST_MODULE})
    [deploy] = [c for c in gcloud.deploys() if c[3] == DIGEST_JOB]
    assert deploy == dj.deploy_argv(job(DIGEST_JOB), f"{IMAGE}@{DIGEST}")
    assert not any(c[:3] == ["run", "jobs", "execute"] for c in gcloud.calls)
    out = capsys.readouterr().out
    assert (f"{DIGEST_JOB}: python -m core.api.digest --send as f42-agent, timeout 0:10:00, 1 task, max retries 1, "
            "GMAIL_APP_PASSWORD mounted, env F42_DATA=bigquery,F42_PROJECT=ogilvy-trends-v2,"
            "GMAIL_USER=jhb.analytics@gmail.com\n") in out


def test_cloudbuild_config_passes_the_commit_sha_to_the_image_as_the_git_sha_build_argument():
    cfg = yaml.safe_load((SETUP / "cloudbuild.jobs.yaml").read_text(encoding="utf-8"))
    args = cfg["steps"][0]["args"]
    assert args[args.index("--build-arg") + 1] == "GIT_SHA=${_TAG}"
    built = dj.build_request(SHA)["steps"][0]["args"]
    assert built[built.index("--build-arg") + 1] == f"GIT_SHA={SHA}"
