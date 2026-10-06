"""Freeze or score a closed week discovery review from JSON files.

Read only apart from the one receipt it writes, which must not already exist.
No network call and no cloud write. Run it as a module with the command
freeze or score.

freeze takes an input path and an output path. Its input is exactly
{"week_start": "YYYY-MM-DD", "frozen_at": "YYYY-MM-DDTHH:MM:SSZ", "markets":
["za", ...], "clusters": [{"cluster_id", "market", "week_start", "eligible"},
...]}, and its receipt carries the frozen sample from freeze_review_sample.
The markets must be exactly the product markets za, ng and ke, in any
order; any other set refuses, so a freeze cannot promise fewer markets.

score takes the freeze receipt path, an input path, an optional thresholds
path and an output path. Its input is exactly {"sample", "reviews"}; the
sample must equal the one in the freeze receipt, and the freeze receipt must
be a freeze receipt whose own digest still holds. The thresholds file must
equal the C07 review thresholds pinned in discovery_review exactly, and its
sha256 is recorded; any other thresholds refuse. Without a thresholds file
the receipt says so and carries no gate, so no gate verdict is ever emitted
without the pinned thresholds. The score receipt lists every promised market
the freeze observed no cluster in as unobserved; the gate reads each as
unmet and the pool as unmet, and such a review is never complete.
The score receipt's top level verdict is the gate's overall C07 verdict, met
only when every promised market and the pool are met, or no_gate without
the pinned thresholds. The freeze receipt proves only that it is
unchanged since it was written; nothing here proves who wrote it.

Every receipt names its command and the sha256 of each file it read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from ops.evaluation.discovery_review import (
    C07_REVIEW_THRESHOLDS,
    PRODUCT_MARKETS,
    freeze_review_sample,
    gate_c07_thresholds,
    score_review_sample,
)

RECEIPT_VERSION = "discovery_review_receipt_v1"
FREEZE_FIELDS = frozenset({"week_start", "frozen_at", "markets", "clusters"})
SCORE_FIELDS = frozenset({"sample", "reviews"})
THRESHOLDS_SOURCE = "C07, 2026-09-12-42-plan-contracts.md"


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _fields(value, expected, code):
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(code)
    return value


def freeze_receipt(document) -> dict:
    values = _fields(document, FREEZE_FIELDS, "freeze_input_invalid")
    markets = values["markets"]
    if (
        not isinstance(markets, list)
        or len(markets) != len(PRODUCT_MARKETS)
        or set(markets) != set(PRODUCT_MARKETS)
    ):
        raise ValueError("markets_not_promised")
    return {
        "sample": freeze_review_sample(
            values["clusters"],
            week_start=values["week_start"],
            frozen_at=values["frozen_at"],
            markets=values["markets"],
        )
    }


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _frozen_sample(raw: bytes):
    receipt = json.loads(raw.decode("utf-8"))
    if (
        not isinstance(receipt, dict)
        or set(receipt)
        != {"receipt_version", "command", "input_sha256", "sample", "receipt_sha256"}
        or receipt["receipt_version"] != RECEIPT_VERSION
        or receipt["command"] != "freeze"
    ):
        raise ValueError("freeze_receipt_invalid")
    unsealed = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if receipt["receipt_sha256"] != _digest(unsealed):
        raise ValueError("freeze_receipt_digest_mismatch")
    return receipt["sample"]


def _pinned_thresholds(raw):
    if raw is None:
        return None
    thresholds = json.loads(raw.decode("utf-8"))
    if (
        not isinstance(thresholds, dict)
        or set(thresholds) != set(C07_REVIEW_THRESHOLDS)
        or any(
            type(thresholds[key]) is not type(value) or thresholds[key] != value
            for key, value in C07_REVIEW_THRESHOLDS.items()
        )
    ):
        raise ValueError("thresholds_not_c07")
    return thresholds


def score_receipt(document, *, freeze_raw: bytes, thresholds_raw) -> dict:
    values = _fields(document, SCORE_FIELDS, "score_input_invalid")
    frozen = _frozen_sample(freeze_raw)
    if values["sample"] != frozen:
        raise ValueError("sample_not_frozen_sample")
    if (
        not isinstance(frozen, dict)
        or not isinstance(frozen.get("markets"), dict)
        or set(frozen["markets"]) != set(PRODUCT_MARKETS)
    ):
        raise ValueError("markets_not_promised")
    thresholds = _pinned_thresholds(thresholds_raw)
    result = score_review_sample(frozen, values["reviews"])
    gate = None if thresholds is None else gate_c07_thresholds(result)
    return {
        "verdict": "no_gate" if gate is None else gate["verdict"],
        "freeze_receipt_sha256": _sha256(freeze_raw),
        "thresholds": thresholds,
        "thresholds_sha256": None
        if thresholds_raw is None
        else _sha256(thresholds_raw),
        "thresholds_source": THRESHOLDS_SOURCE
        if thresholds is not None
        else "no thresholds file supplied; no gate verdict",
        "unobserved_markets": sorted(
            market
            for market, entry in frozen["markets"].items()
            if entry["gate"] == "unmet"
        ),
        "result": result,
        "gate": gate,
    }


def build_receipt(
    command: str, raw: bytes, *, freeze_raw=None, thresholds_raw=None
) -> dict:
    document = json.loads(raw.decode("utf-8"))
    if command == "freeze":
        if freeze_raw is not None or thresholds_raw is not None:
            raise ValueError("freeze_takes_no_receipt_or_thresholds")
        body = freeze_receipt(document)
    elif command == "score":
        if freeze_raw is None:
            raise ValueError("freeze_receipt_required")
        body = score_receipt(
            document, freeze_raw=freeze_raw, thresholds_raw=thresholds_raw
        )
    else:
        raise ValueError("command_invalid")
    receipt = {
        "receipt_version": RECEIPT_VERSION,
        "command": command,
        "input_sha256": _sha256(raw),
        **body,
    }
    receipt["receipt_sha256"] = _digest(receipt)
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ops.evaluation.run_discovery_review",
        description="Freeze or score a closed week discovery review into a receipt.",
    )
    parser.add_argument("command", choices=("freeze", "score"))
    parser.add_argument("--freeze-receipt", type=Path)
    parser.add_argument("--thresholds", type=Path)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        receipt = build_receipt(
            args.command,
            args.input.read_bytes(),
            freeze_raw=None
            if args.freeze_receipt is None
            else args.freeze_receipt.read_bytes(),
            thresholds_raw=None
            if args.thresholds is None
            else args.thresholds.read_bytes(),
        )
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    except (OSError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(f"{args.command} receipt_sha256={receipt['receipt_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
