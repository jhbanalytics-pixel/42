"""The command line that produces the files a services-only release packet is made of (packet flag F6).

    py -3.13 -m core.setup.release.packet capture-baseline --out <file> --gcloud-timeout-seconds <n>
    py -3.13 -m core.setup.release.packet bind    --repo <checkout> --release-commit <40 hex> --attempt <nn> --packet-dir <dir>
        --baseline <file> --durable-manifest <file> --caller-account <email> --gcloud-timeout-seconds <n>
        --http-timeout-seconds <n> --max-smoke-age-minutes <n> --inflight-asks-bytes-cap <n>
    py -3.13 -m core.setup.release.packet lock    --repo <checkout> --packet-dir <dir> [--lock-dir <dir>]
    py -3.13 -m core.setup.release.packet review  --repo <checkout> --packet-dir <dir> --verdict ACCEPT|REJECT [--lock-dir <dir>]
    py -3.13 -m core.setup.release.packet receipt --repo <checkout> --packet-dir <dir> --authorisation-line-file <file>
        [--steps Candidate Promote Rollback Retire] [--lock-dir <dir>]

Release B, the jobs image release, has the same four commands in mode jobs, plus the capture of baseline-J (W8-REL-B v2.1 3.8, JB-05):

    py -3.13 -m core.setup.release.packet capture-baseline-j --out <file> --gcloud-timeout-seconds <n> --a-readback <file>
        --a-retire retired|waits_for_b
    py -3.13 -m core.setup.release.packet bind --mode jobs [--producer-only] --repo <checkout> --release-commit <40 hex> --attempt 01
        --packet-dir <dir> --baseline-j <file> --caller-account <email> --gcloud-timeout-seconds <n> --http-timeout-seconds <n>
        --chain-evidence-bytes-cap <n> --collect-start-tolerance-minutes <0 to 30> --baseline-chain <file> --durable-manifest <file>
        --dry-run-receipt <file> --max-baseline-age-days <n> --max-candidate-age-hours <n>
    py -3.13 -m core.setup.release.packet lock|review|receipt --mode jobs ...   as above, with the jobs steps JobsCandidate JobsUpdate JobsRollback

bind --mode jobs --producer-only writes producer-bindings.json, the keys the chain evidence producer reads, before the baseline chain
manifest exists; the full bind then names that manifest. The rollback digest is a literal of this module (the e77c3819 image of
W8-REL-B v2.1 FB-5) that baseline-J is held to, never a value read from baseline-J, and the schema receipt hash is worked out from the
durable manifest. Every durable file of a jobs release lies in the packet directory, as the paste requires.

Each subcommand wraps a function that already exists (capture_baseline, validate_bindings and stage1, build_lock, lock_problems,
review_for) and writes one file under a fixed name, once: an existing file is never replaced. The packet directory is also the
release directory, so it holds live-bindings.json, independent-review.json, execution-receipt.json and the two receipts the
release paste looks for by name (compat-receipt.json, old-reader-receipt.json).

capture-baseline reads the live system through GcloudReader, which refuses every gcloud command outside bound_readback.READ_PREFIXES,
and it refuses to write a baseline unless live is the a80 pair on the e77c3819 jobs image. Nothing here prints or stores an
environment value, and a refusal names a field, never a value.

Values are taken from where they are proven, not from the caller: the target and tree from git, the hashes from the bytes on disk,
the smoke check count from smoke.py by AST, the build config hash from the commit, the in-flight query hash from the checker, the
environment deltas from the baseline. What the code cannot know (the caller account, the timeouts, the smoke age, the bytes cap)
is a required flag with no default. Exit 0 done, 1 refused, 2 a bad command line, 3 a read that could not complete.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from core.setup import durable_effects_check as dec  # noqa: E402
from core.setup.release import bound_readback as helper  # noqa: E402
from core.setup.release import lock as locklib  # noqa: E402
from core.setup.release import natives  # noqa: E402
from core.setup.release import services_only as so  # noqa: E402
from core.setup.release import chain_evidence as ce  # noqa: E402
from core.setup.release import jobs_baseline as jb  # noqa: E402
from core.setup.release import jobs_only as jo  # noqa: E402
from core.setup.release import jobs_source as js  # noqa: E402

STEPS = ("Candidate", "Promote", "Rollback", "Retire")
BINDINGS_FILE = "live-bindings.json"
REVIEW_FILE = "independent-review.json"
RECEIPT_FILE = "execution-receipt.json"
COMPAT_FILE = "compat-receipt.json"
OLD_READER_FILE = "old-reader-receipt.json"
PASTE = "core/setup/release/SERVICES-PASTE.ps1"
BUILD_CONFIG = "core/api/cloudbuild.yaml"
STATUS = "BOUND_FOR_INDEPENDENT_REVIEW"
# Live must be a80be1d before a release is bound to it (C2 v2.1 section 3.4): the serving revisions, and the jobs image (W8-REL-B v2.1 FB-5).
A80_SERVING = {"f42-agent": "f42-agent-00047-677", "f42-api": "f42-api-00041-lns"}
A80_JOBS_DIGEST_PREFIX = "sha256:e77c3819"
# The sentence Albert types to authorise the one paid Ask (RELEASE-A-PACKET section 5), with the release id as the one place that varies.
# What he types must equal it after whitespace is normalised, figures included: a line that holds every clause and adds a refusal does not.
AUTHORISATION_TEMPLATE = (
    "RELEASE A {release_id}: I authorise exactly one live T1 Ask, market ZA, against the candidate API tag URL only, at most 60 search "
    "credits and a USD 2.00 research budget, with a model hold ceiling of USD 4.32, no second Ask and no retry. The window is quiet and "
    "I will start no manual job until the execution ends. I will type DEPLOY, IDLE and the passcode myself.")
# A later attempt needs the earlier attempts and their ledgers cited by hash, which bind does not carry yet.
SUPPORTED_ATTEMPT = "01"

# Release B (jobs only). The rollback target is the e77c3819 jobs image every job runs today (W8-REL-B v2.1 FB-5); it is pinned here in
# full and baseline-J is held to it, so no value the baseline states about itself decides what the rollback restores.
MODES = ("services-only", "jobs")
ROLLBACK_JOBS_DIGEST = "sha256:e77c3819cc688d7690d4370a5fd575026e4d3e62c0ab322d54b6eabb515c75de"
PRODUCER_FILE = "producer-bindings.json"
SCHEMA_RECEIPT_FILE = "schema-readback-receipt.json"
JOBS_STEPS = ("JobsCandidate", "JobsUpdate", "JobsRollback")
JOBS_BUILD_CONFIG = "core/setup/cloudbuild.jobs.yaml"
JOBS_DOCKERFILE = "core/setup/jobs.Dockerfile"
# The sentence Albert types to authorise Release B, with the release id as the one place that varies and the window, the deadline and
# the job count taken from the code that enforces them. It names the release id and that only the jobs change, and it asks for no passcode
# because Release B runs no Ask. What he types must equal it after whitespace is normalised.
JOBS_AUTHORISATION_TEMPLATE = (
    "RELEASE B {release_id}: I authorise the jobs only release of this release id and nothing else. Only the {n_jobs} Cloud Run jobs "
    "change, to one image built from the release commit; no service revision, traffic entry, tag, scheduler entry, secret, "
    "environment variable or paid Ask changes. JobsUpdate runs on the day of JobsCandidate between {start} and {end} SAST, and an "
    "update that stops is finished by {end} SAST or undone by JobsRollback before {deadline} SAST. The window is quiet and I will "
    "start no manual job until the execution ends. I will type DEPLOY and IDLE myself.")


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


class Refused(Exception):
    """The command will not write its file. The message names a field or a file, never a value."""


# small helpers

def sha_bytes(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_new(path, text):
    """Create the file, once. An existing file is never replaced."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as out:
            out.write(text)
    except FileExistsError:
        raise Refused(f"{path.name} already exists; it is never replaced") from None


def write_json_new(path, value):
    write_new(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def refuse_existing(path):
    if Path(path).exists():
        raise Refused(f"{Path(path).name} already exists; it is never replaced")


def read_input(path, what):
    """(the JSON object, its sha256) from one read of the file, so the bytes that are validated are the bytes that are hashed."""
    path = Path(path)
    if not path.is_file():
        raise Refused(f"{what} is missing: {path.name}")
    data = path.read_bytes()
    try:
        value = json.loads(data.decode("utf-8-sig"))
    except ValueError:
        raise Refused(f"{what} is not readable JSON: {path.name}") from None
    if not isinstance(value, dict):
        raise Refused(f"{what} is not an object: {path.name}")
    return value, hashlib.sha256(data).hexdigest()


def read_json(path, what):
    return read_input(path, what)[0]


def git_program():
    """git as the paste found it in its install folder (core/setup/release/natives.py); never a lookup on PATH."""
    try:
        return natives.native("git")
    except natives.NativeRefused as error:
        raise Refused(str(error)) from None


def git(repo, *args):
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    program = git_program()
    try:
        done = subprocess.run([program, *args], cwd=repo, capture_output=True, encoding="utf-8", timeout=60, env=env,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        raise Refused("git could not be run in the checkout") from None
    if done.returncode != 0:
        raise Refused("git could not read the checkout")
    return done.stdout.strip()


def checkout(repo, *, clean):
    """(HEAD, its tree). The checkout must hold the commit it is bound to; for bind and lock it must also have no change."""
    head, tree = git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "HEAD^{tree}")
    if clean and git(repo, "status", "--porcelain=v1"):
        raise Refused("The checkout is not clean")
    return head, tree


def blob_sha256(repo, commit, path):
    """The hash of a file at a commit with CRLF read as LF: what GcloudReader.source_file_sha256 gives the Freeze check, taken in repo."""
    program = git_program()
    try:
        done = subprocess.run([program, "show", f"{commit}:{path}"], cwd=repo, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        raise Refused("git could not be run in the checkout") from None
    if done.returncode != 0:
        raise Refused(f"{path} is not in the commit")
    return so.sha_bytes(done.stdout.replace(b"\r\n", b"\n"))


def positive_int(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not a whole number") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def load_bindings(packet_dir, mode="services-only"):
    path = Path(packet_dir) / BINDINGS_FILE
    bound = read_json(path, "the bindings")
    try:
        if mode == "jobs":
            jo.validate_jobs_bindings(bound)
        else:
            so.validate_bindings(bound)
    except so.Stop as error:
        raise Refused(f"The bindings are not accepted: {error.code}: {error.message}") from None
    if bound.get("status") != STATUS:
        raise Refused("The bindings are not bound for independent review")
    return path, bound


def lock_file(repo, lock_dir, release_id):
    """Where the lock of a release lives: the committed folder of the checkout, or the folder --lock-dir names. The paste wants the
    checkout at the bound commit with no change, so a lock that must stay out of the checkout is kept in a folder of its own."""
    default = locklib.lock_path(repo, release_id)
    return Path(lock_dir) / default.name if lock_dir else default


def bound_file_paths(bound, bindings_path):
    """The five files the lock hashes, found as the release paste finds them (Resolve-Paths)."""
    root = Path(bound["releaseDir"]).resolve()
    named = {}
    for key, default in (("compatReceiptPath", COMPAT_FILE), ("oldReaderReceiptPath", OLD_READER_FILE)):
        named[key] = so.inside(root, bound[key]) if key in bound else root / default
    return {"bindings": Path(bindings_path), "baseline": Path(bound["baselinePath"]), "durable_manifest": Path(bound["durableManifestPath"]),
            "compat_receipt": named["compatReceiptPath"], "old_reader_receipt": named["oldReaderReceiptPath"]}


def bound_hashes(bound):
    return {"baseline": bound["baselineSha256"], "durable_manifest": bound["durableManifestSha256"],
            "compat_receipt": bound["compatReceiptSha256"], "old_reader_receipt": bound["oldReaderReceiptSha256"]}


# capture-baseline

def baseline_reader(gcloud_timeout):
    """The real read-only reader. Its gcloud calls are limited to bound_readback.READ_PREFIXES and time out."""
    return helper.GcloudReader({"gcloud": gcloud_timeout, "http": gcloud_timeout})


def check_a80(baseline):
    for name, revision in A80_SERVING.items():
        serving = baseline["services"][name]["serving"]["name"]
        if serving != revision:
            raise Refused(f"Live is not a80be1d: {name} serves {serving}, not {revision}")
    for name, view in baseline["jobs"].items():
        if f"@{A80_JOBS_DIGEST_PREFIX}" not in str(view.get("image")):
            raise Refused(f"The job {name} is not on the e77c3819 image")


def cmd_capture_baseline(args):
    out = Path(args.out)
    refuse_existing(out)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    baseline = so.capture_baseline(baseline_reader(args.gcloud_timeout_seconds), now)
    check_a80(baseline)
    write_json_new(out, baseline)
    print(f"wrote {out}\nsha256 {sha_bytes(out)}")
    return 0


# Release B: capture-baseline-j and bind --mode jobs

def read_a_readback(path):
    """Release A's terminal readback, as the file its helper wrote: AfterPromotion or AfterRollback, in services mode, with the release
    id and the time of that phase. It names the terminal state; the live read that follows holds the services to it by revision name."""
    value, _ = read_input(path, "Release A's readback")
    if (value.get("schema_version") != so.SCHEMA_VERSION or value.get("mode") != "services-only"
            or value.get("phase") not in jb.A_TERMINAL_KINDS):
        raise Refused("The file is not a terminal readback of Release A (AfterPromotion or AfterRollback, services mode)")
    release_id, at_utc = value.get("release_id"), value.get("at_utc")
    if not isinstance(release_id, str) or not so.RELEASE_ID.match(release_id) or not isinstance(at_utc, str):
        raise Refused("Release A's readback names no release id or no time")
    return {"kind": value["phase"], "at_utc": at_utc, "a_release_id": release_id}


def cmd_capture_baseline_j(args):
    out = Path(args.out)
    refuse_existing(out)
    a_terminal = read_a_readback(args.a_readback)
    captured = utc_now().replace(microsecond=0).isoformat()
    # An A that was rolled back restores the a80 pair, whose revision names are literals of this module, never typed.
    a80 = dict(A80_SERVING) if a_terminal["kind"] == "AfterRollback" else None
    baseline = jb.capture_baseline_j(baseline_reader(args.gcloud_timeout_seconds), a_terminal=a_terminal, a80_serving=a80,
                                     rollback_digest=ROLLBACK_JOBS_DIGEST, a_retire=args.a_retire, captured_utc=captured)
    digest = jb.write_once(out, baseline)
    print(f"wrote {out}\nsha256 {digest}")
    return 0


def load_baseline_j(path):
    """(baseline-J, its sha256), from one read. Its fourteen images are held to the pinned rollback digest, which the file does not supply."""
    baseline, digest = read_input(path, "baseline-J")
    if baseline.get("kind") != "baseline-J" or baseline.get("schema_version") != so.SCHEMA_VERSION:
        raise Refused("The baseline file is not a baseline-J receipt")
    jo.load_baseline_j({"baselinePath": str(path), "baselineSha256": digest})
    wanted = f"{jo.JOBS_REPO}@{ROLLBACK_JOBS_DIGEST}"
    for name, view in baseline["jobs"].items():
        if not isinstance(view, dict) or view.get("image") != wanted:
            raise Refused(f"The job {name} is not on the pinned rollback digest")
    return baseline, digest


def jobs_inside(root, path):
    """A durable file of a jobs release lies in the release directory, which is the packet directory (Resolve-Paths of the paste)."""
    return so.inside(root, path)


def cmd_bind_jobs(args):
    if args.attempt != SUPPORTED_ATTEMPT:
        raise Refused(f"bind supports attempt {SUPPORTED_ATTEMPT} only: a later attempt must cite the earlier attempts and their ledgers, which it does not carry")
    packet_dir = Path(args.packet_dir)
    out = packet_dir / (PRODUCER_FILE if args.producer_only else BINDINGS_FILE)
    refuse_existing(out)
    repo = Path(args.repo)
    if not re.fullmatch(r"[0-9a-f]{40}", args.release_commit):
        raise Refused("--release-commit must be the full 40 character commit")
    head, tree = checkout(repo, clean=True)
    if head != args.release_commit:
        raise Refused("The checkout is not at the commit named by --release-commit")
    release_id = f"rel-{head[:7]}-{args.attempt}"
    baseline, baseline_sha = load_baseline_j(args.baseline_j)
    root = packet_dir.resolve()
    bound = {
        "schema_version": so.SCHEMA_VERSION, "mode": "jobs", "release_id": release_id, "target": head, "tree": tree, "releaseDir": str(root),
        "baselinePath": str(Path(args.baseline_j).resolve()), "baselineSha256": baseline_sha, "rollbackJobsDigest": ROLLBACK_JOBS_DIGEST,
        "callerAccount": args.caller_account, "readTimeoutSeconds": {"gcloud": args.gcloud_timeout_seconds, "http": args.http_timeout_seconds},
        "chainEvidenceBytesCap": args.chain_evidence_bytes_cap, "templateHashes": ce.template_hashes(),
        "collectStartToleranceMinutes": args.collect_start_tolerance_minutes,
    }
    if args.producer_only:
        jo.validate_jobs_bindings(bound, producer=True)
        write_json_new(out, bound)
        print(f"wrote {out}\nrelease_id {release_id}\nsha256 {sha_bytes(out)}")
        return 0
    from core.setup import deploy_jobs as dj

    chain_path = jobs_inside(root, args.baseline_chain)
    durable_path = jobs_inside(root, args.durable_manifest)
    dry_run_path = jobs_inside(root, args.dry_run_receipt)
    durable_value, durable_sha = read_input(durable_path, "the durable-effects manifest")
    problems = dec.validate_manifest(durable_value)
    if problems:
        raise Refused("The durable-effects manifest is not valid: " + problems[0])
    other = durable_binding_problem(durable_value, release_id, head, tree)
    if other:
        raise Refused(f"The durable-effects manifest is for another release: {other} does not match the release being bound")
    if not dry_run_path.is_file() or not chain_path.is_file():
        raise Refused("The apply dry run receipt or the baseline chain manifest is missing")
    dry_run_bytes = dry_run_path.read_bytes()
    dry_run_sha = so.sha_bytes(dry_run_bytes)
    receipt_problems = js.dry_run_receipt_problems(dry_run_bytes, dry_run_sha, dec.git_tree_files(head, "core", repo))
    if receipt_problems:
        raise Refused("The apply dry run receipt is not clean: " + "; ".join(receipt_problems))
    bound.update({
        "baselineChainPath": str(chain_path), "baselineChainSha256": sha_bytes(chain_path), "buildServiceAccount": dj.DEPLOYER,
        "buildConfigSha256": blob_sha256(repo, head, JOBS_BUILD_CONFIG), "dockerfileSha256": blob_sha256(repo, head, JOBS_DOCKERFILE),
        "durableManifestSha256": durable_sha, "durableManifestPath": str(durable_path), "dryRunReceiptSha256": dry_run_sha,
        "dryRunReceiptPath": str(dry_run_path), "schemaReadbackReceiptPath": str(root / SCHEMA_RECEIPT_FILE),
        "quietTemplateHashes": jo.quiet_template_hashes(), "window": dict(jo.PINNED_WINDOW), "maxBaselineAgeDays": args.max_baseline_age_days,
        "maxCandidateAgeHours": args.max_candidate_age_hours, "priorAttempts": [], "priorLedgers": [], "status": STATUS,
    })
    # What the receipt JobsCandidate leaves must say is worked out from the durable manifest, not typed.
    bound["schemaReadbackReceiptSha256"] = so.fingerprint(jo.schema_receipt_body(bound, durable_value))
    jo.validate_jobs_bindings(bound)
    # The baseline chain manifest says nothing about the tolerance, the cap or the caller it was produced under, so it is tied to the
    # producer bindings written in this packet directory: every key the producer read must be what these bindings say.
    produced = read_json(packet_dir / PRODUCER_FILE, "the producer bindings (run bind --producer-only first)")
    differs = [key for key in (*jo.PRODUCER_BINDINGS, "schema_version", "mode") if produced.get(key) != bound.get(key)]
    if differs:
        raise Refused("The baseline chain manifest was produced under other bindings than these: " + ", ".join(differs))
    chain_head = (read_json(chain_path, "the baseline chain manifest").get("generated_from") or {}).get("head")
    if chain_head != head:
        raise Refused("The baseline chain manifest was generated from another commit than the one being bound")
    # The baseline chain is judged by the check JobsCandidate will make, against baseline-J, the pinned digest and the clock.
    jo.check_baseline_chain(jo.JobsRelease(bound, None, packet_dir, now=utc_now))
    write_json_new(out, bound)
    print(f"wrote {out}\nrelease_id {release_id}\nsha256 {sha_bytes(out)}")
    return 0


# bind

def paste_build_service_account():
    text = (Path(__file__).resolve().parent / "SERVICES-PASTE.ps1").read_text(encoding="utf-8")
    found = set(re.findall(r"serviceAccounts/(f42-deployer@[A-Za-z0-9.-]+\.iam\.gserviceaccount\.com)", text))
    if len(found) != 1:
        raise Refused("The paste does not name one build service account")
    return found.pop()


def load_baseline(path):
    """(the baseline, its sha256), from one read."""
    baseline, digest = read_input(path, "the baseline")
    if baseline.get("kind") != "baseline-A" or baseline.get("schema_version") != so.SCHEMA_VERSION:
        raise Refused("The baseline file is not a baseline-A receipt")
    if set(baseline.get("jobs", {})) != set(so.JOB_NAMES) or set(baseline.get("services", {})) != set(so.SERVICES):
        raise Refused("The baseline does not hold both services and the fourteen jobs")
    check_a80(baseline)
    return baseline, digest


def passing_receipt(path, label):
    """The sha256 of a receipt that says pass, from the same read that showed it."""
    receipt, digest = read_input(path, f"the {label} receipt")
    if receipt.get("schema_version") != so.SCHEMA_VERSION or receipt.get("verdict") != "pass":
        raise Refused(f"The {label} receipt does not say pass")
    return digest


def durable_binding_problem(manifest, release_id, commit, tree):
    """The first field of the durable manifest that does not name this release, or None. The expected values are the ones bind computed
    from git, never read from the manifest; a refusal names the field and never the value."""
    source, generated = manifest.get("source"), manifest.get("generated_from")
    for field, found, expected in (
            ("release_id", manifest.get("release_id"), release_id),
            ("source.commit", source.get("commit") if isinstance(source, dict) else None, commit),
            ("source.tree", source.get("tree") if isinstance(source, dict) else None, tree),
            ("generated_from.head", generated.get("head") if isinstance(generated, dict) else None, commit)):
        if found != expected:
            return field
    return None


def cmd_bind(args):
    if args.mode == "jobs":
        return cmd_bind_jobs(args)
    if args.attempt != SUPPORTED_ATTEMPT:
        raise Refused(f"bind supports attempt {SUPPORTED_ATTEMPT} only: a later attempt must cite the earlier attempts and their ledgers, which it does not carry")
    packet_dir = Path(args.packet_dir)
    out = packet_dir / BINDINGS_FILE
    refuse_existing(out)
    repo = Path(args.repo)
    if not re.fullmatch(r"[0-9a-f]{40}", args.release_commit):
        raise Refused("--release-commit must be the full 40 character commit")
    head, tree = checkout(repo, clean=True)
    if head != args.release_commit:
        raise Refused("The checkout is not at the commit named by --release-commit")
    release_id = f"rel-{head[:7]}-{args.attempt}"
    baseline, baseline_sha = load_baseline(args.baseline)
    compat = passing_receipt(packet_dir / COMPAT_FILE, "compat")
    old = passing_receipt(packet_dir / OLD_READER_FILE, "old-reader")
    durable = Path(args.durable_manifest)
    durable_value, durable_sha = read_input(durable, "the durable-effects manifest")
    problems = dec.validate_manifest(durable_value)
    if problems:
        raise Refused("The durable-effects manifest is not valid: " + problems[0])
    other = durable_binding_problem(durable_value, release_id, head, tree)
    if other:
        raise Refused(f"The durable-effects manifest is for another release: {other} does not match the release being bound")
    build_config = blob_sha256(repo, head, BUILD_CONFIG)
    agent = baseline["canonical"]["f42-agent"]
    short = head[:12]
    bound = {
        "schema_version": so.SCHEMA_VERSION, "mode": "services-only", "status": STATUS, "release_id": release_id, "target": head,
        "tree": tree, "releaseDir": str(packet_dir.resolve()), "baselinePath": str(Path(args.baseline).resolve()),
        "baselineSha256": baseline_sha, "callerAccount": args.caller_account,
        "buildServiceAccount": paste_build_service_account(), "buildConfigSha256": build_config,
        "readTimeoutSeconds": {"gcloud": args.gcloud_timeout_seconds, "http": args.http_timeout_seconds},
        "maxSmokeAgeMinutes": args.max_smoke_age_minutes,
        "expectedSmokeChecks": locklib.smoke_check_count(repo / "core/api/smoke.py"),
        "envDeltas": {"f42-agent": {"F42_VERSION": short},
                      "f42-api": {"F42_VERSION": short, "AGENT_URL": so.tag_url(release_id, agent), "AGENT_AUDIENCE": agent}},
        "compatReceiptSha256": compat, "oldReaderReceiptSha256": old, "durableManifestPath": str(durable.resolve()),
        "durableManifestSha256": durable_sha, "inflightAsksSqlSha256": dec.INFLIGHT_ASKS_SQL_SHA256,
        "inflightAsksBytesCap": args.inflight_asks_bytes_cap,
    }
    try:
        so.validate_bindings(bound)
        so.stage1(bound, baseline)
    except so.Stop as error:
        raise Refused(f"The bindings would not be accepted: {error.code}: {error.message}") from None
    write_json_new(out, bound)
    print(f"wrote {out}\nrelease_id {release_id}\nsha256 {sha_bytes(out)}")
    return 0


# lock

def jobs_bound_file_paths(bound, bindings_path):
    """The five files the jobs lock hashes, found as JOBS-PASTE.ps1 finds them (Resolve-Paths and Test-Packet)."""
    return {"bindings": Path(bindings_path), "baseline": Path(bound["baselinePath"]), "durable_manifest": Path(bound["durableManifestPath"]),
            "baseline_chain": Path(bound["baselineChainPath"]), "dry_run_receipt": Path(bound["dryRunReceiptPath"])}


def jobs_bound_hashes(bound):
    return {"baseline": bound["baselineSha256"], "durable_manifest": bound["durableManifestSha256"],
            "baseline_chain": bound["baselineChainSha256"], "dry_run_receipt": bound["dryRunReceiptSha256"]}


def cmd_lock(args):
    repo, packet_dir = Path(args.repo), Path(args.packet_dir)
    jobs = args.mode == "jobs"
    bindings_path, bound = load_bindings(packet_dir, args.mode)
    head, _ = checkout(repo, clean=True)
    if head != bound["target"]:
        raise Refused("The checkout is not at the bound commit")
    files = jobs_bound_file_paths(bound, bindings_path) if jobs else bound_file_paths(bound, bindings_path)
    for name, expected in (jobs_bound_hashes(bound) if jobs else bound_hashes(bound)).items():
        if not files[name].is_file() or sha_bytes(files[name]) != expected:
            raise Refused(f"The {name} file differs from the bindings")
    path = lock_file(repo, args.lock_dir, bound["release_id"])
    refuse_existing(path)
    lock = locklib.build_lock(repo, bound["release_id"], files, names=locklib.JOBS_BOUND_NAMES) if jobs else locklib.build_lock(repo, bound["release_id"], files)
    problems = locklib.lock_problems(repo, lock)
    if problems:
        raise Refused("The lock does not match the checkout: " + problems[0])
    write_json_new(path, lock)
    print(f"wrote {path}\nsha256 {sha_bytes(path)}")
    return 0


# review

def bound_lock(repo, packet_dir, lock_dir, mode="services-only"):
    """The bindings, the lock that binds them, and the lock's path. Refuses a lock that binds anything else."""
    bindings_path, bound = load_bindings(packet_dir, mode)
    path = lock_file(repo, lock_dir, bound["release_id"])
    lock = read_json(path, "the lock")
    if lock.get("release_id") != bound["release_id"] or lock.get("bound_files", {}).get("bindings") != sha_bytes(bindings_path):
        raise Refused("The lock binds other bindings than the bindings file")
    return bound, lock, path


def cmd_review(args):
    repo, packet_dir = Path(args.repo), Path(args.packet_dir)
    out = packet_dir / REVIEW_FILE
    refuse_existing(out)
    bound, lock, path = bound_lock(repo, packet_dir, args.lock_dir, args.mode)
    head, _ = checkout(repo, clean=False)
    if head != bound["target"]:
        raise Refused("The checkout is not at the bound commit")
    problems = locklib.lock_problems(repo, lock)
    if problems:
        raise Refused("The lock does not match the checkout: " + problems[0])
    write_json_new(out, locklib.review_for(path, bound["target"], args.verdict))
    print(f"wrote {out}\nverdict {args.verdict}\nlock_sha256 {sha_bytes(path)}")
    return 0


# receipt

def jobs_authorisation(release_id):
    window = jo.PINNED_WINDOW
    return JOBS_AUTHORISATION_TEMPLATE.format(release_id=release_id, n_jobs=len(so.JOB_NAMES), start=window["startSast"], end=window["endSast"],
                                              deadline=window["rollbackDeadlineSast"])


def check_authorisation(text, release_id, mode="services-only"):
    expected = jobs_authorisation(release_id) if mode == "jobs" else AUTHORISATION_TEMPLATE.format(release_id=release_id)
    if " ".join(text.split()) != expected:
        raise Refused("The authorisation line is not the one the packet asks Albert to type for this release")


def cmd_receipt(args):
    repo, packet_dir = Path(args.repo), Path(args.packet_dir)
    out = packet_dir / RECEIPT_FILE
    refuse_existing(out)
    jobs = args.mode == "jobs"
    allowed = JOBS_STEPS if jobs else STEPS
    wanted = list(args.steps) if args.steps else list(allowed)
    if [step for step in wanted if step not in allowed]:
        raise Refused(f"A step is not one of the {args.mode} steps: {', '.join(allowed)}")
    bound, lock, path = bound_lock(repo, packet_dir, args.lock_dir, args.mode)
    review_path = packet_dir / REVIEW_FILE
    if not review_path.is_file():
        raise Refused("There is no independent review for this lock")
    review = read_json(review_path, "the independent review")
    if review.get("verdict") != "ACCEPT":
        raise Refused("The independent review does not say ACCEPT")
    if review.get("lock_sha256") != sha_bytes(path) or review.get("target") != bound["target"]:
        raise Refused("The independent review does not bind this lock and target")
    line_file = Path(args.authorisation_line_file)
    if not line_file.is_file():
        raise Refused("The authorisation line file is missing")
    check_authorisation(line_file.read_text(encoding="utf-8-sig"), bound["release_id"], args.mode)
    steps = [step for step in allowed if step in wanted]
    receipt = {"schema_version": so.SCHEMA_VERSION, "release_id": bound["release_id"], "target": bound["target"],
               "declared_steps": steps, "later_explicit_user_instruction": True, "quiet_window_confirmed": True,
               "no_new_manual_starts_until_execution_ends": True, "single_T1_smoke_authorized": True, "max_live_asks": 1}
    if jobs:
        receipt = {"schema_version": so.SCHEMA_VERSION, "release_id": bound["release_id"], "target": bound["target"], "mode": "jobs",
                   "declared_steps": steps, "later_explicit_user_instruction": True, "quiet_window_confirmed": True,
                   "no_new_manual_starts_until_execution_ends": True, "jobs_only": True}
    write_json_new(out, receipt)
    print(f"wrote {out}\nsteps {' '.join(steps)}\nsha256 {sha_bytes(out)}")
    return 0


# the command line

def nonnegative_int(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not a whole number") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return value


# The flags each command needs in each mode. They are checked after parsing, so one subcommand serves both releases and a missing
# flag is still a bad command line (exit 2), as it was when argparse held them.
REQUIRED = {
    ("bind", "services-only"): ("baseline", "durable_manifest", "caller_account", "gcloud_timeout_seconds", "http_timeout_seconds",
                                "max_smoke_age_minutes", "inflight_asks_bytes_cap"),
    ("bind", "jobs"): ("baseline_j", "caller_account", "gcloud_timeout_seconds", "http_timeout_seconds", "chain_evidence_bytes_cap",
                       "collect_start_tolerance_minutes"),
    ("bind", "jobs-full"): ("baseline_chain", "durable_manifest", "dry_run_receipt", "max_baseline_age_days", "max_candidate_age_hours"),
}


def check_required(parser, args):
    if args.command != "bind":
        return
    names = list(REQUIRED[("bind", args.mode)])
    if args.mode == "jobs" and not args.producer_only:
        names += REQUIRED[("bind", "jobs-full")]
    if args.mode != "jobs" and args.producer_only:
        parser.error("--producer-only belongs to --mode jobs")
    missing = [name for name in names if getattr(args, name) is None]
    if missing:
        parser.error("the following arguments are required: " + ", ".join("--" + name.replace("_", "-") for name in missing))


def add_mode(command):
    command.add_argument("--mode", choices=MODES, default="services-only", help="services-only (Release A) or jobs (Release B)")


def build_parser():
    parser = argparse.ArgumentParser(prog="packet", description="Produce the files of a release packet, services only or jobs only.")
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("capture-baseline", help="read live (read-only gcloud) and write the baseline receipt, once")
    capture.add_argument("--out", type=Path, required=True)
    capture.add_argument("--gcloud-timeout-seconds", type=positive_int, required=True)
    capture.set_defaults(handler=cmd_capture_baseline)

    capture_j = sub.add_parser("capture-baseline-j", help="read live (read-only gcloud) after Release A and write baseline-J, once")
    capture_j.add_argument("--out", type=Path, required=True)
    capture_j.add_argument("--gcloud-timeout-seconds", type=positive_int, required=True)
    capture_j.add_argument("--a-readback", type=Path, required=True, help="Release A's AfterPromotion or AfterRollback readback file")
    capture_j.add_argument("--a-retire", choices=jb.A_RETIRE, required=True, help="whether A's Retire is done or waits until after B")
    capture_j.set_defaults(handler=cmd_capture_baseline_j)

    bind = sub.add_parser("bind", help="write live-bindings.json in the packet directory")
    add_mode(bind)
    bind.add_argument("--producer-only", action="store_true", help="mode jobs: write producer-bindings.json, the keys the chain evidence producer reads")
    bind.add_argument("--repo", default=".")
    bind.add_argument("--release-commit", required=True, help="the 40 character commit the lead bound; the checkout must be at it")
    bind.add_argument("--attempt", required=True, help="two digits")
    bind.add_argument("--packet-dir", type=Path, required=True)
    bind.add_argument("--baseline", type=Path)
    bind.add_argument("--baseline-j", type=Path)
    bind.add_argument("--baseline-chain", type=Path)
    bind.add_argument("--durable-manifest", type=Path)
    bind.add_argument("--dry-run-receipt", type=Path)
    bind.add_argument("--caller-account")
    bind.add_argument("--gcloud-timeout-seconds", type=positive_int)
    bind.add_argument("--http-timeout-seconds", type=positive_int)
    bind.add_argument("--max-smoke-age-minutes", type=positive_int)
    bind.add_argument("--inflight-asks-bytes-cap", type=positive_int)
    bind.add_argument("--chain-evidence-bytes-cap", type=positive_int)
    bind.add_argument("--collect-start-tolerance-minutes", type=nonnegative_int)
    bind.add_argument("--max-baseline-age-days", type=positive_int)
    bind.add_argument("--max-candidate-age-hours", type=positive_int)
    bind.set_defaults(handler=cmd_bind)

    lock = sub.add_parser("lock", help="write the packet lock into the checkout, once")
    add_mode(lock)
    lock.add_argument("--repo", default=".")
    lock.add_argument("--packet-dir", type=Path, required=True)
    lock.add_argument("--lock-dir", type=Path, help="keep the lock in this folder, outside the checkout, instead of the committed folder")
    lock.set_defaults(handler=cmd_lock)

    review = sub.add_parser("review", help="record an independent reviewer's verdict on the lock; the verdict is the reviewer's, with no default")
    add_mode(review)
    review.add_argument("--repo", default=".")
    review.add_argument("--packet-dir", type=Path, required=True)
    review.add_argument("--lock-dir", type=Path, help="keep the lock in this folder, outside the checkout, instead of the committed folder")
    review.add_argument("--verdict", choices=("ACCEPT", "REJECT"), required=True)
    review.set_defaults(handler=cmd_review)

    receipt = sub.add_parser("receipt", help="write the execution receipt from the line Albert typed")
    add_mode(receipt)
    receipt.add_argument("--repo", default=".")
    receipt.add_argument("--packet-dir", type=Path, required=True)
    receipt.add_argument("--lock-dir", type=Path, help="keep the lock in this folder, outside the checkout, instead of the committed folder")
    receipt.add_argument("--authorisation-line-file", required=True)
    receipt.add_argument("--steps", nargs="+", choices=(*STEPS, *JOBS_STEPS), help="default: every step of the mode")
    receipt.set_defaults(handler=cmd_receipt)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    check_required(parser, args)
    try:
        return args.handler(args)
    except Refused as error:
        print(f"REFUSED: {error}", file=sys.stderr)
    except so.Stop as error:
        print(f"REFUSED: {error.code}: {error.message}", file=sys.stderr)
    except so.NotFound as error:
        print(f"REFUSED: a resource the command needs does not exist: {error}", file=sys.stderr)
    except so.Probe as error:
        print(f"PROBE: {error}", file=sys.stderr)
        return 3
    except OSError as error:
        print(f"REFUSED: a file could not be read or written: {type(error).__name__}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
