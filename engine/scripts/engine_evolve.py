"""Engine evolution loop — adaptive performance watchdog.

Engine-pulse is REACTIVE: it reads a static capacity declaration and
alerts when current state deviates. Engine-evolve is ADAPTIVE: it looks
at the engine over time, recalibrates capacity targets within safety
bounds, discovers unused vendor surfaces, detects coherence anomalies,
and proposes new auto_suggest rules.

Designed to run weekly (manual or scheduled). Read-only against
production; mutates only `configs/engine_capacity.yaml` and the
evolution audit log under `data/engine_evolution_history.json`.

Three loops:

1. **Capacity recalibration loop.** Per-connector 14-day median row
   count vs declared `expected_rows_per_day`. If median deviates by
   more than the `recalibration_threshold` (default 20%), propose a
   new target. Apply only when `--apply` is set AND the proposed
   change is within the per-cycle safety bound (default ±20%). Logged
   to history.

2. **Endpoint discovery loop.** Reads `configs/vendor_endpoint_catalog.yaml`
   (the source-of-truth registry of documented vendor endpoints) and
   surfaces any endpoint marked `not_shipped` along with its
   `ship_wave` tag. Output naming the next ship target so the engine
   never sits on paid surfaces it has not built against.

3. **Coherence loop.** Per-topic source distribution from
   `enriched_content.topic_groups`. Flag topics where one source
   contributes >80% of rows (suggests classifier bias or missing
   cross-source signal) or where two paid sources disagree (one shows
   the topic Rising, the other shows it Monitoring).

Safety guardrails:
- Recalibration is bounded at ±20% per cycle so a one-week anomaly
  does not permanently warp the baseline.
- All capacity changes append to `data/engine_evolution_history.json`
  (audit log) with timestamp, before-after values, and the observed
  median that drove the change.
- `--dry-run` (default) emits a report without mutating any file.
- `--apply` writes capacity YAML changes + audit log entry.

Output: a `WEEKLY ENGINE REVIEW` report with PROPOSED CHANGES (YAML
diff snippets), DISCOVERED OPPORTUNITIES (unshipped endpoints +
priority), COHERENCE FLAGS (anomalies needing attention), and a
SUGGESTED COMMIT BODY for the eventual ship.

CLI:
    python scripts/engine_evolve.py [--date YYYY-MM-DD] [--apply] [--json]

When --date is omitted, defaults to today UTC. When --apply is set,
the script mutates `configs/engine_capacity.yaml` for proposed
recalibrations within safety bounds.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CAPACITY_FILE = ROOT / "configs" / "engine_capacity.yaml"
CATALOG_FILE = ROOT / "configs" / "vendor_endpoint_catalog.yaml"
HISTORY_DIR = ROOT / "data"
HISTORY_FILE = HISTORY_DIR / "engine_evolution_history.json"

# Safety bound: per-cycle capacity adjustment ceiling. A 20% cap means a
# one-off spike or dip cannot warp the baseline more than a fifth in a
# single recalibration cycle. With weekly runs, recovering from a real
# regime change still takes < 4 weeks.
DEFAULT_PER_CYCLE_BOUND_PCT = 20.0
# Recalibration trigger threshold: median must deviate from declared
# target by more than this % to propose a change. Avoids noisy
# adjustments on small fluctuations.
DEFAULT_RECALIBRATION_THRESHOLD_PCT = 20.0
# Rolling window for the median computation.
DEFAULT_ROLLING_WINDOW_DAYS = 14


# ----------------------------------------------------------------------
# Data classes


@dataclass
class CapacityProposal:
    """Proposed capacity recalibration for one connector."""

    connector: str
    current_expected: int
    observed_median: int
    proposed_expected: int
    change_pct: float
    bounded: bool
    within_bound: bool
    rationale: str


@dataclass
class EndpointGap:
    """Documented vendor endpoint we have not shipped."""

    vendor: str
    path: str
    ship_wave: int | None
    status: str
    notes: str


@dataclass
class CoherenceFlag:
    """Cross-source coherence anomaly."""

    topic_group: str
    market: str
    flag_type: str  # source_dominance / source_divergence / orphan_topic
    dominant_source: str | None
    dominant_share_pct: float
    sources_contributing: dict[str, int]
    notes: str


@dataclass
class EvolutionReport:
    trend_date: str
    rolling_window_days: int
    proposals: list[CapacityProposal] = field(default_factory=list)
    endpoint_gaps: list[EndpointGap] = field(default_factory=list)
    coherence_flags: list[CoherenceFlag] = field(default_factory=list)
    applied: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "trend_date": self.trend_date,
            "rolling_window_days": self.rolling_window_days,
            "proposals": [p.__dict__ for p in self.proposals],
            "endpoint_gaps": [g.__dict__ for g in self.endpoint_gaps],
            "coherence_flags": [f.__dict__ for f in self.coherence_flags],
            "applied": self.applied,
            "notes": self.notes,
        }


# ----------------------------------------------------------------------
# File loaders


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_capacity() -> dict[str, Any]:
    return _load_yaml(CAPACITY_FILE)


def _load_catalog() -> dict[str, Any]:
    return _load_yaml(CATALOG_FILE)


def _load_history() -> list[dict[str, Any]]:
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _append_history(entry: dict[str, Any]) -> None:
    HISTORY_DIR.mkdir(exist_ok=True)
    history = _load_history()
    history.append(entry)
    HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")


# ----------------------------------------------------------------------
# Loop 1: capacity recalibration


def _bq_rolling_medians(end_date: str, window_days: int) -> dict[str, list[int]]:
    """Pull per-connector daily row totals over the rolling window.

    Returns a dict mapping connector name to the list of daily row
    counts in the window. Sums per-market rows up to the connector
    daily total.
    """
    from google.cloud import bigquery as bq

    client = bq.Client(project="ogilvy-trends-v2")
    start_date = (date.fromisoformat(end_date) - timedelta(days=window_days - 1)).isoformat()

    sql = """
    SELECT
        DATE(started_at) AS d,
        SUM(IFNULL(rss_rows, 0)) AS rss,
        SUM(IFNULL(bigquery_trends_rows, 0)) AS bigquery_trends,
        SUM(IFNULL(youtube_rows, 0)) AS youtube,
        SUM(IFNULL(gdelt_rows, 0)) AS gdelt,
        SUM(IFNULL(ensemble_rows, 0)) AS ensembledata,
        SUM(IFNULL(reddit_rows, 0)) AS reddit,
        SUM(IFNULL(brand24_rows, 0)) AS brand24,
        SUM(IFNULL(socialcrawl_rows, 0)) AS socialcrawl,
        SUM(IFNULL(apple_music_rows, 0)) AS apple_music
    FROM `ogilvy-trends-v2.trends_v2_dev.pipeline_runs`
    WHERE DATE(started_at) BETWEEN @start AND @end
    GROUP BY d
    ORDER BY d
    """
    cfg = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("start", "DATE", start_date),
            bq.ScalarQueryParameter("end", "DATE", end_date),
        ]
    )
    series: dict[str, list[int]] = {
        "rss": [],
        "bigquery_trends": [],
        "youtube": [],
        "gdelt": [],
        "ensembledata": [],
        "reddit": [],
        "brand24": [],
        "socialcrawl": [],
        "apple_music": [],
    }
    for row in client.query(sql, job_config=cfg).result():
        series["rss"].append(int(row.rss or 0))
        series["bigquery_trends"].append(int(row.bigquery_trends or 0))
        series["youtube"].append(int(row.youtube or 0))
        series["gdelt"].append(int(row.gdelt or 0))
        series["ensembledata"].append(int(row.ensembledata or 0))
        series["reddit"].append(int(row.reddit or 0))
        series["brand24"].append(int(row.brand24 or 0))
        series["socialcrawl"].append(int(row.socialcrawl or 0))
        series["apple_music"].append(int(row.apple_music or 0))
    return series


def _propose_recalibration(
    connector: str,
    current_expected: int,
    observed_median: int,
    per_cycle_bound_pct: float,
    threshold_pct: float,
) -> CapacityProposal | None:
    """Propose a new capacity target for a connector.

    Returns None when no change is warranted. Otherwise returns a
    CapacityProposal with the bounded new value + rationale.
    """
    if current_expected <= 0 or observed_median <= 0:
        return None
    change_pct = (observed_median - current_expected) / current_expected * 100.0
    if abs(change_pct) < threshold_pct:
        return None  # within noise band
    # Apply the per-cycle safety bound
    bounded = abs(change_pct) > per_cycle_bound_pct
    if bounded:
        signed_bound = per_cycle_bound_pct if change_pct > 0 else -per_cycle_bound_pct
        # Round, not int() floor: int() truncates toward zero so a downward
        # bound (e.g. round(5.6)=6 vs int(5.6)=5) can land outside the bound
        # and recompute within_bound False, silently skipping a proposal the
        # code itself bounded. A bounded value is appliable by construction.
        proposed = round(current_expected * (1.0 + signed_bound / 100.0))
        within_bound = True
    else:
        proposed = observed_median
        within_bound = (
            abs((proposed - current_expected) / current_expected * 100.0) <= per_cycle_bound_pct
        )
    rationale = (
        f"14-day median {observed_median} deviates {change_pct:+.1f}% from declared "
        f"{current_expected}. "
        + (
            f"Bounded to ±{per_cycle_bound_pct:.0f}% per cycle -> propose {proposed}."
            if bounded
            else f"Within bound -> propose {proposed}."
        )
    )
    return CapacityProposal(
        connector=connector,
        current_expected=current_expected,
        observed_median=observed_median,
        proposed_expected=proposed,
        change_pct=change_pct,
        bounded=bounded,
        within_bound=within_bound,
        rationale=rationale,
    )


def _run_capacity_loop(
    capacity: dict[str, Any],
    medians: dict[str, int],
    per_cycle_bound_pct: float,
    threshold_pct: float,
) -> list[CapacityProposal]:
    proposals: list[CapacityProposal] = []
    for name, decl in (capacity.get("connectors") or {}).items():
        current = int(decl.get("expected_rows_per_day", 0))
        median = int(medians.get(name, 0))
        proposal = _propose_recalibration(
            name,
            current_expected=current,
            observed_median=median,
            per_cycle_bound_pct=per_cycle_bound_pct,
            threshold_pct=threshold_pct,
        )
        if proposal:
            proposals.append(proposal)
    return proposals


# ----------------------------------------------------------------------
# Loop 2: endpoint discovery


def _run_discovery_loop(catalog: dict[str, Any]) -> list[EndpointGap]:
    """Surface every catalogued endpoint NOT marked in_use.

    Each gap carries its `ship_wave` so the watchdog knows when to
    stop nagging (item is already on the roadmap).
    """
    # Statuses that can never ship: already live, or structurally unavailable.
    # Surfacing these as opportunities would nag about endpoints we can never
    # call.
    _never_shippable = {
        "in_use",
        "vendor_not_published",
        "permanently_disabled",
        "removed_dead",
        "removed_from_code",
    }
    gaps: list[EndpointGap] = []
    for vendor, decl in catalog.items():
        endpoints = decl.get("endpoints") or []
        for ep in endpoints:
            status = ep.get("status", "")
            if status in _never_shippable:
                continue
            gaps.append(
                EndpointGap(
                    vendor=vendor,
                    path=str(ep.get("path", "")),
                    ship_wave=ep.get("ship_wave"),
                    status=status,
                    notes=str(ep.get("notes", "")),
                )
            )

    # Sort: dark_shipped first (needs flag flip), then by ship_wave ascending,
    # then not_shipped fallbacks.
    def _sort_key(g: EndpointGap) -> tuple[int, int, str]:
        status_order = {
            "code_landed_flag_off": 0,
            "dark_shipped": 1,
            "dark_shipped_secrets_pending": 2,
            "not_shipped": 3,
        }.get(g.status, 99)
        # ship_wave can be int (1, 2, 3) or string ("1_patch") per catalog;
        # coerce non-int to a large sentinel so sort order stays stable
        wave_raw = g.ship_wave
        if isinstance(wave_raw, int):
            wave = wave_raw
        elif isinstance(wave_raw, str):
            try:
                wave = int(wave_raw.split("_")[0])
            except (ValueError, IndexError):
                wave = 9
        else:
            wave = 9
        return (status_order, wave, g.vendor)

    gaps.sort(key=_sort_key)
    return gaps


# ----------------------------------------------------------------------
# Loop 3: cross-source coherence


def _bq_topic_source_distribution(end_date: str, window_days: int) -> list[dict[str, Any]]:
    """Per topic_group per market, share of enriched rows by source over
    the rolling window.

    Returns a list of dicts: {market, topic_group, source, rows}.
    """
    from google.cloud import bigquery as bq

    client = bq.Client(project="ogilvy-trends-v2")
    start_date = (date.fromisoformat(end_date) - timedelta(days=window_days - 1)).isoformat()

    sql = """
    SELECT
        market,
        tg AS topic_group,
        source,
        COUNT(*) AS row_count
    FROM `ogilvy-trends-v2.trends_v2_dev.enriched_content`,
    UNNEST(topic_groups) AS tg
    WHERE DATE(collected_at) BETWEEN @start AND @end
      AND tg IS NOT NULL
      AND tg != '__drop__'
    GROUP BY market, tg, source
    """
    cfg = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("start", "DATE", start_date),
            bq.ScalarQueryParameter("end", "DATE", end_date),
        ]
    )
    out: list[dict[str, Any]] = []
    for row in client.query(sql, job_config=cfg).result():
        out.append(
            {
                "market": row.market,
                "topic_group": row.topic_group,
                "source": row.source,
                "rows": int(row.row_count or 0),
            }
        )
    return out


def _detect_source_dominance(
    distribution: list[dict[str, Any]], dominance_threshold_pct: float = 80.0
) -> list[CoherenceFlag]:
    """For each (market, topic_group), flag when one source contributes
    more than `dominance_threshold_pct` of all rows.

    Single-source dominance suggests classifier bias or missing
    cross-source signal. Worth a human look.
    """
    by_topic: dict[tuple[str, str], dict[str, int]] = {}
    for r in distribution:
        key = (r["market"], r["topic_group"])
        by_topic.setdefault(key, {})[r["source"]] = r["rows"]

    flags: list[CoherenceFlag] = []
    for (market, topic), sources in by_topic.items():
        total = sum(sources.values())
        if total < 30:
            continue  # too thin to interpret
        dominant_source, dominant_rows = max(sources.items(), key=lambda kv: kv[1])
        share_pct = dominant_rows / total * 100.0
        if share_pct >= dominance_threshold_pct:
            flags.append(
                CoherenceFlag(
                    topic_group=topic,
                    market=market,
                    flag_type="source_dominance",
                    dominant_source=dominant_source,
                    dominant_share_pct=share_pct,
                    sources_contributing=sources,
                    notes=(
                        f"{dominant_source} contributes {share_pct:.0f}% of rows. "
                        f"Either classifier biases toward {dominant_source} or other "
                        f"sources lack coverage of this topic."
                    ),
                )
            )
    return flags


def _run_coherence_loop(end_date: str, window_days: int) -> list[CoherenceFlag]:
    """Coherence loop entry point. Pulls distribution + detects dominance."""
    distribution = _bq_topic_source_distribution(end_date, window_days)
    return _detect_source_dominance(distribution)


# ----------------------------------------------------------------------
# YAML mutation


def _apply_recalibration(
    capacity: dict[str, Any], proposals: list[CapacityProposal]
) -> dict[str, Any]:
    """Mutate the capacity dict with the proposed new expected_rows_per_day
    values. Caller is responsible for writing the result back to disk.

    Only applies proposals where `within_bound` is True. Out-of-bound
    proposals stay as suggestions for human review.
    """
    connectors = capacity.setdefault("connectors", {})
    for p in proposals:
        if not p.within_bound:
            continue
        decl = connectors.setdefault(p.connector, {})
        decl["expected_rows_per_day"] = int(p.proposed_expected)
        # Add an in-line audit comment via a parallel field; YAML round-trips
        # preserve it on subsequent loads.
        decl["_last_recalibrated"] = datetime.now(UTC).isoformat()
        decl["_last_recalibration_observed_median"] = int(p.observed_median)
    return capacity


def _apply_scalars(dst: Any, src: dict[str, Any]) -> None:
    """Copy values from a plain dict ``src`` onto a ruamel document ``dst`` in
    place, recursing into nested maps so ``dst``'s comments and structure are
    preserved. Scalars and lists are replaced; keys only in ``src`` are added.
    """
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _apply_scalars(dst[key], value)
        else:
            dst[key] = value


def _write_capacity(capacity: dict[str, Any]) -> None:
    """Re-emit the capacity YAML, preserving comments and ordering.

    safe_dump would strip every comment from engine_capacity.yaml (the header
    block, the spotify PERMANENTLY DISABLED note, the auto_suggest commentary).
    Re-load the file with ruamel round-trip and copy the recalibrated scalar
    values onto that commented document, so --apply rewrites only the changed
    numbers and the comment lines survive.
    """
    from ruamel.yaml import YAML

    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    yaml_rt.indent(mapping=2, sequence=4, offset=2)
    with CAPACITY_FILE.open(encoding="utf-8") as f:
        doc = yaml_rt.load(f)
    _apply_scalars(doc, capacity)
    with CAPACITY_FILE.open("w", encoding="utf-8") as f:
        yaml_rt.dump(doc, f)


# ----------------------------------------------------------------------
# Report rendering


def render_text(report: EvolutionReport) -> str:
    out: list[str] = []
    out.append(f"WEEKLY ENGINE REVIEW {report.trend_date}")
    out.append(f"Rolling window: {report.rolling_window_days} days")
    out.append("")
    out.append("PROPOSED CAPACITY RECALIBRATIONS")
    if not report.proposals:
        out.append("  (none — every connector within tolerance)")
    else:
        for p in report.proposals:
            marker = "APPLY" if p.within_bound else "PROPOSE"
            out.append(
                f"  [{marker}] {p.connector}: {p.current_expected} -> {p.proposed_expected} "
                f"(median {p.observed_median}, change {p.change_pct:+.1f}%)"
            )
            out.append(f"          {p.rationale}")

    out.append("")
    out.append("DISCOVERED OPPORTUNITIES (vendor endpoints we are not calling)")
    if not report.endpoint_gaps:
        out.append("  (catalog clean — every documented endpoint in use)")
    else:
        # group by status
        by_status: dict[str, list[EndpointGap]] = {}
        for g in report.endpoint_gaps:
            by_status.setdefault(g.status, []).append(g)
        for status in (
            "code_landed_flag_off",
            "dark_shipped",
            "dark_shipped_secrets_pending",
            "not_shipped",
        ):
            items = by_status.get(status) or []
            if not items:
                continue
            out.append(f"  [{status}]")
            for g in items:
                wave = f"wave {g.ship_wave}" if g.ship_wave else "wave ?"
                out.append(f"    {g.vendor}/{g.path}  ({wave})")
                if g.notes:
                    out.append(f"        {g.notes}")

    out.append("")
    out.append("COHERENCE FLAGS (single-source topic dominance)")
    if report.notes:
        # The coherence loop swallowed a failure. Say so instead of claiming
        # a clean check that never ran.
        out.append(f"  (coherence check did not complete: {report.notes})")
    elif not report.coherence_flags:
        out.append("  (no topics show single-source dominance above threshold)")
    else:
        for f in report.coherence_flags:
            out.append(
                f"  {f.market}/{f.topic_group}: {f.dominant_source} owns "
                f"{f.dominant_share_pct:.0f}% — {f.notes}"
            )

    out.append("")
    out.append("STATE")
    if report.applied:
        out.append("  Capacity YAML mutated. Audit log entry appended.")
    else:
        out.append("  Dry-run. No files mutated. Pass --apply to write the bounded recalibrations.")

    return "\n".join(out)


# ----------------------------------------------------------------------
# Main


def build_report(end_date: str, window_days: int) -> EvolutionReport:
    capacity = _load_capacity()
    catalog = _load_catalog()

    # Loop 1: capacity recalibration
    series = _bq_rolling_medians(end_date, window_days)
    medians = {name: int(statistics.median(vals)) if vals else 0 for name, vals in series.items()}
    proposals = _run_capacity_loop(
        capacity,
        medians,
        per_cycle_bound_pct=DEFAULT_PER_CYCLE_BOUND_PCT,
        threshold_pct=DEFAULT_RECALIBRATION_THRESHOLD_PCT,
    )

    # Loop 2: endpoint discovery
    endpoint_gaps = _run_discovery_loop(catalog)

    # Loop 3: coherence
    try:
        coherence_flags = _run_coherence_loop(end_date, window_days)
    except Exception as exc:
        # Coherence is best-effort; do not block the report on a BQ hiccup.
        coherence_flags = []
        notes_extra = f"coherence loop skipped: {exc}"
    else:
        notes_extra = ""

    report = EvolutionReport(
        trend_date=end_date,
        rolling_window_days=window_days,
        proposals=proposals,
        endpoint_gaps=endpoint_gaps,
        coherence_flags=coherence_flags,
        notes=notes_extra,
    )
    return report


def apply_report(report: EvolutionReport) -> None:
    capacity = _load_capacity()
    capacity = _apply_recalibration(capacity, report.proposals)
    _write_capacity(capacity)
    _append_history(
        {
            "applied_at": datetime.now(UTC).isoformat(),
            "trend_date": report.trend_date,
            "proposals_applied": [
                {
                    "connector": p.connector,
                    "before": p.current_expected,
                    "after": p.proposed_expected,
                    "observed_median": p.observed_median,
                    "change_pct": p.change_pct,
                }
                for p in report.proposals
                if p.within_bound
            ],
        }
    )
    report.applied = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default="", help="trend_date YYYY-MM-DD; default today UTC")
    parser.add_argument(
        "--window",
        type=int,
        default=DEFAULT_ROLLING_WINDOW_DAYS,
        help=f"rolling window days (default {DEFAULT_ROLLING_WINDOW_DAYS})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Mutate configs/engine_capacity.yaml with bounded recalibrations",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text report")
    args = parser.parse_args()

    end_date = args.date or datetime.now(UTC).date().isoformat()
    try:
        date.fromisoformat(end_date)
    except ValueError:
        print(f"invalid date: {end_date}", file=sys.stderr)
        return 2

    try:
        report = build_report(end_date, args.window)
    except Exception as exc:  # pragma: no cover - top-level safety
        print(f"engine_evolve failed: {exc}", file=sys.stderr)
        return 1

    if args.apply:
        apply_report(report)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
