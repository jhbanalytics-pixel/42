"""CLI for seed_candidates human review (V3 Track C3).

Never writes configs. Approved candidates emit a ready-to-paste YAML snippet.

Usage:
    py -3.13 scripts/review_seed_candidates.py --list
    py -3.13 scripts/review_seed_candidates.py --approve CANDIDATE_ID --target topic_group:music_amapiano
    py -3.13 scripts/review_seed_candidates.py --reject CANDIDATE_ID --reason "too niche"
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent

env_path = repo_root / ".env"
try:
    from dotenv import load_dotenv

    if env_path.exists():
        load_dotenv(env_path, override=False)
except ImportError:
    pass

sys.path.insert(0, str(repo_root))

from google.cloud import bigquery as bq
from src.utils.bigquery import get_client, get_dataset


def _fetch_candidate(client: bq.Client, dataset: str, candidate_id: str) -> dict | None:
    sql = f"""
    SELECT *
    FROM `{client.project}.{dataset}.seed_candidates`
    WHERE candidate_id = @id
    ORDER BY proposed_date DESC
    LIMIT 1
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[bq.ScalarQueryParameter("id", "STRING", candidate_id)]
    )
    rows = list(client.query(sql, job_config=job_config).result())
    if not rows:
        return None
    return dict(rows[0].items())


def _list_pending(client: bq.Client, dataset: str, market: str | None) -> None:
    where = "status = 'pending'"
    params: list[bq.ScalarQueryParameter] = []
    if market:
        where += " AND market = @market"
        params.append(bq.ScalarQueryParameter("market", "STRING", market.lower()))
    sql = f"""
    SELECT candidate_id, proposed_date, market, candidate_type, candidate_value,
           lane, score, source
    FROM `{client.project}.{dataset}.seed_candidates`
    WHERE {where}
    ORDER BY proposed_date DESC, score DESC
    LIMIT 100
    """
    job_config = bq.QueryJobConfig(query_parameters=params)
    rows = client.query(sql, job_config=job_config).result()
    count = 0
    for r in rows:
        count += 1
        print(
            f"{r.candidate_id}\t{r.proposed_date}\t{r.market}\t{r.lane}\t"
            f"{r.candidate_type}:{r.candidate_value}\tscore={r.score:.3f}"
        )
    if count == 0:
        print("(no pending candidates)")


def _yaml_snippet(row: dict, target: str | None) -> str:
    market = row["market"]
    value = row["candidate_value"]
    ctype = row["candidate_type"]
    if ctype == "slang":
        path = f"configs/keywords/{market}.yaml"
        block = f"    - {value}"
        hint = "Add under slang.high_signal.terms (or appropriate slang block)"
    elif target and target.startswith("topic_group:"):
        tg = target.split(":", 1)[1]
        path = f"configs/topic_groups/{market}.yaml"
        block = f"    - {value}"
        hint = f"Add under topic_groups.{tg}.keywords"
    else:
        path = f"configs/topic_groups/{market}.yaml"
        block = f"    - {value}"
        hint = "Pick a topic_group and add under its keywords list"
    return f"# Target: {path}\n# {hint}\n{block}\n"


def _update_status(
    client: bq.Client,
    dataset: str,
    candidate_id: str,
    status: str,
    *,
    by: str,
    rationale: str | None = None,
) -> bool:
    now = datetime.now(UTC).isoformat()
    sql = f"""
    UPDATE `{client.project}.{dataset}.seed_candidates`
    SET status = @status,
        status_by = @by,
        status_at = @at,
        rationale = @rationale
    WHERE candidate_id = @id
      AND status = 'pending'
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("status", "STRING", status),
            bq.ScalarQueryParameter("by", "STRING", by),
            bq.ScalarQueryParameter("at", "TIMESTAMP", now),
            bq.ScalarQueryParameter("rationale", "STRING", rationale),
            bq.ScalarQueryParameter("id", "STRING", candidate_id),
        ]
    )
    client.query(sql, job_config=job_config).result()
    check = _fetch_candidate(client, dataset, candidate_id)
    return bool(check and check.get("status") == status)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review seed_candidates proposals")
    parser.add_argument("--list", action="store_true", help="List pending candidates")
    parser.add_argument("--market", default=None, help="Filter --list by market")
    parser.add_argument("--approve", metavar="ID", help="Approve a candidate")
    parser.add_argument("--reject", metavar="ID", help="Reject a candidate")
    parser.add_argument("--target", default=None, help="topic_group:slug for --approve YAML hint")
    parser.add_argument("--reason", default=None, help="Rationale for --reject")
    parser.add_argument("--by", default="albert", help="Reviewer id for status_by")
    args = parser.parse_args(argv)

    if not any([args.list, args.approve, args.reject]):
        parser.error("one of --list, --approve, --reject required")

    client = get_client()
    dataset = get_dataset()

    if args.list:
        _list_pending(client, dataset, args.market)
        return 0

    cid = args.approve or args.reject
    if not cid:
        parser.error("--approve/--reject requires a candidate_id")
    row = _fetch_candidate(client, dataset, cid)
    if not row:
        print(f"unknown candidate_id: {cid}", file=sys.stderr)
        return 1
    if row.get("status") != "pending":
        print(f"candidate {cid} is not pending (status={row.get('status')})", file=sys.stderr)
        return 1

    if args.approve:
        ok = _update_status(client, dataset, cid, "approved", by=args.by)
        if not ok:
            print(f"approve failed for {cid}", file=sys.stderr)
            return 1
        print(_yaml_snippet(row, args.target))
        return 0

    if not args.reason:
        print("--reason required for --reject", file=sys.stderr)
        return 1
    ok = _update_status(client, dataset, cid, "rejected", by=args.by, rationale=args.reason)
    if not ok:
        print(f"reject failed for {cid}", file=sys.stderr)
        return 1
    print(f"rejected {cid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
