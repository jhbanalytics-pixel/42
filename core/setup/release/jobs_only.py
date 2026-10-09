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


# executions: the fields the rules read, from a Cloud Run v1 execution description

SAST = dt.timezone(dt.timedelta(hours=2), "SAST")
JOB_LABEL = "run.googleapis.com/job"


def execution_view(raw):
    """The digest is the 71 character `sha256:` form taken from the container image reference of this description, and nothing
    else supplies it; a description without one is a read that could not complete."""
    meta, spec, status = raw.get("metadata", {}), raw.get("spec", {}), raw.get("status", {})
    containers = spec.get("template", {}).get("spec", {}).get("containers", [])
    reference = containers[0].get("image", "") if len(containers) == 1 else ""
    match = re.search(r"@(sha256:[0-9a-f]{64})\Z", str(reference))
    if match is None:
        raise Probe("An execution description carries no image digest")
    return {"name": meta.get("name"), "job": (meta.get("labels") or {}).get(JOB_LABEL), "image_digest": match.group(1),
            "start": status.get("startTime"), "completion": status.get("completionTime"),
            "succeeded": status.get("succeededCount", 0), "failed": status.get("failedCount", 0),
            "cancelled": status.get("cancelledCount", 0), "retried": status.get("retriedCount")}


def describe_execution(reader, name):
    """One named execution; the description must be for the name asked, else it is a contradiction."""
    view = execution_view(reader.execution(name))
    require(view["name"] == name, "EXECUTION_IDENTITY", "An execution description is for another execution than the one asked")
    return view


def on_run_date(start, day):
    return start is not None and aware(start).astimezone(SAST).date() == day


def is_active(entry):
    """A listed execution with no completion time is running or queued."""
    return not entry.get("status", {}).get("completionTime")


# the quiet snapshot (3.7): a read, not a lock

QUIET_TEMPLATES = {
    "chain_rows": ("SELECT run_id, stage, run_date, status, started_at, finished_at FROM `ogilvy-trends-v2.intelligence_42_agent`.runs "
                   "WHERE run_date IN UNNEST(@days) AND stage IN UNNEST(@stages) AND status != 'skipped_duplicate'"),
}
QUIET_MAX_AGE_MINUTES = 15


def quiet_template_hashes():
    return {name: so.sha_text(text) for name, text in QUIET_TEMPLATES.items()}


def row_order(row):
    return (aware(row["finished_at"] or row["started_at"]), row["finished_at"] is not None)


def quiet_snapshot(reader, runner, taken_at):
    """Today's and yesterday's executions of the 14 jobs with their states, and the latest non-skipped runs row of each chain
    stage for both days with its version. It records; assert_quiet decides. A running watchdog or side job is recorded and
    allowed."""
    today = aware(taken_at).astimezone(SAST).date()
    days = [today, today - dt.timedelta(days=1)]
    executions, active_chain, active_side = {}, [], []
    for job in so.JOB_NAMES:
        kept = []
        for entry in reader.executions(job):
            start = entry.get("status", {}).get("startTime")
            if start is not None and aware(start).astimezone(SAST).date() not in days:
                continue
            state = "active" if is_active(entry) else "completed"
            kept.append({"name": entry["metadata"]["name"], "state": state})
            if state == "active":
                (active_chain if job in STAGE_JOB.values() else active_side).append(entry["metadata"]["name"])
        executions[job] = sorted(kept, key=lambda e: e["name"])
    rows = runner.run("chain_rows", {"days": days, "stages": list(CHAIN_STAGES)})
    latest, running_today = {}, []
    for stage in CHAIN_STAGES:
        for day in days:
            mine = [r for r in rows if r["stage"] == stage and str(r["run_date"]) == day.isoformat()]
            if not mine:
                continue
            by_run = {}
            for row in mine:
                by_run.setdefault(row["run_id"], []).append(row)
            run_id, group = max(by_run.items(), key=lambda item: max(row_order(r) for r in item[1]))
            group = sorted(group, key=row_order)
            latest[f"{stage}:{day.isoformat()}"] = {"run_id": run_id, "status": group[-1]["status"], "version": len(group)}
            if day == today and group[-1]["status"] == "running":
                running_today.append(stage)
    snapshot = {"taken_at": aware(taken_at).isoformat(), "executions": executions, "chain_rows": latest,
                "active": {"chain": sorted(active_chain), "side": sorted(active_side), "chain_rows_running_today": running_today}}
    snapshot["sha256"] = so.fingerprint({k: v for k, v in snapshot.items() if k != "taken_at"})
    return snapshot


def assert_quiet(snapshot):
    active = snapshot["active"]
    require(not active["chain"] and not active["chain_rows_running_today"], "CHAIN_ACTIVE",
            "A chain job is running or queued, or a chain stage's latest row for today is running")


def chain_view(snapshot):
    """What a second read must find unchanged: the chain jobs' executions and the latest chain rows."""
    return {"executions": {job: snapshot["executions"][job] for job in STAGE_JOB.values()}, "chain_rows": snapshot["chain_rows"]}


def snapshot_age_minutes(snapshot, now):
    return (aware(now) - aware(snapshot["taken_at"])).total_seconds() / 60


def window_open(bound, now):
    local = aware(now).astimezone(SAST)
    start, end = (dt.time.fromisoformat(bound["window"][k]) for k in ("startSast", "endSast"))
    return start <= local.time() < end


# the phases

PHASES = ("BeforeAnyWrite", "FreezeJobs", "BeforeJobsUpdate", "AfterJobsUpdate", "BeforeJobsRollback", "AfterJobsRollback")
PRINCIPAL_PHASES = ("BeforeAnyWrite", "BeforeJobsUpdate", "BeforeJobsRollback")
# What a rollback leaves behind (3.9 item 3): recorded in AfterJobsRollback, never silently accepted, never removed by B.
RESIDUE = ("tvf_post_items", "v_item_locality_checked", "v_item_locality_current", "core.early_signal", "core.item_locality",
           "core.item_locality_post", "core.item_locality_verified", "core.post_item_lineage", "core.post_item_end",
           "core.post_items.linked_on", "core.post_items.link_market")


def image_tag(bound):
    return f"{JOBS_REPO}:{bound['target'][:12]}-{bound['release_id'][-2:]}"


class JobsRelease:
    def __init__(self, bound, reader, evidence=None, now=None, sleep=None, bq=None):
        self.root = validate_jobs_bindings(bound)
        self.bound, self.reader, self.evidence, self.sleep, self._now = bound, reader, evidence, sleep, now
        self.rid = bound["release_id"]
        self.baseline = load_baseline_j(bound)
        self.bq = bq
        self.observations: dict = {}
        self.blocking: dict = {}

    def now(self):
        return self._now() if callable(self._now) else (self._now or dt.datetime.now(dt.timezone.utc))

    def runner(self):
        from core.setup.release.chain_evidence import BqRunner

        require(self.bq is not None, "BINDINGS", "This phase reads BigQuery and was given no client")
        return BqRunner(self.bq, self.bound["chainEvidenceBytesCap"], QUIET_TEMPLATES)

    def manifest(self):
        """The digest FreezeJobs froze, from the release manifest with its hash recomputed."""
        return frozen_digest(self.root)

    def last_readback(self, phase):
        folder = self.root / "readbacks"
        found = sorted(folder.glob(f"{phase}-*.json")) if folder.exists() else []
        return json.loads(found[-1].read_text(encoding="utf-8")) if found else None


def split_view(view):
    return {k: v for k, v in view.items() if k != "image"}


def check_job_after_update(reader, baseline, job, digest):
    """JU-03: every field but the image equals baseline-J (JOB_DRIFT), and the image is the reference of this digest (JOB_IMAGE)."""
    view = so.job_view(reader.job(job))
    require(split_view(view) == split_view(baseline["jobs"][job]), "JOB_DRIFT",
            f"{job} differs from baseline-J in a field other than the image")
    require(view["image"] == image_reference(digest), "JOB_IMAGE", f"{job} does not run the expected digest")
    return view


def job_states(rel, digest):
    """Per job whether it still equals baseline-J ("old") or equals it with the new image ("new")."""
    out = {}
    for name in UPDATE_ORDER:
        view = so.job_view(rel.reader.job(name))
        base = rel.baseline["jobs"][name]
        if view == base:
            out[name] = "old"
        elif view == with_image(base, digest):
            out[name] = "new"
        else:
            require(split_view(view) == split_view(base), "JOB_DRIFT", f"{name} differs from baseline-J in a field other than the image")
            raise Stop("JOB_IMAGE", f"{name} runs neither the baseline image nor the manifest digest")
    return out


def check_baseline_images(rel):
    """Every baseline image is the bound rollback digest: baseline-J is the rollback state, or B has nothing to roll back to."""
    for name in so.JOB_NAMES:
        require(rel.baseline["jobs"][name]["image"] == image_reference(rel.bound["rollbackJobsDigest"]), "BASELINE",
                f"The baseline image of {name} is not the bound rollback digest")


def check_services(rel, *, a_state=True):
    live = live_services(rel.reader)
    if a_state:
        check_a_state(rel.baseline, live)
    check_services_unchanged(rel.baseline, live)


def check_registry_digest(rel, digest):
    require(rel.reader.registry_digest(image_tag(rel.bound)) == digest, "TAG_MOVED", "The image tag no longer resolves to the manifest digest")


CHAIN_CODES = ("IMAGE", "WINDOW", "NOT_ONE_RUN", "TERMINAL", "BOUND", "RETRIED", "ORDER", "UPSTREAM", "SUBSTAGES", "LINEAGE", "DEGRADED",
               "SERVICES", "MANUAL")
SIGNATURE = ("cannot round-trip through string representation", "PARSE_JSON")


def check_baseline_chain(rel):
    """The baseline chain manifest is the file the bindings hash, was made against this baseline-J and this rollback digest,
    qualifies or is a baseline by line, has no WINDOW reason, and is recent enough. Its verdict is not taken on its word: the
    reasons must agree with qualifies, and a by-line claim must be supported by the degradation recorded in the same manifest."""
    path = Path(rel.bound["baselineChainPath"])
    require(path.is_file(), "BINDINGS", "The baseline chain manifest does not exist")
    data = path.read_bytes()
    require(so.sha_bytes(data) == rel.bound["baselineChainSha256"], "BINDINGS", "Bound receipt hash changed: baseline chain manifest")
    manifest = json.loads(data.decode("utf-8"))
    require(manifest.get("kind") == "chain-evidence" and manifest.get("schema_version") == SCHEMA_VERSION, "BASELINE",
            "The baseline chain file is not a chain evidence manifest")
    require(manifest.get("role") == "baseline", "BASELINE", "The baseline chain manifest was not produced as a baseline")
    require(manifest.get("jobs_image", {}).get("expected_digest") == rel.bound["rollbackJobsDigest"], "BASELINE",
            "The baseline chain was judged against another digest than the bound rollback digest")
    terminal = rel.baseline["aTerminal"]
    require(manifest.get("a_terminal") == {"kind": terminal["kind"], "at_utc": terminal["at_utc"]}, "BASELINE",
            "The baseline chain manifest was made against another Release A terminal readback")
    verdict = manifest.get("verdict", {})
    reasons = verdict.get("reasons")
    require(isinstance(reasons, list) and set(reasons) <= set(CHAIN_CODES) and verdict.get("qualifies") is (not reasons), "BASELINE",
            "The baseline chain verdict does not agree with its own reasons")
    require("WINDOW" not in reasons, "BASELINE", "The baseline chain ran before Release A's terminal readback")
    if not verdict["qualifies"]:
        degradation = manifest.get("stages", {}).get("understand", {}).get("degradation") or {}
        lines = list((degradation.get("cluster_error_first_lines") or {}).values())
        supported = (verdict.get("baseline_by_line") is True and reasons == ["DEGRADED"] and not degradation.get("partial")
                     and not degradation.get("embed_error") and not degradation.get("enrich_error") and bool(lines)
                     and all(all(word in line for word in SIGNATURE) for line in lines))
        require(supported, "BASELINE", "The baseline chain neither qualifies nor is a baseline by line")
    day = dt.date.fromisoformat(manifest["run_date"])
    today = rel.now().astimezone(SAST).date()
    require(0 <= (today - day).days <= rel.bound["maxBaselineAgeDays"], "BASELINE_AGE",
            f"The baseline chain of {day.isoformat()} is outside the bound maximum age")
    rel.observations["baseline_chain"] = {"run_date": day.isoformat(), "qualifies": verdict["qualifies"],
                                          "baseline_by_line": bool(verdict.get("baseline_by_line")), "sha256": rel.bound["baselineChainSha256"]}


def phase_before_any_write(rel, phase, result):
    check_baseline_images(rel)
    require(rel.reader.registry_digest(image_tag(rel.bound)) is None, "TAG_MOVED", "The image tag already exists in the registry")
    states = job_states(rel, rel.bound["rollbackJobsDigest"])
    require(all(state == "old" for state in states.values()), "BASELINE", "A job already differs from baseline-J")
    check_services(rel)
    check_baseline_chain(rel)


def phase_freeze_jobs(rel, phase, result):
    raise Stop("NOT_BUILT", "FreezeJobs binds the build result to the registry digest (JB-02), which this tooling does not carry yet")


def check_candidate(rel):
    """JobsUpdate runs on the day of JobsCandidate (Q5) and inside maxCandidateAgeHours of it."""
    candidate = rel.last_readback("BeforeAnyWrite")
    require(candidate is not None, "CANDIDATE_AGE", "JobsCandidate left no BeforeAnyWrite readback")
    then, now = aware(candidate["at_utc"]), rel.now()
    require(dt.timedelta(0) <= now - then <= dt.timedelta(hours=rel.bound["maxCandidateAgeHours"]), "CANDIDATE_AGE",
            "JobsCandidate is older than the bound maximum")
    require(then.astimezone(SAST).date() == now.astimezone(SAST).date(), "CANDIDATE_AGE", "JobsCandidate ran on another day than JobsUpdate")


def phase_before_jobs_update(rel, phase, result):
    digest = rel.manifest()
    check_candidate(rel)
    check_services(rel)
    check_registry_digest(rel, digest)
    states = job_states(rel, digest)
    new = [name for name in UPDATE_ORDER if states[name] == "new"]
    require(new == list(UPDATE_ORDER[:len(new)]), "JOB_ORDER", "The jobs already updated are not a prefix of the update order")
    require(window_open(rel.bound, rel.now()), "WINDOW", "JobsUpdate runs only inside the bound window")
    snapshot = quiet_snapshot(rel.reader, rel.runner(), rel.now())
    assert_quiet(snapshot)
    result["quiet_snapshot"] = snapshot
    rel.observations["jobs_already_updated"] = new


def phase_after_jobs_update(rel, phase, result):
    digest = rel.manifest()
    for name in UPDATE_ORDER:
        check_job_after_update(rel.reader, rel.baseline, name, digest)
    check_services(rel, a_state=False)
    check_registry_digest(rel, digest)
    began = rel.last_readback("BeforeJobsUpdate")
    require(began is not None, "ORDER", "AfterJobsUpdate ran without a BeforeJobsUpdate readback")
    since = aware(began["at_utc"])
    listed = []
    for job in so.JOB_NAMES:
        for entry in rel.reader.executions(job):
            start = entry.get("status", {}).get("startTime")
            if start is not None and aware(start) >= since:
                view = describe_execution(rel.reader, entry["metadata"]["name"])
                listed.append({"job": job, "name": view["name"], "start": view["start"], "image_digest": view["image_digest"]})
    rel.observations["jobs_at_digest"] = len(UPDATE_ORDER)
    rel.observations["executions_in_window"] = sorted(listed, key=lambda e: (e["start"], e["name"]))


def phase_before_jobs_rollback(rel, phase, result):
    """A rollback never refuses for drift: services and jobs are recorded. It refuses only while a chain execution is live."""
    live = live_services(rel.reader)
    changed_services = [name for name in so.SERVICES if live[name] != rel.baseline["services"][name]]
    if changed_services:
        rel.blocking["services"] = {"blocking": False, "changed": changed_services}
    rel.observations["services_changed"] = changed_services
    changed_jobs = [name for name in so.JOB_NAMES if so.job_view(rel.reader.job(name)) != rel.baseline["jobs"][name]]
    if changed_jobs:
        rel.blocking["jobs"] = {"blocking": False, "changed": changed_jobs}
    rel.observations["jobs_changed"] = changed_jobs
    rel.observations["rollback_digest_sha256"] = so.sha_text(rel.bound["rollbackJobsDigest"])
    active = []
    for job in so.JOB_NAMES:
        for entry in rel.reader.executions(job):
            if is_active(entry):
                active.append({"job": job, "name": entry["metadata"]["name"]})
    rel.observations["active_executions"] = active
    require(not [a for a in active if a["job"] in STAGE_JOB.values()], "CHAIN_ACTIVE",
            "A chain execution is running or queued; a rollback inside a live chain makes the reverse pairs")


def phase_after_jobs_rollback(rel, phase, result):
    check_baseline_images(rel)
    digest = rel.bound["rollbackJobsDigest"]
    for name in so.JOB_NAMES:
        view = check_job_after_update(rel.reader, rel.baseline, name, digest)
        require(view == rel.baseline["jobs"][name], "JOB_DRIFT", f"{name} does not equal baseline-J")
    check_services(rel, a_state=False)
    active = []
    for job in so.JOB_NAMES:
        for entry in rel.reader.executions(job):
            if is_active(entry):
                view = describe_execution(rel.reader, entry["metadata"]["name"])
                active.append({"job": job, "name": view["name"], "image_digest": view["image_digest"]})
    rel.observations["active_executions"] = active
    rel.observations["residue"] = list(RESIDUE)
    rel.observations["residue_probed"] = False


PHASE_FUNCTIONS = {
    "BeforeAnyWrite": phase_before_any_write, "FreezeJobs": phase_freeze_jobs, "BeforeJobsUpdate": phase_before_jobs_update,
    "AfterJobsUpdate": phase_after_jobs_update, "BeforeJobsRollback": phase_before_jobs_rollback,
    "AfterJobsRollback": phase_after_jobs_rollback,
}


def write_readback(rel, phase, result):
    folder = rel.root / "readbacks"
    folder.mkdir(parents=True, exist_ok=True)
    number = len(list(folder.glob(f"{phase}-*.json"))) + 1
    target = folder / f"{phase}-{number:02d}.json"
    with target.open("x", encoding="utf-8") as out:
        out.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    result["file"] = target.name


def run_phase(bound, phase, reader, evidence=None, *, now=None, sleep=None, bq=None):
    require(phase in PHASES, "BINDINGS", f"Unknown phase: {phase}")
    rel = JobsRelease(bound, reader, evidence, now, sleep, bq)
    so.check_config(rel)
    if phase in PRINCIPAL_PHASES:
        so.check_principals(rel)
    result = {"schema_version": SCHEMA_VERSION, "mode": "jobs", "phase": phase, "release_id": rel.rid, "at_utc": rel.now().isoformat()}
    PHASE_FUNCTIONS[phase](rel, phase, result)
    result["observations"], result["blocking"] = rel.observations, rel.blocking
    write_readback(rel, phase, result)
    return result
