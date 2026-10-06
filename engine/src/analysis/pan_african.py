"""Pan-African story detection: a standalone post-scoring stage.

Reads trend_scores across za/ng/ke for one trend_date, normalises each
query_group to a shared topic family (music_amapiano / music_afrobeats /
music_gengetone -> music), and finds families that are rising in two or
more markets at the same time. Those cross-market families are written to
pan_african_stories so the email + Pulse can surface "this is moving
across the continent, not just one market" stories.

Gated behind PAN_AFRICAN_ENABLED. When the flag is off the runner is a
no-op and writes nothing.

This wave the stage is deliberately standalone (invoked separately, not
wired into run_rss_now.py) so it does not collide with the connector
registry edit owned by another Wave 2 group. Wiring point for a later
wave: call run_pan_african_stage(trend_date) in
scripts/run_rss_now.py right after trend_scores is merged for the day
(after the merge_dataframe(..., "trend_scores", ...) call, around the
end of the per-market scoring loop), behind the same env gate.

Schema written matches the Wave 2 contract pan_african_stories table:
trend_date, story_id, story_label, markets ARRAY<STRING>,
topic_keys ARRAY<STRING>, total_item_count INT64,
momentum_composite FLOAT64.
"""

from __future__ import annotations

import datetime
import os
from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd

from src.utils.bigquery import get_client, get_dataset, merge_dataframe
from src.utils.log_redactor import get_logger

logger = get_logger(__name__)

# Markets the stage reads. Pan-African by definition needs more than one.
MARKETS = ("za", "ng", "ke")


# A query_group is named family_subtopic (e.g. music_amapiano,
# sports_football, tech_gemini_ai). The shared family is everything before
# the first underscore. This collapses music_amapiano / music_afrobeats /
# music_gengetone to "music" and sports_football (NG) + sports_football
# (KE) to "sports", which is exactly the cross-market grouping we want.
def normalise_family(query_group: str) -> str:
    """Collapse a query_group to its shared topic family.

    music_amapiano -> music, sports_football -> sports, tech_gemini_ai ->
    tech. A query_group with no underscore maps to itself. Empty / None
    maps to "other" so it never crashes the grouping.
    """
    if not query_group:
        return "other"
    head = str(query_group).strip().split("_", 1)[0]
    return head.lower() or "other"


# Human-friendly labels for the families we expect to travel. Anything not
# listed falls back to a title-cased family name, so a new family added to
# the taxonomy still renders without a code change.
_FAMILY_LABELS = {
    "music": "Music",
    "food": "Food and rituals",
    "sports": "Sport",
    "tech": "Tech and AI",
    "economy": "Economy and hustle",
    "fashion": "Fashion",
    "politics": "Politics",
    "genz": "Lifestyle and language",
    "finance": "Money and saving",
    "fintech": "Mobile money",
    "film": "Film",
    "culture": "Culture",
    "diaspora": "Diaspora",
    "infra": "Infrastructure",
    "education": "Education",
    "transport": "Transport",
}


def family_label(family: str) -> str:
    """Render a display label for a topic family."""
    return _FAMILY_LABELS.get(family, family.replace("_", " ").title())


def _flag_enabled() -> bool:
    return os.environ.get("PAN_AFRICAN_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass
class FamilyMarketSignal:
    """One market's contribution to a topic family on a trend_date."""

    market: str
    query_groups: list[str] = field(default_factory=list)
    item_count: int = 0
    max_velocity: float = 0.0


@dataclass
class PanAfricanStory:
    """A topic family rising across two or more markets."""

    trend_date: datetime.date
    family: str
    markets: list[str]
    topic_keys: list[str]
    total_item_count: int
    momentum_composite: float

    @property
    def story_id(self) -> str:
        # Stable per (date, family) so a same-day re-run MERGEs in place
        # instead of duplicating.
        return f"{self.trend_date.isoformat()}_{self.family}"

    @property
    def story_label(self) -> str:
        return family_label(self.family)

    def to_row(self) -> dict:
        return {
            "trend_date": self.trend_date,
            "story_id": self.story_id,
            "story_label": self.story_label,
            "markets": sorted(self.markets),
            "topic_keys": sorted(self.topic_keys),
            "total_item_count": int(self.total_item_count),
            "momentum_composite": float(self.momentum_composite),
        }


def detect_pan_african_stories(
    rows: list[dict],
    trend_date: datetime.date,
    rising_velocity_min: float = 0.05,
    min_markets: int = 2,
) -> list[PanAfricanStory]:
    """Pure detection: group trend_scores rows into cross-market stories.

    rows: dicts with at least market, query_group, velocity_score,
    item_count. A query_group counts as "rising in a market" when its
    velocity_score meets rising_velocity_min. A family becomes a story when
    rising query_groups span min_markets or more distinct markets.

    Returns one PanAfricanStory per qualifying family, sorted by
    momentum_composite descending then family name for a stable order.
    Pure function: no BigQuery, safe to unit-test on fixtures. Empty input
    returns an empty list.
    """
    # family -> market -> FamilyMarketSignal, built only from rising rows.
    families: dict[str, dict[str, FamilyMarketSignal]] = {}

    for row in rows:
        market = str(row.get("market") or "").strip().lower()
        query_group = str(row.get("query_group") or "").strip()
        if not market or not query_group:
            continue
        velocity = float(row.get("velocity_score") or 0.0)
        if velocity < rising_velocity_min:
            continue
        item_count = int(row.get("item_count") or 0)

        family = normalise_family(query_group)
        market_map = families.setdefault(family, {})
        signal = market_map.get(market)
        if signal is None:
            signal = FamilyMarketSignal(market=market)
            market_map[market] = signal
        signal.query_groups.append(query_group)
        signal.item_count += item_count
        signal.max_velocity = max(signal.max_velocity, velocity)

    stories: list[PanAfricanStory] = []
    for family, market_map in families.items():
        if len(market_map) < min_markets:
            continue
        markets = list(market_map.keys())
        topic_keys: list[str] = []
        total_item_count = 0
        # momentum_composite sums each contributing market's strongest
        # rising velocity. More markets and faster motion both raise it,
        # so the email/Pulse can rank stories by how broadly they travelled.
        momentum_composite = 0.0
        for signal in market_map.values():
            topic_keys.extend(signal.query_groups)
            total_item_count += signal.item_count
            momentum_composite += signal.max_velocity
        stories.append(
            PanAfricanStory(
                trend_date=trend_date,
                family=family,
                markets=markets,
                topic_keys=topic_keys,
                total_item_count=total_item_count,
                momentum_composite=momentum_composite,
            )
        )

    stories.sort(key=lambda s: (-s.momentum_composite, s.family))
    return stories


def _fetch_trend_scores(trend_date: datetime.date, *, client=None, dataset=None) -> list[dict]:
    """Read trend_scores for the date across the pan-African markets.

    run_query only binds scalar params, so the DATE + the markets ARRAY are
    bound on a direct client.query call here.
    """
    from google.cloud import bigquery

    sql = (
        "SELECT market, query_group, velocity_score, item_count "
        "FROM `{project}.{dataset}.trend_scores` "
        "WHERE trend_date = @trend_date AND LOWER(market) IN UNNEST(@markets)"
    )
    client = client if client is not None else get_client()
    dataset = dataset if dataset is not None else get_dataset()
    sql = sql.replace("{project}", client.project).replace("{dataset}", dataset)
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("trend_date", "DATE", trend_date),
                bigquery.ArrayQueryParameter("markets", "STRING", list(MARKETS)),
            ]
        ),
    )
    return job.to_dataframe().to_dict("records")


def read_pan_african_stories(trend_date: datetime.date, *, client=None, dataset=None) -> list[dict]:
    """Read the persisted pan-African stories for one date, for the email render.

    The pan-African stage writes stories to pan_african_stories during the run,
    but the email is composed later on a separate code path that does not hold
    the in-memory stories, so render_html reads them back here. Returns the dicts
    the renderer consumes (story_label, markets, total_item_count,
    momentum_composite). Empty list on any read error so the section degrades to
    nothing rather than breaking the email.
    """
    from google.cloud import bigquery

    sql = (
        "SELECT story_label, markets, total_item_count, momentum_composite "
        "FROM `{project}.{dataset}.pan_african_stories` "
        "WHERE trend_date = @trend_date "
        "ORDER BY momentum_composite DESC"
    )
    client = client if client is not None else get_client()
    dataset = dataset if dataset is not None else get_dataset()
    sql = sql.replace("{project}", client.project).replace("{dataset}", dataset)
    try:
        job = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("trend_date", "DATE", trend_date),
                ]
            ),
        )
        records = job.to_dataframe().to_dict("records")
        # markets is a BQ ARRAY<STRING>, which to_dataframe yields as a numpy
        # array; coerce to a plain list so the renderer (which expects a list)
        # never trips on numpy truthiness.
        for rec in records:
            mk = rec.get("markets")
            rec["markets"] = list(mk) if mk is not None else []
        return records
    except Exception:
        logger.exception("Pan-African story read failed for %s (non-fatal)", trend_date)
        return []


def run_pan_african_stage(
    trend_date: datetime.date,
    rising_velocity_min: float = 0.05,
    min_markets: int = 2,
    *,
    client=None,
    dataset=None,
    row_sink: Callable[[list[dict]], int] | None = None,
    enabled: bool | None = None,
) -> list[PanAfricanStory]:
    """Standalone post-scoring stage: detect + persist pan-African stories.

    No-op when PAN_AFRICAN_ENABLED is off (returns []). Reads trend_scores
    for the date, detects cross-market families, and MERGEs them into
    pan_african_stories keyed on (trend_date, story_id) so re-runs are
    idempotent. Empty / single-market days write nothing.
    """
    if enabled is not None and type(enabled) is not bool:
        raise ValueError("pan_african enabled must be a bool")
    if not (_flag_enabled() if enabled is None else enabled):
        logger.info("PAN_AFRICAN_ENABLED off; pan-African stage skipped")
        return []

    rows = _fetch_trend_scores(trend_date, client=client, dataset=dataset)
    stories = detect_pan_african_stories(
        rows,
        trend_date,
        rising_velocity_min=rising_velocity_min,
        min_markets=min_markets,
    )
    if not stories:
        logger.info("No pan-African stories for %s", trend_date)
        return []

    output = [s.to_row() for s in stories]
    if row_sink is None:
        merge_dataframe(
            pd.DataFrame(output), "pan_african_stories", merge_keys=["trend_date", "story_id"]
        )
    else:
        written = row_sink(output)
        if type(written) is not int or written != len(output):
            raise ValueError("pan_african row sink incomplete")
    logger.info("Wrote %d pan-African stories for %s", len(stories), trend_date)
    return stories
