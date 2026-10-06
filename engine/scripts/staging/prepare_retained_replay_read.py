"""Prepare the owner's retained replay read of the pinned 2026-09-07 protected capture.

No command makes a cloud write, and every file a command writes is new; it refuses
when a target already exists. Only read-result-row reaches the network, for one
BigQuery SELECT. The commands work in one folder outside the checkout, so the checkout
stays clean for the runner.

    find-capture WORK [--root FOLDER]
                          copy the earlier local private copy of the capture, found under
                          the home folder (or FOLDER) by its name, size and sha256, to
                          WORK/capture.json
    read-result-row WORK  read the capture's result ledger row with the BigQuery client:
                          one parameterised SELECT, dry run first with the cache off and
                          refused above RESULT_ROW_MAXIMUM_BYTES_BILLED, then run under
                          the same cap; print its payload digest beside the registry's
                          pinned result digest and save it to WORK/result_row.json only
                          when they match. Zero rows refuse as result_row_not_found, a
                          BigQuery error prints its message, and missing or expired
                          credentials refuse as credentials_unavailable or
                          credentials_refresh_failed with their message
    save-result-row WORK  the same checks and write for a JSON array of rows read from
                          stdin; any refusal prints the first 500 characters it read
    prepare WORK          from WORK/capture.json (and WORK/result_row.json when present),
                          write WORK/signals.json and WORK/manifest.json for
                          run_replay_manifest.py

The binding's six registry pins come from the protected context registry. The capture
sha256, snapshot digest and table digests are read from the capture itself, so they are
self consistent only; the result ledger row, authenticated by the registry's pinned
result digest, is what lets the runner verify authority. No released signals exist for
this cutoff: prepare writes one signal per component the replay composes from the
capture, with every quality flag false, and marks quality as not measured.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CUTOFF = "2026-09-07"
# The earlier local private copy the 2026-09-07 checkpoint records.
CAPTURE_NAME = "protected-source-capture-20260907-private.json"
CAPTURE_SIZE = 63_731_275
CAPTURE_SHA256 = "6fb2abd2d37025b064cf3fc6604fa97fda14230644a40a08f5a9faf035a841f3"
RESULT_ROW_FIELDS = frozenset(
    {
        "result_contract_version",
        "result_id",
        "consumption_id",
        "approval_id",
        "manifest_sha256",
        "operation",
        "execution_name",
        "result_reference",
        "canonical_result_json",
        "result_digest",
        "status",
        "completed_at",
    }
)


PROJECT = "ogilvy-trends-v2"
RESULT_ROW_SQL = (
    "SELECT result_contract_version, result_id, consumption_id, approval_id, "
    "manifest_sha256, operation, execution_name, result_reference, canonical_result_json, "
    "result_digest, status, "
    'FORMAT_TIMESTAMP("%Y-%m-%dT%H:%M:%E6SZ", completed_at, "UTC") AS completed_at '
    "FROM `ogilvy-trends-v2.trends_v2_staging_approvals.open_intelligence_execution_results_v1` "
    "WHERE result_id = @result_id"
)
# The v1 result ledger holds one row per completed execution, each a compact receipt of a
# few kilobytes, so a full scan of it is far below BigQuery's 10 MB minimum billed per
# query. 64 MiB clears that minimum with room for growth and stays well under 1 GiB.
RESULT_ROW_MAXIMUM_BYTES_BILLED = 64 * 1024 * 1024


class PrepareRefusal(ValueError):
    pass


def _entry():
    from src.analysis.open_intelligence import protected_context_registry

    entry = protected_context_registry.profile_for(CUTOFF)
    if entry is None or not entry.pinned:
        raise PrepareRefusal("registry_entry_unpinned")
    return entry


def _provider():
    from scripts.staging.replay_open_intelligence import LexicalSimilarityProviderV2

    return LexicalSimilarityProviderV2()


def _write_new(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(data)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def find_capture(work: Path, root: Path | None = None) -> Path:
    target = work / "capture.json"
    if target.exists():
        raise PrepareRefusal("capture_exists")
    root = Path.home() if root is None else root
    if not root.is_dir():
        raise PrepareRefusal("search_root_missing")
    for folder, directories, files in os.walk(root):
        directories[:] = sorted(name for name in directories if not name.startswith("."))
        if CAPTURE_NAME not in files:
            continue
        candidate = Path(folder) / CAPTURE_NAME
        if candidate.stat().st_size == CAPTURE_SIZE and _sha256_file(candidate) == CAPTURE_SHA256:
            _write_new(target, candidate.read_bytes())
            return candidate
    raise PrepareRefusal("no_local_copy")


def _refused_input(code: str, text: str) -> PrepareRefusal:
    """A refusal that first shows the start of what was read, such as an error message."""
    print("input_start", repr(text[:500]))
    return PrepareRefusal(code)


def _one_row(rows: object, refuse) -> dict:
    """The single ledger row, or a refusal through refuse(code)."""
    if rows == []:
        raise refuse("result_row_not_found")
    if not isinstance(rows, list) or len(rows) != 1:
        raise refuse("result_row_count_not_one")
    (row,) = rows
    if not isinstance(row, dict) or set(row) != RESULT_ROW_FIELDS:
        raise refuse("result_row_fields_invalid")
    if not all(isinstance(value, str) for value in row.values()):
        raise refuse("result_row_fields_invalid")
    return row


def _result_row_target(work: Path) -> Path:
    target = work / "result_row.json"
    if target.exists():
        raise PrepareRefusal("result_row_exists")
    return target


def save_result_row(work: Path, text: str) -> None:
    target = _result_row_target(work)
    try:
        rows = json.loads(text)
    except ValueError as error:
        raise _refused_input("result_row_output_invalid", text) from error
    row = _one_row(rows, lambda code: _refused_input(code, text))
    _write_pinned_row(target, row)


def _bigquery_client():
    from google.cloud import bigquery

    return bigquery.Client(project=PROJECT)


def _refused_rows(code: str, rows: list) -> PrepareRefusal:
    print("rows_returned", len(rows))
    if rows and isinstance(rows[0], dict):
        print("columns", " ".join(sorted(rows[0])))
    return PrepareRefusal(code)


def read_result_row(work: Path, client_factory) -> None:
    """Read the pinned result row; the overwrite check runs before any client exists."""
    from google.api_core.exceptions import GoogleAPICallError
    from google.auth.exceptions import DefaultCredentialsError, RefreshError
    from google.cloud import bigquery

    target = _result_row_target(work)
    entry = _entry()
    try:
        client = client_factory()
    except DefaultCredentialsError as error:
        print("credentials_error", error)
        raise PrepareRefusal("credentials_unavailable") from error

    def config(*, dry_run: bool):
        return bigquery.QueryJobConfig(
            dry_run=dry_run,
            use_query_cache=False,
            maximum_bytes_billed=RESULT_ROW_MAXIMUM_BYTES_BILLED,
            query_parameters=[
                bigquery.ScalarQueryParameter("result_id", "STRING", entry.result_id)
            ],
        )

    try:
        dry = client.query(RESULT_ROW_SQL, job_config=config(dry_run=True))
        planned = dry.total_bytes_processed
        if type(planned) is not int or planned < 0:
            raise PrepareRefusal("result_row_dry_run_without_estimate")
        print("dry_run_bytes", planned)
        print("maximum_bytes_billed", RESULT_ROW_MAXIMUM_BYTES_BILLED)
        if planned > RESULT_ROW_MAXIMUM_BYTES_BILLED:
            raise PrepareRefusal("result_row_over_cap")
        job = client.query(RESULT_ROW_SQL, job_config=config(dry_run=False))
        rows = [dict(row.items()) for row in job.result()]
    except GoogleAPICallError as error:
        print("bigquery_error", error.message)
        raise PrepareRefusal("result_row_query_failed") from error
    except RefreshError as error:
        print("credentials_error", error)
        raise PrepareRefusal("credentials_refresh_failed") from error
    print("bytes_billed", job.total_bytes_billed)
    row = _one_row(rows, lambda code: _refused_rows(code, rows))
    _write_pinned_row(target, row)


def _write_pinned_row(target: Path, row: dict) -> None:
    entry = _entry()
    payload_sha256 = hashlib.sha256(row["canonical_result_json"].encode("utf-8")).hexdigest()
    matches = (
        payload_sha256 == entry.result_digest
        and row["result_digest"] == entry.result_digest
        and row["result_id"] == entry.result_id
    )
    print("registry_result_id", entry.result_id)
    print("row_result_id", row["result_id"])
    print("registry_result_digest", entry.result_digest)
    print("row_payload_sha256", payload_sha256)
    print("result_row_matches_registry", "yes" if matches else "no")
    if not matches:
        raise PrepareRefusal("result_row_not_the_pinned_result")
    _write_new(target, json.dumps(row, indent=1, sort_keys=True).encode("utf-8"))


def prepare(work: Path) -> None:
    from scripts.staging.replay_manifest import retained_replay_payload

    for name in ("signals.json", "manifest.json"):
        if (work / name).exists():
            raise PrepareRefusal(f"{name}_exists")
    capture = work / "capture.json"
    raw = capture.read_bytes()
    capture_sha256 = hashlib.sha256(raw).hexdigest()
    if len(raw) != CAPTURE_SIZE or capture_sha256 != CAPTURE_SHA256:
        raise PrepareRefusal("capture_not_the_pinned_copy")
    snapshot = json.loads(raw)["capture"]["assembly"]["snapshot"]
    entry = _entry()
    binding = {
        "profile_id": entry.profile_id,
        "cutoff_date": entry.cutoff_date,
        "consumption_id": entry.consumption_id,
        "manifest_sha256": entry.manifest_sha256,
        "result_id": entry.result_id,
        "result_digest": entry.result_digest,
        "capture_sha256": capture_sha256,
        "snapshot_digest": snapshot["source_digest"],
        "table_digests": dict(snapshot["table_digests"]),
        "known_event_set_digest": None,
    }
    payload = retained_replay_payload(
        capture_raw=raw, binding=binding, semantic_provider=_provider()
    )
    signals = [
        {
            "signal_id": f"component_signal_{index:05d}",
            "market": component["market"],
            "cluster_signature": candidate_id,
            "member_identities": list(component["member_identities"]),
            "source_max_observed_at": f"{entry.cutoff_date}T23:59:59Z",
            "membership_complete": False,
            "evidence_ready": False,
            "geo_proven": False,
        }
        for index, (candidate_id, component) in enumerate(
            sorted(payload["components_by_candidate"].items())
        )
    ]
    has_row = (work / "result_row.json").is_file()
    manifest = {
        "bundles": [
            {
                "kind": "retained_capture",
                "capture_path": "capture.json",
                "binding": binding,
                "signals_path": "signals.json",
                "known_event_set_path": None,
                "result_row_path": "result_row.json" if has_row else None,
                "quality_measured": False,
                "sample_per_market": 5,
            }
        ]
    }
    _write_new(work / "signals.json", json.dumps(signals, indent=1).encode("utf-8"))
    _write_new(work / "manifest.json", json.dumps(manifest, indent=1).encode("utf-8"))
    print("capture_sha256", capture_sha256)
    print("snapshot_digest", binding["snapshot_digest"])
    print("components", len(signals))
    print("result_row", "present" if has_row else "absent")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare the owner's retained replay read.")
    parser.add_argument(
        "command", choices=("find-capture", "read-result-row", "save-result-row", "prepare")
    )
    parser.add_argument("work", type=Path)
    parser.add_argument("--root", type=Path, default=None)
    return parser


def main(argv: Sequence[str] | None = None, *, stdin: str | None = None, client=None) -> int:
    args = build_parser().parse_args(argv)
    work = args.work
    try:
        if not work.is_dir():
            raise PrepareRefusal("work_folder_missing")
        if args.root is not None and args.command != "find-capture":
            raise PrepareRefusal("root_only_for_find_capture")
        if args.command == "find-capture":
            print("found", find_capture(work, args.root))
        elif args.command == "read-result-row":
            read_result_row(work, _bigquery_client if client is None else lambda: client)
        elif args.command == "save-result-row":
            save_result_row(work, sys.stdin.read() if stdin is None else stdin)
        else:
            prepare(work)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("refused", type(error).__name__, error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
