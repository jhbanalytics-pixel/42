"""Release B, the jobs image release (W8-REL-B v2.1): bindings, the baseline-J receipt and the readback phases for mode jobs.
Read only: nothing here writes to a cloud resource.

    run_phase(bound, phase, reader, evidence, now=None, sleep=time.sleep, bq=None) -> the readback, or Stop or Probe

It reuses the services-only machinery unchanged: the views, Stop and Probe, the canonical hashing, the bound receipt
loader. Every expectation comes from the bindings or from baseline-J, which the bindings pin by sha256 and which is
recomputed on every read. A value the system reports about itself (a job's image reference, an execution's name, a
row's own execution field) is an observation to compare against those, never an admission.

baseline-J (kind "baseline-J", schema_version 1) is captured after Release A's terminal readback:
    jobs        the 14 job views (services_only.job_view)
    services    per service the state service_state() returns, which is the services part of baseline-A
    aTerminal   {kind: AfterPromotion | AfterRollback, at_utc, a_release_id}
    a80Serving  the a80 serving revision name per service, used when A was rolled back
    captured_utc
"""
from __future__ import annotations

import copy
import json
import datetime as dt
import re
from pathlib import Path

from core.setup.release import services_only as so
from core.setup.release.services_only import Probe, Stop, require

SCHEMA_VERSION = 1
JOBS_REPO = f"{so.REGION}-docker.pkg.dev/{so.PROJECT}/intelligence-42/jobs"
CHAIN_STAGES = ("collect", "understand", "detect", "brief")
STAGE_JOB = {"collect": "f42-collect", "understand": "f42-understand", "detect": "f42-detect", "brief": "f42-brief"}
CHAIN_GROUP = ("f42-brief", "f42-detect", "f42-understand", "f42-collect")
UPDATE_ORDER = ("f42-watchdog", "f42-probe", "f42-gdelt", "f42-gdelt-daily", "f42-reconcile", "f42-drift", "f42-learn",
                "f42-calendar", "f42-digest", "f42-scheduled-asks", *CHAIN_GROUP)
A_TERMINAL_KINDS = ("AfterPromotion", "AfterRollback")
JOBS_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
HHMM = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]\Z")

# The bindings the chain evidence producer needs, and the rest that the release actions need. The producer runs before the
# baseline chain manifest exists (it is the producer that writes it), so the manifest path and hash are not among its keys.
PRODUCER_BINDINGS = ("release_id", "target", "tree", "releaseDir", "baselinePath", "baselineSha256", "rollbackJobsDigest",
                     "callerAccount", "readTimeoutSeconds", "chainEvidenceBytesCap", "templateHashes",
                     "collectStartToleranceMinutes")
RELEASE_BINDINGS = (*PRODUCER_BINDINGS, "baselineChainPath", "baselineChainSha256", "buildServiceAccount", "buildConfigSha256",
                    "dockerfileSha256", "durableManifestSha256", "dryRunReceiptSha256", "window", "maxBaselineAgeDays",
                    "maxCandidateAgeHours", "priorAttempts", "priorLedgers")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
HASH_KEYS = ("baselineSha256", "baselineChainSha256", "buildConfigSha256", "dockerfileSha256", "durableManifestSha256",
             "dryRunReceiptSha256")


def validate_jobs_bindings(bound, *, producer=False):
    """The jobs bindings (3.8). A missing or malformed field is a BINDINGS stop. `producer` asks only for the keys the
    chain evidence producer reads."""
    require(isinstance(bound, dict), "BINDINGS", "The bindings are not an object")
    require(bound.get("schema_version") == SCHEMA_VERSION, "SCHEMA_VERSION", "Unknown bindings schema_version")
    require(bound.get("mode") == "jobs", "MODE", "The bindings are not for jobs mode")
    for key in (PRODUCER_BINDINGS if producer else RELEASE_BINDINGS):
        require(key in bound, "BINDINGS", f"The bindings lack {key}")
    require(so.RELEASE_ID.match(str(bound["release_id"])), "BINDINGS", "The release id is not rel-<sha7>-<nn>")
    require(re.fullmatch(r"[0-9a-f]{40}", str(bound["target"])) and str(bound["release_id"])[4:11] == bound["target"][:7],
            "BINDINGS", "The release id does not name the target commit")
    require(JOBS_DIGEST.match(str(bound["rollbackJobsDigest"])), "BINDINGS", "rollbackJobsDigest is not a sha256 digest")
    for key in HASH_KEYS:
        if key in bound:
            require(SHA256.match(str(bound[key])), "BINDINGS", f"{key} is not a sha256")
    timeouts = bound["readTimeoutSeconds"]
    require(isinstance(timeouts, dict) and so._positive(timeouts.get("gcloud")) and so._positive(timeouts.get("http")),
            "BINDINGS", "readTimeoutSeconds must give a positive gcloud and http value")
    require(isinstance(bound["chainEvidenceBytesCap"], int) and not isinstance(bound["chainEvidenceBytesCap"], bool)
            and bound["chainEvidenceBytesCap"] > 0, "BINDINGS", "chainEvidenceBytesCap is not a positive integer")
    templates = bound["templateHashes"]
    require(isinstance(templates, dict) and templates and all(SHA256.match(str(v)) for v in templates.values()), "BINDINGS",
            "templateHashes is not a table of sha256 values")
    tolerance = bound["collectStartToleranceMinutes"]
    require(isinstance(tolerance, int) and not isinstance(tolerance, bool) and tolerance >= 0, "BINDINGS",
            "collectStartToleranceMinutes is not a non-negative integer")
    if not producer:
        window = bound["window"]
        require(isinstance(window, dict) and set(window) == {"startSast", "endSast", "rollbackDeadlineSast"}
                and all(HHMM.match(str(v)) for v in window.values()), "BINDINGS", "window must give startSast, endSast and rollbackDeadlineSast as HH:MM")
        for key in ("maxBaselineAgeDays", "maxCandidateAgeHours"):
            require(so._positive(bound[key]), "BINDINGS", f"{key} is not usable")
        require(isinstance(bound["priorAttempts"], list) and isinstance(bound["priorLedgers"], list), "BINDINGS",
                "priorAttempts and priorLedgers must be lists")
    return Path(bound["releaseDir"]).resolve()


def load_baseline_j(bound):
    """baseline-J, recomputed against the hash the bindings pin, then checked for shape. The pinned hash is the
    expectation; the file is the thing being proven."""
    baseline = so.load_bound_json(bound["baselinePath"], bound["baselineSha256"], "baseline-J")
    require(baseline.get("kind") == "baseline-J", "BASELINE", "The bound receipt is not a baseline-J")
    require(set(baseline.get("jobs", {})) == set(so.JOB_NAMES), "BASELINE", "baseline-J does not hold the fourteen jobs")
    require(set(baseline.get("services", {})) == set(so.SERVICES), "BASELINE", "baseline-J does not hold the two services")
    terminal = baseline.get("aTerminal")
    require(isinstance(terminal, dict) and terminal.get("kind") in A_TERMINAL_KINDS and isinstance(terminal.get("at_utc"), str)
            and bool(terminal.get("at_utc")), "A_STATE", "baseline-J does not bind Release A's terminal readback")
    return baseline


def image_reference(digest):
    return f"{JOBS_REPO}@{digest}"


def with_image(view, digest):
    """A job view as it reads after `jobs update --image` and nothing else."""
    changed = copy.deepcopy(view)
    changed["image"] = image_reference(digest)
    return changed


# the services, as they stand

def service_state(reader, name):
    """The part of a service Release B compares with the bound post-A state: annotations, template, traffic, the revision
    list and the one serving revision. Identical in shape to the services part of baseline-A."""
    view = so.service_view(reader.service(name))
    serving = [e for e in view["traffic"]["status"] if e["percent"] > 0]
    require(len(serving) == 1 and serving[0]["percent"] == 100, "SERVICE_CHANGED", f"{name} does not serve one revision at 100%")
    return {"annotations": view["annotations"], "template": view["template"], "traffic": view["traffic"],
            "revisions": sorted(reader.revisions(name)), "serving": so.revision_view(reader.revision(serving[0]["revision"]))}


def expected_serving_names(baseline):
    """The serving revision each service must show for the terminal state baseline-J says Release A reached. After a
    promotion it is the candidate named for A's own release id; after A's rollback it is the a80 revision."""
    terminal = baseline["aTerminal"]
    if terminal["kind"] == "AfterPromotion":
        a_id = terminal.get("a_release_id")
        require(isinstance(a_id, str) and so.RELEASE_ID.match(a_id), "A_STATE", "baseline-J names no release id for Release A")
        return {name: f"{name}-{a_id}" for name in so.SERVICES}
    a80 = baseline.get("a80Serving")
    require(isinstance(a80, dict) and set(a80) == set(so.SERVICES), "A_STATE", "baseline-J names no a80 serving revisions")
    return dict(a80)


def check_a_state(baseline, live):
    """A_STATE (JU-10): the services are in the state Release A's terminal readback left, and baseline-J says so by name.
    The a80 pair with A never run is neither state."""
    expected = expected_serving_names(baseline)
    for name in so.SERVICES:
        require(baseline["services"][name]["serving"]["name"] == expected[name], "A_STATE",
                f"baseline-J holds {name} on a revision that is not Release A's terminal state")
        require(live[name]["serving"]["name"] == expected[name], "A_STATE",
                f"{name} does not serve the revision Release A's terminal state requires")


def check_services_unchanged(baseline, live):
    for name in so.SERVICES:
        require(live[name] == baseline["services"][name], "SERVICE_CHANGED", f"{name} differs from the bound post-A state")


def live_services(reader):
    return {name: service_state(reader, name) for name in so.SERVICES}


def parse_utc(text):
    return so.parse_utc(text)


def aware(value):
    """A datetime from a BigQuery value or an ISO string, always timezone aware."""
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    return so.parse_utc(str(value))


def frozen_digest(release_dir):
    """The digest FreezeJobs froze, read from release-manifest.json only after the manifest's own hash is recomputed and
    its sidecar agrees. The file is the thing being proven, so its stated hash is never taken as given."""
    root = Path(release_dir)
    path, sidecar = root / "release-manifest.json", root / "release-manifest.sha256"
    require(path.exists() and sidecar.exists(), "MANIFEST_HASH", "The release manifest or its sidecar does not exist")
    manifest = json.loads(path.read_bytes().decode("utf-8"))
    require(isinstance(manifest, dict) and manifest.get("schema_version") == SCHEMA_VERSION, "SCHEMA_VERSION",
            "Unknown release manifest schema_version")
    require(sidecar.read_text(encoding="utf-8").strip() == manifest.get("manifest_sha256"), "MANIFEST_HASH",
            "The release manifest sidecar does not hold the manifest hash")
    require(so.manifest_hash(manifest) == manifest.get("manifest_sha256"), "MANIFEST_HASH",
            "The release manifest hash does not match its content")
    image = manifest.get("image", {})
    require(JOBS_DIGEST.match(str(image.get("digest"))) and image.get("digest") == image.get("registry_digest"), "DIGEST",
            "The release manifest does not bind one digest from the build result and the registry")
    return image["digest"]
