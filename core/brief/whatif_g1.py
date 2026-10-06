"""Read-only forecast for the items a brief held at G1 (TRUST.md section 2, BUILD.md 1.12).

For one brief date it reads each market's current brief (v_briefs_current), takes every held item whose rule is G1,
and runs the brief's data gates on it again with each of the last three market-days on its main platform treated as
valid: core/brief/job.py _gate, which applies core/trust/gate.py gate_card and then the brief's own data holds
(platform-generic tag, no readable name, paid key, likely coordinated and the post floors). Only a copy of the gate
context is changed; gate_card and _gate run as they are. Items held at G1 never reached the later gates, so this is
how many of them would pass on a day G1 clears, before confirm, critic and support model checks.

A stored held item keeps item_id, title, rule, reason, reason_text, evidence (the pack's posts), numbers (the pack's
pinned numbers less growth while untested), count_line and failed_reason. It does not keep the gate inputs: lane
classes, market scope counts, sponsored share, hashtags, canonical key, state, map status, authenticity or the main
platform. Only current-market candidates are stored in a payload, so every stored held item had market scope
"market" when the brief ran. The missing inputs are read again for the same date, read-only, with the brief's own
queries: v_item_state_current with the cultural map (the candidates read's columns that the gates use),
core/brief/market_scope.py and core/brief/gatectx.py on the stored evidence and numbers. When an input cannot be
read, the item is reported as unknown at the first gate that needs it, never guessed.

Every query is SELECT only, dry run first and refused over the cap, then run with maximum_bytes_billed at the cap
(64 MiB, as core/brief/holds_report.py, unless --max-mib is given). Nothing is written and no model is called.

Entry point: python -m core.brief.whatif_g1 YYYY-MM-DD [--max-mib N]
"""

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date

from core.api.today import without_hidden
from core.brief import gatectx, holds_report
from core.brief.evidence import SuppressionUnreadable, read_hidden
from core.brief.job import MARKETS, _gate, readable_title
from core.brief.market_scope import read_market_scope
from core.detect import sqlrun
from core.detect.sqlrun import AGENT, CORE

PROJECT = holds_report.PROJECT
MAX_BYTES = holds_report.MAX_BYTES
BRIEFS = holds_report.BRIEFS
# The candidates read's columns (core/brief/sql/brief.sql candidates) that gate_card and _gate read, for the held
# items only.
ROWS = """SELECT s.*, cm.kind map_kind, cm.status map_status, cm.label, cm.canonical_key
FROM {core}.v_item_state_current s
LEFT JOIN {core}.cultural_map cm ON cm.item_id = s.item_id AND cm.valid_to IS NULL
WHERE s.metric_date = @d AND s.market = @market AND s.item_id IN UNNEST(SPLIT(@item_ids, ','))"""

# The order _gate applies its holds in once G1 passes. G8 and G10 publish, so a card that reaches "end" passes.
ORDER = ("G3", "G6", "G5", "G5b", "G4b", "G2", "name", "paid_key", "G4", "floor", "end")
LABELS = {"name": "no readable name", "paid_key": "G5b", "floor": "post floor"}
PASSES = "passes data gates; still needs confirm, critic and support model checks"
ROW_FIELDS = "item_state row (sponsored_share, state, map_status, authenticity, canonical_key, hashtags)"
_WRITE = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|TRUNCATE|ALTER)\b", re.IGNORECASE)


class Refused(RuntimeError):
    """A query that is not a plain SELECT, or that would read more than the cap."""


def _code(sql):
    return "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--")).strip()


def read_only(sql):
    code = _code(sql)
    return code.upper().startswith(("SELECT", "WITH")) and not _WRITE.search(code) and ";" not in code.rstrip(";")


class _Done:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class CappedClient:
    """Stands in for the BigQuery client that sqlrun.query, gatectx and market_scope call. Each distinct query
    runs once: dry run first, refused over the cap or when it is not a SELECT, then run with maximum_bytes_billed.
    A repeated query with the same parameters is answered from memory, so it is neither run nor billed again."""

    def __init__(self, client, max_bytes=MAX_BYTES):
        self.client, self.max_bytes, self.bytes, self._memo = client, max_bytes, 0, {}

    def query(self, sql, job_config=None):
        from google.cloud import bigquery

        if not read_only(sql):
            raise Refused("refused: not a read-only SELECT")
        params = list(getattr(job_config, "query_parameters", None) or [])
        key = (sql, tuple(sorted((p.name, p.type_, str(p.value)) for p in params)))
        if key not in self._memo:
            dry = self.client.query(sql, job_config=bigquery.QueryJobConfig(
                query_parameters=params, dry_run=True, use_query_cache=False))
            kind = getattr(dry, "statement_type", "SELECT")
            if kind not in (None, "SELECT"):
                raise Refused(f"refused: dry run says {kind}, not SELECT")
            if dry.total_bytes_processed > self.max_bytes:
                raise Refused(f"refused: query would read {dry.total_bytes_processed} bytes, over {self.max_bytes}")
            job = self.client.query(sql, job_config=bigquery.QueryJobConfig(
                query_parameters=params, maximum_bytes_billed=self.max_bytes))
            self._memo[key] = [dict(r.items()) for r in job.result()]
            self.bytes += dry.total_bytes_processed
        return _Done(self._memo[key])


@dataclass
class Outcome:
    market: str
    item_id: str
    title: str | None
    platform: str | None
    verdict: str  # pass | moments | held | unknown
    gate: str | None  # the rule that held it, or the first gate that cannot be judged
    text: str

    def line(self):
        return f"{self.market} | {self.title or self.item_id[:12]} | {self.platform or 'no main platform'} | " \
               f"{self.text}"


def main_platform(client, row, d, market, core=CORE, agent=AGENT):
    """The item's main platform as core/brief/gatectx.py build_ctx picks it: the main series' platform, else the
    platform with most sightings in the market over 14 days. Same queries and parameters, so these are memo hits."""
    def run(name, params):
        return sqlrun.query(client, gatectx.QUERIES[name], params, core=core, agent=agent)

    if row.get("main_series_id"):
        rows = run("series_platform", {"series_id": row["main_series_id"], "d": d})
        if rows and rows[0]["platform"]:
            return rows[0]["platform"]
    per = {}
    for r in run("sightings", {"item_id": row["item_id"], "market": market, "d": d}):
        if r["platform"]:
            per[r["platform"]] = per.get(r["platform"], 0) + r["n"]
    return min(per, key=lambda p: (-per[p], p)) if per else None


def _why(error):
    """A refusal says why in fixed words; any other failure is named by its class only."""
    return str(error) if isinstance(error, Refused) else type(error).__name__


def _step(decision, cand):
    if decision.publish:
        return "end"
    if decision.rule == "G5b" and (decision.reason or "").endswith("marks paid posts"):
        return "paid_key"
    if decision.rule in ORDER:
        return decision.rule
    if cand.get("floor_held"):
        return "floor"
    return "name"


def _pinned_matters(client, evidence, numbers, ctx, d, market, core, agent):
    """True when the stored numbers cannot settle G4b: the item is political and corroboration would change with
    a pinned number the stored record does not show."""
    if not ctx["political"] or any(n.get("query_id") for n in numbers if isinstance(n, dict)):
        return False
    ids = sorted({r["id"] for r in evidence if r.get("id")})
    if not ids:
        return False
    measured = {r["post_id"] for r in sqlrun.query(client, gatectx.QUERIES["post_lanes"],
                                                   {"post_ids": ",".join(ids), "market": market, "d": d},
                                                   core=core, agent=agent)}
    return gatectx._corroborated(evidence, measured, [{"query_id": "pinned"}]) != ctx["corroborated_unbiased"]


def judge(client, d, market, item, row, hidden, *, campaign_hashtags, political_terms, core=CORE, agent=AGENT):
    """One G1-held item's Outcome with G1 treated as clear. row: its v_item_state_current row, or None."""
    item_id = item["item_id"]
    # The stored title is masked again, as Today does at read time: the list can grow after the brief ran.
    title = without_hidden(item["title"], hidden) if isinstance(item.get("title"), str) else None
    missing = {}  # gate -> what could not be read

    def lack(gate, what):
        missing.setdefault(gate, []).append(what)

    if row is None:
        lack("G5", ROW_FIELDS)
        row = {"item_id": item_id}
    evidence = item.get("evidence")
    if not isinstance(evidence, list):
        lack("G5", "stored evidence")
        evidence = []
    numbers = item.get("numbers") if isinstance(item.get("numbers"), list) else []

    # As _prepare in core/brief/job.py: the suppression mask, the title, then the market scope read.
    row = without_hidden(dict(row), hidden)
    row["kind"] = row.get("kind") or row.get("map_kind")
    readable = readable_title(row.get("label"), row.get("canonical_key"))
    row["title"] = without_hidden(readable or f"Unnamed {row['kind']}", hidden)
    row["nameless"] = readable is None
    try:
        scope = read_market_scope(client, row, d, market, core=core, agent=agent)
        if scope.get("market_scope") != "market":
            scope["market_scope"] = "global"
    except Exception as e:
        lack("G6", f"market scope counts (read failed: {_why(e)})")
        scope = {"market_scope": "market", "market_posts7": 1, "total_posts7": 1, "market_share7": 1.0}
    row.update(scope)

    try:
        ctx = gatectx.build_ctx(client, row, d, market, evidence, campaign_hashtags=campaign_hashtags,
                                political_terms=political_terms, numbers=numbers, core=core, agent=agent)
        platform = main_platform(client, row, d, market, core=core, agent=agent)
        if _pinned_matters(client, evidence, numbers, ctx, d, market, core, agent):
            lack("G4b", "stored numbers (no pinned query id to settle corroboration)")
    except Exception as e:
        return Outcome(market, item_id, title, None, "unknown", "G3",
                       f"unknown at G3: gate context (read failed: {_why(e)})")

    whatif = {**ctx, "valid_days": [True, True, True]}  # a copy; the stored and live contexts are not touched
    cand = {"row": row, "ctx": whatif, "market": market,
            "pack": {"evidence": evidence, "numbers": numbers, "facts": []}}
    decision = _gate(cand, None)
    step = _step(decision, cand)
    first = min(missing, key=ORDER.index) if missing else "end"
    if step != "end" and ORDER.index(step) < ORDER.index(first):
        reason = decision.reason or ""
        if step == "floor":
            reason += " (a confirm search can still add posts)"
        return Outcome(market, item_id, title, platform, "held", LABELS.get(step, step),
                       f"held by {LABELS.get(step, step)}: {reason}")
    if missing:
        return Outcome(market, item_id, title, platform, "unknown", first,
                       f"unknown at {first}: {'; '.join(missing[first])}")
    if decision.where == "moments":
        return Outcome(market, item_id, title, platform, "moments", "G8",
                       f"passes data gates to Moments, not Today (G8): {decision.reason}")
    return Outcome(market, item_id, title, platform, "pass", None, PASSES)


def g1_items(briefs):
    """{market: G1-held items} from each market's current brief row."""
    out = {}
    for b in briefs:
        payload = json.loads(b["payload"]) if isinstance(b["payload"], str) else b["payload"]
        items = ((payload or {}).get("held_back") or {}).get("items") or []
        out[b["market"]] = [i for i in items if i.get("rule") == "G1"]
    return out


def forecast(client, d, *, hidden=None, campaign_hashtags=None, political_terms=None, core=CORE, agent=AGENT):
    """(the markets read, Outcomes in market and stored order). client: a CappedClient."""
    briefs = sqlrun.query(client, BRIEFS, {"d": d}, core=core, agent=agent)
    held = g1_items(briefs)
    if any(held.values()) and hidden is None:
        hidden = read_hidden(client, core=core, agent=agent)
    campaign_hashtags = gatectx.load_campaign_hashtags() if campaign_hashtags is None else campaign_hashtags
    outcomes = []
    for market in [m for m in MARKETS if m in held] + sorted(set(held) - set(MARKETS)):
        items = held[market]
        if not items:
            continue
        terms = (political_terms or {}).get(market)
        terms = gatectx.load_political_terms(market) if terms is None else terms
        ids = ",".join(sorted({i["item_id"] for i in items}))
        rows = {r["item_id"]: r for r in sqlrun.query(client, ROWS, {"d": d, "market": market, "item_ids": ids},
                                                       core=core, agent=agent)}
        for item in items:
            outcomes.append(judge(client, d, market, item, rows.get(item["item_id"]), hidden,
                                  campaign_hashtags=campaign_hashtags, political_terms=terms, core=core,
                                  agent=agent))
    return [m for m in MARKETS if m in held] + sorted(set(held) - set(MARKETS)), outcomes


def summary(markets, outcomes):
    """One count line per market read, then the total."""
    lines = []
    for market in markets + ["all"]:
        mine = [o for o in outcomes if market in ("all", o.market)]
        held = Counter(o.gate for o in mine if o.verdict == "held")
        n = Counter(o.verdict for o in mine)
        breakdown = ", ".join(f"{g} {k}" for g, k in held.most_common())
        lines.append(f"{market}: {len(mine)} held at G1; {n['pass']} pass data gates, {n['moments']} to Moments, "
                     f"{n['held']} held{f' ({breakdown})' if breakdown else ''}, {n['unknown']} unknown")
    return lines


def main(argv, client=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # a title a narrow console cannot show never stops the report
    parser = argparse.ArgumentParser(prog="core.brief.whatif_g1")
    parser.add_argument("date", type=date.fromisoformat)
    parser.add_argument("--max-mib", type=int, default=MAX_BYTES // (1024 * 1024))
    opts = parser.parse_args(argv[1:])
    if client is None:
        from google.cloud import bigquery

        client = bigquery.Client(project=PROJECT, location="US")
    capped = CappedClient(client, opts.max_mib * 1024 * 1024)
    try:
        markets, outcomes = forecast(capped, opts.date)
    except (Refused, SuppressionUnreadable) as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"brief {opts.date.isoformat()}: items held at G1, gated again with the last 3 market-days treated as "
          "valid; pass counts are an upper bound (data gates only: confirm, critic, support and the per-market "
          "candidate cap still apply)")
    for o in outcomes:
        print(o.line())
    print()
    for line in summary(markets, outcomes):
        print(line)
    print(f"bytes read: {capped.bytes}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
