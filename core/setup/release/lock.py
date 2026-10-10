"""The packet lock of a release (W8-REL 2.11, R2): one committed file per release id that lists the sha256 of everything the
independent review binds, so a change to any one of them invalidates the review for all.

    core/setup/release/locks/PACKET-LOCK-<release id>.json   schema_version 1

repo_files    the sources the paste runs, by repository path. Hashed with CRLF read as LF, so a checkout on Windows and one
              on Linux agree.
bound_files   the release's own durable inputs (bindings, baseline, durable-effects manifest, compat and old-reader receipts),
              hashed as they are.
tests         the test sources of section 5, by repository path.
expected_smoke_checks   the number of checks smoke.py records, derived from its source by AST, never typed.

A lock for an attempt that never ran is kept, never removed: priorAttempts and priorLedgers cite locks and ledgers by hash.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

SCHEMA_VERSION = 1
REPO_FILES = (
    "core/setup/release/SERVICES-PASTE.ps1",
    "core/setup/release/JOBS-PASTE.ps1",
    "core/setup/release/chain_evidence.py",
    "core/setup/release/jobs_only.py",
    "core/setup/release/jobs_run.py",
    "core/setup/cloudbuild.jobs.yaml",
    "core/setup/jobs.Dockerfile",
    "core/setup/deploy_jobs.py",
    "core/setup/release/bound_readback.py",
    "core/setup/release/natives.py",
    "core/setup/release/services_only.py",
    "core/setup/release/declared_env_removals.py",
    "core/setup/release/plan.py",
    "core/setup/release/lock.py",
    "core/setup/release/packet.py",
    "core/setup/release/durable-effects-manifest.template.json",
    "core/setup/durable_effects_check.py",
    "core/api/deploy_candidate.sh",
    "core/api/deploy.sh",
    "core/api/deploy_flags.env",
    "core/api/smoke.py",
    # Release B, the files the jobs paste and its children execute, found as the first party import closure of the scripts the paste starts
    # (core/setup/tests/test_jobs_lock.py holds this list to that closure), the schema files core.schema.apply runs and the release tooling
    # the lead runs to produce and judge the packet.
    "core/__init__.py",
    "core/collect/__init__.py",
    "core/collect/chain.py",
    "core/collect/curated_creators.py",
    "core/collect/gdelt.py",
    "core/collect/google_rss.py",
    "core/collect/google_trends.py",
    "core/collect/ids.py",
    "core/collect/job.py",
    "core/collect/local_sources.py",
    "core/collect/location_sources.py",
    "core/collect/parse.py",
    "core/collect/public_feed_collect.py",
    "core/collect/public_feed_rows.py",
    "core/collect/research_terms.py",
    "core/collect/seeds.py",
    "core/collect/socialcrawl_client.py",
    "core/collect/stores.py",
    "core/collect/telegram_collect.py",
    "core/collect/writers.py",
    "core/collect/x_discovery.py",
    "core/config/caps.py",
    "core/detect/__init__.py",
    "core/detect/aggregate.py",
    "core/detect/geo.py",
    "core/detect/items.py",
    "core/detect/sqlrun.py",
    "core/public_feeds/catalog.py",
    "core/public_feeds/reader.py",
    "core/public_feeds/telegram.py",
    "core/schema/__init__.py",
    "core/schema/agent.sql",
    "core/schema/apply.py",
    "core/schema/core.sql",
    "core/schema/early_signal.sql",
    "core/setup/__init__.py",
    "core/setup/release/__init__.py",
    "core/setup/release/env_scan.py",
    "core/setup/release/jobs_baseline.py",
    "core/setup/release/jobs_effects.py",
    "core/setup/release/jobs_freeze.py",
    "core/setup/release/jobs_source.py",
    "core/setup/schedule.py",
    "core/setup/smoke.py",
    "core/setup/stamp.py",
    "core/setup/watchdog.py",
    "core/trust/__init__.py",
    "core/trust/locality.py",
)
TEST_FILES = (
    "core/setup/tests/cloud_world.py",
    "core/setup/tests/jobs_paste_world.py",
    "core/setup/tests/jobs_world.py",
    "core/setup/tests/range_hygiene.py",
    "core/setup/tests/test_chain_evidence.py",
    "core/setup/tests/test_jobs_checks.py",
    "core/setup/tests/test_jobs_hygiene.py",
    "core/setup/tests/test_jobs_paste.py",
    "core/setup/tests/test_jobs_plan.py",
    "core/setup/tests/test_jobs_prefix.py",
    "core/setup/tests/test_jobs_readback.py",
    "core/setup/tests/test_jobs_update.py",
    "core/setup/tests/update_support.py",
    "core/api/tests/compat_harness.py",
    "core/api/tests/old_reader_harness.py",
    "core/api/tests/smoke_support.py",
    "core/api/tests/test_deploy_candidate.py",
    "core/api/tests/test_deploy_script.py",
    "core/api/tests/test_smoke.py",
    "core/api/tests/test_smoke_state.py",
    "core/api/tests/test_agent_compat.py",
    "core/api/tests/test_old_readers.py",
    "core/schema/tests/test_durable_effects.py",
    "core/setup/tests/packet_world.py",
    "core/setup/tests/paste_world.py",
    "core/setup/tests/release_prereq.py",
    "core/setup/tests/release_test_ids.json",
    "core/setup/tests/release_world.py",
    "core/setup/tests/test_bound_readback_services.py",
    "core/setup/tests/test_bound_readback_services_edges.py",
    "core/setup/tests/test_declared_env_removals.py",
    "core/setup/tests/test_natives.py",
    "core/setup/tests/test_release_map.py",
    "core/setup/tests/test_release_packet.py",
    "core/setup/tests/test_release_packet_paste.py",
    "core/setup/tests/test_release_plan.py",
    "core/setup/tests/test_release_retry.py",
    "core/setup/tests/test_services_paste.py",
    # Release B
    "core/brief/tests/test_brief_view_columns.py",
    "core/brief/tests/test_candidate_view_column.py",
    "core/collect/tests/__init__.py",
    "core/collect/tests/test_chain_record.py",
    "core/detect/tests/__init__.py",
    "core/detect/tests/duck.py",
    "core/detect/tests/fixtures.py",
    "core/detect/tests/test_js07_switch_columns.py",
    "core/schema/tests/__init__.py",
    "core/setup/tests/__init__.py",
    "core/setup/tests/e2e_shim.py",
    "core/setup/tests/jobs_e2e_world.py",
    "core/setup/tests/jobs_packet_world.py",
    "core/setup/tests/lock_closure.py",
    "core/setup/tests/test_deploy_jobs.py",
    "core/setup/tests/test_jobs_baseline.py",
    "core/setup/tests/test_jobs_build.py",
    "core/setup/tests/test_jobs_effects.py",
    "core/setup/tests/test_jobs_envscan.py",
    "core/setup/tests/test_jobs_final.py",
    "core/setup/tests/test_jobs_freeze.py",
    "core/setup/tests/test_jobs_image.py",
    "core/setup/tests/test_jobs_lock.py",
    "core/setup/tests/test_jobs_packet.py",
    "core/setup/tests/test_jobs_source.py",
    "core/setup/tests/test_jobs_wiring.py",
)
BOUND_NAMES = ("bindings", "baseline", "durable_manifest", "compat_receipt", "old_reader_receipt")
# The jobs release binds other durable inputs: baseline-J, the baseline chain manifest and the apply.py dry run receipt (JS-09).
JOBS_BOUND_NAMES = ("bindings", "baseline", "durable_manifest", "baseline_chain", "dry_run_receipt")


def sha_file(path, *, text=True):
    """sha256 of the file; for text, a CRLF reads as LF. A missing file is None."""
    path = Path(path)
    if not path.is_file():
        return None
    data = path.read_bytes()
    return hashlib.sha256(data.replace(b"\r\n", b"\n") if text else data).hexdigest()


def lock_path(repo, release_id):
    return Path(repo) / "core" / "setup" / "release" / "locks" / f"PACKET-LOCK-{release_id}.json"


def smoke_check_count(smoke_py):
    """The checks smoke.run records, counted from the source."""
    tree = ast.parse(Path(smoke_py).read_text(encoding="utf-8"))
    run = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run")
    return sum(1 for c in ast.walk(run) if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "record" and c.args
               and isinstance(c.args[0], ast.Constant) and isinstance(c.args[0].value, str))


def build_lock(repo, release_id, bound_files, names=BOUND_NAMES):
    repo = Path(repo)
    missing = [name for name in names if name not in bound_files]
    if missing:
        raise ValueError("the lock needs bound files: " + ", ".join(missing))
    return {
        "schema_version": SCHEMA_VERSION, "release_id": release_id,
        "repo_files": {rel: sha_file(repo / rel) for rel in REPO_FILES if (repo / rel).is_file()},
        "bound_files": {name: sha_file(path, text=False) for name, path in sorted(bound_files.items())},
        "expected_smoke_checks": smoke_check_count(repo / "core/api/smoke.py"),
        "tests": {rel: sha_file(repo / rel) for rel in TEST_FILES if (repo / rel).is_file()},
    }


def write_lock(repo, lock):
    path = lock_path(repo, lock["release_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as out:
        out.write(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    return path


def review_for(lock_file, target, verdict="ACCEPT"):
    """The shape of the independent review that binds a lock. The builder never writes a real one; tests do."""
    return {"schema_version": SCHEMA_VERSION, "verdict": verdict, "target": target, "lock_sha256": sha_file(lock_file, text=False)}


def lock_problems(repo, lock):
    """What differs between the lock and the repository: files, tests, the smoke count, an unknown version."""
    repo = Path(repo)
    if lock.get("schema_version") != SCHEMA_VERSION:
        return ["unknown lock schema_version"]
    problems = []
    for section in ("repo_files", "tests"):
        for rel, expected in lock.get(section, {}).items():
            actual = sha_file(repo / rel)
            if actual != expected:
                problems.append(f"{section}: {rel} {'is missing' if actual is None else 'differs from the lock'}")
    for rel in REPO_FILES:
        if (repo / rel).is_file() and rel not in lock.get("repo_files", {}):
            problems.append(f"repo_files: {rel} is not in the lock")
    if lock.get("expected_smoke_checks") != smoke_check_count(repo / "core/api/smoke.py"):
        problems.append("expected_smoke_checks differs from the checks recorded in smoke.py")
    return problems
