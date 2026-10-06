"""P1 production capture for certification (E03).

Runs the production baseline capture from ops/deploy/production_baseline.py
through a read client that accepts an anchored empty BigQuery table list (see
ops/certification/empty_table_list.py). Each accepted empty list is recorded
in the capture's transport_normalizations after the normalizations the
baseline capture records itself, which is the order the reads happen in,
and the content digest is computed again over the result. The command takes
the same arguments and returns the same exit codes as the baseline capture
command, and its output is read by ops/certification/production_comparison.py.

The command is a read: it lists and gets resources and policies, and never
writes to the project.
"""

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

from ops.certification.empty_table_list import AnchoredEmptyTableListClient
from ops.deploy.production_baseline import (
    ReadClient,
    _canonical_digest,
    _default_client,
    capture_baseline,
    write_capture,
)


def capture_production(
    inventory: dict,
    client: ReadClient,
    *,
    identity: str,
    private_hash_key: bytes,
    observed_at: str | None = None,
) -> dict:
    wrapped = AnchoredEmptyTableListClient(client)
    result = capture_baseline(
        inventory,
        wrapped,
        identity=identity,
        private_hash_key=private_hash_key,
        observed_at=observed_at,
    )
    if not wrapped.normalizations:
        return result
    material = {key: value for key, value in result.items() if key != "content_digest"}
    if result.get("content_digest") != _canonical_digest(material):
        raise ValueError("capture_content_digest_mismatch")
    material["transport_normalizations"] = [
        *material.get("transport_normalizations", []),
        *wrapped.normalizations,
    ]
    material["content_digest"] = _canonical_digest(material)
    return material


def main(
    argv: list[str] | None = None,
    *,
    client_factory: Callable[[str], ReadClient] = _default_client,
) -> int:
    parser = argparse.ArgumentParser(prog="production_capture")
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("--inventory", type=Path, required=True)
    capture_parser.add_argument("--output", type=Path, required=True)
    capture_parser.add_argument("--identity", required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError(f"output_exists:{args.output}")
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    hash_key = os.environ.get("R00_BASELINE_HASH_KEY", "").encode("utf-8")
    if not hash_key:
        raise ValueError("private_hash_key_missing:R00_BASELINE_HASH_KEY")
    result = capture_production(
        inventory,
        client_factory(args.identity),
        identity=args.identity,
        private_hash_key=hash_key,
    )
    write_capture(args.output, result)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "complete": result["complete"],
                "content_digest": result["content_digest"],
            }
        )
    )
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    sys.exit(main())
