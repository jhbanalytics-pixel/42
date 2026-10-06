"""SocialCrawl staged-rollout proposer. Read-only.

Evaluates the ladder in configs/socialcrawl_rollout.yaml against the live credit
balance of the funded account and the last fortnight of that account's ledger,
and prints at most ONE proposed stage. It never writes configs/sources.yaml and never calls a charging endpoint;
the only network call is GET /v1/credits/balance, which the vendor bills at zero.

A stage that clears the budget gates here is then put to
``source_policy.evaluate_route_activation``, which is the activation gate for the
whole estate: the request is built by ``source_inventory.route_activation_request``
so the credits, the kind of increment, the shipped capability and the pending
quality increments are derived from the ladder and the connector rather than
asserted by this script, and a stage the gate refuses is reported with its reasons
instead of being proposed. No probe has been retained for any route yet, so the
gate refuses every stage today; a retained probe in the ladder file is what lets
the ramp move again.

The policy it enforces (decided 24 Jul 2026): the credits we hold are all we get,
so the allowed daily burn is derived from the balance rather than picked:

    allowed_daily = balance / min_runway_days

Headroom is that number minus the current 7-day median burn. A stage is only
proposed when it fits in the headroom AND every gate passes. As the balance
falls the headroom closes and the ramp stops on its own.

Balance and burn describe one account. The balance is read with the funded
secret and the burn and clean runs from the funded ledger, never from
pipeline_runs, which records the jhb_core cron spending the other key. The
ladder declares the lane its stages are for (policy.credential_lane), and a
ladder on any lane other than the one that spends the funded account is held
with a named reason before any number is derived from that balance.

Usage:
    python scripts/socialcrawl_rollout.py [--date YYYY-MM-DD] [--json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analysis.open_intelligence.source_inventory import (
    activation_policy,
    ladder_increments,
    route_activation_request,
    route_evidence,
)
from src.analysis.open_intelligence.source_policy import (
    POLICY_MARKETS,
    STATE_ORDER,
    RouteProbe,
    evaluate_route_activation,
)
from src.ingestion.connectors.socialcrawl import SocialCrawlConnector
from src.utils.secrets import get_secret

ROLLOUT_FILE = ROOT / "configs" / "socialcrawl_rollout.yaml"
SOURCES_FILE = ROOT / "configs" / "sources.yaml"
BALANCE_URL = "https://www.socialcrawl.dev/v1/credits/balance"
# The funded account holds the credits the burn and runway gates divide; the
# account behind SOCIALCRAWL_API_KEY holds none, so reading it made every gate
# see a balance of zero. This is the secret the funded lane itself reads
# (funded_lane_runtime.SECRET_ID), resolved through the repo's secret reader.
BALANCE_SECRET_ID = "SOCIALCRAWL_OGILVY_API_KEY"
# The lane that spends the account behind BALANCE_SECRET_ID, and the ledger that
# lane writes. Pinned here, never read from the ladder file: the ladder only
# says which lane its stages are for, and is held unless that is this lane.
BALANCE_CREDENTIAL_LANE = "ogilvy_funded"
CREDENTIAL_LANES = frozenset({"jhb_core", BALANCE_CREDENTIAL_LANE})
PROJECT = "ogilvy-trends-v2"
LEDGER_DATASET = "trends_v2_staging_funded"
LEDGER_TABLE = "socialcrawl_credit_ledger_v1"


@dataclass
class Gate:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class FundedGateReading:
    """This script's own funding reading, in the shape the activation gate validates.

    The activation gate refuses anything that is not a readable gate, so the
    balance is a counted credit reading or None and the allowance is the headroom
    this run measured. Nothing here is a default standing in for an unread number.
    """

    allowed: bool
    reasons: tuple[str, ...]
    current_balance: int | None
    run_allowance: int


@dataclass
class Proposal:
    trend_date: str
    balance: int | None = None
    allowed_daily: int | None = None
    burn_median: int | None = None
    burn_source: str = "declared"
    declared_burn: int | None = None
    headroom: int | None = None
    runway_days: float | None = None
    stage_id: str | None = None
    stage_description: str | None = None
    config_delta: dict[str, Any] = field(default_factory=dict)
    gates: list[Gate] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    activation_refusals: list[str] = field(default_factory=list)
    verdict: str = "NO_PROPOSAL"

    def to_dict(self) -> dict[str, Any]:
        return {
            "trend_date": self.trend_date,
            "verdict": self.verdict,
            "balance": self.balance,
            "allowed_daily": self.allowed_daily,
            "burn_median": self.burn_median,
            "burn_source": self.burn_source,
            "declared_burn": self.declared_burn,
            "headroom": self.headroom,
            "runway_days": self.runway_days,
            "stage_id": self.stage_id,
            "stage_description": self.stage_description,
            "config_delta": self.config_delta,
            "gates": [g.__dict__ for g in self.gates],
            "skipped": self.skipped,
            "activation_refusals": self.activation_refusals,
        }


def load_rollout() -> dict[str, Any]:
    return yaml.safe_load(ROLLOUT_FILE.read_text(encoding="utf-8")) or {}


def load_sources() -> dict[str, Any]:
    return yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8")) or {}


def retained_probes(cfg: dict[str, Any]) -> dict[str, RouteProbe]:
    """The retained response shape probes the ladder file carries, one per vendor route.

    A probe that cannot be read is not a missing probe: it is recorded as
    unreadable so the refusal says which, rather than reading as "no probe yet".
    """
    probes: dict[str, RouteProbe] = {}
    declared = cfg.get("retained_probes") or {}
    for route, fields in declared.items():
        if not isinstance(fields, dict):
            raise ValueError(f"retained_probe_unreadable:{route}")
        probes[str(route)] = RouteProbe(route=str(route), **fields)
    return probes


def fetch_balance(timeout: int = 30) -> int | None:
    """GET /v1/credits/balance on the funded account. Zero credits.

    None when the funded secret is absent or malformed, or the call fails. A
    key with surrounding whitespace is refused rather than trimmed, as the
    funded lane's own balance read refuses it, so this script cannot report a
    balance the funded run would then fail to read.
    """
    key = get_secret(BALANCE_SECRET_ID)
    if not isinstance(key, str) or not key or key.strip() != key:
        return None
    try:
        resp = requests.get(BALANCE_URL, headers={"x-api-key": key}, timeout=timeout)
        payload = resp.json()
    except (requests.RequestException, ValueError):
        return None
    if not payload.get("success"):
        return None
    data = payload.get("data") or {}
    try:
        return int(data.get("balance"))
    except (TypeError, ValueError):
        return None


def fetch_history(end_date: str, days: int = 14) -> list[dict[str, Any]]:
    """Per-day debit and run closes on the funded ledger. Empty list when unreachable.

    One row per trend date. ``credits`` is the ledger's conservative breaker
    debit (phase closes and attribution gaps), rounded up so a runway is never
    divided by an understated burn. ``ok`` counts executions that closed their
    run exactly once as complete and ``bad`` every other execution that day. The
    ledger records credits and closes, not ingested rows, so ``rows`` is 0 and
    the clean run window reads the closes.
    """
    try:
        from google.cloud import bigquery as bq
    except ImportError:
        return []
    start = (date.fromisoformat(end_date) - timedelta(days=days - 1)).isoformat()
    sql = f"""
    WITH per_execution AS (
        SELECT
            trend_date AS d,
            execution_id,
            SUM(IF(event_type IN ('phase_close', 'attribution_gap'), budget_debit_credits, 0))
                AS credits,
            COUNTIF(event_type = 'run_close' AND attribution_state = 'complete')
                AS complete_closes,
            COUNTIF(event_type = 'run_close') AS run_closes
        FROM `{PROJECT}.{LEDGER_DATASET}.{LEDGER_TABLE}`
        WHERE credential_lane = @credential_lane
            AND trend_date BETWEEN @start AND @end
        GROUP BY d, execution_id
    )
    SELECT
        d,
        SUM(credits) AS credit_total,
        COUNTIF(complete_closes = 1 AND run_closes = 1) AS ok,
        COUNTIF(NOT (complete_closes = 1 AND run_closes = 1)) AS bad
    FROM per_execution
    GROUP BY d
    ORDER BY d
    """
    cfg = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("credential_lane", "STRING", BALANCE_CREDENTIAL_LANE),
            bq.ScalarQueryParameter("start", "DATE", start),
            bq.ScalarQueryParameter("end", "DATE", end_date),
        ]
    )
    try:
        client = bq.Client(project=PROJECT, location="US")
        return [
            {
                "d": str(r.d),
                "rows": 0,
                "credits": math.ceil(r.credit_total or 0),
                "ok": int(r.ok or 0),
                "bad": int(r.bad or 0),
            }
            for r in client.query(sql, job_config=cfg).result()
        ]
    except Exception as exc:
        # A silent [] here reads downstream as "zero clean runs" and holds the
        # ramp forever, so say why rather than swallowing it.
        print(f"  WARN  funded ledger unreadable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return []


def _measured_burn(history: list[dict[str, Any]], min_days: int = 3, window: int = 7) -> int | None:
    """Median daily credit spend over the days that actually recorded it.

    A day that predates the socialcrawl_credits column reports 0, and a 0 there
    is an UNMEASURED day, not a free one. Counting those zeros would drag the
    median down and hand the ramp headroom it has not got, so they are dropped
    rather than treated as data. Returns None until `min_days` real readings
    exist, and the caller falls back to the declared target for that window.
    """
    spends = [int(h.get("credits") or 0) for h in history]
    real = [s for s in spends if s > 0]
    if len(real) < min_days:
        return None
    return _median(real[-window:])


def _median(values: list[int]) -> int:
    if not values:
        return 0
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) // 2


def current_stage_index(sources: dict[str, Any], stages: list[dict[str, Any]]) -> int:
    """How far up the ladder the live config already sits.

    A stage counts as applied when every key in its config_delta is present in
    sources.yaml with at least the declared value. Wildcard deltas are treated
    as not-applied, since they cannot be compared numerically.
    """
    applied = 0
    for i, stage in enumerate(stages):
        delta = stage.get("config_delta") or {}
        if not delta or any("*" in k for k in delta):
            break
        if all(_delta_met(sources, k, v) for k, v in delta.items()):
            applied = i + 1
        else:
            break
    return applied


def _delta_met(sources: dict[str, Any], dotted: str, want: Any) -> bool:
    node: Any = sources
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    if isinstance(want, bool):
        return bool(node) == want
    if isinstance(want, (int, float)) and isinstance(node, (int, float)):
        return node >= want
    return node == want


def evaluate(target_date: str) -> Proposal:
    cfg = load_rollout()
    policy = cfg.get("policy") or {}
    caps = cfg.get("capabilities") or {}
    stages = cfg.get("stages") or []
    sources = load_sources()

    p = Proposal(trend_date=target_date)
    p.balance = fetch_balance()
    history = fetch_history(target_date)

    min_runway = int(policy.get("min_runway_days", 45))
    hard_floor = int(policy.get("balance_floor_hard", 5000))
    clean_needed = int(policy.get("clean_crons_required", 3))
    overrun_allowed = int(policy.get("burn_overrun_pct", 25))

    # Burn is MEASURED from pipeline_runs.socialcrawl_credits once enough days
    # carry it, and falls back to the declared target only to bootstrap. The
    # declared number is a guess that does not move when the caps do: on
    # 27 Jul 2026 it read 290 against ~346 actually billed, which overstated
    # runway by a fifth and headroom by more than double.
    declared_burn = int(
        ((sources.get("socialcrawl") or {}).get("actual_units_target"))
        or _declared_burn_from_capacity()
        or 290
    )
    p.declared_burn = declared_burn
    measured_burn = _measured_burn(history)
    if measured_burn:
        p.burn_median, p.burn_source = measured_burn, "measured"
    else:
        p.burn_median, p.burn_source = declared_burn, "declared"

    if p.balance is None:
        p.gates.append(
            Gate(
                "balance_readable",
                False,
                f"{BALANCE_SECRET_ID} unavailable or balance call failed",
            )
        )
        p.verdict = "UNKNOWN"
        return p

    # The ladder's stages are spent by the lane it is declared for. A lane that
    # cannot spend the funded account gets no runway, allowance or headroom out
    # of that account's balance, and no stage: held, with the reason named.
    ladder_lane = policy.get("credential_lane")
    if ladder_lane not in CREDENTIAL_LANES:
        p.gates.append(
            Gate(
                "ladder_lane_spends_balance_account",
                False,
                f"ladder policy.credential_lane {ladder_lane!r} is not a known lane; "
                f"the balance is the {BALANCE_CREDENTIAL_LANE} account ({BALANCE_SECRET_ID})",
            )
        )
        p.verdict = "HOLD"
        return p
    if ladder_lane != BALANCE_CREDENTIAL_LANE:
        p.gates.append(
            Gate(
                "ladder_lane_spends_balance_account",
                False,
                f"ladder stages are for the {ladder_lane} lane, which cannot spend the "
                f"{BALANCE_CREDENTIAL_LANE} account ({BALANCE_SECRET_ID}) this balance "
                "and ledger describe",
            )
        )
        p.verdict = "HOLD"
        return p
    p.gates.append(
        Gate(
            "ladder_lane_spends_balance_account",
            True,
            f"ladder lane {ladder_lane} spends {BALANCE_SECRET_ID}",
        )
    )

    burn = p.burn_median or declared_burn
    p.runway_days = round(p.balance / burn, 1) if burn else None
    p.allowed_daily = int(p.balance / min_runway)
    p.headroom = p.allowed_daily - burn

    p.gates.append(
        Gate(
            "balance_above_hard_floor",
            p.balance >= hard_floor,
            f"{p.balance} vs floor {hard_floor}",
        )
    )
    p.gates.append(
        Gate(
            "runway_above_min",
            bool(p.runway_days and p.runway_days >= min_runway),
            f"{p.runway_days}d vs floor {min_runway}d",
        )
    )

    # Declared in the policy since day one and never enforced, because there was
    # no measured burn to compare against. Real spend running well over the model
    # means the last stage cost more than it said it would, so stop climbing.
    overrun_pct = 0
    if p.burn_source == "measured" and declared_burn:
        overrun_pct = round((p.burn_median - declared_burn) / declared_burn * 100)
        p.gates.append(
            Gate(
                "burn_within_model",
                overrun_pct <= overrun_allowed,
                f"measured {p.burn_median}/day vs modelled {declared_burn}/day "
                f"({overrun_pct:+d}%, allowed +{overrun_allowed}%)",
            )
        )

    # A day on which every run failed carries no rows and no successes, and
    # dropping it made the days either side of it read as consecutive. Any day
    # that ran at all stays in the window, so a total failure day is a day the
    # window sees and the clean run gate refuses.
    recent = [h for h in history if h["rows"] > 0 or h["ok"] > 0 or h["bad"] > 0][-clean_needed:]
    clean = len(recent) >= clean_needed and all(h["bad"] == 0 and h["ok"] > 0 for h in recent)
    p.gates.append(
        Gate(
            "consecutive_clean_crons",
            clean,
            f"{len(recent)} of {clean_needed} clean runs in window",
        )
    )

    if not all(g.passed for g in p.gates):
        p.verdict = "HOLD"
        return p

    applied = current_stage_index(sources, stages)
    activated = tuple(str(stage["id"]) for stage in stages[:applied])
    ladder = ladder_increments(cfg)
    increments = {item.stage_id: item for item in ladder}
    policy = activation_policy(cfg)
    probes = retained_probes(cfg)
    # The named runs this window actually carried, not a count of them: three
    # clean runs and one run counted three times are not the same evidence.
    clean_run_ids = tuple(str(h["d"]) for h in recent)
    route_states = _socialcrawl_route_states(sources)
    gate = FundedGateReading(
        allowed=all(g.passed for g in p.gates),
        reasons=tuple(g.name for g in p.gates if not g.passed),
        current_balance=p.balance,
        run_allowance=max(0, p.headroom or 0),
    )
    for stage in stages[applied:]:
        need = stage.get("requires_capability")
        if need and not caps.get(need, False):
            p.skipped.append(f"{stage['id']}: needs connector capability '{need}', not shipped")
            continue
        cost = int(stage.get("credits_per_day", 0))
        if cost > (p.headroom or 0):
            p.skipped.append(f"{stage['id']}: costs {cost}/day, headroom is {p.headroom}/day")
            continue
        projected_runway = p.balance / (declared_burn + cost) if (declared_burn + cost) else 0
        if projected_runway < min_runway:
            p.skipped.append(
                f"{stage['id']}: would drop runway to {projected_runway:.0f}d, floor is {min_runway}d"
            )
            continue
        refusal = activation_refusal(
            increments.get(str(stage["id"])),
            ladder=ladder,
            policy=policy,
            probes=probes,
            gate=gate,
            clean_run_ids=clean_run_ids,
            burn_overrun_pct=max(0, overrun_pct),
            increments_open=0 if clean else 1,
            route_states=route_states,
            activated=activated,
        )
        if refusal:
            p.skipped.append(f"{stage['id']}: {refusal}")
            p.activation_refusals.append(f"{stage['id']}: {refusal}")
            continue
        p.stage_id = stage["id"]
        p.stage_description = stage.get("description", "")
        p.config_delta = stage.get("config_delta") or {}
        p.verdict = "PROPOSE"
        return p

    p.verdict = "NO_PROPOSAL"
    return p


def _socialcrawl_route_states(sources: dict[str, Any]) -> dict[str, dict[str, str]]:
    """The state the estate gives each socialcrawl ROUTE, per market.

    The source level state is not an answer about a route: it is read from the
    source switch alone and says nothing about a route the configuration switched
    off underneath it. What the gate is asked is whether one route may be opened,
    so it is given route states.
    """
    records = route_evidence(sources, connectors=(("socialcrawl", SocialCrawlConnector),))
    states: dict[str, dict[str, str]] = {market: {} for market in POLICY_MARKETS}
    for (_source, market, route), record in records.items():
        states[market][route] = str(record["state"])
    return states


def _route_state_for(states: dict[str, str], route: str) -> str:
    """The state to put to the gate for the vendor route an increment opens.

    When the estate models the route by that name, the route's own state is the
    answer. It does not today: the ladder names SocialCrawl's vendor endpoints
    (`tiktok/search/top`) and the estate models the connector's phases and its
    Wave 1 routes, two vocabularies with no name in common. Rather than pass the
    source's state and call it the route's, the request carries the weakest state
    the estate gives any route of that source and market, because a route cannot
    be opened on a stronger claim than the estate makes about the source it
    belongs to.
    """
    if route in states:
        return states[route]
    if not states:
        raise ValueError(f"socialcrawl_route_states_missing:{route}")
    return min(states.values(), key=STATE_ORDER.index)


def activation_refusal(
    increment,
    *,
    ladder,
    policy,
    probes: dict[str, RouteProbe],
    gate: FundedGateReading,
    clean_run_ids: tuple[str, ...],
    burn_overrun_pct: int,
    increments_open: int,
    route_states: dict[str, dict[str, str]],
    activated: tuple[str, ...],
) -> str:
    """Why the activation gate refuses this increment, or an empty string when it does not.

    Every market the increment would open is put to the gate, because a config
    delta on the shared caps opens the route in all three.
    """
    if increment is None:
        return "is not on the activation ladder, so the activation gate cannot read it"
    refusals = []
    for market in POLICY_MARKETS:
        request = route_activation_request(
            increment,
            market=market,
            route_state=_route_state_for(route_states[market], increment.activation_route or ""),
            clean_run_ids=clean_run_ids,
            burn_overrun_pct=burn_overrun_pct,
            increments_open=increments_open,
            probe=probes.get(increment.activation_route or ""),
            funded_gate=gate,
            activated=activated,
            increments=ladder,
            policy=policy,
        )
        if request is None:
            return (
                "names no vendor route in the ladder, so no retained probe can be bound "
                "to it and the activation gate refuses it"
            )
        decision = evaluate_route_activation(request)
        if not decision.activate:
            refusals.append(f"{market} {', '.join(decision.reasons)}")
    return "activation gate refused: " + "; ".join(refusals) if refusals else ""


def _declared_burn_from_capacity() -> int | None:
    path = ROOT / "configs" / "engine_capacity.yaml"
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sc = (data.get("connectors") or {}).get("socialcrawl") or {}
    try:
        return int(sc.get("actual_units_target"))
    except (TypeError, ValueError):
        return None


def render(p: Proposal) -> str:
    out = [f"SOCIALCRAWL ROLLOUT  {p.trend_date}", ""]
    out.append(
        f"  balance {p.balance}  burn {p.burn_median}/day ({p.burn_source})  "
        f"runway {p.runway_days}d  allowed {p.allowed_daily}/day  headroom {p.headroom}/day"
    )
    if p.burn_source == "declared":
        out.append(
            "  burn is the declared target, not a reading. Needs 3 days of "
            "funded ledger debit before it is measured."
        )
    out.append("")
    out.append("  GATES")
    for g in p.gates:
        out.append(f"    {'PASS' if g.passed else 'FAIL'}  {g.name:26} {g.detail}")
    if p.skipped:
        out.append("")
        out.append("  SKIPPED")
        for skipped in p.skipped:
            out.append(f"    - {skipped}")
    if p.activation_refusals:
        out.append("")
        out.append("  ACTIVATION GATE")
        out.append("    Every proposal is put to evaluate_route_activation; these were refused.")
    out.append("")
    if p.verdict == "PROPOSE":
        out.append(f"  PROPOSE  {p.stage_id}")
        out.append(f"    {p.stage_description}")
        for k, v in p.config_delta.items():
            out.append(f"    {k}: {v}")
        out.append("    Apply by hand in configs/sources.yaml. This script never writes it.")
    elif p.verdict == "HOLD":
        out.append("  HOLD  a gate failed, ramp paused")
    elif p.verdict == "UNKNOWN":
        out.append("  UNKNOWN  could not read the credit balance")
    else:
        out.append("  NO PROPOSAL  ladder exhausted or every remaining stage is out of headroom")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    p = evaluate(args.date)
    print(json.dumps(p.to_dict(), indent=2) if args.json else render(p))
    return 0


if __name__ == "__main__":
    sys.exit(main())
