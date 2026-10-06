"""Unit tests for the Wave 2 tone-split lexicon producer bridge.

``src/analysis/tone_lexicon.py`` rolls the per-row sentiment_lexicon_score on
enriched_content up to a per-topic list and tags it onto the brief dicts as
``sentiment_lexicon_scores``, so the PULSE tone-split section can bin them. Tests
pin the BQ read shape, the tagger key-shape contract, and the non-fatal path.
"""

import datetime
import re
from unittest import mock

from src.analysis import tone_lexicon
from src.analysis.tone_lexicon import (
    fetch_lexicon_scores,
    tag_briefs_with_lexicon_scores,
)


def _row(market, query_group, scores):
    r = mock.Mock()
    r.market = market
    r.query_group = query_group
    r.scores = scores
    return r


def _fake_client(rows):
    fake_job = mock.Mock()
    fake_job.result.return_value = rows
    fake_client = mock.Mock()
    fake_client.project = "p"
    fake_client.query.return_value = fake_job
    return fake_client


def test_fetch_non_fatal_on_bq_error():
    """A client/BQ failure yields {} (never raises), so the cron is never blocked."""
    with mock.patch("src.analysis.tone_lexicon.get_client", side_effect=RuntimeError("bq down")):
        out = fetch_lexicon_scores(datetime.date(2026, 6, 21))
    assert out == {}


def test_fetch_happy_path_keys_and_lists():
    rows = [
        _row("za", "music_amapiano", [0.4, -0.2, 0.1]),
        _row("ng", "music_afrobeats", [0.0]),
    ]
    client = _fake_client(rows)
    with (
        mock.patch("src.analysis.tone_lexicon.get_client", return_value=client),
        mock.patch("src.analysis.tone_lexicon.get_dataset", return_value="ds"),
    ):
        out = fetch_lexicon_scores(datetime.date(2026, 6, 21))
    assert out[("za", "music_amapiano")] == [0.4, -0.2, 0.1]
    assert out[("ng", "music_afrobeats")] == [0.0]


def test_fetch_sql_orders_sample_deterministically():
    """The ARRAY_AGG sample is ordered by a stable hash before the LIMIT.

    Without an ORDER BY, BigQuery returns an arbitrary storage-ordered subset of
    the first 2000 scores, which clusters by source and ingest batch and changes
    across re-runs with no data change. Ordering by FARM_FINGERPRINT of the score
    makes the sample a stable, unbiased pseudo-random subset. This pins that the
    SQL carries that ORDER BY inside the ARRAY_AGG, so the sample is reproducible.
    """
    client = _fake_client([_row("za", "music_amapiano", [0.1])])
    with (
        mock.patch("src.analysis.tone_lexicon.get_client", return_value=client),
        mock.patch("src.analysis.tone_lexicon.get_dataset", return_value="ds"),
    ):
        fetch_lexicon_scores(datetime.date(2026, 6, 21))
    sql = client.query.call_args[0][0]
    normalised = re.sub(r"\s+", " ", sql).upper()
    # The ORDER BY must sit inside the ARRAY_AGG, before the LIMIT, so it controls
    # which rows survive the truncation, not just the order of the returned list.
    assert "ARRAY_AGG(" in normalised
    assert "FARM_FINGERPRINT(" in normalised
    agg = normalised[normalised.index("ARRAY_AGG(") :]
    agg = agg[: agg.index(") AS SCORES")]
    assert "ORDER BY FARM_FINGERPRINT(" in agg
    assert agg.index("ORDER BY") < agg.rindex("LIMIT")


def test_fetch_omits_empty_score_lists():
    """A topic that aggregated to an empty list is dropped, not tagged with []."""
    client = _fake_client([_row("za", "genz_sheng", [])])
    with (
        mock.patch("src.analysis.tone_lexicon.get_client", return_value=client),
        mock.patch("src.analysis.tone_lexicon.get_dataset", return_value="ds"),
    ):
        out = fetch_lexicon_scores(datetime.date(2026, 6, 21))
    assert out == {}


def test_tag_matches_and_misses():
    briefs = {
        ("za", "music_amapiano"): {"topic": "music_amapiano"},
        ("ng", "music_afrobeats"): {"topic": "music_afrobeats"},
    }
    lookup = {("za", "music_amapiano"): [0.5, -0.1]}
    tagged = tag_briefs_with_lexicon_scores(briefs, lookup)
    assert tagged == 1
    assert briefs[("za", "music_amapiano")]["sentiment_lexicon_scores"] == [0.5, -0.1]
    assert "sentiment_lexicon_scores" not in briefs[("ng", "music_afrobeats")]


def test_tag_empty_list_leaves_brief_untouched():
    briefs = {("za", "music_amapiano"): {"topic": "music_amapiano"}}
    tagged = tag_briefs_with_lexicon_scores(briefs, {("za", "music_amapiano"): []})
    assert tagged == 0
    assert "sentiment_lexicon_scores" not in briefs[("za", "music_amapiano")]
