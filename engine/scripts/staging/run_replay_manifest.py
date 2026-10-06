"""Evaluate authorized retained replay inputs, one per cutoff, into one receipt.

Read only. The command reads a run manifest naming one retained replay input
bundle per cutoff, the sha256 its bytes are bound to and the replay signals
evaluated against it, runs ``evaluate_replay_manifest`` over each, and writes
one receipt file that must not already exist. It makes no network call and no
cloud write.

Run manifest, a JSON object with exactly these fields:

    {"bundles": [{"input_path": "...", "expected_input_sha256": "...",
                  "signals_path": "...", "sample_per_market": 5}, ...]}

Paths are relative to the manifest file. ``input_path`` is a retained replay
input payload as ``validate_replay_input`` admits it. ``signals_path`` is a
JSON list of objects with exactly the ``ReplaySignal`` fields, with
``source_max_observed_at`` as an ISO 8601 instant. Known events are never
supplied: the evaluator is offered the admitted bundle's own known event set,
so recall is measured against the set the input's authority fixed.

A bundle whose digest, authority validation, signal file or metric
computation refuses is recorded in the receipt as refused with its reason and
contributes no metric, as does a second bundle for a cutoff already
evaluated. The command exits 1 when any bundle refused, so a receipt with a
refusal is evidence of the refusal and never a clean run.

The build version must equal the git HEAD of the checkout running the
command, and that checkout must be clean: git status in porcelain form, with
all untracked files and every submodule requested explicitly so local status
settings cannot hide them, must print nothing, and git ls-files -v, run from
the repository top level so the whole checkout is listed, must list no
entry flagged skip-worktree (tag S) or assume-unchanged (a lowercase tag),
since porcelain status cannot see changes hidden behind those flags. Ignored
files are out of scope: neither check reads them, so an ignored file in the
checkout does not refuse. Every check is recorded in the receipt, and any
failing refuses before any bundle is read.

A bundle of the first form is synthetic evidence. ``validate_replay_input``
admits a source snapshot and a known event set only when their
``fixture_scope`` is ``synthetic_test_only``, so its receipt says it is
synthetic in its own fields and never carries a real mark.

A bundle of the second form is a retained protected capture:

    {"kind": "retained_capture", "capture_path": "...", "binding": {...},
     "signals_path": "...", "known_event_set_path": null,
     "result_row_path": "..." or null, "quality_measured": false,
     "sample_per_market": 5}

``capture_path`` is a local copy of the capture.json the capture command stored
in the source artifact bucket, read as bytes; the command never fetches it.
``binding`` names exactly the identity the owner pinned: profile_id,
cutoff_date, consumption_id, manifest_sha256, result_id, result_digest,
capture_sha256, snapshot_digest, table_digests and known_event_set_digest (null
when no known event set is supplied). ``evaluate_retained_replay`` checks it
against the protected context registry and the capture before any metric and
refuses on any mismatch. Those checks prove only that the capture agrees with
itself. ``result_row_path`` names a local copy of the capture's result ledger
row; the registry's pinned result digest authenticates it and it proves the
capture, so a section says authority_verified only with a row that holds.
``quality_measured`` says whether the signal flags are measurements; when it is
false every quality rate is null. Without a known event set recall is unknown,
never zero and never a pass, and the receipt states that gap.

The receipt carries an authority and a real mark derived from the sections, never
set by the command itself: real is true only when every bundle is a retained
capture, none refused, and every section says authority_verified and real.
Anything else is not real.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.staging import replay_open_intelligence as replay
from scripts.staging.replay_manifest import (
    AUTHORITY_UNVERIFIED,
    AUTHORITY_VERIFIED,
    KNOWN_EVENT_GAP,
    admitted_known_events,
    evaluate_replay_manifest,
    evaluate_retained_replay,
    require_input_digest,
)
from scripts.staging.replay_open_intelligence import ReplaySignal

RECEIPT_VERSION = "open_intelligence_replay_manifest_receipt_v1"
MANIFEST_FIELDS = frozenset({"bundles"})
BUNDLE_FIELDS = frozenset(
    {"input_path", "expected_input_sha256", "signals_path", "sample_per_market"}
)
SIGNAL_FIELDS = frozenset(
    {
        "signal_id",
        "market",
        "cluster_signature",
        "member_identities",
        "source_max_observed_at",
        "membership_complete",
        "evidence_ready",
        "geo_proven",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BUILD_VERSION = re.compile(r"^[0-9a-f]{40}$")
RETAINED_BUNDLE_FIELDS = frozenset(
    {
        "kind",
        "capture_path",
        "binding",
        "signals_path",
        "known_event_set_path",
        "result_row_path",
        "quality_measured",
        "sample_per_market",
    }
)
RETAINED_KIND = "retained_capture"
RETAINED_NOTE = (
    "every bundle is a retained protected capture proven against the result ledger row "
    "that the registry's pinned result digest authenticates. " + KNOWN_EVENT_GAP
)
UNVERIFIED_NOTE = (
    "authority unverified: every bundle is a retained protected capture that agrees with "
    "itself, its binding and the registry, but at least one has no result ledger row "
    "proving it, so the receipt is not real. " + KNOWN_EVENT_GAP
)
NOT_ESTABLISHED = "not_established"
MIXED_NOTE = (
    "the receipt mixes synthetic bundles, refused bundles or both with retained captures, "
    "so it is not real evidence as a whole; each retained section says whether it is real"
)
EVIDENCE_SCOPE = "synthetic_test_only"
EVIDENCE_NOTE = (
    "validate_replay_input admits only fixture_scope synthetic_test_only, so these "
    "metrics are synthetic evidence until a real retained authority exists"
)


class ReplayManifestRefusal(ValueError):
    pass


def _digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _json_file(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _signals(value: object) -> tuple[ReplaySignal, ...]:
    if not isinstance(value, list) or not value:
        raise ReplayManifestRefusal("signals_invalid")
    signals = []
    for row in value:
        if not isinstance(row, Mapping) or set(row) != SIGNAL_FIELDS:
            raise ReplayManifestRefusal("signal_fields_invalid")
        if not isinstance(row["member_identities"], list) or not isinstance(
            row["source_max_observed_at"], str
        ):
            raise ReplayManifestRefusal("signal_fields_invalid")
        observed = datetime.fromisoformat(row["source_max_observed_at"].replace("Z", "+00:00"))
        signals.append(
            ReplaySignal(
                signal_id=row["signal_id"],
                market=row["market"],
                cluster_signature=row["cluster_signature"],
                member_identities=tuple(row["member_identities"]),
                source_max_observed_at=observed,
                membership_complete=row["membership_complete"],
                evidence_ready=row["evidence_ready"],
                geo_proven=row["geo_proven"],
            )
        )
    return tuple(signals)


def _metric(metric) -> dict[str, object]:
    numerator, denominator, rate = metric.as_tuple()
    return {"numerator": numerator, "denominator": denominator, "rate": rate}


def _retained_entry(entry: Mapping[str, object]) -> Mapping[str, object]:
    if set(entry) != RETAINED_BUNDLE_FIELDS or entry["kind"] != RETAINED_KIND:
        raise ReplayManifestRefusal("retained_bundle_fields_invalid")
    for field in ("capture_path", "signals_path"):
        if not isinstance(entry[field], str) or not entry[field]:
            raise ReplayManifestRefusal(f"bundle_{field}_invalid")
    known = entry["known_event_set_path"]
    if known is not None and (not isinstance(known, str) or not known):
        raise ReplayManifestRefusal("bundle_known_event_set_path_invalid")
    row = entry["result_row_path"]
    if row is not None and (not isinstance(row, str) or not row):
        raise ReplayManifestRefusal("bundle_result_row_path_invalid")
    if type(entry["quality_measured"]) is not bool:
        raise ReplayManifestRefusal("bundle_quality_measured_invalid")
    if not isinstance(entry["binding"], Mapping):
        raise ReplayManifestRefusal("bundle_binding_invalid")
    sample = entry["sample_per_market"]
    if type(sample) is not int or sample < 1:
        raise ReplayManifestRefusal("bundle_sample_per_market_invalid")
    return entry


def evaluate_retained_bundle(entry: Mapping[str, object], *, base_dir: Path) -> dict[str, object]:
    """Evaluate one local retained capture through evaluate_retained_replay."""
    capture_raw = (base_dir / str(entry["capture_path"])).read_bytes()
    signals = _signals(_json_file(base_dir / str(entry["signals_path"])))
    known_path = entry["known_event_set_path"]
    known = None if known_path is None else _json_file(base_dir / str(known_path))
    row_path = entry["result_row_path"]
    row = None if row_path is None else _json_file(base_dir / str(row_path))
    result = evaluate_retained_replay(
        capture_raw=capture_raw,
        binding=dict(entry["binding"]),
        signals=signals,
        known_event_set=known,
        sample_per_market=entry["sample_per_market"],
        result_row=row,
        quality_measured=entry["quality_measured"],
    )
    return {"input_kind": RETAINED_KIND, **result}


def _bundle_entry(entry: object) -> Mapping[str, object]:
    if isinstance(entry, Mapping) and "kind" in entry:
        return _retained_entry(entry)
    if not isinstance(entry, Mapping) or set(entry) != BUNDLE_FIELDS:
        raise ReplayManifestRefusal("bundle_fields_invalid")
    for field in ("input_path", "signals_path"):
        if not isinstance(entry[field], str) or not entry[field]:
            raise ReplayManifestRefusal(f"bundle_{field}_invalid")
    expected = entry["expected_input_sha256"]
    if not isinstance(expected, str) or _SHA256.match(expected) is None:
        raise ReplayManifestRefusal("bundle_expected_input_sha256_invalid")
    sample = entry["sample_per_market"]
    if type(sample) is not int or sample < 1:
        raise ReplayManifestRefusal("bundle_sample_per_market_invalid")
    return entry


def evaluate_bundle(entry: Mapping[str, object], *, base_dir: Path) -> dict[str, object]:
    """Evaluate one retained input and return its receipt section.

    The digest and the authority validator run inside
    ``evaluate_replay_manifest`` before any metric; the admitted bundle is
    validated again here only to read its counts, known event set and
    membership sections for the receipt, after the evaluator has admitted it.
    """
    payload = _json_file(base_dir / str(entry["input_path"]))
    if not isinstance(payload, dict):
        raise ReplayManifestRefusal("input_payload_invalid")
    signals = _signals(_json_file(base_dir / str(entry["signals_path"])))
    expected = str(entry["expected_input_sha256"])
    # Known events offered to the evaluator are the admitted set itself, read
    # after the digest check; the evaluator then refuses any drift between
    # what it admits and what it was offered.
    require_input_digest(payload, expected)
    offered_events = admitted_known_events(replay.validate_replay_input(payload))
    metrics = evaluate_replay_manifest(
        input_payload=payload,
        expected_input_sha256=expected,
        signals=signals,
        known_events=offered_events,
        sample_per_market=int(entry["sample_per_market"]),
    )
    bundle = replay.validate_replay_input(payload)
    snapshot = bundle.source_snapshot
    return {
        "cutoff": metrics.cutoff.isoformat(),
        "input_sha256": expected,
        "counts": {
            "row_counts_by_table": dict(sorted(snapshot["row_counts_by_table"].items())),
            "section_counts": dict(sorted(snapshot["section_counts"].items())),
            "event_ledger_rows": metrics.row_count,
            "signals": metrics.signal_count,
            "known_events": len(offered_events),
        },
        "known_event_set": {
            "set_id": bundle.known_event_set["set_id"],
            "digest": bundle.known_event_set["digest"],
        },
        "known_event_recall": _metric(metrics.known_event_recall),
        "known_event_recall_by_market": {
            market: _metric(metric)
            for market, metric in sorted(metrics.known_event_recall_by_market.items())
        },
        "membership_proof": {
            "source_digest": snapshot["source_digest"],
            "memberships_by_candidate_sha256": _digest(payload["memberships_by_candidate"]),
            "receipts_by_member_sha256": _digest(payload["receipts_by_member"]),
            "receipt_completeness": _metric(metrics.receipt_completeness),
            "evidence_coverage": _metric(metrics.evidence_coverage),
            "geo_coverage": _metric(metrics.geo_coverage),
        },
        "quality": {
            "duplicate_rate": _metric(metrics.duplicate_rate),
            "foreign_leakage": _metric(metrics.foreign_leakage),
            "human_coherence_status": metrics.human_coherence_status,
        },
        "exclusions": {
            market: {
                "state": history.state,
                "gaps": list(history.gaps),
                "excluded_rows": history.excluded_rows,
                "unavailable_rows": history.unavailable_rows,
            }
            for market, history in sorted(metrics.history_normalization.items())
        },
        "review_sample_signal_ids": [signal.signal_id for signal in metrics.review_sample],
    }


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def checkout_state(root: Path = ROOT) -> tuple[str, str]:
    """The git HEAD and porcelain status of the running checkout. Local only.

    Untracked files and submodules are requested explicitly, so a local
    status.showUntrackedFiles or submodule setting cannot hide a change.
    """
    head = _git(root, "rev-parse", "HEAD").strip()
    if _BUILD_VERSION.match(head) is None:
        raise ReplayManifestRefusal("checkout_head_unreadable")
    status = _git(
        root,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    return head, status


def flagged_index_entries(listing: str) -> tuple[str, ...]:
    """The git ls-files -v lines whose tag hides a change from status.

    Tag S marks skip-worktree, and a lowercase tag marks assume-unchanged.
    """
    return tuple(
        line for line in listing.splitlines() if line and (line[0] == "S" or line[0].islower())
    )


def hidden_index_entries(root: Path = ROOT) -> tuple[str, ...]:
    """Index entries of the whole checkout flagged so status cannot see them.

    The listing runs from the repository top level, not from engine, so an
    entry at the root, under ops or under app is read as well, and every path
    is the full path from that top level.
    """
    top = Path(_git(root, "rev-parse", "--show-toplevel").strip())
    return flagged_index_entries(_git(top, "ls-files", "-v", "--full-name"))


def run_replay_manifest(
    manifest: object, *, base_dir: Path, build_version: str
) -> dict[str, object]:
    """Evaluate every bundle a run manifest names into one receipt."""
    if not isinstance(build_version, str) or _BUILD_VERSION.match(build_version) is None:
        raise ReplayManifestRefusal("build_version_invalid")
    try:
        head, status = checkout_state()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReplayManifestRefusal("checkout_head_unreadable") from error
    if head != build_version:
        raise ReplayManifestRefusal("build_version_differs_from_checkout")
    if status.strip():
        raise ReplayManifestRefusal("checkout_not_clean")
    try:
        hidden = hidden_index_entries()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReplayManifestRefusal("checkout_index_unreadable") from error
    if hidden:
        raise ReplayManifestRefusal("checkout_index_flags_hidden_changes")
    if not isinstance(manifest, Mapping) or set(manifest) != MANIFEST_FIELDS:
        raise ReplayManifestRefusal("manifest_fields_invalid")
    entries = manifest["bundles"]
    if not isinstance(entries, list) or not entries:
        raise ReplayManifestRefusal("manifest_bundles_required")
    evaluated: dict[str, dict[str, object]] = {}
    refused = []
    retained_seen = False
    for index, raw in enumerate(entries):
        try:
            if isinstance(raw, Mapping) and "kind" in raw:
                retained_seen = True
            entry = _bundle_entry(raw)
            if entry.get("kind") == RETAINED_KIND:
                section = evaluate_retained_bundle(entry, base_dir=base_dir)
            else:
                section = evaluate_bundle(entry, base_dir=base_dir)
            if section["cutoff"] in evaluated:
                raise ReplayManifestRefusal("duplicate_cutoff")
        except (OSError, ValueError, TypeError, KeyError) as error:
            refused.append(
                {
                    "index": index,
                    "input_path": (
                        raw.get("capture_path", raw.get("input_path"))
                        if isinstance(raw, Mapping)
                        else None
                    ),
                    "reason": type(error).__name__,
                    "detail": str(error),
                }
            )
            continue
        evaluated[str(section["cutoff"])] = section
    receipt: dict[str, object] = {
        "receipt_version": RECEIPT_VERSION,
        "build_version": build_version,
        "checkout_head": head,
        "checkout_clean": True,
        "checkout_status_command": (
            "git status --porcelain --untracked-files=all --ignore-submodules=none"
        ),
        "checkout_index_command": "git ls-files -v --full-name, from the top level",
        "checkout_hidden_index_entries": len(hidden),
        "evidence_scope": EVIDENCE_SCOPE,
        "evidence_note": EVIDENCE_NOTE,
    }
    if retained_seen:
        # The receipt's authority and real mark are derived from the sections: real only
        # when every bundle is a retained capture, none refused and every section says
        # authority_verified and real. Synthetic bundles never become real.
        only_retained = all(isinstance(item, Mapping) and "kind" in item for item in entries)
        all_verified = all(
            section.get("input_kind") == RETAINED_KIND
            and section.get("authority") == AUTHORITY_VERIFIED
            and section.get("real") is True
            for section in evaluated.values()
        )
        if only_retained and evaluated and not refused:
            authority = AUTHORITY_VERIFIED if all_verified else AUTHORITY_UNVERIFIED
            receipt.update(
                evidence_scope=RETAINED_KIND,
                evidence_note=RETAINED_NOTE if all_verified else UNVERIFIED_NOTE,
                authority=authority,
                real=authority == AUTHORITY_VERIFIED,
            )
        elif only_retained:
            receipt.update(
                evidence_scope="retained_capture_refused",
                evidence_note=MIXED_NOTE,
                authority=NOT_ESTABLISHED,
                real=False,
            )
        else:
            receipt.update(
                evidence_scope="mixed",
                evidence_note=MIXED_NOTE,
                authority=NOT_ESTABLISHED,
                real=False,
            )
    receipt.update(
        {
            "bundle_count": len(entries),
            "evaluated_count": len(evaluated),
            "refused_count": len(refused),
            "cutoffs": [evaluated[cutoff] for cutoff in sorted(evaluated)],
            "refused": refused,
        }
    )
    receipt["receipt_sha256"] = _digest(receipt)
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/staging/run_replay_manifest.py",
        description="Evaluate authorized retained replay inputs into one receipt.",
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--build-version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = _json_file(args.manifest)
        receipt = run_replay_manifest(
            manifest, base_dir=args.manifest.parent, build_version=args.build_version
        )
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(
        f"evaluated={receipt['evaluated_count']} refused={receipt['refused_count']} "
        f"receipt_sha256={receipt['receipt_sha256']}"
    )
    return 1 if receipt["refused_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
