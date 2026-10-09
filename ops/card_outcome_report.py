"""Read-only runner for the card and hold outcome measure (core/eval/card_outcome.py).

    py -3.13 ops/card_outcome_report.py --start 2026-09-24 --end 2026-10-01 --out <folder>

Runs the three queries of core/eval/sql/card_outcome.sql on BigQuery for the run dates START to END (the states and
good detect days are read 14 days past END), each dry run first, each with maximum_bytes_billed, and writes
card_outcome_<start>_<end>.md and .json to the folder. SELECT only: every query is checked before the first call, and
a query that is not a SELECT or WITH stops the run with nothing sent. The outputs hold counts and rates per market,
hold reason and stratum, with the bytes each query billed; no item row, title, post text or creator is written.
Credentials are the builder service account by impersonation, as in the approved read pattern. No table is created,
no job is started, nothing is written to the warehouse.
"""

import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google.cloud import bigquery  # noqa: E402

from core.eval import card_outcome as co  # noqa: E402
from core.eval import card_outcome_read as rd  # noqa: E402

PROJECT = "ogilvy-trends-v2"
BUILDER = "f42-builder@ogilvy-trends-v2.iam.gserviceaccount.com"
GB = rd.GB
QUERY_CAP = rd.QUERY_CAP
TOTAL_CAP = rd.TOTAL_CAP
QUERIES = rd.QUERIES
Refused = rd.Refused


def make_client():
    import google.auth
    from google.auth import impersonated_credentials

    source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    credentials = impersonated_credentials.Credentials(
        source_credentials=source, target_principal=BUILDER,
        target_scopes=["https://www.googleapis.com/auth/cloud-platform"], lifetime=600)
    return bigquery.Client(project=PROJECT, credentials=credentials, location="US")


def _jsonable(o):
    return o.isoformat() if isinstance(o, date) else str(o)


def run(client, start, end, out_dir, *, query_cap=QUERY_CAP, total_cap=TOTAL_CAP):
    got, queries = rd.read_inputs(client, QUERIES, start, end, query_cap=query_cap, total_cap=total_cap)
    detect_days = [r["run_date"] for r in got["detect_days"]]
    rows = co.build_outcomes(rd.briefs(got["briefs"]), got["states"], detect_days)
    horizons = {str(h): co.summarize(rows, end=end, horizon=h) for h in co.HORIZONS}
    seen = co.persisting_seen(rows)
    statuses = Counter(r["status"] for r in got["briefs"])
    report = {"definition": co.DEFINITION, "start": start, "end": end, "queries": queries,
              "total_bytes_billed": sum(q["bytes_billed"] or 0 for q in queries),
              "briefs": len(got["briefs"]), "brief_statuses": dict(sorted(statuses.items())),
              "briefs_skipped": sum(n for st, n in statuses.items() if st in co.SKIPPED_STATUSES),
              "outcome_rows": len(rows), "persisting_seen": seen,
              "state_rows": {"total": len(got["states"]), "untested": sum(bool(r["untested"]) for r in got["states"])},
              "state_distribution": co.state_distribution(rows), "state_mix": co.state_mix(rows),
              "good_detect_days": {"count": len(detect_days), "first": min(detect_days, default=None),
                                   "last": max(detect_days, default=None)},
              "horizons": horizons}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"card_outcome_{start.isoformat()}_{end.isoformat()}"
    md = co.report_markdown(rows, {h: horizons[str(h)] for h in co.HORIZONS}, end=end)
    (out / f"{stem}.md").write_text(md, encoding="utf-8")
    (out / f"{stem}.json").write_text(json.dumps(report, indent=2, default=_jsonable), encoding="utf-8")
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--start", type=date.fromisoformat, required=True)
    p.add_argument("--end", type=date.fromisoformat, required=True)
    p.add_argument("--out", required=True, help="folder for the markdown and json files")
    a = p.parse_args(argv)
    report = run(make_client(), a.start, a.end, a.out)
    for q in report["queries"]:
        print(json.dumps(q, default=_jsonable))
    print(f"total bytes billed {report['total_bytes_billed']}; outcome rows {report['outcome_rows']}")


if __name__ == "__main__":
    main()
