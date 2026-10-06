"""BUILD.md 3.3 check: three known recurring items find their earlier waves.

    py -3.13 -m core.eval.history_check [ITEM_ID ...] [--market ZA|NG|KE]

With no item ids it picks three item and market pairs in v_item_waves with at least two waves of 8 or more peak
posts (waves that count for Recurring), most such waves first; named item ids are checked as given. It runs Ask's
history tool on each and prints every earlier wave that counts for Recurring (a wave before the latest with a peak of
8 or more posts) with the query id that produced it. It passes when at least three items were checked and every one
shows at least one such wave. Every read goes through the agent's sql_query guard as f42-builder; nothing is
written and no SocialCrawl call is made. Exit code 0 on a pass, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from core.agent.context import RunContext
from core.agent.tools.dates import SAST
from core.agent.tools.history import MARKETS, RECURRING_PEAK, history
from core.agent.tools.sql_query import sql_query

WANTED = 3


def pick(ctx: RunContext, warehouse, market: str | None) -> list[dict]:
    params, where = {"n": WANTED}, ""
    if market:
        params["market"] = market
        where = "WHERE w.market = @market "
    sql = ("SELECT w.item_id, w.market, COUNT(*) AS waves, "
           f"COUNTIF(w.peak_posts >= {RECURRING_PEAK}) AS big_waves "
           f"FROM intelligence_42_core.v_item_waves w {where}"
           f"GROUP BY w.item_id, w.market HAVING COUNTIF(w.peak_posts >= {RECURRING_PEAK}) >= 2 "
           "ORDER BY big_waves DESC, waves DESC, w.item_id, w.market LIMIT @n")
    return sql_query(ctx, warehouse, sql, purpose="history_check: items with two or more waves of 8 or more posts",
                     params=params)["rows"]


def main(argv=None, warehouse=None, out=sys.stdout, now=None) -> int:
    parser = argparse.ArgumentParser(description="BUILD.md 3.3: known recurring items find their earlier waves.")
    parser.add_argument("item_ids", nargs="*")
    parser.add_argument("--market", choices=MARKETS)
    args = parser.parse_args(argv)
    if warehouse is None:
        from core.agent.tools.sql_query import BigQueryWarehouse

        warehouse = BigQueryWarehouse()
    as_of = (now or datetime.now(timezone.utc)).astimezone(SAST).replace(microsecond=0)
    ctx = RunContext(run_id=f"history_check_{as_of:%Y%m%d_%H%M%S}", tier="T0", as_of=as_of)

    if args.item_ids:
        targets = [(item_id, args.market) for item_id in args.item_ids]
    else:
        picked = pick(ctx, warehouse, args.market)
        if not picked:
            print(f"No item in {args.market or 'any market'} has more than one wave of 8 or more posts in "
                  "v_item_waves.", file=out)
            return 1
        targets = [(row["item_id"], row["market"]) for row in picked]

    found = 0
    for item_id, market in targets:
        result = history(ctx, warehouse, item_id=item_id, market=market)
        earlier = [w for w in result["waves"] if not w["latest"] and w["counts_for_recurring"]]
        name = f"{item_id} {market}" if market else item_id
        if not earlier:
            print(f"{item_id}: no earlier wave of {RECURRING_PEAK} or more posts ({len(result['waves'])} wave(s) in "
                  f"all) [{result['query_id']}]",
                  file=out)
            continue
        found += 1
        label = earlier[0].get("label") or item_id
        print(f"{name} ({label}): {len(earlier)} earlier wave{'' if len(earlier) == 1 else 's'}", file=out)
        for w in earlier:
            where = "" if market else f"{w['market']} "
            print(f"  {where}{w['wave_start']} to {w['wave_end']}, peak {w['peak_posts']} posts on {w['peak_date']}"
                  f"{', same time last year' if w['same_time_last_year'] else ''} [{w['query_id']}]", file=out)
        for m in result["moments"]:
            print(f"  near {m['name']} ({m['moment_date']}) peak {m['peak_date']} [{m['query_id']}]", file=out)

    short = len(targets) < WANTED
    verdict = "PASS" if found == len(targets) and not short else "FAIL"
    print(f"{verdict}: {found} of {len(targets)} items found their earlier waves"
          f"{f'; the BUILD check needs {WANTED}' if short else ''}", file=out)
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
