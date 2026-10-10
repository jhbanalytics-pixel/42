"""The steps of a services-only release as data (W8-REL 2.11, 3.1, 3.3, 3.5): what the release paste runs, in order, as
argument lists, and the exact commands of every recovery row.

Nothing here runs a command. The paste renders these lists, and a test holds them to the contract: promotion order
(the agent, then the API, only after the helper has read the agent), traffic only ever to a revision by name (never
--to-latest, never a tag command other than removal), rollback in the order the compatibility test covers (the API
first), tag removal only after the readback that proves where traffic is, and a positive allowlist that every external
invocation must match exactly once (K2).

Rollback targets, canonical URLs and the tag come from the bound baseline and the bindings, not from literals here.
"""
from __future__ import annotations

import dataclasses
import os
import re
from dataclasses import dataclass

from core.setup.release import jobs_only as jo
from core.setup.release.services_only import PHASES

PROJECT = "ogilvy-trends-v2"
REGION = "us-central1"
SERVICES = ("f42-agent", "f42-api")
GCS_STAGING = "gs://ogilvy-trends-v2-f42-media-staging/build-source"
BUILD_ACCOUNT = f"projects/{PROJECT}/serviceAccounts/f42-deployer@{PROJECT}.iam.gserviceaccount.com"
IDENTITY_FIELDS = "json(core.account,core.project,auth.impersonate_service_account)"
# The extract runs the Windows system tar by its full path, so the first tar on PATH (Git's GNU tar reads C: as a host) is never used.
# A host with no System32 has only the PATH one.
SYSTEM_TAR = r"C:\Windows\System32\tar.exe" if os.name == "nt" else "tar"
SYSTEM_TAR_PATH = re.compile(r"[A-Za-z]:\\[^\\]+\\System32\\tar\.exe", re.I)
CHECKER_KINDS = ("validate", "readbacks", "rollback-blockers", "inflight-asks")
FORBIDDEN_TRAFFIC_FLAGS = ("--to-latest", "--to-tags", "--update-tags", "--set-tags", "--clear-tags")
RELEASE_ID = re.compile(r"rel-[0-9a-f]{7}-[0-9]{2}")
IMAGE_SUBSTITUTION = re.compile(rf"_IMAGE={re.escape(REGION)}-docker\.pkg\.dev/{PROJECT}/intelligence-42/f42-web:[0-9a-f]{{12}}-[0-9]{{2}}")
API_TAG_URL = re.compile(r"https://rel-[0-9a-f]{7}-[0-9]{2}---f42-api-[a-z0-9]+-uc\.a\.run\.app")


@dataclass(frozen=True)
class Ctx:
    """What a plan is rendered from: the release, the bound paths and the a80 revisions to restore."""

    release_id: str
    commit: str
    helper: str
    bindings: str
    evidence: str
    manifest: str
    manifest_sha256: str
    api_tag_url: str
    a80: dict
    image_tag: str
    archive: str = "<archive>"
    extract_dir: str = "<extract dir>"
    schema_effects: bool = False


@dataclass(frozen=True)
class Step:
    name: str
    argv: tuple
    requires: str | None = None  # the step that must have passed before this one is issued
    conditional: str | None = None  # a readback fact the step depends on
    via: str | None = None  # the orchestrator that issues the step, when the paste does not


def step(name, argv, **kw):
    return Step(name, tuple(argv), **kw)


# single commands

def helper(ctx, phase, tag=False):
    argv = ["py", "-3.13", ctx.helper, "--mode", "services-only", "--phase", phase, "--bindings", ctx.bindings, "--evidence", ctx.evidence]
    return step(f"helper:{phase}", argv + (["--tag", ctx.release_id] if tag else []))


def traffic_to(service, revision, name):
    return step(name, ["gcloud", "run", "services", "update-traffic", service, "--project", PROJECT, "--region", REGION,
                       f"--to-revisions={revision}=100", "--quiet"])


def remove_tag(ctx, service):
    return step(f"remove_tag:{service}", ["gcloud", "run", "services", "update-traffic", service, "--project", PROJECT,
                                          "--region", REGION, "--remove-tags", ctx.release_id, "--quiet"])


def checker(ctx, kind):
    return step(f"checker:{kind}", ["py", "-3.13", "core/setup/durable_effects_check.py", "--manifest", ctx.manifest, "--check", kind,
                                    "--evidence", ctx.evidence])


def declared_env_reads():
    """The two reads that show the operator, before the typed DEPLOY, which environment variables the candidate deploy of
    f42-agent removes: the live service description, then the digest match of its names by the module the deploy script uses."""
    return [step("declared_env:describe", ["gcloud", "run", "services", "describe", "f42-agent", "--project", PROJECT, "--region", REGION,
                                           "--format=json"]),
            step("declared_env:match", ["py", "-3.13", "-B", "-m", "core.setup.release.declared_env_removals", "--service", "f42-agent"])]


def pin(ctx):
    return [traffic_to(s, ctx.a80[s], f"pin:{s}") for s in SERVICES]


def restore_all(ctx):
    """Both services to the a80 revisions by name, the API first (Q6): after the first command the a80 API calls the
    canonical URL, which still serves the candidate agent; after the second, a80 with a80."""
    return [traffic_to("f42-api", ctx.a80["f42-api"], "restore:f42-api"), traffic_to("f42-agent", ctx.a80["f42-agent"], "restore:f42-agent")]


def remove_both_tags(ctx):
    return [remove_tag(ctx, "f42-api"), remove_tag(ctx, "f42-agent")]


# actions

def candidate(ctx):
    """3.1 Action Candidate."""
    steps = [
        step("assert:head", ["git", "rev-parse", "HEAD"]), step("assert:tree", ["git", "rev-parse", "HEAD^{tree}"]),
        step("assert:status", ["git", "status", "--porcelain=v1"]),
        step("assert:identity", ["gcloud", "config", "list", f"--format={IDENTITY_FIELDS}"]),
        *declared_env_reads(),
        helper(ctx, "BeforeAnyWrite"),
        step("archive", ["git", "archive", "--format=tar", f"--output={ctx.archive}", ctx.commit]),
        step("extract", [SYSTEM_TAR, "-xf", ctx.archive, "-C", ctx.extract_dir]),
        step("build", ["gcloud", "builds", "submit", "--project", PROJECT, "--region", REGION, "--config", "core/api/cloudbuild.yaml",
                       "--substitutions", f"_IMAGE={ctx.image_tag}", "--gcs-source-staging-dir", GCS_STAGING,
                       "--service-account", BUILD_ACCOUNT, "."]),
        helper(ctx, "Freeze"),
    ]
    steps.append(checker(ctx, "validate"))
    if ctx.schema_effects:
        steps.append(step("schema_apply", ["py", "-3.13", "-m", "core.schema.apply", "--apply"]))
    steps += pin(ctx)
    steps += [
        helper(ctx, "BeforeCandidate"),
        step("deploy_candidate", ["bash", "core/api/deploy_candidate.sh", ctx.manifest, ctx.manifest_sha256]),
        helper(ctx, "BeforeSmoke"),
        # The one smoke runs against the API tag URL of the manifest, never the public API.
        step("smoke", ["py", "-3.13", "core/api/smoke.py", ctx.api_tag_url, "--market", "ZA", "--mode", "live", "--ask-timeout", "900"]),
        helper(ctx, "AfterSmoke"),
    ]
    return steps


def promote(ctx):
    """3.1 Action Promote: the agent first, the API only after the helper has read the agent."""
    api = dataclasses.replace(traffic_to("f42-api", f"f42-api-{ctx.release_id}", "promote:f42-api"), requires="helper:AfterAgentPromotion")
    return [helper(ctx, "BeforePromotion"), traffic_to("f42-agent", f"f42-agent-{ctx.release_id}", "promote:f42-agent"),
            helper(ctx, "AfterAgentPromotion"), api, helper(ctx, "AfterPromotion")]


def rollback(ctx):
    """3.5 Action Rollback: it never waits for a quiet window and never asserts a clean checkout."""
    return [helper(ctx, "BeforeRollback"), *restore_all(ctx), helper(ctx, "AfterRollback")]


def retire(ctx):
    """2.4 Action Retire: tags are removed only after the readback that proves nothing still uses them."""
    return [helper(ctx, "BeforeRetire", tag=True), *remove_both_tags(ctx), helper(ctx, "AfterRetire", tag=True)]


ACTIONS = {"Candidate": candidate, "Promote": promote, "Rollback": rollback, "Retire": retire}


# the recovery table (3.3)

def recovery_rows(ctx):
    """Row id -> the commands, in order. A step with a `conditional` is issued only when the readback before it says so."""
    api_tag = dataclasses.replace(remove_tag(ctx, "f42-api"), conditional="BeforeRollback reports that the API tag exists")
    return {
        "C1": [helper(ctx, "BeforeAnyWrite")],
        "C2": [*pin(ctx), helper(ctx, "BeforeCandidate")],
        "C3": [helper(ctx, "BeforeRollback"), api_tag, remove_tag(ctx, "f42-agent"), helper(ctx, "AfterRetire", tag=True)],
        "C4": [*restore_all(ctx), helper(ctx, "AfterRollback"), *remove_both_tags(ctx), helper(ctx, "AfterRetire", tag=True)],
        "C5": [checker(ctx, "readbacks")],
        "1": [helper(ctx, "AfterSmoke"), *remove_both_tags(ctx), helper(ctx, "AfterRetire", tag=True)],
        "2": [helper(ctx, "BeforeRollback"), traffic_to("f42-agent", ctx.a80["f42-agent"], "restore:f42-agent"),
              helper(ctx, "AfterRollback"), *remove_both_tags(ctx), helper(ctx, "AfterRetire", tag=True)],
        "3": [helper(ctx, "BeforeRollback"), *restore_all(ctx), helper(ctx, "AfterRollback"), *remove_both_tags(ctx),
              helper(ctx, "AfterRetire", tag=True)],
        "4": [checker(ctx, "rollback-blockers")],
        "4:blocked": [helper(ctx, "BeforeRollback")],
    }


# the positive allowlist (K2)

def _traffic(a):
    if len(a) < 6 or a[:4] != ["gcloud", "run", "services", "update-traffic"] or a[4] not in SERVICES:
        return False
    rest = a[5:]
    if rest[:4] != ["--project", PROJECT, "--region", REGION] or rest[-1] != "--quiet":
        return False
    action = rest[4:-1]
    if len(action) == 1:
        return bool(re.fullmatch(r"--to-revisions=[a-z0-9-]+=100", action[0]))
    return len(action) == 2 and action[0] == "--remove-tags" and bool(RELEASE_ID.fullmatch(action[1]))


def _helper(a):
    if len(a) < 11 or a[:2] != ["py", "-3.13"] or not a[2].endswith("bound_readback.py"):
        return False
    if a[3:6] != ["--mode", "services-only", "--phase"] or a[6] not in PHASES or a[7] != "--bindings" or a[9] != "--evidence":
        return False
    tail = a[11:]
    return tail == [] or (len(tail) == 2 and tail[0] == "--tag" and bool(RELEASE_ID.fullmatch(tail[1])))


def _git_archive(a):
    return len(a) == 5 and a[:3] == ["git", "archive", "--format=tar"] and a[3].startswith("--output=") and bool(re.fullmatch(r"[0-9a-f]{40}", a[4]))


def _tar(a):
    program = bool(SYSTEM_TAR_PATH.fullmatch(a[0])) if os.name == "nt" else a[0] == "tar"
    return len(a) == 5 and program and a[1] == "-xf" and a[3] == "-C"


def _build(a):
    if a[:7] != ["gcloud", "builds", "submit", "--project", PROJECT, "--region", REGION] or a[-1] != ".":
        return False
    pairs = dict(zip(a[7:-1:2], a[8:-1:2]))
    return (len(a) == 16 and pairs.get("--config") == "core/api/cloudbuild.yaml" and pairs.get("--gcs-source-staging-dir") == GCS_STAGING
            and pairs.get("--service-account") == BUILD_ACCOUNT and bool(IMAGE_SUBSTITUTION.fullmatch(pairs.get("--substitutions", ""))))


def _deploy(a):
    return len(a) == 4 and a[:2] == ["bash", "core/api/deploy_candidate.sh"] and bool(re.fullmatch(r"[0-9a-f]{64}", a[3]))


def _smoke(a):
    return (len(a) == 10 and a[:3] == ["py", "-3.13", "core/api/smoke.py"] and bool(API_TAG_URL.fullmatch(a[3]))
            and a[4:] == ["--market", "ZA", "--mode", "live", "--ask-timeout", "900"])


def _checker(a):
    if len(a) < 7 or a[:3] != ["py", "-3.13", "core/setup/durable_effects_check.py"]:
        return False
    flags = dict(zip(a[3::2], a[4::2]))
    return len(a) % 2 == 1 and set(flags) <= {"--manifest", "--bindings", "--check", "--evidence"} and flags.get("--check") in CHECKER_KINDS \
        and "--evidence" in flags


def _declared_env_read(a):
    return a in (["gcloud", "run", "services", "describe", "f42-agent", "--project", PROJECT, "--region", REGION, "--format=json"],
                 ["py", "-3.13", "-B", "-m", "core.setup.release.declared_env_removals", "--service", "f42-agent"])


ALLOWED = {
    "git_read": lambda a: a in (["git", "rev-parse", "HEAD"], ["git", "rev-parse", "HEAD^{tree}"], ["git", "rev-parse", "--short", "HEAD"],
                                ["git", "rev-parse", "--short=12", "HEAD"], ["git", "status", "--porcelain=v1"], ["git", "remote", "get-url", "origin"]),
    "archive": lambda a: _git_archive(a) or _tar(a),
    "identity": lambda a: a == ["gcloud", "config", "list", f"--format={IDENTITY_FIELDS}"],
    "declared_env_read": _declared_env_read,
    "build": _build,
    "deploy": _deploy,
    "traffic": _traffic,
    "helper": _helper,
    "smoke": _smoke,
    "schema_apply": lambda a: a == ["py", "-3.13", "-m", "core.schema.apply", "--apply"],
    "checker": _checker,
}


def matching_entries(argv):
    argv = list(argv)
    return [name for name, accepts in ALLOWED.items() if accepts(argv)]


def check_allowlist(steps):
    """Every external invocation matches exactly one entry; the first that does not is returned, else None."""
    for s in steps:
        if len(matching_entries(s.argv)) != 1:
            return s
    return None


# The calls deploy_candidate.sh makes (the DS call log), judged apart from the paste's allowlist because the paste never
# issues them itself. They are the one deploy of each service, and the one flag that removes environment variables is
# allowed on f42-agent only, with a list of plain names (finding 3). The names themselves never appear in the repository:
# the script derives them from the live service and the digest table.
PLAIN_NAME_LIST = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(,[A-Za-z_][A-Za-z0-9_]*)*")
IMAGE_BY_DIGEST = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")
# Every flag the two deploys carry, from the script and from core/api/deploy_flags.env, and nothing else. A flag takes its value
# as the next argument or after an equals sign; a switch takes none and refuses an equals sign. Each may appear once.
DEPLOY_VALUED = {"--project", "--region", "--image", "--revision-suffix", "--tag", "--service-account", "--update-env-vars", "--remove-env-vars",
                 "--set-secrets", "--min-instances", "--max-instances", "--timeout", "--memory"}
DEPLOY_SWITCHES = {"--no-traffic", "--no-cpu-throttling", "--no-invoker-iam-check"}
DEPLOY_ONLY_ON = {"--remove-env-vars": "f42-agent", "--no-cpu-throttling": "f42-agent", "--memory": "f42-agent", "--no-invoker-iam-check": "f42-api"}
DEPLOY_NUMBERS = ("--min-instances", "--max-instances", "--timeout")


def _deploy_flags(tokens):
    """{flag: value} for a list of arguments made only of allowed flags used once, else None."""
    found, i = {}, 0
    while i < len(tokens):
        name, equals, value = tokens[i].partition("=")
        if not name.startswith("--") or name in found:
            return None
        if name in DEPLOY_SWITCHES and not equals:
            found[name], i = True, i + 1
        elif name in DEPLOY_VALUED and equals:
            found[name], i = value, i + 1
        elif name in DEPLOY_VALUED:
            if i + 1 >= len(tokens) or tokens[i + 1].startswith("--"):
                return None
            found[name], i = tokens[i + 1], i + 2
        else:
            return None
    return found


def deploy_call_allowed(argv):
    """True when a gcloud call recorded from deploy_candidate.sh (without the leading gcloud) is a no-traffic deploy of one of the
    two services made only of the flags in the positive list above, with the project and region pinned, the release tag as tag and
    revision suffix, an image by digest, and each service-specific flag on its own service. The one flag that removes environment
    variables is allowed on f42-agent only, with a list of plain names (finding 3)."""
    a = list(argv)
    if len(a) < 3 or a[:2] != ["run", "deploy"] or a[2] not in SERVICES:
        return False
    flags = _deploy_flags(a[3:])
    if flags is None or flags.get("--project") != PROJECT or flags.get("--region") != REGION or "--no-traffic" not in flags:
        return False
    tag = flags.get("--tag", "")
    if not (RELEASE_ID.fullmatch(tag) and tag == flags.get("--revision-suffix")) or not IMAGE_BY_DIGEST.fullmatch(flags.get("--image", "")):
        return False
    if not flags.get("--service-account") or "--update-env-vars" not in flags:
        return False
    if any(DEPLOY_ONLY_ON[f] != a[2] for f in flags if f in DEPLOY_ONLY_ON):
        return False
    if any(not re.fullmatch(r"[0-9]+", flags[f]) for f in DEPLOY_NUMBERS if f in flags):
        return False
    return "--remove-env-vars" not in flags or bool(PLAIN_NAME_LIST.fullmatch(flags["--remove-env-vars"]))


# Release B, the jobs image release (W8-REL-B 3.4, 3.6): the same discipline for three actions. The update order is the
# contract's (jobs_only.UPDATE_ORDER) and is not a field of the context.

JOBS_REPO = jo.JOBS_REPO
JOB_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
CHAIN_ROLES = ("baseline", "first-b")
JOBS_RUN_ACTIONS = ("snapshot", "update", "rollback", "schema-receipt")
BUILD_BUCKET = "gs://ogilvy-trends-v2-f42-media-staging/build-source"
BUILDS_URL = f"https://cloudbuild.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/builds"


@dataclass(frozen=True)
class JobsCtx:
    """What a jobs plan is rendered from. The digests come from the frozen manifest and the bindings, never from a live job."""

    release_id: str
    commit: str
    helper: str
    runner: str
    bindings: str
    evidence: str
    digest: str
    rollback_digest: str
    image_tag: str
    archive: str = "<archive>"
    extract_dir: str = "<extract dir>"
    manifest: str = "<durable manifest>"
    schema_effects: bool = False


def _digest(value):
    if not isinstance(value, str) or not JOB_DIGEST.fullmatch(value):
        raise ValueError("a jobs digest is sha256: and 64 lowercase hex characters")
    return value


def jobs_helper(ctx, phase):
    return step(f"helper:{phase}", ["py", "-3.13", ctx.helper, "--mode", "jobs", "--phase", phase, "--bindings", ctx.bindings, "--evidence", ctx.evidence])


def jobs_run(ctx, action):
    return step(f"jobs_run:{action}", ["py", "-3.13", ctx.runner, action, "--bindings", ctx.bindings, "--evidence", ctx.evidence])


def job_update(job, digest, name):
    return step(name, ["gcloud", "run", "jobs", "update", job, "--image", f"{JOBS_REPO}@{_digest(digest)}", "--project", PROJECT, "--region", REGION,
                       "--quiet"])


def job_readback(job, name):
    return step(name, ["gcloud", "run", "jobs", "describe", job, "--project", PROJECT, "--region", REGION, "--format=json"])


def update_pairs(order, digest, label):
    """The 14 updates, each followed at once by the read of that job's description."""
    pairs = []
    for job in order:
        pairs += [job_update(job, digest, f"{label}:{job}"), job_readback(job, f"readback:{job}")]
    return pairs


def jobs_candidate(ctx):
    """3.4 JobsCandidate. The quiet snapshot and the window come before the schema apply (JU-07)."""
    steps = [
        step("assert:head", ["git", "rev-parse", "HEAD"]), step("assert:tree", ["git", "rev-parse", "HEAD^{tree}"]),
        step("assert:status", ["git", "status", "--porcelain=v1"]),
        step("assert:identity", ["gcloud", "config", "list", f"--format={IDENTITY_FIELDS}"]),
        jobs_helper(ctx, "BeforeAnyWrite"),
        step("archive", ["git", "archive", "--format=tar", f"--output={ctx.archive}", ctx.commit]),
        step("extract", [SYSTEM_TAR, "-xf", ctx.archive, "-C", ctx.extract_dir]),
        step("build:upload", ["gcloud", "storage", "cp", ctx.archive, f"{BUILD_BUCKET}/jobs-{ctx.commit[:12]}-{ctx.release_id[-2:]}.tar.gz",
                              "--no-clobber", f"--project={PROJECT}"]),
        step("build:create", ["POST", BUILDS_URL]),
        jobs_helper(ctx, "FreezeJobs"),
        checker(_services_view(ctx), "validate"),
        step("snapshot", ["py", "-3.13", ctx.runner, "snapshot", "--bindings", ctx.bindings, "--evidence", ctx.evidence]),
    ]
    if ctx.schema_effects:
        steps.append(step("schema_apply", ["py", "-3.13", "-m", "core.schema.apply", "--apply"]))
    steps.append(checker(_services_view(ctx), "readbacks"))
    steps.append(jobs_run(ctx, "schema-receipt"))
    return steps


def _services_view(ctx):
    return Ctx(release_id=ctx.release_id, commit=ctx.commit, helper=ctx.helper, bindings=ctx.bindings, evidence=ctx.evidence,
               manifest=ctx.manifest, manifest_sha256="0" * 64, api_tag_url="", a80={}, image_tag=ctx.image_tag)


def jobs_update(ctx):
    """3.4 JobsUpdate: one snapshot read before the words, the 14 updates through the orchestrator, then the readback."""
    wrapper = dataclasses.replace(jobs_run(ctx, "update"), name="jobs_run:update")
    pairs = [dataclasses.replace(s, via="jobs_run") for s in update_pairs(jo.UPDATE_ORDER, ctx.digest, "update")]
    return [jobs_helper(ctx, "BeforeJobsUpdate"), wrapper, *pairs, jobs_helper(ctx, "AfterJobsUpdate")]


def jobs_rollback(ctx):
    """3.9 JobsRollback: collect first, watchdog last, each with the rollback digest, and nothing typed."""
    wrapper = jobs_run(ctx, "rollback")
    pairs = [dataclasses.replace(s, via="jobs_run") for s in update_pairs(tuple(reversed(jo.UPDATE_ORDER)), ctx.rollback_digest, "restore")]
    return [jobs_helper(ctx, "BeforeJobsRollback"), wrapper, *pairs, jobs_helper(ctx, "AfterJobsRollback")]


JOBS_ACTIONS = {"JobsCandidate": jobs_candidate, "JobsUpdate": jobs_update, "JobsRollback": jobs_rollback}


def _jobs_helper(a):
    return (len(a) == 11 and a[:2] == ["py", "-3.13"] and a[2].endswith("bound_readback.py") and a[3:6] == ["--mode", "jobs", "--phase"]
            and a[6] in jo.PHASES and a[7] == "--bindings" and a[9] == "--evidence")


def _jobs_update(a):
    return (len(a) == 12 and a[:4] == ["gcloud", "run", "jobs", "update"] and a[4] in so_jobs() and a[5] == "--image"
            and bool(re.fullmatch(rf"{re.escape(JOBS_REPO)}@sha256:[0-9a-f]{{64}}", a[6])) and a[7:] == ["--project", PROJECT, "--region", REGION, "--quiet"])


def so_jobs():
    from core.setup.release.services_only import JOB_NAMES

    return JOB_NAMES


def _jobs_read(a):
    return len(a) == 10 and a[:4] == ["gcloud", "run", "jobs", "describe"] and a[4] in so_jobs() and a[5:] == ["--project", PROJECT, "--region", REGION, "--format=json"]


def _executions_read(a):
    tail = ["--project", PROJECT, "--region", REGION, "--format=json"]
    if len(a) == 12 and a[:5] == ["gcloud", "run", "jobs", "executions", "list"] and a[5] == "--job" and a[6] in so_jobs() and a[7:] == tail:
        return True
    return (len(a) == 11 and a[:5] == ["gcloud", "run", "jobs", "executions", "describe"] and bool(re.fullmatch(r"f42-[a-z0-9-]+", a[5]))
            and a[6:] == tail)


def _chain_evidence(a):
    return (len(a) == 11 and a[:3] == ["py", "-3.13", "core/setup/release/chain_evidence.py"] and a[3] == "--date"
            and bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", a[4])) and a[5] == "--bindings" and a[7] == "--evidence" and a[9] == "--expect"
            and a[10] in CHAIN_ROLES)


def _jobs_run(a):
    return (len(a) == 8 and a[:2] == ["py", "-3.13"] and a[2].endswith("jobs_run.py") and a[3] in JOBS_RUN_ACTIONS and a[4] == "--bindings"
            and a[6] == "--evidence")


def _jobs_build(a):
    if len(a) == 7 and a[:3] == ["gcloud", "storage", "cp"]:
        return (bool(re.fullmatch(rf"{re.escape(BUILD_BUCKET)}/jobs-[0-9a-f]{{12}}-[0-9]{{2}}\.tar\.gz", a[4])) and a[5:] == ["--no-clobber", f"--project={PROJECT}"])
    return a == ["POST", BUILDS_URL]


# The positive list of the jobs actions. It is its own table: the services allowlist above stays services only, so the services
# paste can never pass a jobs update, and the jobs paste can never pass a traffic change or a services phase.
JOBS_ALLOWED = {
    "git_read": ALLOWED["git_read"], "archive": ALLOWED["archive"], "identity": ALLOWED["identity"], "checker": ALLOWED["checker"],
    "schema_apply": ALLOWED["schema_apply"],
    "helper": _jobs_helper, "jobs_update": _jobs_update, "jobs_read": _jobs_read, "executions_read": _executions_read,
    "chain_evidence": _chain_evidence, "jobs_run": _jobs_run, "jobs_build": _jobs_build,
}


def jobs_matching_entries(argv):
    argv = list(argv)
    return [name for name, accepts in JOBS_ALLOWED.items() if accepts(argv)]


def check_jobs_allowlist(steps):
    """Every external invocation of a jobs action matches exactly one entry; the first that does not is returned, else None."""
    for s in steps:
        if len(jobs_matching_entries(s.argv)) != 1:
            return s
    return None


def executions_list(job, name):
    return step(name, ["gcloud", "run", "jobs", "executions", "list", "--job", job, "--project", PROJECT, "--region", REGION, "--format=json"])


def execution_describe(execution, name):
    return step(name, ["gcloud", "run", "jobs", "executions", "describe", execution, "--project", PROJECT, "--region", REGION, "--format=json"])
