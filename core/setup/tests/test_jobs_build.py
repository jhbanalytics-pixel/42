"""Release B image build and digest pin (W8-REL-B v2.1 section 3.5, JB-01 to JB-04). No cloud access: a fake gcloud, a fake git
and a fake Cloud Build session record every call."""
import re
from pathlib import Path

import pytest
import yaml

from core.setup import deploy_jobs as dj
from core.setup import stamp

SETUP = Path(dj.__file__).resolve().parent
COMMIT = "5e1c0de9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3"
OTHER = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "sha256:" + "d" * 64
IMAGE = dj.IMAGE
BUCKET = "ogilvy-trends-v2-f42-media-staging"


def tag_for(attempt):
    return f"{COMMIT[:12]}-{attempt:02d}"


class FakeGcloud:
    def __init__(self):
        self.calls = []

    def run(self, args):
        self.calls.append(list(args))
        if args[:4] == ["artifacts", "docker", "images", "describe"]:
            return DIGEST + "\n"
        return ""


class FakeResponse:
    def __init__(self, body, status_code=200):
        self.body, self.status_code, self.text = body, status_code, str(body)

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, tag, results=None):
        self.tag = tag
        self.results = {"images": [{"name": f"{IMAGE}:{tag}", "digest": DIGEST}]} if results is None else results
        self.posts, self.gets = [], []

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        return FakeResponse({"metadata": {"build": {"id": "b-1", "status": "QUEUED"}}})

    def get(self, url, timeout=None):
        self.gets.append(url)
        return FakeResponse({"id": "b-1", "status": "SUCCESS", "results": self.results})


class FakeGit:
    def __init__(self, head=COMMIT, dirty=""):
        self.head, self.dirty, self.calls = head, dirty, []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:1] == ["rev-parse"]:
            return (self.head[:7] if "--short" in args else self.head) + "\n"
        if args[:1] == ["status"]:
            return self.dirty
        return ""


def build(argv, head=COMMIT, dirty="", attempt=1):
    gcloud, git, session = FakeGcloud(), FakeGit(head, dirty), FakeSession(tag_for(attempt))
    code = dj.main(argv, gcloud=gcloud, git=git, exists=lambda module: True, workdir="unused", session=session,
                   sleep=lambda seconds: None)
    return code, gcloud, git, session


def docker_args(body):
    return body["steps"][0]["args"]


# JB-01

def test_jb01_the_image_tag_is_the_first_twelve_hex_of_the_commit_and_the_attempt_number():
    assert dj.image_tag(COMMIT, 1) == f"{COMMIT[:12]}-01"
    assert dj.image_tag(COMMIT, 12) == f"{COMMIT[:12]}-12"
    for bad in (0, 100, -1, True, "1", 1.0):
        with pytest.raises(ValueError):
            dj.image_tag(COMMIT, bad)
    for sha in (COMMIT[:7], COMMIT[:39], COMMIT + "0", COMMIT.upper(), "g" * 40, "", None):
        with pytest.raises(ValueError):
            dj.image_tag(sha, 1)


def test_jb01_a_build_pushes_the_attempt_numbered_tag_and_the_build_argument_is_the_full_commit_not_the_tag():
    code, gcloud, git, session = build(["--build", "--attempt", "2"], attempt=2)
    assert code == 0
    body = session.posts[0][1]
    args = docker_args(body)
    assert args[args.index("-t") + 1] == f"{IMAGE}:{tag_for(2)}"
    assert body["images"] == [f"{IMAGE}:{tag_for(2)}"]
    assert args[args.index("--build-arg") + 1] == f"GIT_SHA={COMMIT}"
    assert body["source"]["storageSource"]["object"].endswith(f"jobs-{tag_for(2)}.tar.gz")
    assert "${" not in str(body)


def test_jb01_the_attempt_defaults_to_one_and_an_out_of_range_attempt_is_refused_before_any_call():
    _, _, _, session = build(["--build"])
    assert session.posts[0][1]["images"] == [f"{IMAGE}:{tag_for(1)}"]
    for bad in ("0", "100", "-3", "x"):
        gcloud, git, session = FakeGcloud(), FakeGit(), FakeSession(tag_for(1))
        with pytest.raises(SystemExit):
            dj.main(["--build", "--attempt", bad], gcloud=gcloud, git=git, exists=lambda m: True, workdir="unused", session=session)
        assert gcloud.calls == [] and session.posts == []


def test_jb01_a_head_that_is_not_a_forty_hex_commit_is_refused_before_any_call():
    gcloud, git, session = FakeGcloud(), FakeGit(head="abc1234"), FakeSession(tag_for(1))
    with pytest.raises(SystemExit):
        dj.main(["--build"], gcloud=gcloud, git=git, exists=lambda m: True, workdir="unused", session=session)
    assert gcloud.calls == [] and session.posts == []


def test_jb01_cloudbuild_yaml_has_no_default_for_the_tag_or_the_git_sha():
    cfg = yaml.safe_load((SETUP / "cloudbuild.jobs.yaml").read_text(encoding="utf-8"))
    assert set(cfg.get("substitutions", {})) == {"_IMAGE"}
    args = cfg["steps"][0]["args"]
    assert args[args.index("--build-arg") + 1] == "GIT_SHA=${_GIT_SHA}"
    assert "${_IMAGE}:${_TAG}" in args and cfg["images"] == ["${_IMAGE}:${_TAG}"]
    with pytest.raises(TypeError):
        dj.build_request(tag_for(1))  # a tag alone is not enough: the commit is a separate, required argument


def test_jb01_the_stamp_of_an_image_built_this_way_records_the_commit_and_a_tag_suffix_would_record_null():
    body = dj.build_request(tag_for(3), COMMIT)
    args = docker_args(body)
    handed_in = args[args.index("--build-arg") + 1].split("=", 1)[1]
    assert stamp.build({"F42_GIT_SHA": handed_in}, SETUP.parents[1])["git_sha"] == COMMIT
    # the suffixed tag is not a sha: handed in as the build argument it would record nothing (the reason for the separate argument)
    assert stamp.build({"F42_GIT_SHA": tag_for(3)}, SETUP.parents[1])["git_sha"] is None


# JB-03

def test_jb03_the_archive_is_made_from_the_bound_commit_sha_and_not_from_a_tag_or_branch():
    _, gcloud, git, _ = build(["--build"])
    archive = [c for c in git.calls if c[:1] == ["archive"]]
    assert len(archive) == 1 and COMMIT in archive[0] and tag_for(1) not in archive[0]
    assert archive[0][-2:] == ["core", "docs/full-42/reference/sc_routes.json"]


def test_jb03_a_bound_commit_that_is_not_head_is_refused_before_any_call():
    gcloud, git, session = FakeGcloud(), FakeGit(), FakeSession(tag_for(1))
    with pytest.raises(SystemExit) as stop:
        dj.main(["--build", "--commit", OTHER], gcloud=gcloud, git=git, exists=lambda m: True, workdir="unused", session=session)
    assert "bound commit" in str(stop.value.code)
    assert gcloud.calls == [] and session.posts == [] and [c for c in git.calls if c[:1] == ["archive"]] == []


def test_jb03_a_bound_commit_that_is_not_forty_hex_is_refused():
    for bad in (COMMIT[:12], COMMIT.upper(), "HEAD", ""):
        gcloud, git, session = FakeGcloud(), FakeGit(), FakeSession(tag_for(1))
        with pytest.raises(SystemExit):
            dj.main(["--build", "--commit", bad], gcloud=gcloud, git=git, exists=lambda m: True, workdir="unused", session=session)
        assert gcloud.calls == [] and session.posts == []


def test_jb03_the_bound_commit_that_equals_head_builds():
    code, _, _, session = build(["--build", "--commit", COMMIT])
    assert code == 0 and len(session.posts) == 1


def test_jb03_a_dirty_tree_refuses_the_build_before_any_gcloud_or_api_call():
    gcloud, git, session = FakeGcloud(), FakeGit(dirty=" M core/collect/chain.py\n"), FakeSession(tag_for(1))
    with pytest.raises(SystemExit):
        dj.main(["--build", "--commit", COMMIT], gcloud=gcloud, git=git, exists=lambda m: True, workdir="unused", session=session)
    assert gcloud.calls == [] and session.posts == []


def test_jb03_each_attempt_uploads_under_its_own_object_and_never_overwrites_one():
    objects = []
    for attempt in (1, 2):
        _, gcloud, _, _ = build(["--build", "--attempt", str(attempt)], attempt=attempt)
        upload = gcloud.calls[0]
        assert upload[:2] == ["storage", "cp"] and "--no-clobber" in upload
        objects.append(upload[3])
    assert objects[0] != objects[1]
    assert objects[0].endswith(f"jobs-{tag_for(1)}.tar.gz") and objects[1].endswith(f"jobs-{tag_for(2)}.tar.gz")


def test_jb03_the_build_runs_as_the_deployer_and_logs_to_cloud_logging_whatever_the_config_says(tmp_path, monkeypatch):
    config = tmp_path / "cloudbuild.yaml"
    cfg = yaml.safe_load((SETUP / "cloudbuild.jobs.yaml").read_text(encoding="utf-8"))
    cfg["options"] = {"logging": "GCS_ONLY"}
    config.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    monkeypatch.setattr(dj, "CONFIG", config)
    body = dj.build_request(tag_for(1), COMMIT)
    assert body["serviceAccount"] == dj.DEPLOYER and body["serviceAccount"].startswith("projects/ogilvy-trends-v2/serviceAccounts/f42-deployer@")
    assert body["options"]["logging"] == "CLOUD_LOGGING_ONLY"


# JB-04

def dockerfile():
    return (SETUP / "jobs.Dockerfile").read_text(encoding="utf-8")


def test_jb04_the_run_after_user_imports_the_clustering_stack_and_fits_umap_as_the_job_user():
    text = dockerfile()
    after = text[text.index("USER 10001") + len("USER 10001"):]
    runs = [" ".join(m.group(0).split()) for m in re.finditer(r"^RUN\b(?:.*\\\n)*.*$", after, re.M)]
    fits = [r for r in runs if "UMAP" in r]
    assert len(fits) == 1, "no single RUN after USER 10001 fits UMAP"
    assert fits[0].startswith("RUN python ")
    for needle in ("bertopic", "umap", "hdbscan", "numba", ".fit("):
        assert needle in fits[0], needle
    assert "|| true" not in fits[0] and "; true" not in fits[0] and "exit 0" not in fits[0]


def test_jb04_the_only_build_argument_is_git_sha_and_it_is_stamped_after_the_pip_layers():
    text = dockerfile()
    assert [ln.strip() for ln in text.splitlines() if ln.strip().startswith("ARG ")] == ["ARG GIT_SHA=unknown"]
    assert re.search(r"^ENV F42_GIT_SHA=\$\{?GIT_SHA\}?$", text, re.M)
    assert text.index("ARG GIT_SHA") > text.index("pip install --no-cache-dir --no-deps bertopic")


# RJ-4: JB-03 reads "built from a clean clone at the bound commit"; there is no archive mode

def test_rj4_the_tool_says_it_is_built_from_a_clean_clone_at_the_bound_commit(capsys):
    assert "clean clone at the bound commit" in dj.__doc__
    assert "built from a git archive of HEAD" not in dj.__doc__
    build(["--build", "--commit", COMMIT])
    out = capsys.readouterr().out
    assert f"built from a clean clone at commit {COMMIT}" in out
    assert "built from a git archive of commit" not in out


def test_rj4_there_is_no_archive_mode_so_the_tool_offers_no_option_to_run_from_an_extracted_archive():
    for flag in ("--from-archive", "--archive", "--source", "--no-git"):
        gcloud, git, session = FakeGcloud(), FakeGit(), FakeSession(tag_for(1))
        with pytest.raises(SystemExit) as stop:
            dj.main(["--build", flag], gcloud=gcloud, git=git, exists=lambda m: True, workdir="unused", session=session)
        assert stop.value.code == 2
        assert gcloud.calls == [] and session.posts == []


def test_rj4_a_tree_that_is_not_a_git_clone_is_refused_in_words_before_any_gcloud_or_api_call():
    class NoGit(FakeGit):
        def __call__(self, args):
            self.calls.append(list(args))
            raise dj.GcloudError("git rev-parse HEAD failed: fatal: not a git repository")

    gcloud, git, session = FakeGcloud(), NoGit(), FakeSession(tag_for(1))
    with pytest.raises(SystemExit) as stop:
        dj.main(["--build", "--commit", COMMIT], gcloud=gcloud, git=git, exists=lambda m: True, workdir="unused", session=session)
    assert "clean clone" in str(stop.value.code) and "bound commit" in str(stop.value.code)
    assert gcloud.calls == [] and session.posts == [] and [c for c in git.calls if c[:1] == ["archive"]] == []
