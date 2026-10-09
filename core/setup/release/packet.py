"""The command line that produces the files a services-only release packet is made of (packet flag F6).

    py -3.13 -m core.setup.release.packet capture-baseline --out <file> --gcloud-timeout-seconds <n>
    py -3.13 -m core.setup.release.packet bind    --repo <checkout> --release-commit <40 hex> --attempt <nn> --packet-dir <dir>
        --baseline <file> --durable-manifest <file> --caller-account <email> --gcloud-timeout-seconds <n>
        --http-timeout-seconds <n> --max-smoke-age-minutes <n> --inflight-asks-bytes-cap <n>
    py -3.13 -m core.setup.release.packet lock    --repo <checkout> --packet-dir <dir> [--lock-dir <dir>]
    py -3.13 -m core.setup.release.packet review  --repo <checkout> --packet-dir <dir> --verdict ACCEPT|REJECT [--lock-dir <dir>]
    py -3.13 -m core.setup.release.packet receipt --repo <checkout> --packet-dir <dir> --authorisation-line-file <file>
        [--steps Candidate Promote Rollback Retire] [--lock-dir <dir>]

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
from core.setup.release import services_only as so  # noqa: E402

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
# The clauses of the sentence Albert types to authorise the one paid Ask (RELEASE-A-PACKET section 5). The figures in it are the lead's to
# recheck and are not compared here.
AUTHORISATION_CLAUSES = ("I authorise exactly one live T1 Ask", "market ZA", "against the candidate API tag URL only",
                         "no second Ask and no retry", "The window is quiet and I will start no manual job until the execution ends",
                         "I will type DEPLOY, IDLE and the passcode myself")


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


def read_json(path, what):
    path = Path(path)
    if not path.is_file():
        raise Refused(f"{what} is missing: {path.name}")
    try:
        value = json.loads(path.read_bytes().decode("utf-8-sig"))
    except ValueError:
        raise Refused(f"{what} is not readable JSON: {path.name}") from None
    if not isinstance(value, dict):
        raise Refused(f"{what} is not an object: {path.name}")
    return value


def git(repo, *args):
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        done = subprocess.run(["git", *args], cwd=repo, capture_output=True, encoding="utf-8", timeout=60, env=env,
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
    try:
        done = subprocess.run(["git", "show", f"{commit}:{path}"], cwd=repo, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
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


def load_bindings(packet_dir):
    path = Path(packet_dir) / BINDINGS_FILE
    bound = read_json(path, "the bindings")
    try:
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


# bind

def paste_build_service_account():
    text = (Path(__file__).resolve().parent / "SERVICES-PASTE.ps1").read_text(encoding="utf-8")
    found = set(re.findall(r"serviceAccounts/(f42-deployer@[A-Za-z0-9.-]+\.iam\.gserviceaccount\.com)", text))
    if len(found) != 1:
        raise Refused("The paste does not name one build service account")
    return found.pop()


def load_baseline(path):
    baseline = read_json(path, "the baseline")
    if baseline.get("kind") != "baseline-A" or baseline.get("schema_version") != so.SCHEMA_VERSION:
        raise Refused("The baseline file is not a baseline-A receipt")
    if set(baseline.get("jobs", {})) != set(so.JOB_NAMES) or set(baseline.get("services", {})) != set(so.SERVICES):
        raise Refused("The baseline does not hold both services and the fourteen jobs")
    check_a80(baseline)
    return baseline


def passing_receipt(path, label):
    receipt = read_json(path, f"the {label} receipt")
    if receipt.get("schema_version") != so.SCHEMA_VERSION or receipt.get("verdict") != "pass":
        raise Refused(f"The {label} receipt does not say pass")
    return sha_bytes(path)


def cmd_bind(args):
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
    baseline = load_baseline(args.baseline)
    compat = passing_receipt(packet_dir / COMPAT_FILE, "compat")
    old = passing_receipt(packet_dir / OLD_READER_FILE, "old-reader")
    durable = Path(args.durable_manifest)
    problems = dec.validate_manifest(read_json(durable, "the durable-effects manifest"))
    if problems:
        raise Refused("The durable-effects manifest is not valid: " + problems[0])
    build_config = blob_sha256(repo, head, BUILD_CONFIG)
    agent = baseline["canonical"]["f42-agent"]
    short = head[:12]
    bound = {
        "schema_version": so.SCHEMA_VERSION, "mode": "services-only", "status": STATUS, "release_id": release_id, "target": head,
        "tree": tree, "releaseDir": str(packet_dir.resolve()), "baselinePath": str(Path(args.baseline).resolve()),
        "baselineSha256": sha_bytes(args.baseline), "callerAccount": args.caller_account,
        "buildServiceAccount": paste_build_service_account(), "buildConfigSha256": build_config,
        "readTimeoutSeconds": {"gcloud": args.gcloud_timeout_seconds, "http": args.http_timeout_seconds},
        "maxSmokeAgeMinutes": args.max_smoke_age_minutes,
        "expectedSmokeChecks": locklib.smoke_check_count(repo / "core/api/smoke.py"),
        "envDeltas": {"f42-agent": {"F42_VERSION": short},
                      "f42-api": {"F42_VERSION": short, "AGENT_URL": so.tag_url(release_id, agent), "AGENT_AUDIENCE": agent}},
        "compatReceiptSha256": compat, "oldReaderReceiptSha256": old, "durableManifestPath": str(durable.resolve()),
        "durableManifestSha256": sha_bytes(durable), "inflightAsksSqlSha256": dec.INFLIGHT_ASKS_SQL_SHA256,
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

def cmd_lock(args):
    repo, packet_dir = Path(args.repo), Path(args.packet_dir)
    bindings_path, bound = load_bindings(packet_dir)
    head, _ = checkout(repo, clean=True)
    if head != bound["target"]:
        raise Refused("The checkout is not at the bound commit")
    files = bound_file_paths(bound, bindings_path)
    for name, expected in bound_hashes(bound).items():
        if not files[name].is_file() or sha_bytes(files[name]) != expected:
            raise Refused(f"The {name} file differs from the bindings")
    path = lock_file(repo, args.lock_dir, bound["release_id"])
    refuse_existing(path)
    lock = locklib.build_lock(repo, bound["release_id"], files)
    problems = locklib.lock_problems(repo, lock)
    if problems:
        raise Refused("The lock does not match the checkout: " + problems[0])
    write_json_new(path, lock)
    print(f"wrote {path}\nsha256 {sha_bytes(path)}")
    return 0


# review

def bound_lock(repo, packet_dir, lock_dir):
    """The bindings, the lock that binds them, and the lock's path. Refuses a lock that binds anything else."""
    bindings_path, bound = load_bindings(packet_dir)
    path = lock_file(repo, lock_dir, bound["release_id"])
    lock = read_json(path, "the lock")
    if lock.get("release_id") != bound["release_id"] or lock.get("bound_files", {}).get("bindings") != sha_bytes(bindings_path):
        raise Refused("The lock binds other bindings than the bindings file")
    return bound, lock, path


def cmd_review(args):
    repo, packet_dir = Path(args.repo), Path(args.packet_dir)
    out = packet_dir / REVIEW_FILE
    refuse_existing(out)
    bound, lock, path = bound_lock(repo, packet_dir, args.lock_dir)
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

def check_authorisation(text, release_id):
    line = " ".join(text.split())
    if not line.startswith(f"RELEASE A {release_id}:") or any(clause not in line for clause in AUTHORISATION_CLAUSES):
        raise Refused("The authorisation line is not the one the packet asks Albert to type for this release")


def cmd_receipt(args):
    repo, packet_dir = Path(args.repo), Path(args.packet_dir)
    out = packet_dir / RECEIPT_FILE
    refuse_existing(out)
    bound, lock, path = bound_lock(repo, packet_dir, args.lock_dir)
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
    check_authorisation(line_file.read_text(encoding="utf-8-sig"), bound["release_id"])
    steps = [step for step in STEPS if step in args.steps]
    receipt = {"schema_version": so.SCHEMA_VERSION, "release_id": bound["release_id"], "target": bound["target"],
               "declared_steps": steps, "later_explicit_user_instruction": True, "quiet_window_confirmed": True,
               "no_new_manual_starts_until_execution_ends": True, "single_T1_smoke_authorized": True, "max_live_asks": 1}
    write_json_new(out, receipt)
    print(f"wrote {out}\nsteps {' '.join(steps)}\nsha256 {sha_bytes(out)}")
    return 0


# the command line

def build_parser():
    parser = argparse.ArgumentParser(prog="packet", description="Produce the files of a services-only release packet.")
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("capture-baseline", help="read live (read-only gcloud) and write the baseline receipt, once")
    capture.add_argument("--out", type=Path, required=True)
    capture.add_argument("--gcloud-timeout-seconds", type=positive_int, required=True)
    capture.set_defaults(handler=cmd_capture_baseline)

    bind = sub.add_parser("bind", help="write live-bindings.json in the packet directory")
    bind.add_argument("--repo", default=".")
    bind.add_argument("--release-commit", required=True, help="the 40 character commit the lead bound; the checkout must be at it")
    bind.add_argument("--attempt", required=True, help="two digits")
    bind.add_argument("--packet-dir", type=Path, required=True)
    bind.add_argument("--baseline", type=Path, required=True)
    bind.add_argument("--durable-manifest", type=Path, required=True)
    bind.add_argument("--caller-account", required=True)
    bind.add_argument("--gcloud-timeout-seconds", type=positive_int, required=True)
    bind.add_argument("--http-timeout-seconds", type=positive_int, required=True)
    bind.add_argument("--max-smoke-age-minutes", type=positive_int, required=True)
    bind.add_argument("--inflight-asks-bytes-cap", type=positive_int, required=True)
    bind.set_defaults(handler=cmd_bind)

    lock = sub.add_parser("lock", help="write the packet lock into the checkout, once")
    lock.add_argument("--repo", default=".")
    lock.add_argument("--packet-dir", type=Path, required=True)
    lock.add_argument("--lock-dir", type=Path, help="keep the lock in this folder, outside the checkout, instead of the committed folder")
    lock.set_defaults(handler=cmd_lock)

    review = sub.add_parser("review", help="record an independent reviewer's verdict on the lock; the verdict is the reviewer's, with no default")
    review.add_argument("--repo", default=".")
    review.add_argument("--packet-dir", type=Path, required=True)
    review.add_argument("--lock-dir", type=Path, help="keep the lock in this folder, outside the checkout, instead of the committed folder")
    review.add_argument("--verdict", choices=("ACCEPT", "REJECT"), required=True)
    review.set_defaults(handler=cmd_review)

    receipt = sub.add_parser("receipt", help="write the execution receipt from the line Albert typed")
    receipt.add_argument("--repo", default=".")
    receipt.add_argument("--packet-dir", type=Path, required=True)
    receipt.add_argument("--lock-dir", type=Path, help="keep the lock in this folder, outside the checkout, instead of the committed folder")
    receipt.add_argument("--authorisation-line-file", required=True)
    receipt.add_argument("--steps", nargs="+", choices=STEPS, default=list(STEPS))
    receipt.set_defaults(handler=cmd_receipt)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
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
