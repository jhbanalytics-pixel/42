"""Where one Ask's time went, read from its stored run row: research, writing, and the checks after writing.

Read-only. Steps carry their own second-resolution "at", so every figure is a difference of two stored stamps.

    py -3.13 -m core.eval.ask_timing a_20261003_91b2509f
"""

from __future__ import annotations

import json
import sys
from datetime import datetime

RUN_SQL = ("SELECT record FROM `ogilvy-trends-v2.intelligence_42_agent.runs` "
           "WHERE stage = 'ask' AND JSON_VALUE(record, '$.ask_id') = @ask_id ORDER BY finished_at DESC LIMIT 1")


def _at(value) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _record(value) -> dict:
    """The row's record as a dict. runs.record is a JSON column, which the BigQuery client already decodes; a
    string (TO_JSON_STRING, an older client, a fixture) is decoded here."""
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def timing(record: dict) -> dict:
    """{"total_s", "phases": {research_s, writing_s, checks_s}, "by_kind": {kind: seconds}, "slowest": [...]}.
    Research runs from the ask's start to the first write step, writing from there to the first check step (the
    writer and any number repair), and checks from there to the finish (code checks, support and field checks).
    A step's time is the gap to the next step, so it is the time spent after it was announced."""
    steps = [s for s in record.get("steps") or [] if s.get("at")]
    start, end = _at(record["created_at"]), _at(record["finished_at"])
    write = next((_at(s["at"]) for s in steps if s.get("kind") == "write"), None)
    check = next((_at(s["at"]) for s in steps if s.get("kind") == "check"), None)
    phases = {"research_s": ((write or end) - start).total_seconds()}
    if write is not None:
        phases["writing_s"] = ((check or end) - write).total_seconds()
    if check is not None:
        phases["checks_s"] = (end - check).total_seconds()
    by_kind, gaps = {}, []
    for step, after in zip(steps, steps[1:] + [{"at": record["finished_at"]}]):
        seconds = (_at(after["at"]) - _at(step["at"])).total_seconds()
        by_kind[step.get("kind")] = by_kind.get(step.get("kind"), 0.0) + seconds
        gaps.append((seconds, step.get("kind"), step.get("text")))
    gaps.sort(key=lambda g: -g[0])
    return {"total_s": (end - start).total_seconds(), "steps": len(steps), "phases": phases, "by_kind": by_kind,
            "slowest": [{"seconds": s, "kind": k, "text": t} for s, k, t in gaps[:8]]}


def main(argv: list[str], client=None) -> int:
    if len(argv) != 1:
        print("usage: python -m core.eval.ask_timing <ask_id>")
        return 2
    from google.cloud import bigquery

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # step text never stops the print on a narrow Windows console
    client = client or bigquery.Client(project="ogilvy-trends-v2")
    config = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("ask_id", "STRING", argv[0])])
    rows = list(client.query(RUN_SQL, job_config=config).result())
    if not rows:
        print(f"no ask run row for {argv[0]}")
        return 1
    print(json.dumps(timing(_record(rows[0]["record"])), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
