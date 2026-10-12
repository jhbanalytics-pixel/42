"""baseline-J capture (W8-REL-B v2.1 section 3.8, JB-05). Read only: it reads job and service descriptions and writes one file.

    capture_baseline_j(reader, a_terminal=..., a80_serving=..., rollback_digest=..., a_retire=..., captured_utc=..., prior=None) -> baseline-J
    validate_prior_jobs_release(record) -> the record, or Stop PRIOR_RELEASE
    write_once(path, baseline) -> the sha256 of the bytes written, which the bindings bind

baseline-J (kind "baseline-J", schema_version 1) is what Release B compares everything against: the 14 job views, the two
service states in the shape services_only.capture_baseline stores them, Release A's terminal readback as a bound record
(kind, time, and A's release id), the a80 serving revisions for an A that was rolled back, and whether A's Retire is done or
waits until after B. Every expectation is a bound input: A's release id and the a80 revision names name the serving revision
each service must show, the rollback digest names the one image all 14 jobs must run, and the retire declaration says what
the tag set must be. The live read is the thing being proven, so nothing is taken from it as an expectation.

A release that follows a jobs release (Release D on the live Release B) is bound to that release by a prior jobs release record, which the
packet command derives from the prior release's own lock and packet. The record carries the digest the prior release froze, the terminal
readback its chain is counted from (AfterJobsUpdate) and the fingerprint of each of the fourteen job definitions its JobsUpdate left. With
a record the jobs must run that digest and be those definitions, the rollback digest is the record's, and baseline-J carries the record, so
that a rollback restores the prior release exactly. Without a record nothing changes: the jobs run the digest the caller pinned.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import re
from pathlib import Path

from core.setup.release import services_only as so
from core.setup.release.services_only import Stop, require

SCHEMA_VERSION = so.SCHEMA_VERSION
JOBS_REPO = f"{so.REGION}-docker.pkg.dev/{so.PROJECT}/intelligence-42/jobs"
A_TERMINAL_KINDS = ("AfterPromotion", "AfterRollback")
A_RETIRE = ("retired", "waits_for_b")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
PRIOR_KIND = "prior-jobs-release"
PRIOR_TERMINAL_KIND = "AfterJobsUpdate"
PRIOR_FIELDS = ("schema_version", "kind", "release_id", "commit", "image_digest", "terminal", "job_definitions", "sources")
PRIOR_SOURCES = ("lock_sha256", "bindings_sha256", "manifest_sha256", "after_update_sha256", "update_log_sha256")


def aware_utc(text):
    try:
        value = so.parse_utc(text)
    except (AttributeError, TypeError, ValueError):
        return None
    return value if value.tzinfo is not None else None


def validate_prior_jobs_release(record):
    """The record of a jobs release that is live, checked for shape and nothing else: exactly its fields, a digest, a release id that names
    its commit, a timezone aware AfterJobsUpdate terminal, one definition fingerprint for each of the fourteen jobs, and the hashes of the
    files it was derived from. What makes it true is the derivation that wrote it and the live read it is held to."""
    def bad(message):
        raise Stop("PRIOR_RELEASE", message)

    if not (isinstance(record, dict) and set(record) == set(PRIOR_FIELDS)):
        bad("The prior jobs release record does not hold exactly its fields")
    if record["schema_version"] != SCHEMA_VERSION or record["kind"] != PRIOR_KIND:
        bad("The prior jobs release record is not a prior-jobs-release of this schema version")
    rid, commit = record["release_id"], record["commit"]
    if not (isinstance(rid, str) and so.RELEASE_ID.match(rid) and isinstance(commit, str) and COMMIT.match(commit) and rid[4:11] == commit[:7]):
        bad("The prior jobs release record names a release id that does not name its commit")
    if not (isinstance(record["image_digest"], str) and DIGEST.match(record["image_digest"])):
        bad("The prior jobs release record holds no sha256 image digest")
    terminal = record["terminal"]
    if not (isinstance(terminal, dict) and set(terminal) == {"kind", "at_utc"} and terminal["kind"] == PRIOR_TERMINAL_KIND
            and aware_utc(terminal["at_utc"]) is not None):
        bad("The prior jobs release record holds no timezone aware AfterJobsUpdate terminal")
    definitions = record["job_definitions"]
    if not (isinstance(definitions, dict) and set(definitions) == set(so.JOB_NAMES)
            and all(isinstance(v, str) and SHA256.match(v) for v in definitions.values())):
        bad("The prior jobs release record does not hold one definition hash for each of the fourteen jobs")
    sources = record["sources"]
    if not (isinstance(sources, dict) and set(sources) == set(PRIOR_SOURCES)
            and all(isinstance(v, str) and SHA256.match(v) for v in sources.values())):
        bad("The prior jobs release record does not hold the hashes of the files it was derived from")
    return record


def service_state(reader, name):
    """The part of a service Release B compares: annotations, template, traffic, the revision list and the one serving
    revision, which must take 100% of the traffic. The shape of the services part of baseline-A."""
    view = so.service_view(reader.service(name))
    serving = [e for e in view["traffic"]["status"] if e["percent"] > 0]
    require(len(serving) == 1 and serving[0]["percent"] == 100, "BASELINE", f"{name} does not serve one revision at 100%")
    return {"annotations": view["annotations"], "template": view["template"], "traffic": view["traffic"],
            "revisions": sorted(reader.revisions(name)), "serving": so.revision_view(reader.revision(serving[0]["revision"]))}


def tags_of(state):
    entries = state["traffic"]["spec"] + state["traffic"]["status"]
    return sorted({e["tag"] for e in entries if e.get("tag")})


def expected_serving(a_terminal, a80_serving):
    if a_terminal["kind"] == "AfterPromotion":
        return {name: f"{name}-{a_terminal['a_release_id']}" for name in so.SERVICES}
    require(isinstance(a80_serving, dict) and set(a80_serving) == set(so.SERVICES)
            and all(isinstance(v, str) and v for v in a80_serving.values()), "BASELINE",
            "No a80 serving revision is named for each service, which an A that was rolled back needs")
    return dict(a80_serving)


def check_terminal(a_terminal, captured_utc):
    require(isinstance(a_terminal, dict) and a_terminal.get("kind") in A_TERMINAL_KINDS, "A_STATE",
            "Release A's terminal readback is not AfterPromotion or AfterRollback")
    at, captured = aware_utc(a_terminal.get("at_utc")), aware_utc(captured_utc)
    require(at is not None, "A_STATE", "Release A's terminal readback has no timezone aware time")
    require(captured is not None and at <= captured, "A_STATE", "Release A's terminal readback is after the capture")
    require(isinstance(a_terminal.get("a_release_id"), str) and so.RELEASE_ID.match(a_terminal["a_release_id"]), "A_STATE",
            "Release A's release id is not rel-<sha7>-<nn>")


def capture_baseline_j(reader, *, a_terminal, a80_serving, rollback_digest, a_retire, captured_utc, prior=None):
    check_terminal(a_terminal, captured_utc)
    require(isinstance(rollback_digest, str) and DIGEST.match(rollback_digest), "BASELINE", "The rollback digest is not a sha256 digest")
    if prior is not None:
        validate_prior_jobs_release(prior)
        require(rollback_digest == prior["image_digest"], "BASELINE", "The rollback digest is not the digest the prior jobs release left")
        require(aware_utc(prior["terminal"]["at_utc"]) <= aware_utc(captured_utc), "BASELINE",
                "The prior jobs release's terminal readback is after the capture")
    require(a_retire in A_RETIRE, "BASELINE", "Release A's Retire is neither declared done nor declared waiting until after B")
    jobs = {name: so.job_view(reader.job(name)) for name in so.JOB_NAMES}
    images = {view["image"] for view in jobs.values()}
    require(len(images) == 1, "BASELINE", "The 14 jobs do not run one digest")
    require(images == {f"{JOBS_REPO}@{rollback_digest}"}, "BASELINE", "The jobs do not run the bound rollback digest")
    if prior is not None:
        for name in so.JOB_NAMES:
            require(so.fingerprint(jobs[name]) == prior["job_definitions"][name], "BASELINE",
                    f"The job {name} is not the definition the prior jobs release left")
    services = {name: service_state(reader, name) for name in so.SERVICES}
    expected = expected_serving(a_terminal, a80_serving)
    for name in so.SERVICES:
        require(services[name]["serving"]["name"] == expected[name], "BASELINE",
                f"{name} is neither Release A's promoted candidate nor the restored a80 revision")
        has_tag = a_terminal["a_release_id"] in tags_of(services[name])
        require(has_tag == (a_retire == "waits_for_b"), "BASELINE",
                f"The tag set of {name} contradicts the declared state of Release A's Retire")
    baseline = {"schema_version": SCHEMA_VERSION, "kind": "baseline-J", "captured_utc": captured_utc,
                "aTerminal": {"kind": a_terminal["kind"], "at_utc": a_terminal["at_utc"], "a_release_id": a_terminal["a_release_id"]},
                "aRetire": a_retire, "rollbackJobsDigest": rollback_digest, "jobs": jobs, "services": services}
    if a80_serving is not None:
        baseline["a80Serving"] = dict(a80_serving)
    if prior is not None:
        baseline["priorJobsRelease"] = copy.deepcopy(prior)
    return baseline


def write_once(path, baseline):
    path = Path(path)
    require(not path.exists(), "BASELINE", "baseline-J already exists; it is written once")
    data = (json.dumps(baseline, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as out:
        out.write(data)
    return so.sha_bytes(data)
