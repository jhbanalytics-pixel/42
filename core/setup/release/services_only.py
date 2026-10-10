"""Services-only release readbacks (W8-REL 2.5 to 2.9, 3.4, 3.5). Read only: nothing here writes to a cloud resource.

    run_phase(bound, phase, reader, evidence, tag=None, now=None, sleep=time.sleep) -> the readback, or Stop or Probe

A Reader (core/setup/release/bound_readback.py holds the real one, the tests hold a fake) answers every question from
a native system. The checks compare what it says with values the release pinned before it started: the bindings, the
bound baseline receipt, and the Stage 1 fields re-derived from both on every phase. Nothing is trusted because the
file under test says so: the manifest never supplies an expectation (MANIFEST_STAGE1), a digest is accepted only
when the build result, the registry and the revision agree, and a value the system echoes about itself (the served
version) is an observation, never an admission.

Stop is a failed check (helper exit 1). Probe is a read that could not complete (exit 3). They are never the same.
Raw descriptions and environment values stay in memory: only hashes and names reach a readback.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
import time
from pathlib import Path

from core.setup.release.declared_env_removals import declared_removal

SCHEMA_VERSION = 1
PROJECT = "ogilvy-trends-v2"
REGION = "us-central1"
SERVICES = ("f42-agent", "f42-api")
REPO = f"{REGION}-docker.pkg.dev/{PROJECT}/intelligence-42/f42-web"
JOB_NAMES = ("f42-probe", "f42-gdelt", "f42-gdelt-daily", "f42-collect", "f42-understand", "f42-detect", "f42-brief",
             "f42-reconcile", "f42-watchdog", "f42-drift", "f42-learn", "f42-calendar", "f42-digest",
             "f42-scheduled-asks")
PHASES = ("BeforeAnyWrite", "Freeze", "BeforeCandidate", "BeforeSmoke", "AfterSmoke", "BeforePromotion",
          "AfterAgentPromotion", "AfterPromotion", "BeforeRollback", "AfterRollback", "BeforeRetire", "AfterRetire")
PRINCIPAL_PHASES = ("BeforeAnyWrite", "BeforeCandidate", "BeforePromotion", "BeforeRollback")
RETIRE_PHASES = ("BeforeRetire", "AfterRetire")
RELEASE_ID = re.compile(r"rel-[0-9a-f]{7}-[0-9]{2}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
ACCESS_ANNOTATIONS = ("run.googleapis.com/ingress", "run.googleapis.com/invoker-iam-disabled")
SCALING_ANNOTATIONS = ("autoscaling.knative.dev/minScale", "autoscaling.knative.dev/maxScale",
                       "run.googleapis.com/cpu-throttling")
MANAGED_ANNOTATIONS = {"client.knative.dev/user-image", "run.googleapis.com/client-name",
                       "run.googleapis.com/client-version", "run.googleapis.com/operation-id"}
HEALTH_RETRIES = 3
HEALTH_RETRY_SECONDS = 10


class Stop(Exception):
    """A check failed. The code is stable and printed after STOP:."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class Probe(Exception):
    """A read could not complete: it timed out, stalled or came back unreadable."""


class NotFound(Exception):
    """The resource a read asked for does not exist."""


def require(ok, code, message):
    if not ok:
        raise Stop(code, message)


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(value):
    return sha_bytes(canonical(value).encode("utf-8"))


def sha_text(text):
    return sha_bytes(text.encode("utf-8"))


def literal(value):
    """How a non-secret environment literal appears in a view: its hash, never the text."""
    return {"value_sha256": sha_text(value)}


def manifest_hash(manifest):
    return fingerprint({k: v for k, v in manifest.items() if k != "manifest_sha256"})


# views: the parts of a Cloud Run description the checks compare, with environment values hashed

def env_map(entries):
    out = {}
    for entry in entries or []:
        name = entry["name"]
        require(name not in out, "ENV", "Duplicate environment name")
        if "valueFrom" in entry:
            out[name] = {"valueFrom": entry["valueFrom"]}
        elif "value_sha256" in entry:
            out[name] = {"value_sha256": entry["value_sha256"]}
        else:
            out[name] = literal(str(entry.get("value", "")))
    return out


def _clean(annotations):
    return {k: v for k, v in (annotations or {}).items() if k not in MANAGED_ANNOTATIONS}


def _ready(status):
    ready = [c for c in status.get("conditions", []) if c.get("type") == "Ready"]
    return len(ready) == 1 and ready[0].get("status") in ("True", True)


def _entry(e):
    return {"revision": e.get("revisionName"), "percent": e.get("percent", 0), "tag": e.get("tag"), "url": e.get("url"),
            "latest": bool(e.get("latestRevision"))}


def service_view(raw):
    meta, spec, status = raw.get("metadata", {}), raw.get("spec", {}), raw.get("status", {})
    template = spec.get("template", {})
    task, tmeta = template.get("spec", {}), template.get("metadata", {})
    containers = task.get("containers", [])
    c = containers[0] if len(containers) == 1 else {}
    ann = meta.get("annotations", {}) or {}
    return {
        "name": meta.get("name"), "url": status.get("url"),
        "annotations": {k: ann.get(k) for k in ACCESS_ANNOTATIONS},
        "template": {"containers": len(containers), "image": c.get("image"), "env": env_map(c.get("env", [])),
                     "command": c.get("command", []), "args": c.get("args", []), "resources": c.get("resources", {}),
                     "service_account": task.get("serviceAccountName"), "timeout_seconds": str(task.get("timeoutSeconds")),
                     "annotations": _clean(tmeta.get("annotations")),
                     "volumes": bool(task.get("volumes") or c.get("volumeMounts"))},
        "traffic": {"spec": [_entry(e) for e in spec.get("traffic", [])],
                    "status": [_entry(e) for e in status.get("traffic", [])]},
        "latest_created": status.get("latestCreatedRevisionName"), "latest_ready": status.get("latestReadyRevisionName"),
        "ready": _ready(status),
    }


def revision_view(raw):
    meta, spec, status = raw.get("metadata", {}), raw.get("spec", {}), raw.get("status", {})
    containers = spec.get("containers", [])
    c = containers[0] if len(containers) == 1 else {}
    return {"name": meta.get("name"), "ready": _ready(status), "containers": len(containers), "image": c.get("image"),
            "image_digest": status.get("imageDigest"), "env": env_map(c.get("env", [])), "command": c.get("command", []),
            "args": c.get("args", []), "resources": c.get("resources", {}), "service_account": spec.get("serviceAccountName"),
            "timeout_seconds": str(spec.get("timeoutSeconds")), "annotations": _clean(meta.get("annotations"))}


def job_view(raw):
    meta, spec = raw.get("metadata", {}), raw.get("spec", {})
    outer = spec.get("template", {})
    inner = outer.get("spec", {}).get("template", {})
    task, tmeta = inner.get("spec", {}), inner.get("metadata", {})
    containers = task.get("containers", [])
    c = containers[0] if len(containers) == 1 else {}
    return {"name": meta.get("name"), "containers": len(containers), "image": c.get("image"), "command": c.get("command", []),
            "args": c.get("args", []), "env": env_map(c.get("env", [])), "resources": c.get("resources", {}),
            "service_account": task.get("serviceAccountName"), "timeout_seconds": str(task.get("timeoutSeconds")),
            "max_retries": task.get("maxRetries"), "parallelism": outer.get("spec", {}).get("parallelism"),
            "task_count": outer.get("spec", {}).get("taskCount"), "annotations": _clean(tmeta.get("annotations")),
            "job_annotations": _clean(meta.get("annotations"))}


def policy_public(policy):
    return any(m in ("allUsers", "allAuthenticatedUsers") for b in policy.get("bindings", [])
               if b.get("role") == "roles/run.invoker" for m in b.get("members", []))


def capture_baseline(reader, captured_utc):
    """The bound baseline receipt (kind baseline-A), read only, captured and hashed before review. It stores views,
    never raw descriptions, and the one literal the release must be able to restore: the serving API revision's
    AGENT_URL, which must equal the agent's status.url or a rollback would not restore the canonical URL."""
    services, canonical_urls, api_literal = {}, {}, None
    for name in SERVICES:
        raw = reader.service(name)
        view = service_view(raw)
        serving_entries = [e for e in view["traffic"]["status"] if e["percent"] > 0]
        require(len(serving_entries) == 1 and serving_entries[0]["percent"] == 100, "BASELINE",
                f"{name} does not serve one revision at 100%")
        serving_raw = reader.revision(serving_entries[0]["revision"])
        if name == "f42-api":
            entries = serving_raw["spec"]["containers"][0].get("env", [])
            api_literal = next((e.get("value") for e in entries if e["name"] == "AGENT_URL"), None)
        services[name] = {"annotations": view["annotations"], "template": view["template"], "traffic": view["traffic"],
                          "revisions": sorted(reader.revisions(name)), "serving": revision_view(serving_raw)}
        canonical_urls[name] = view["url"]
    require(isinstance(api_literal, str) and api_literal == canonical_urls["f42-agent"], "BASELINE",
            "The serving API AGENT_URL is not the agent status.url")
    return {"schema_version": SCHEMA_VERSION, "kind": "baseline-A", "captured_utc": captured_utc,
            "canonical": canonical_urls, "api_agent_url_literal": api_literal, "services": services,
            "jobs": {name: job_view(reader.job(name)) for name in JOB_NAMES},
            "agent_policy_sha256": fingerprint(reader.policy())}


# bindings, paths, Stage 1

def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


REQUIRED_BINDINGS = ("release_id", "target", "tree", "releaseDir", "baselinePath", "baselineSha256", "callerAccount",
                     "buildServiceAccount", "buildConfigSha256", "readTimeoutSeconds", "maxSmokeAgeMinutes",
                     "expectedSmokeChecks", "envDeltas", "compatReceiptSha256", "oldReaderReceiptSha256",
                     "durableManifestSha256")
PATH_KEYS = {"manifestPath": "release-manifest.json", "sidecarPath": "release-manifest.sha256",
             "ledgerPath": "tag-ledger.json", "smokeReceiptPath": "smoke-receipt.json",
             "compatReceiptPath": "compat-receipt.json", "oldReaderReceiptPath": "old-reader-receipt.json",
             "preservationPath": "preservation-state.json"}


def inside(root, path):
    """A durable path must lie under the release directory (HR-54); the evidence folder is never one."""
    resolved = Path(path).resolve()
    require(resolved == root or root in resolved.parents, "BINDINGS",
            f"A durable path is outside the release directory: {Path(path).name}")
    return resolved


# The a80 packet bound rollback targets from before a80: revisions that no longer serve, and the digests of the images
# they ran. Release A rebuilds its bindings from a fresh baseline and never carries them (PS-06).
STALE_TARGET_KEYS = ("rollbackRevision", "rollbackImageDigest")
STALE_TARGET_VALUES = ("f42-agent-00046-wks", "f42-api-00040-cw5", "sha256:6c7b718b", "sha256:17ad022e")


def stale_target(value):
    """The first stale rollback target found anywhere in the bindings, or None."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in STALE_TARGET_KEYS:
                return f"the key {key}"
            found = stale_target(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = stale_target(item)
            if found:
                return found
    elif isinstance(value, str) and any(old in value for old in STALE_TARGET_VALUES):
        return "a pre-a80 revision or digest"
    return None


def validate_bindings(bound):
    require(isinstance(bound, dict), "BINDINGS", "The bindings are not an object")
    require(bound.get("schema_version") == SCHEMA_VERSION, "SCHEMA_VERSION", "Unknown bindings schema_version")
    require(bound.get("mode") == "services-only", "MODE", "The bindings are not for services-only mode")
    for key in REQUIRED_BINDINGS:
        require(key in bound, "BINDINGS", f"The bindings lack {key}")
    found = stale_target(bound)
    require(found is None, "BINDINGS", f"The bindings carry {found}; Release A rebuilds its rollback targets from a fresh baseline")
    timeouts = bound["readTimeoutSeconds"]
    require(isinstance(timeouts, dict) and _positive(timeouts.get("gcloud")) and _positive(timeouts.get("http")),
            "BINDINGS", "readTimeoutSeconds must give a positive gcloud and http value")
    require(RELEASE_ID.match(str(bound["release_id"])), "BINDINGS", "The release id is not rel-<sha7>-<nn>")
    require(re.fullmatch(r"[0-9a-f]{40}", str(bound["target"])) and str(bound["release_id"])[4:11] == bound["target"][:7],
            "BINDINGS", "The release id does not name the target commit")
    require(_positive(bound["maxSmokeAgeMinutes"]) and isinstance(bound["expectedSmokeChecks"], int)
            and not isinstance(bound["expectedSmokeChecks"], bool), "BINDINGS",
            "maxSmokeAgeMinutes or expectedSmokeChecks is not usable")
    root = Path(bound["releaseDir"]).resolve()
    for key in PATH_KEYS:
        if key in bound:
            inside(root, bound[key])
    return root


def short12(bound):
    return bound["target"][:12]


def image_tag(bound):
    return f"{REPO}:{short12(bound)}-{bound['release_id'][-2:]}"


def tag_url(rid, canonical_url):
    return "https://" + rid + "---" + canonical_url[len("https://"):]


def load_bound_json(path, expected_sha, label):
    data = Path(path).read_bytes()
    require(sha_bytes(data) == expected_sha, "BINDINGS", f"Bound receipt hash changed: {label}")
    value = json.loads(data.decode("utf-8-sig"))
    require(isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION, "SCHEMA_VERSION",
            f"Unknown schema_version: {label}")
    return value


def env_with(base_env, deltas, removals=()):
    env = copy.deepcopy(base_env)
    for name, value in deltas.items():
        env[name] = literal(value)
    for name in removals:
        env.pop(name, None)
    return env


def candidate_deltas(bound, baseline, service, rid):
    deltas = dict(bound["envDeltas"].get(service, {}))
    require(deltas.get("F42_VERSION") == short12(bound), "BINDINGS", "The F42_VERSION delta is not the short commit")
    if service == "f42-api":
        agent = baseline["canonical"]["f42-agent"]
        require(set(deltas) == {"F42_VERSION", "AGENT_URL", "AGENT_AUDIENCE"}
                and deltas["AGENT_URL"] == tag_url(rid, agent) and deltas["AGENT_AUDIENCE"] == agent,
                "BINDINGS", "The API deltas are not the agent tag URL and the canonical audience")
    else:
        require(set(deltas) == {"F42_VERSION"}, "BINDINGS", "The agent takes only the version delta")
    return deltas


def expected_env_variants(baseline, bound, service, rid):
    """The one environment each candidate must carry: the baseline template plus the declared deltas, without the names
    the release deploy is declared to remove. The deploy removes them, so a candidate that still carries one is a stop."""
    deltas = candidate_deltas(bound, baseline, service, rid)
    base = baseline["services"][service]["template"]["env"]
    removals = tuple(sorted(n for n in base if declared_removal(service, n)))
    return [env_with(base, deltas, removals)], removals


def env_matches(actual, baseline, bound, service, rid):
    """True when the environment is the expected one: the baseline template plus the declared deltas, without the names
    declared as removed."""
    return actual == expected_env_variants(baseline, bound, service, rid)[0][0]


def stage1(bound, baseline):
    """Everything the release knows before any write, re-derived from the bindings and the bound baseline."""
    rid = bound["release_id"]
    services = baseline["services"]
    prior = bound.get("priorAttempts", [])
    candidates = {}
    for name in SERVICES:
        variants, removals = expected_env_variants(baseline, bound, name, rid)
        candidates[name] = {"revision": f"{name}-{rid}", "tag": rid, "tag_url": tag_url(rid, baseline["canonical"][name]),
                            "env_deltas": candidate_deltas(bound, baseline, name, rid),
                            "env_removals_declared_sha256": sorted(sha_text(n) for n in removals), "expected_env_sha256": fingerprint(variants[0])}
    return {
        "release_id": rid, "mode": "services-only",
        "source": {"commit": bound["target"], "tree": bound["tree"], "short7": bound["target"][:7], "short12": short12(bound)},
        "canonical": {"agent_url": baseline["canonical"]["f42-agent"], "api_url": baseline["canonical"]["f42-api"],
                      "api_agent_url_literal": baseline["api_agent_url_literal"]},
        "candidates": candidates,
        "allowed_revisions": {name: list(services[name]["revisions"]) + [r for p in prior for r in p.get("revisions", {}).get(name, [])]
                              for name in SERVICES},
        "rollback": {name: {"revision": services[name]["serving"]["name"], "image_digest": services[name]["serving"]["image_digest"]}
                     for name in SERVICES},
        "jobs": {"baseline_sha256": fingerprint(baseline["jobs"])},
        "build_expect": {"service_account": bound["buildServiceAccount"], "config_sha256": bound["buildConfigSha256"],
                         "image_tag": image_tag(bound)},
        "prior_attempts": prior, "prior_ledgers": bound.get("priorLedgers", []),
        "compat": {"receipt_sha256": bound["compatReceiptSha256"]},
        "old_readers": {"receipt_sha256": bound["oldReaderReceiptSha256"]},
        "durable": {"manifest_sha256": bound["durableManifestSha256"]},
        "smoke": {"expected_checks": bound["expectedSmokeChecks"]},
    }


STAGE1_KEYS = ("release_id", "mode", "source", "canonical", "candidates", "allowed_revisions", "rollback", "jobs",
               "build_expect", "prior_attempts", "prior_ledgers", "compat", "old_readers", "durable", "smoke")


def save_once(path, value):
    path = Path(path)
    if path.exists():
        require(json.loads(path.read_text(encoding="utf-8")) == value, "PRESERVATION", f"Preserved state changed: {path.name}")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as out:
            out.write(json.dumps(value, indent=2, sort_keys=True) + "\n")


# the release: bindings, baseline, manifest and live reads

class Release:
    def __init__(self, bound, reader, evidence=None, now=None, sleep=time.sleep):
        self.root = validate_bindings(bound)
        self.bound, self.reader, self.sleep, self.evidence = bound, reader, sleep, evidence
        self._now = now
        self.rid = bound["release_id"]
        self.baseline = load_bound_json(bound["baselinePath"], bound["baselineSha256"], "baseline")
        require(self.baseline.get("kind") == "baseline-A", "BASELINE", "The bound receipt is not a baseline")
        require(set(self.baseline["jobs"]) == set(JOB_NAMES), "BASELINE", "The baseline does not hold the fourteen jobs")
        self.stage1 = stage1(bound, self.baseline)
        self.observations: dict = {}
        self.blocking: dict = {}

    def now(self):
        return self._now() if callable(self._now) else (self._now or dt.datetime.now(dt.timezone.utc))

    def path(self, key):
        return inside(self.root, self.bound[key]) if key in self.bound else self.root / PATH_KEYS[key]

    def live(self):
        services = {name: service_view(self.reader.service(name)) for name in SERVICES}
        names = {name: list(self.reader.revisions(name)) for name in SERVICES}
        return Live(self, services, names)

    def manifest(self):
        path = self.path("manifestPath")
        require(path.exists(), "MANIFEST_HASH", "The manifest does not exist")
        manifest = json.loads(path.read_bytes().decode("utf-8"))
        require(manifest.get("schema_version") == SCHEMA_VERSION, "SCHEMA_VERSION", "Unknown manifest schema_version")
        sidecar = self.path("sidecarPath")
        require(sidecar.exists() and sidecar.read_text(encoding="utf-8").strip() == manifest.get("manifest_sha256"),
                "MANIFEST_HASH", "The manifest sidecar does not hold the manifest hash")
        require(manifest_hash(manifest) == manifest.get("manifest_sha256"), "MANIFEST_HASH",
                "The manifest hash does not match its content")
        for key in STAGE1_KEYS:
            require(manifest.get(key) == self.stage1[key], "MANIFEST_STAGE1",
                    f"Manifest field {key} differs from the one re-derived from the bindings and the baseline")
        return manifest

    def last_readback(self, phase):
        folder = self.root / "readbacks"
        found = sorted(folder.glob(f"{phase}-*.json")) if folder.exists() else []
        return json.loads(found[-1].read_text(encoding="utf-8")) if found else None


class Live:
    def __init__(self, release, services, revision_names):
        self.release, self.services, self.revision_names = release, services, revision_names
        self._revisions: dict = {}

    def revision(self, name):
        """The view of a revision described by name, or None when it does not exist."""
        if name not in self._revisions:
            try:
                self._revisions[name] = revision_view(self.release.reader.revision(name))
            except NotFound:
                self._revisions[name] = None
        return self._revisions[name]

    def serving(self, service):
        return [e for e in self.services[service]["traffic"]["status"] if e["percent"] > 0]

    def tag_entries(self, service, tag):
        return [e for e in self.services[service]["traffic"]["status"] if e["tag"] == tag]


# checks

def check_config(rel):
    config = rel.reader.config()
    require(config.get("core", {}).get("account") == rel.bound["callerAccount"], "IDENTITY", "The CLI caller changed")
    require(config.get("core", {}).get("project") == PROJECT, "IDENTITY", "The CLI project changed")
    require(not config.get("auth", {}).get("impersonate_service_account"), "IDENTITY", "CLI impersonation is set")


def check_principals(rel):
    try:
        rel.reader.principals(rel.bound)
    except (Stop, Probe):
        raise
    except Exception as error:
        raise Stop("IDENTITY", "Current identity validation failed: " + type(error).__name__) from None


def check_policy_and_access(rel, live, *, blocking=True):
    """C10: the agent policy equals the baseline with no public invoker, and access annotations equal the baseline."""
    policy = rel.reader.policy()
    drift = []
    if policy_public(policy):
        require(not blocking, "POLICY", "The agent has a public invoker binding")
        drift.append("public_invoker")
    if fingerprint(policy) != rel.baseline["agent_policy_sha256"]:
        require(not blocking, "POLICY", "The agent policy differs from the baseline")
        drift.append("policy_changed")
    for name in SERVICES:
        for key in ACCESS_ANNOTATIONS:
            if live.services[name]["annotations"][key] != rel.baseline["services"][name]["annotations"][key]:
                require(not blocking, "POLICY", f"An access annotation changed on {name}: {key.rsplit('/', 1)[-1]}")
                drift.append(f"{name}:{key.rsplit('/', 1)[-1]}")
    if drift:
        rel.blocking["policy"] = {"blocking": False, "drift": drift}


def check_anonymous(rel, url, *, blocking=True):
    """C11: an anonymous request is refused with 403. The path requested is /api/health as the a80 helper does; the
    check proves the IAM boundary rejects the caller before the application sees it, not that any route exists."""
    status = rel.reader.anonymous_status(url)
    if status != 403:
        require(not blocking, "AGENT_PUBLIC", "The private agent did not refuse an anonymous caller")
        rel.blocking.setdefault("agent_public", {"blocking": False, "statuses": []})["statuses"].append(status)
    return status


def check_jobs(rel, *, blocking=True):
    """C9: the fourteen jobs equal the baseline fingerprint."""
    changed = [name for name in JOB_NAMES if job_view(rel.reader.job(name)) != rel.baseline["jobs"][name]]
    if changed:
        require(not blocking, "JOB_CHANGED", "A job differs from its baseline: " + ", ".join(changed))
        rel.blocking["jobs"] = {"blocking": False, "changed": changed}


def baseline_name(rel, service):
    return rel.baseline["services"][service]["serving"]["name"]


def check_baseline_serving(rel, live, code):
    """The a80 serving revisions equal the baseline, exactly, and the API revision keeps its canonical URL."""
    for name in SERVICES:
        view = live.revision(baseline_name(rel, name))
        require(view is not None, code, f"The a80 revision of {name} is gone")
        require(view == rel.baseline["services"][name]["serving"], code, f"The a80 revision of {name} differs from the baseline")
    api = live.revision(baseline_name(rel, "f42-api"))
    require(api["env"].get("AGENT_URL") == literal(rel.baseline["api_agent_url_literal"]), code,
            "The a80 API revision AGENT_URL differs from the baseline literal")
    require("AGENT_AUDIENCE" not in api["env"], code, "The a80 API revision carries an audience variable")


def check_one_revision_at_100(rel, live, name, allowed):
    nonzero = live.serving(name)
    require(len(nonzero) == 1 and nonzero[0]["percent"] == 100, "TRAFFIC", f"{name} traffic is not one revision at 100%")
    require(nonzero[0]["revision"] in allowed, "TRAFFIC", f"{name} serves a revision this release does not know")
    return nonzero[0]["revision"]


def check_no_latest(live, name):
    view = live.services[name]
    require(not any(e["latest"] for e in view["traffic"]["status"] + view["traffic"]["spec"]), "TRAFFIC",
            f"{name} follows LATEST")


def check_no_tag_or_candidate(rel, live):
    """Nothing carries this release's tag, no tag is outside the retained set, and the revision set is the baseline's
    plus the declared prior attempts (C4, C8)."""
    retained = rel.bound.get("retainedTags", {})
    for name in SERVICES:
        for e in live.services[name]["traffic"]["status"] + live.services[name]["traffic"]["spec"]:
            if e["tag"]:
                require(e["tag"] != rel.rid, "TAG_MAPPING", f"{name} already holds the release tag")
                require(e["tag"] in retained.get(name, {}), "TAG_MAPPING", f"{name} carries a tag that no ledger retains")
        extra = sorted(set(live.revision_names[name]) - set(rel.stage1["allowed_revisions"][name]))
        require(not extra, "UNRELATED_REVISION", f"{name} has revisions outside the allowed set: {len(extra)}")


def check_template_baseline(rel, live):
    """The service template (the latest created revision) equals the baseline. After an earlier failed attempt it may
    carry that attempt's image and environment, so then only the rest is compared."""
    for name in SERVICES:
        got, want = live.services[name]["template"], rel.baseline["services"][name]["template"]
        for key in want:
            if rel.bound.get("priorAttempts") and key in ("image", "env"):
                continue
            require(got[key] == want[key], "BASELINE", f"The {name} template differs from the baseline: {key}")


def check_registry_tag(rel, manifest):
    """C3: the registry's digest for the image tag is the manifest digest."""
    got = rel.reader.registry_digest(image_tag(rel.bound))
    require(got == manifest["image"]["registry_digest"] == manifest["image"]["digest"], "TAG_MOVED",
            "The image tag no longer resolves to the manifest digest")


def check_receipts(rel, manifest):
    """C13: the compat and old-reader receipts equal the bound hashes and say pass."""
    for key, code, path_key, bound_key in (("compat", "COMPAT", "compatReceiptPath", "compatReceiptSha256"),
                                           ("old_readers", "OLD_READER", "oldReaderReceiptPath", "oldReaderReceiptSha256")):
        path = rel.path(path_key)
        require(path.exists(), code, f"The {key} receipt is missing")
        data = path.read_bytes()
        require(sha_bytes(data) == rel.bound[bound_key] == manifest[key]["receipt_sha256"], code,
                f"The {key} receipt hash differs from the bound one")
        receipt = json.loads(data.decode("utf-8"))
        require(receipt.get("schema_version") == SCHEMA_VERSION, "SCHEMA_VERSION", f"Unknown {key} receipt version")
        require(receipt.get("verdict") == "pass", code, f"The {key} receipt does not say pass")


def check_tags(rel, live):
    """C4: per service the manifest tag exists once, maps to the candidate revision, with the expected URL; no other tag
    exists but the retained ones. The traffic the tag carries is judged by check_traffic_candidate."""
    retained = rel.bound.get("retainedTags", {})
    for name in SERVICES:
        cand = rel.stage1["candidates"][name]
        mine = live.tag_entries(name, rel.rid)
        require(len(mine) == 1, "TAG_MAPPING", f"{name} does not hold the release tag exactly once")
        e = mine[0]
        require(e["revision"] == cand["revision"], "TAG_MAPPING", f"The {name} tag does not name the candidate revision")
        require(e["url"] == cand["tag_url"], "TAG_MAPPING", f"The {name} tag URL is not the expected one")
        for other in live.services[name]["traffic"]["status"] + live.services[name]["traffic"]["spec"]:
            if other["tag"] and other["tag"] != rel.rid:
                require(other["tag"] in retained.get(name, {}), "TAG_MAPPING", f"{name} carries an unexpected tag")


def check_revision_set(rel, live):
    """C8: the revision list is the baseline list, the declared prior attempts and the candidate."""
    for name in SERVICES:
        cand = rel.stage1["candidates"][name]["revision"]
        extra = sorted(set(live.revision_names[name]) - set(rel.stage1["allowed_revisions"][name]) - {cand})
        require(not extra, "UNRELATED_REVISION", f"{name} has revisions outside the allowed set: {len(extra)}")
        require(cand in live.revision_names[name], "CANDIDATE_REVISION", f"The {name} candidate revision does not exist")


def check_candidate_revisions(rel, live, manifest):
    """C5 and C6: each candidate is judged from `revisions describe <manifest candidate>` and from nothing else."""
    reference = f"{REPO}@{manifest['image']['digest']}"
    for name in SERVICES:
        cand = rel.stage1["candidates"][name]
        view = live.revision(cand["revision"])
        require(view is not None and view["ready"], "CANDIDATE_REVISION", f"The {name} candidate is not Ready")
        require(view["containers"] == 1, "CANDIDATE_REVISION", f"The {name} candidate has more or fewer than one container")
        require(view["image_digest"] == reference, "DIGEST", f"The {name} candidate digest differs from the manifest")
        require(view["image"] == reference, "DIGEST", f"The {name} candidate image is not the digest form")
        want = rel.baseline["services"][name]["template"]
        require(view["service_account"] == want["service_account"], "CANDIDATE_REVISION", f"{name} service_account changed")
        require(view["timeout_seconds"] == want["timeout_seconds"], "CANDIDATE_REVISION", f"{name} timeout_seconds changed")
        require(view["command"] == want["command"] and view["args"] == want["args"], "CANDIDATE_REVISION", f"{name} command changed")
        for res in ("memory", "cpu"):
            require(view["resources"].get("limits", {}).get(res) == want["resources"].get("limits", {}).get(res),
                    "CANDIDATE_REVISION", f"{name} resources {res} changed")
        for key in SCALING_ANNOTATIONS:
            require(view["annotations"].get(key) == want["annotations"].get(key), "CANDIDATE_REVISION",
                    f"{name} {key.rsplit('/', 1)[-1]} changed")
        removals = expected_env_variants(rel.baseline, rel.bound, name, rel.rid)[1]
        require(env_matches(view["env"], rel.baseline, rel.bound, name, rel.rid), "ENV",
                f"The {name} candidate environment differs from the expected one")
        if removals:
            rel.observations.setdefault("declared_env_removals", {})[name] = {
                sha_text(n): ("still_present" if n in view["env"] else "removed") for n in removals}


def check_template_candidate(rel, live, manifest):
    """The only allowed change to a template before promotion: the candidate image and environment, and the latest
    created revision naming the candidate."""
    for name in SERVICES:
        got, want = live.services[name]["template"], rel.baseline["services"][name]["template"]
        require(got["image"] == f"{REPO}@{manifest['image']['digest']}", "PRESERVATION", f"The {name} template image is not the candidate digest")
        require(env_matches(got["env"], rel.baseline, rel.bound, name, rel.rid), "PRESERVATION",
                f"The {name} template environment differs from the expected one")
        for key in want:
            if key not in ("image", "env"):
                require(got[key] == want[key], "PRESERVATION", f"The {name} template differs from the baseline: {key}")
        require(live.services[name]["latest_created"] == rel.stage1["candidates"][name]["revision"], "PRESERVATION",
                f"The {name} latest created revision is not the candidate")


def check_traffic_candidate(rel, live, expected):
    """C7: per service, the nonzero entries are exactly the revisions and percents of the phase; none follows LATEST."""
    for name in SERVICES:
        check_no_latest(live, name)
        nonzero = {e["revision"]: e["percent"] for e in live.serving(name)}
        require(nonzero == expected[name], "TRAFFIC", f"{name} traffic is not {expected[name]}")


def traffic_for(rel, *, agent_candidate, api_candidate):
    a80, cand = baseline_name, lambda svc: rel.stage1["candidates"][svc]["revision"]
    return {"f42-agent": {(cand("f42-agent") if agent_candidate else a80(rel, "f42-agent")): 100},
            "f42-api": {(cand("f42-api") if api_candidate else a80(rel, "f42-api")): 100}}


def check_api_agent_link(rel, live):
    """C12 part 1, attested: a serving API candidate names this release's agent tag URL and the canonical audience, and
    that tag sits on a revision that serves canonical agent traffic. A serving a80 API names the canonical URL."""
    agent = rel.stage1["candidates"]["f42-agent"]
    tagged = live.tag_entries("f42-agent", rel.rid)
    agent_serving = {e["revision"] for e in live.serving("f42-agent")}
    for e in live.serving("f42-api"):
        view = live.revision(e["revision"])
        require(view is not None, "API_AGENT", "A serving API revision cannot be read")
        if e["revision"] == rel.stage1["candidates"]["f42-api"]["revision"]:
            require(view["env"].get("AGENT_URL") == literal(agent["tag_url"]), "API_AGENT", "The serving API names another agent URL")
            require(view["env"].get("AGENT_AUDIENCE") == literal(rel.baseline["canonical"]["f42-agent"]), "API_AGENT",
                    "The serving API audience is not the canonical agent URL")
            require(len(tagged) == 1 and tagged[0]["revision"] in agent_serving, "API_AGENT",
                    "The agent tag the serving API names is not on the revision serving canonical agent traffic")


def check_tag_references(rel, live):
    """C14: an API revision with traffic that names an agent URL must name the canonical one, this release's tag URL or
    a retained one. Matching compares the full literal, so the API tag host (same tag value) never counts."""
    allowed = [literal(rel.baseline["canonical"]["f42-agent"]), literal(rel.stage1["candidates"]["f42-agent"]["tag_url"]),
               *[literal(u) for u in rel.bound.get("retainedAgentUrls", [])]]
    for e in live.serving("f42-api"):
        view = live.revision(e["revision"])
        require(view is not None and view["env"].get("AGENT_URL") in allowed, "TAG_REFERENCE",
                "A serving API revision names an agent URL outside the declared set")


def check_health(rel, url, *, label, expect_version=None):
    """C12 part 2: `GET <url>/api/health` is ok with checks.agent == ok. unreachable may be repeated up to three times, ten
    seconds apart (a zero-traffic or just-restored revision may be starting); anything else fails at once. The echoed
    version is an observation only."""
    attempts = []
    for attempt_no in range(HEALTH_RETRIES + 1):
        status, body = rel.reader.health(url)
        body = body if isinstance(body, dict) else {}
        agent = body.get("checks", {}).get("agent") if isinstance(body.get("checks"), dict) else None
        attempts.append({"http": status, "ok": body.get("ok"), "agent": agent})
        if status == 200 and body.get("ok") is True and agent == "ok":
            rel.observations.setdefault("health", {})[label] = {"attempts": attempts, "version": body.get("version"),
                                                                "version_matches": body.get("version") == expect_version}
            return
        if status == 200 and agent == "unreachable" and attempt_no < HEALTH_RETRIES:
            rel.sleep(HEALTH_RETRY_SECONDS)
            continue
        break
    rel.observations.setdefault("health", {})[label] = {"attempts": attempts}
    raise Stop("API_AGENT", f"{label} health did not report an agent reached with a token it accepted")


def check_smoke_receipt(rel, manifest, *, required):
    path = rel.path("smokeReceiptPath")
    if not path.exists():
        require(not required, "RECEIPT", "The smoke receipt does not exist")
        return None
    receipt = json.loads(path.read_text(encoding="utf-8"))
    checks = (("schema_version", receipt.get("schema_version") == SCHEMA_VERSION),
              ("release_id", receipt.get("release_id") == rel.rid),
              ("argv_url", receipt.get("argv_url") == rel.stage1["candidates"]["f42-api"]["tag_url"]),
              ("manifest_sha256", receipt.get("manifest_sha256") == manifest["manifest_sha256"]),
              ("candidates", receipt.get("candidates") == {n: rel.stage1["candidates"][n]["revision"] for n in SERVICES}),
              ("exit_code", receipt.get("exit_code") == 0),
              ("checks_total", receipt.get("checks_total") == rel.bound["expectedSmokeChecks"]),
              ("checks_passed", receipt.get("checks_passed") == receipt.get("checks_total")),
              ("log_sha256", isinstance(receipt.get("log_sha256"), str) and bool(receipt.get("log_sha256"))),
              ("ended_utc", isinstance(receipt.get("ended_utc"), str)))
    problems = [name for name, ok in checks if not ok]
    require(not (required and problems), "SMOKE", "The smoke receipt is not valid: " + ", ".join(problems))
    return {"valid": not problems, "problems": problems, "ended_utc": receipt.get("ended_utc")}


def parse_utc(text):
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def state_hash(rel, live, manifest):
    return fingerprint({"candidates": {n: live.revision(rel.stage1["candidates"][n]["revision"]) for n in SERVICES},
                        "services": {n: live.services[n] for n in SERVICES}, "manifest": manifest["manifest_sha256"]})


# phases

def pre_candidate(rel, phase, live):
    """BeforeAnyWrite, Freeze, BeforeCandidate: the world is the bound baseline, and at BeforeCandidate also pinned."""
    base = rel.baseline
    for name in SERVICES:
        require(live.services[name]["url"] == base["canonical"][name], "BASELINE", f"The {name} URL differs from the bound baseline")
    require(base["api_agent_url_literal"] == base["canonical"]["f42-agent"], "BASELINE",
            "The baseline API AGENT_URL is not the agent status.url, so a rollback would not restore the canonical URL")
    check_baseline_serving(rel, live, "BASELINE")
    for name in SERVICES:
        check_one_revision_at_100(rel, live, name, {baseline_name(rel, name)})
        if phase == "BeforeCandidate":
            check_no_latest(live, name)
    check_no_tag_or_candidate(rel, live)
    check_template_baseline(rel, live)
    check_policy_and_access(rel, live)
    check_anonymous(rel, base["canonical"]["f42-agent"])
    check_jobs(rel)


def candidate_state(rel, live, manifest, *, traffic):
    check_registry_tag(rel, manifest)
    check_receipts(rel, manifest)
    check_revision_set(rel, live)
    check_tag_references(rel, live)
    check_traffic_candidate(rel, live, traffic)
    check_api_agent_link(rel, live)
    check_tags(rel, live)
    check_candidate_revisions(rel, live, manifest)
    check_template_candidate(rel, live, manifest)
    check_baseline_serving(rel, live, "PRESERVATION")
    check_policy_and_access(rel, live)
    check_anonymous(rel, rel.baseline["canonical"]["f42-agent"])
    check_anonymous(rel, rel.stage1["candidates"]["f42-agent"]["tag_url"])
    check_jobs(rel)


def preserved_state(rel):
    base = rel.baseline
    return {"schema_version": SCHEMA_VERSION, "release_id": rel.rid, "baseline_sha256": rel.bound["baselineSha256"],
            "serving": {n: fingerprint(base["services"][n]["serving"]) for n in SERVICES},
            "jobs_sha256": fingerprint(base["jobs"]), "agent_policy_sha256": base["agent_policy_sha256"]}


def phase_before_any_write(rel, phase, live, result):
    require(rel.reader.registry_digest(image_tag(rel.bound)) is None, "TAG_MOVED", "The image tag already exists in the registry")
    pre_candidate(rel, phase, live)
    save_once(rel.path("preservationPath"), preserved_state(rel))


def read_freeze_inputs(rel):
    require(rel.evidence is not None, "BINDINGS", "Freeze needs the evidence folder the paste wrote its inputs to")
    path = Path(rel.evidence) / "freeze-inputs.json"
    require(path.exists(), "BINDINGS", "The paste did not write freeze-inputs.json")
    value = json.loads(path.read_text(encoding="utf-8"))
    require(all(isinstance(value.get(k), str) and value[k] for k in ("build_id", "uploaded_source", "paste_started_utc")),
            "BINDINGS", "freeze-inputs.json lacks the build id, the uploaded source or the paste start")
    version = value.get("schema_version")
    require(isinstance(version, int) and not isinstance(version, bool) and version == SCHEMA_VERSION, "SCHEMA_VERSION",
            "Unknown freeze-inputs.json schema_version")
    return value


def freeze_stage2(rel, inputs):
    """Digests are not known until the build ends. The paste's claims (the build id, what it uploaded, when it started)
    are checked against two native reads that are made separately: the build record, and the registry."""
    tag = image_tag(rel.bound)
    build = rel.reader.build(inputs["build_id"])
    require(build.get("id") == inputs["build_id"], "BUILD", "The build record is not the build the paste started")
    require(build.get("status") == "SUCCESS", "BUILD", "The services build did not succeed")
    require(build.get("serviceAccount") == f"projects/{PROJECT}/serviceAccounts/" + rel.bound["buildServiceAccount"].split("serviceAccounts/")[-1],
            "BUILD", "The build ran as another service account")
    require(build.get("substitutions", {}).get("_IMAGE") == tag, "BUILD", "The build produced another image tag")
    require(parse_utc(build["createTime"]) >= parse_utc(inputs["paste_started_utc"]), "BUILD", "The build was created before the paste started")
    source = build.get("source", {}).get("storageSource", {})
    require(f"gs://{source.get('bucket')}/{source.get('object')}" == inputs["uploaded_source"], "BUILD",
            "The build source object is not the one the paste uploaded")
    config = rel.reader.source_file_sha256(rel.bound["target"], "core/api/cloudbuild.yaml")
    require(config == rel.bound["buildConfigSha256"], "BUILD", "The build config at the commit differs from the bound hash")
    results = [i for i in build.get("results", {}).get("images", []) if i.get("name") == tag]
    require(len(results) == 1 and DIGEST.match(str(results[0].get("digest", ""))), "DIGEST", "The build result holds no digest for the image tag")
    registry = rel.reader.registry_digest(tag)
    require(isinstance(registry, str) and DIGEST.match(registry), "DIGEST", "The registry holds no well formed digest for the image tag")
    require(results[0]["digest"] == registry, "DIGEST", "The build result and the registry disagree about the image digest")
    return {"build": {"id": build["id"], "status": build["status"], "service_account": build["serviceAccount"],
                      "substitutions": {"_IMAGE": tag}, "create_time": build["createTime"], "source_object": inputs["uploaded_source"],
                      "config_sha256": config, "result_digest": results[0]["digest"]},
            "image": {"repository": REPO, "registry_digest": registry, "digest": registry, "reference": f"{REPO}@{registry}"}}


def phase_freeze(rel, phase, live, result):
    pre_candidate(rel, phase, live)
    save_once(rel.path("preservationPath"), preserved_state(rel))
    path, sidecar = rel.path("manifestPath"), rel.path("sidecarPath")
    require(not path.exists() and not sidecar.exists(), "MANIFEST_HASH", "The manifest already exists; Freeze does not overwrite it")
    manifest = {"schema_version": SCHEMA_VERSION, **rel.stage1, **freeze_stage2(rel, read_freeze_inputs(rel))}
    manifest["manifest_sha256"] = manifest_hash(manifest)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as out:
        out.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    with sidecar.open("x", encoding="utf-8") as out:
        out.write(manifest["manifest_sha256"] + "\n")
    result["manifest_sha256"] = manifest["manifest_sha256"]
    result["image_digest"] = manifest["image"]["digest"]


def phase_before_candidate(rel, phase, live, result):
    manifest = rel.manifest()
    result["manifest_sha256"] = manifest["manifest_sha256"]
    check_registry_tag(rel, manifest)
    check_receipts(rel, manifest)
    pre_candidate(rel, phase, live)
    save_once(rel.path("preservationPath"), preserved_state(rel))


def api_tag_health(rel, manifest):
    check_health(rel, rel.stage1["candidates"]["f42-api"]["tag_url"], label="api_tag", expect_version=short12(rel.bound))


def phase_before_smoke(rel, phase, live, result):
    manifest = rel.manifest()
    result["manifest_sha256"] = manifest["manifest_sha256"]
    candidate_state(rel, live, manifest, traffic=traffic_for(rel, agent_candidate=False, api_candidate=False))
    check_smoke_receipt(rel, manifest, required=False)
    require(not rel.path("smokeReceiptPath").exists(), "SMOKE", "A smoke receipt exists before the smoke")
    api_tag_health(rel, manifest)
    append_ledger(rel, "CANDIDATE", phase, live, manifest)


def phase_after_smoke(rel, phase, live, result):
    manifest = rel.manifest()
    result["manifest_sha256"] = manifest["manifest_sha256"]
    candidate_state(rel, live, manifest, traffic=traffic_for(rel, agent_candidate=False, api_candidate=False))
    result["smoke_receipt"] = check_smoke_receipt(rel, manifest, required=False)
    result["state_sha256"] = state_hash(rel, live, manifest)
    api_tag_health(rel, manifest)


def phase_before_promotion(rel, phase, live, result):
    manifest = rel.manifest()
    result["manifest_sha256"] = manifest["manifest_sha256"]
    candidate_state(rel, live, manifest, traffic=traffic_for(rel, agent_candidate=False, api_candidate=False))
    receipt = check_smoke_receipt(rel, manifest, required=True)
    after = rel.last_readback("AfterSmoke")
    require(after is not None, "RECEIPT", "AfterSmoke never ran")
    require(after.get("manifest_sha256") == manifest["manifest_sha256"], "RECEIPT", "AfterSmoke was recorded for another manifest")
    require(after.get("state_sha256") == state_hash(rel, live, manifest), "STATE_DRIFT",
            "A candidate resource changed since AfterSmoke")
    age = rel.now() - parse_utc(receipt["ended_utc"])
    require(age <= dt.timedelta(minutes=rel.bound["maxSmokeAgeMinutes"]), "STALE", "The smoke is older than the bound maximum")
    api_tag_health(rel, manifest)


def phase_after_agent_promotion(rel, phase, live, result):
    manifest = rel.manifest()
    result["manifest_sha256"] = manifest["manifest_sha256"]
    candidate_state(rel, live, manifest, traffic=traffic_for(rel, agent_candidate=True, api_candidate=False))
    check_health(rel, rel.baseline["canonical"]["f42-api"], label="public", expect_version=None)


def phase_after_promotion(rel, phase, live, result):
    manifest = rel.manifest()
    result["manifest_sha256"] = manifest["manifest_sha256"]
    candidate_state(rel, live, manifest, traffic=traffic_for(rel, agent_candidate=True, api_candidate=True))
    check_health(rel, rel.baseline["canonical"]["f42-api"], label="public", expect_version=short12(rel.bound))
    append_ledger(rel, "SERVING", phase, live, manifest)


def rollback_states(rel, live):
    """3.5: which of the allowed mixed states the services are in, and nothing else is accepted."""
    states = {}
    for name in SERVICES:
        cand = rel.stage1["candidates"][name]["revision"]
        revision = check_one_revision_at_100(rel, live, name, {baseline_name(rel, name), cand})
        states[name] = "a80" if revision == baseline_name(rel, name) else "candidate"
        target = live.revision(baseline_name(rel, name))
        require(target is not None and target["image_digest"] == rel.baseline["services"][name]["serving"]["image_digest"],
                "DIGEST", f"The rollback target of {name} is missing or has another digest")
        check_no_latest(live, name)
    return states


def phase_before_rollback(rel, phase, live, result):
    states = rollback_states(rel, live)
    if "candidate" in states.values():
        rel.manifest()
    check_policy_and_access(rel, live, blocking=False)
    check_anonymous(rel, rel.baseline["canonical"]["f42-agent"], blocking=False)
    check_jobs(rel, blocking=False)
    result["state"] = f"agent {states['f42-agent']}, api {states['f42-api']}"
    result["tags"] = {n: bool(live.tag_entries(n, rel.rid)) for n in SERVICES}
    result["candidates_present"] = {n: rel.stage1["candidates"][n]["revision"] in live.revision_names[n] for n in SERVICES}


def phase_after_rollback(rel, phase, live, result):
    """3.4: both services on the a80 revisions by name at 100%, with the a80 API configuration: the canonical agent URL
    and no audience variable, the baseline digests, jobs, policy and access, and a healthy public API."""
    base = rel.baseline
    for name in SERVICES:
        check_one_revision_at_100(rel, live, name, {baseline_name(rel, name)})
        check_no_latest(live, name)
        view = live.revision(baseline_name(rel, name))
        require(view is not None and view["image_digest"] == base["services"][name]["serving"]["image_digest"], "DIGEST",
                f"The serving digest of {name} is not the baseline digest")
    api = live.revision(baseline_name(rel, "f42-api"))
    require(api["env"].get("AGENT_URL") == literal(base["api_agent_url_literal"]) and base["api_agent_url_literal"] == live.services["f42-agent"]["url"],
            "ENV", "The serving API AGENT_URL is not the canonical agent URL")
    require("AGENT_AUDIENCE" not in api["env"], "ENV", "The serving API still carries AGENT_AUDIENCE")
    check_baseline_serving(rel, live, "PRESERVATION")
    check_policy_and_access(rel, live)
    check_anonymous(rel, base["canonical"]["f42-agent"])
    check_jobs(rel)
    check_health(rel, base["canonical"]["f42-api"], label="public")
    residue = {n: {"candidate_revision_present": rel.stage1["candidates"][n]["revision"] in live.revision_names[n],
                   "tags": sorted(e["tag"] for e in live.services[n]["traffic"]["status"] if e["tag"]),
                   "template_matches_baseline": live.services[n]["template"] == base["services"][n]["template"]} for n in SERVICES}
    result["residue"] = residue


def tag_retained(rel, live):
    """2.4: a tag is kept while an API revision with traffic names its agent tag URL, or the agent revision it names
    serves canonical traffic (the a80 revisions of the rollback set name the canonical URL, so they hold nothing)."""
    agent_url = literal(rel.stage1["candidates"]["f42-agent"]["tag_url"])
    reasons = []
    for e in live.serving("f42-api"):
        view = live.revision(e["revision"])
        if view is not None and view["env"].get("AGENT_URL") == agent_url:
            reasons.append("api_revision_with_traffic_names_the_agent_tag_url")
    tagged = live.tag_entries("f42-agent", rel.rid)
    serving = {e["revision"] for e in live.serving("f42-agent")}
    if tagged and tagged[0]["revision"] in serving:
        reasons.append("agent_revision_serves_canonical_traffic")
    return reasons


def phase_before_retire(rel, phase, live, result):
    reasons = tag_retained(rel, live)
    require(not reasons, "TAG_REFERENCE", "The tag is still referenced: " + ", ".join(sorted(set(reasons))))
    result["tags"] = {n: bool(live.tag_entries(n, rel.rid)) for n in SERVICES}


def phase_after_retire(rel, phase, live, result):
    for name in SERVICES:
        require(not live.tag_entries(name, rel.rid), "TAG_MAPPING", f"{name} still holds the release tag")
        check_one_revision_at_100(rel, live, name, {baseline_name(rel, name), rel.stage1["candidates"][name]["revision"]})
    result["tags"] = {n: False for n in SERVICES}
    append_ledger(rel, "RETIRED", phase, live, None)


def append_ledger(rel, state, phase, live, manifest):
    """tag-ledger.json is append only: earlier events are read back and written unchanged."""
    path = rel.path("ledgerPath")
    ledger = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"schema_version": SCHEMA_VERSION, "release_id": rel.rid, "events": []}
    require(ledger.get("schema_version") == SCHEMA_VERSION, "SCHEMA_VERSION", "Unknown ledger schema_version")
    for name in SERVICES:
        cand = rel.stage1["candidates"][name]
        view = live.revision(cand["revision"])
        ledger["events"].append({"phase": phase, "at_utc": rel.now().isoformat(), "service": name, "tag": rel.rid,
                                 "revision": cand["revision"], "tag_url": cand["tag_url"],
                                 "digest": view["image_digest"] if view else None, "state": state,
                                 "manifest_sha256": manifest["manifest_sha256"] if manifest else None})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")


PHASE_FUNCTIONS = {
    "BeforeAnyWrite": phase_before_any_write, "Freeze": phase_freeze, "BeforeCandidate": phase_before_candidate,
    "BeforeSmoke": phase_before_smoke, "AfterSmoke": phase_after_smoke, "BeforePromotion": phase_before_promotion,
    "AfterAgentPromotion": phase_after_agent_promotion, "AfterPromotion": phase_after_promotion,
    "BeforeRollback": phase_before_rollback, "AfterRollback": phase_after_rollback,
    "BeforeRetire": phase_before_retire, "AfterRetire": phase_after_retire,
}


def write_readback(rel, phase, result):
    folder = rel.root / "readbacks"
    folder.mkdir(parents=True, exist_ok=True)
    number = len(list(folder.glob(f"{phase}-*.json"))) + 1
    target = folder / f"{phase}-{number:02d}.json"
    with target.open("x", encoding="utf-8") as out:
        out.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    result["file"] = target.name


def run_phase(bound, phase, reader, evidence=None, *, tag=None, now=None, sleep=time.sleep):
    require(phase in PHASES, "BINDINGS", f"Unknown phase: {phase}")
    rel = Release(bound, reader, evidence, now, sleep)
    if phase in RETIRE_PHASES:
        require(tag == rel.rid, "TAG_MAPPING", "The tag argument is not the release id")
    else:
        require(tag is None, "BINDINGS", "A tag argument belongs to the retire phases only")
    check_config(rel)
    if phase in PRINCIPAL_PHASES:
        check_principals(rel)
    live = rel.live()
    result = {"schema_version": SCHEMA_VERSION, "mode": "services-only", "phase": phase, "release_id": rel.rid,
              "at_utc": rel.now().isoformat(), "manifest_sha256": None}
    PHASE_FUNCTIONS[phase](rel, phase, live, result)
    result["observations"], result["blocking"] = rel.observations, rel.blocking
    write_readback(rel, phase, result)
    return result
