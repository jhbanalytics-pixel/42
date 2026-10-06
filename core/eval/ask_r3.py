from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core.eval import ask_r2 as shared


ROOT = Path(__file__).resolve().parents[2]
PREFIX_ROOT = "l3-ask-20260930-r3"
OUTPUT_PATH = Path("C:/Users/AlbertMeintjes/dev/42-inputs/ASK-2026-09-30-R3.md")
DATA_DIR = Path("C:/Users/AlbertMeintjes/dev/42-inputs/ASK-2026-09-30-R3-data")
GATE_PATH = ROOT / "plans" / "r3-health-gate.json"
INITIAL_REPORT_PATH = OUTPUT_PATH.with_name("ASK-2026-09-30-R3-initial-stop.md")


def main(argv=None, *, repo_root=ROOT, gate_path=None, output_path=OUTPUT_PATH,
         data_dir=DATA_DIR, morning_dir=shared.MORNING_DIR):
    parser = argparse.ArgumentParser(prog="py -3.13 -m core.eval.ask_r3")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args, _ = parser.parse_known_args(argv)

    gate_path = Path(gate_path) if gate_path is not None else Path(repo_root) / "plans" / "r3-health-gate.json"
    if args.execute:
        try:
            shared._module_origin(repo_root, shared)
            shared._module_origin(repo_root, sys.modules[__name__])
        except shared.OperationRefused as exc:
            print(f"Refused: {exc.args[0] if exc.args else 'module_origin_unverified'}")
            return shared.REFUSED

    original_prefix = shared.PREFIX_ROOT
    shared.PREFIX_ROOT = PREFIX_ROOT
    try:
        return shared.main(
            argv,
            repo_root=repo_root,
            runner_path=Path(__file__).resolve(),
            gate_path=gate_path,
            output_path=output_path,
            data_dir=data_dir,
            morning_dir=morning_dir,
            evaluation_label="R3",
            provider_500_retries=1,
            allow_resume=args.resume,
            r3_anchor_report_path=INITIAL_REPORT_PATH,
        )
    finally:
        shared.PREFIX_ROOT = original_prefix


if __name__ == "__main__":
    raise SystemExit(main())
