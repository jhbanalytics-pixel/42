"""A simulated Cloud Run world and a fake Reader for the services-only readback tests (no cloud, no network).

The world holds descriptions in the knative v1 shape `gcloud run ... describe --format=json` returns. Mutators move it
through a release the way the real commands would: Pin, candidate deploy at 0%, promotion of the agent and then the API
by name, rollback by name, tag removal. A test walks a Scenario to a phase, breaks one thing, and runs the next phase.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from pathlib import Path

from core.setup.release import services_only as so

PROJECT = so.PROJECT
REPO = so.REPO
COMMIT = "d666ef64cd7a0123456789abcdef0123456789ab"
SHORT12 = COMMIT[:12]
TREE = "9aaee874e0e06e54d3354ae58bf6e576685b5303"
RID = "rel-d666ef6-01"
A80_VERSION = "a80be1dee7f4"
A80_DIGEST = "sha256:" + "a80d" + "0" * 60
CAND_DIGEST = "sha256:" + "c4" + "0" * 62
OTHER_DIGEST = "sha256:" + "ee" + "0" * 62
JOBS_DIGEST = "sha256:" + "e77c" + "0" * 60
CANON = {"f42-agent": "https://f42-agent-fibxg5ynpq-uc.a.run.app", "f42-api": "https://f42-api-fibxg5ynpq-uc.a.run.app"}
A80_REV = {"f42-agent": "f42-agent-00047-677", "f42-api": "f42-api-00041-lns"}
OLDER_REV = {"f42-agent": "f42-agent-00046-wks", "f42-api": "f42-api-00040-cw5"}
CAND_REV = {name: f"{name}-{RID}" for name in so.SERVICES}
TAG_URL = {name: so.tag_url(RID, CANON[name]) for name in so.SERVICES}
CALLER = "jhb.analytics@gmail.com"
BUILD_SA = "projects/ogilvy-trends-v2/serviceAccounts/f42-deployer@ogilvy-trends-v2.iam.gserviceaccount.com"
BUILD_ID = "9f00e5ce-3375-45fb-b557-c5b937acc59f"
UPLOADED = "gs://ogilvy-trends-v2-f42-media-staging/build-source/rel.tgz"
CONFIG_SHA = "c0" * 32
SECRET = {"valueFrom": {"secretKeyRef": {"name": "SECRET", "key": "latest"}}}


# Names that stand in for the two declared ones in every behaviour test. A test makes them declared by patching the digest
# table (synthetic_digests); only the one test that ties the real digests to the real names reads the names file.
SYNTHETIC_DECLARED = ("F42_SYNTHETIC_DECLARED_ONE", "F42_SYNTHETIC_DECLARED_TWO")


def synthetic_digests():
    return {"f42-agent": tuple(hashlib.sha256(name.encode("utf-8")).hexdigest() for name in SYNTHETIC_DECLARED)}


def real_declared_names():
    """The environment variable names Release A declares removed, one per line, from a file outside the repository
    (F42_DECLARED_ENV_NAMES_FILE, else wave8/release-packet/declared-env-removals.txt beside the repository). Empty when
    the file is absent."""
    import os

    path = Path(os.environ.get("F42_DECLARED_ENV_NAMES_FILE") or Path(__file__).resolve().parents[4] / "42-handoff" / "wave8" / "release-packet" / "declared-env-removals.txt")
    return tuple(line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()) if path.is_file() else ()


REAL_NAMES = real_declared_names()
AGENT_ENV = {"APP_MODULE": "core.api.agent_app:app", "F42_DATA": "bigquery", "F42_PROJECT": PROJECT, "F42_VERSION": A80_VERSION,
             "MODEL_PROVIDER": "gemini", "GEMINI_MODEL": "gemini-3.8-flash", "F42_T2_READY": "1",
             "SOCIALCRAWL_OGILVY_API_KEY": SECRET}
API_ENV = {"APP_MODULE": "core.api.app:app", "F42_DATA": "bigquery", "F42_PROJECT": PROJECT, "F42_VERSION": A80_VERSION,
           "AGENT_URL": CANON["f42-agent"], "UI_PASSCODE": SECRET}
SA = {"f42-agent": "f42-agent@ogilvy-trends-v2.iam.gserviceaccount.com", "f42-api": "f42-web@ogilvy-trends-v2.iam.gserviceaccount.com"}
SCALE = {"f42-agent": {"autoscaling.knative.dev/minScale": "1", "autoscaling.knative.dev/maxScale": "1",
                       "run.googleapis.com/cpu-throttling": "false"},
         "f42-api": {"autoscaling.knative.dev/minScale": "1", "autoscaling.knative.dev/maxScale": "3"}}
RESOURCES = {"f42-agent": {"limits": {"cpu": "1000m", "memory": "1Gi"}}, "f42-api": {"limits": {"cpu": "1000m", "memory": "512Mi"}}}
ACCESS = {"f42-agent": {"run.googleapis.com/ingress": "all"},
          "f42-api": {"run.googleapis.com/ingress": "all", "run.googleapis.com/invoker-iam-disabled": "true"}}
POLICY = {"bindings": [{"role": "roles/run.invoker", "members": ["serviceAccount:f42-web@ogilvy-trends-v2.iam.gserviceaccount.com"]}]}
READY = [{"type": "Ready", "status": "True"}]
HEALTHY = (200, {"ok": True, "checks": {"agent": "ok"}, "version": SHORT12})


def env_entries(env):
    return [{"name": k, **(v if isinstance(v, dict) else {"value": v})} for k, v in env.items()]


def revision_raw(name, service, image, digest, env, annotations=None):
    return {"metadata": {"name": name, "annotations": {**SCALE[service], **(annotations or {})}},
            "spec": {"containers": [{"image": image, "env": env_entries(env), "resources": copy.deepcopy(RESOURCES[service])}],
                     "serviceAccountName": SA[service], "timeoutSeconds": 3600},
            "status": {"imageDigest": f"{REPO}@{digest}", "conditions": copy.deepcopy(READY)}}


def job_raw(name):
    return {"metadata": {"name": name, "annotations": {}},
            "spec": {"template": {"spec": {"parallelism": 1, "taskCount": 1, "template": {
                "metadata": {"annotations": {}},
                "spec": {"containers": [{"image": f"{so.REGION}-docker.pkg.dev/{PROJECT}/intelligence-42/jobs@{JOBS_DIGEST}",
                                         "command": ["python"], "args": ["-m", name], "env": env_entries({"F42_PROJECT": PROJECT, "S": SECRET}),
                                         "resources": {"limits": {"cpu": "1000m", "memory": "2Gi"}}}],
                         "serviceAccountName": "f42-jobs@ogilvy-trends-v2.iam.gserviceaccount.com", "timeoutSeconds": 3600,
                         "maxRetries": 0}}}}}}


class World:
    """The services, their revisions, the jobs and everything a Reader can ask about."""

    def __init__(self, declared=()):
        """declared: environment names f42-agent carries on every live revision (the ones a test declares removed)."""
        self.svc, self.revisions, self.revision_names = {}, {}, {}
        for name, env in (("f42-agent", {**AGENT_ENV, **{n: PROJECT for n in declared}}), ("f42-api", API_ENV)):
            rev = A80_REV[name]
            self.revisions[rev] = revision_raw(rev, name, f"{REPO}:{A80_VERSION}", A80_DIGEST, env)
            self.revisions[OLDER_REV[name]] = revision_raw(OLDER_REV[name], name, f"{REPO}:old", OTHER_DIGEST, env)
            self.revision_names[name] = [OLDER_REV[name], rev]
            self.svc[name] = {"annotations": dict(ACCESS[name]), "tmpl_ann": dict(SCALE[name]), "image": f"{REPO}:{A80_VERSION}",
                              "env": copy.deepcopy(env), "traffic": [{"latestRevision": True, "percent": 100}],
                              "latest_created": rev, "latest_ready": rev, "sa": SA[name], "timeout": 3600,
                              "url": CANON[name]}
        self.jobs = {name: job_raw(name) for name in so.JOB_NAMES}
        self.policy = copy.deepcopy(POLICY)
        self.config = {"core": {"account": CALLER, "project": PROJECT}}
        self.principals_ok = True
        self.registry, self.builds = {}, {}
        self.source_sha = CONFIG_SHA
        self.anonymous = {}
        self.health = {url: [HEALTHY] for url in (CANON["f42-api"], TAG_URL["f42-api"])}

    # description builders ------------------------------------------------------------------------------------------
    def service_raw(self, name):
        st = self.svc[name]
        status_traffic = []
        for entry in st["traffic"]:
            shown = dict(entry)
            if entry.get("latestRevision"):
                shown["revisionName"] = st["latest_ready"]
            if entry.get("tag"):
                shown["url"] = entry.get("_url") or so.tag_url(entry["tag"], CANON[name])
            shown.pop("_url", None)
            status_traffic.append(shown)
        return {"metadata": {"name": name, "annotations": dict(st["annotations"])},
                "spec": {"template": {"metadata": {"annotations": dict(st["tmpl_ann"])},
                                      "spec": {"containers": [{"image": st["image"], "env": env_entries(st["env"]),
                                                               "resources": copy.deepcopy(RESOURCES[name])}],
                                               "serviceAccountName": st["sa"], "timeoutSeconds": st["timeout"]}},
                         "traffic": [{k: v for k, v in e.items() if k != "_url"} for e in st["traffic"]]},
                "status": {"url": st["url"], "traffic": status_traffic, "latestCreatedRevisionName": st["latest_created"],
                           "latestReadyRevisionName": st["latest_ready"], "conditions": copy.deepcopy(READY)}}

    # mutators, the way the real commands change the world ------------------------------------------------------------
    def pin(self, name=None):
        for n in [name] if name else so.SERVICES:
            self.svc[n]["traffic"] = [{"revisionName": A80_REV[n], "percent": 100}, *[e for e in self.svc[n]["traffic"] if e.get("tag")]]

    def deploy_candidate(self, name, *, env_override=None, remove_env=(), digest=CAND_DIGEST, rid=RID):
        st = self.svc[name]
        env = copy.deepcopy(st["env"])
        env["F42_VERSION"] = SHORT12
        if name == "f42-api":
            env["AGENT_URL"] = so.tag_url(rid, CANON["f42-agent"])
            env["AGENT_AUDIENCE"] = CANON["f42-agent"]
        for key in remove_env:
            env.pop(key, None)
        env.update(env_override or {})
        rev = f"{name}-{rid}"
        self.revisions[rev] = revision_raw(rev, name, f"{REPO}@{digest}", digest, env)
        st.update(image=f"{REPO}@{digest}", env=env, latest_created=rev, latest_ready=rev)
        st["traffic"].append({"revisionName": rev, "percent": 0, "tag": rid})
        self.revision_names[name].append(rev)

    def promote(self, name, rid=RID):
        rev = f"{name}-{rid}"
        self.svc[name]["traffic"] = [{"revisionName": rev, "percent": 100, "tag": rid}]

    def restore(self, name, keep_tag=True):
        tags = [e for e in self.svc[name]["traffic"] if e.get("tag") and keep_tag]
        self.svc[name]["traffic"] = [{"revisionName": A80_REV[name], "percent": 100},
                                     *[{**e, "percent": 0} for e in tags if e["revisionName"] != A80_REV[name]]]

    def remove_tag(self, name, tag=RID):
        st = self.svc[name]
        st["traffic"] = [e for e in st["traffic"] if e.get("tag") != tag]
        if not any(e.get("percent") for e in st["traffic"]):
            st["traffic"].insert(0, {"revisionName": A80_REV[name], "percent": 100})

    def set_env(self, revision, name, value):
        for entry in self.revisions[revision]["spec"]["containers"][0]["env"]:
            if entry["name"] == name:
                entry.clear()
                entry.update({"name": name, **(value if isinstance(value, dict) else {"value": value})})
                return
        self.revisions[revision]["spec"]["containers"][0]["env"].append({"name": name, **(value if isinstance(value, dict) else {"value": value})})

    def drop_env(self, revision, name):
        env = self.revisions[revision]["spec"]["containers"][0]["env"]
        env[:] = [e for e in env if e["name"] != name]


class FakeReader:
    """Answers every question from the world and records each call. It has no way to write."""

    def __init__(self, world):
        self.world, self.calls = world, []

    def _log(self, *args):
        self.calls.append(args)

    def config(self):
        self._log("config")
        return copy.deepcopy(self.world.config)

    def service(self, name):
        self._log("service", name)
        return self.world.service_raw(name)

    def revision(self, name):
        self._log("revision", name)
        if name not in self.world.revisions:
            raise so.NotFound(name)
        return copy.deepcopy(self.world.revisions[name])

    def revisions(self, service):
        self._log("revisions", service)
        return list(self.world.revision_names[service])

    def job(self, name):
        self._log("job", name)
        return copy.deepcopy(self.world.jobs[name])

    def policy(self):
        self._log("policy")
        return copy.deepcopy(self.world.policy)

    def build(self, build_id):
        self._log("build", build_id)
        if build_id not in self.world.builds:
            raise so.NotFound(build_id)
        return copy.deepcopy(self.world.builds[build_id])

    def registry_digest(self, tag):
        self._log("registry_digest", tag)
        return self.world.registry.get(tag)

    def source_file_sha256(self, commit, path):
        self._log("source_file_sha256", commit, path)
        return self.world.source_sha

    def anonymous_status(self, url):
        self._log("anonymous_status", url)
        return self.world.anonymous.get(url, 403)

    def health(self, url):
        self._log("health", url)
        sequence = self.world.health[url]
        shown = sequence.pop(0) if len(sequence) > 1 else sequence[0]
        return shown

    def principals(self, bound):
        self._log("principals")
        if not self.world.principals_ok:
            raise so.Stop("IDENTITY", "unverifiable")
        return {"verified_principals_match": True}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Scenario:
    """One release attempt. Walk it with to(<phase>), change the world, then run the phase under test."""

    ORDER = ("BeforeAnyWrite", "Freeze", "BeforeCandidate", "BeforeSmoke", "AfterSmoke", "BeforePromotion",
             "AfterAgentPromotion", "AfterPromotion")

    def __init__(self, tmp_path, *, bound_over=None, world=None, prior=None, release_id=RID, retained=None, baseline_from=None):
        self.tmp = Path(tmp_path)
        self.world = world or World()
        self.reader = FakeReader(self.world)
        self.rid = release_id
        self.release_dir = self.tmp / "releases" / release_id
        self.evidence = self.tmp / "run"
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.release_dir.mkdir(parents=True, exist_ok=True)
        if baseline_from is None:
            baseline = so.capture_baseline(self.reader, "2026-10-08T10:00:00+00:00")
            baseline_sha = write_json(self.tmp / "baseline" / "baseline-A.json", baseline)
            self.baseline_path = self.tmp / "baseline" / "baseline-A.json"
        else:
            self.baseline_path, baseline_sha = baseline_from.baseline_path, baseline_from.bound["baselineSha256"]
        compat = write_json(self.release_dir / "compat-receipt.json", {"schema_version": 1, "verdict": "pass"})
        old = write_json(self.release_dir / "old-reader-receipt.json", {"schema_version": 1, "verdict": "pass"})
        self.sleeps = []
        self.now = dt.datetime(2026, 10, 9, 8, 0, tzinfo=dt.timezone.utc)
        agent = CANON["f42-agent"]
        self.bound = {
            "schema_version": 1, "mode": "services-only", "release_id": release_id, "target": COMMIT, "tree": TREE,
            "releaseDir": str(self.release_dir), "baselinePath": str(self.baseline_path),
            "baselineSha256": baseline_sha, "callerAccount": CALLER, "buildServiceAccount": BUILD_SA,
            "buildConfigSha256": CONFIG_SHA, "readTimeoutSeconds": {"gcloud": 120, "http": 30}, "maxSmokeAgeMinutes": 360,
            "expectedSmokeChecks": 6,
            "envDeltas": {"f42-agent": {"F42_VERSION": SHORT12},
                          "f42-api": {"F42_VERSION": SHORT12, "AGENT_URL": so.tag_url(release_id, agent), "AGENT_AUDIENCE": agent}},
            "compatReceiptSha256": compat, "oldReaderReceiptSha256": old, "durableManifestSha256": "d0" * 32,
        }
        if prior:
            self.bound["priorAttempts"] = prior
        if retained:
            self.bound["retainedTags"] = retained
        self.bound.update(bound_over or {})

    # running -----------------------------------------------------------------------------------------------------------
    def run(self, phase, tag=None):
        return so.run_phase(self.bound, phase, self.reader, self.evidence, tag=tag, now=lambda: self.now, sleep=self.sleeps.append)

    def tag(self):
        return so.image_tag(self.bound)

    def build_world_for_freeze(self):
        self.world.registry[self.tag()] = CAND_DIGEST
        self.world.builds[BUILD_ID] = {
            "id": BUILD_ID, "status": "SUCCESS", "serviceAccount": BUILD_SA, "createTime": "2026-10-08T21:00:00+00:00",
            "substitutions": {"_IMAGE": self.tag()},
            "source": {"storageSource": {"bucket": "ogilvy-trends-v2-f42-media-staging", "object": "build-source/rel.tgz"}},
            "results": {"images": [{"name": self.tag(), "digest": CAND_DIGEST}]}}
        (self.evidence / "freeze-inputs.json").write_text(json.dumps(
            {"schema_version": 1, "build_id": BUILD_ID, "uploaded_source": UPLOADED, "paste_started_utc": "2026-10-08T20:55:00+00:00"}), encoding="utf-8")

    def write_smoke_receipt(self, **over):
        manifest = json.loads((self.release_dir / "release-manifest.json").read_text(encoding="utf-8"))
        receipt = {"schema_version": 1, "release_id": self.rid, "argv_url": so.tag_url(self.rid, CANON["f42-api"]),
                   "manifest_sha256": manifest["manifest_sha256"], "candidates": {n: f"{n}-{self.rid}" for n in so.SERVICES}, "started_utc": "2026-10-09T07:00:00+00:00", "ended_utc": "2026-10-09T07:30:00+00:00",
                   "exit_code": 0, "checks_total": 6, "checks_passed": 6, "log_sha256": "ab" * 32}
        receipt.update(over)
        path = self.release_dir / "smoke-receipt.json"
        if path.exists():
            path.unlink()
        write_json(path, receipt)

    def manifest(self):
        return json.loads((self.release_dir / "release-manifest.json").read_text(encoding="utf-8"))

    def rewrite_manifest(self, **changes):
        """Rewrite the manifest and its sidecar together, as someone editing the file after Freeze would."""
        manifest = self.manifest()
        manifest.update(changes)
        manifest["manifest_sha256"] = so.manifest_hash(manifest)
        write_json(self.release_dir / "release-manifest.json", manifest)
        (self.release_dir / "release-manifest.sha256").write_text(manifest["manifest_sha256"] + "\n", encoding="utf-8")

    # the happy path ------------------------------------------------------------------------------------------------------
    def step(self, phase):
        """Make the world what the real commands before this phase leave it."""
        {
            "BeforeAnyWrite": lambda: None,
            "Freeze": self.build_world_for_freeze,
            "BeforeCandidate": self.world.pin,
            "BeforeSmoke": lambda: [self.world.deploy_candidate(n, rid=self.rid) for n in so.SERVICES],
            "AfterSmoke": self.write_smoke_receipt,
            "BeforePromotion": lambda: None,
            "AfterAgentPromotion": lambda: self.world.promote("f42-agent", self.rid),
            "AfterPromotion": lambda: self.world.promote("f42-api", self.rid),
        }[phase]()

    def to(self, phase):
        """Walk the happy path through phase, running every phase on the way, and return its readback."""
        result = None
        for name in self.ORDER:
            self.step(name)
            result = self.run(name)
            if name == phase:
                return result
        raise AssertionError(phase)

    def ready(self, phase):
        """Walk the happy path up to the phase before it and make the world ready for it, without running it."""
        for name in self.ORDER[:self.ORDER.index(phase)]:
            self.step(name)
            self.run(name)
        self.step(phase)
        return self

    def stop(self, phase, tag=None):
        try:
            self.run(phase, tag)
        except so.Stop as error:
            return error
        raise AssertionError(f"{phase} did not stop")
