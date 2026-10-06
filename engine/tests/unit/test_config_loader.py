"""Unit tests for config_loader utilities."""

from pathlib import Path

import pytest
import yaml
from src.utils.config_loader import (
    CONFIGS_DIR,
    get_active_markets,
    load_alerts,
    load_entity_aliases,
    load_market_creators,
    load_market_keywords,
    load_scoring,
    load_sources,
    load_trend_cycles,
    load_yaml,
)


def test_load_sources_returns_dict():
    """load_sources parses configs/sources.yaml with expected top-level keys."""
    sources = load_sources()
    assert isinstance(sources, dict)
    assert "rss_feeds" in sources
    assert "youtube_queries" in sources
    assert "gdelt_queries" in sources
    assert "ensembledata" in sources


def test_load_sources_rss_has_za_market():
    """The rss_feeds block contains a list of feeds for the za market."""
    sources = load_sources()
    rss = sources["rss_feeds"]
    assert "za" in rss
    assert isinstance(rss["za"], list)
    assert len(rss["za"]) > 0
    assert "url" in rss["za"][0]
    assert "name" in rss["za"][0]


def test_load_scoring_weights_sum_to_one():
    """Composite trend scoring weights sum to 1.0 within 0.001.

    Momentum, when present, is carved FROM the velocity weight in code (not added
    on top), so it is excluded from the bundle sum and must not exceed velocity.
    """
    scoring = load_scoring()
    weights = scoring["weights"]
    total = sum(v for k, v in weights.items() if k != "momentum")
    assert abs(total - 1.0) < 0.001, f"weights (ex-momentum) sum to {total}, expected 1.0"
    assert float(weights.get("momentum", 0.0)) <= float(weights.get("velocity", 0.0))


def test_load_scoring_has_thresholds():
    """Scoring config exposes trending, emerging, and monitoring thresholds."""
    scoring = load_scoring()
    thresholds = scoring["thresholds"]
    assert "trending" in thresholds
    assert "emerging" in thresholds
    assert "monitoring" in thresholds


def test_load_scoring_thresholds_are_ordered_and_in_range():
    """Sanity guard: monotonically decreasing, all in (0, 1).

    Prevents accidental yaml edits that would flip the ordering or
    push a floor outside the composite-score range.
    """
    thresholds = load_scoring()["thresholds"]
    trending = float(thresholds["trending"])
    emerging = float(thresholds["emerging"])
    monitoring = float(thresholds["monitoring"])
    assert 0.0 < monitoring < emerging < trending < 1.0


def test_load_scoring_thresholds_match_28_may_recalibration():
    """Anchored on the 28 May 2026 recalibration to observed distribution.

    Trending 0.45 = p90, Emerging 0.30 = p75, Monitoring 0.18 = p50
    of nonzero rows. If yaml drifts, this test fires so the in-code
    fallbacks in src/alerts/email_digest.score_to_tier and the
    src/analysis/prompts/trend_brief status_tag rule get reviewed
    together.
    """
    thresholds = load_scoring()["thresholds"]
    assert float(thresholds["trending"]) == 0.45
    assert float(thresholds["emerging"]) == 0.30
    assert float(thresholds["monitoring"]) == 0.18


def test_load_sources_gdelt_days_spans_full_prior_partition():
    """gdelt.days must be >= 2 for the early 00:30 UTC primary slot.

    gdelt_gkg.sql filters _PARTITIONTIME >= CURRENT_TIMESTAMP() - INTERVAL
    @days DAY against a DAY-partitioned table. _PARTITIONTIME is day-truncated,
    so at the 00:30 fire a 1-day window resolves to only the ~33 min current-day
    partition (the 3 Jun run got gdelt 16/67/43 vs 169/479/303 at the old 06:30
    slot). days >= 2 spans the full prior-day partition. If this drifts back to
    1, GDELT silently starves on the early slot, so this test fires.
    """
    gdelt = load_sources()["gdelt"]
    assert int(gdelt["days"]) >= 2


def test_load_market_keywords_za():
    """Parses ZA keywords with expected top-level keys."""
    config = load_market_keywords("za")
    assert config["market"] == "za"
    assert "topic_groups" in config
    assert "regional_markers" in config
    assert "genz_markers" in config
    assert "slang" in config


def test_load_market_keywords_missing_market_raises():
    """A market with no keyword file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_market_keywords("zz")


def test_load_market_creators_za():
    """Parses ZA creators config with tiered watchlists."""
    config = load_market_creators("za")
    assert config["market"] == "za"
    assert "watchlists" in config
    assert "tier_weights" in config


def test_load_market_creators_missing_market_raises():
    """A market with no creators file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_market_creators("zz")


def test_get_active_markets_returns_list():
    """get_active_markets returns a list containing at least za."""
    markets = get_active_markets()
    assert isinstance(markets, list)
    assert "za" in markets


def test_load_alerts_returns_dict():
    """load_alerts returns a dict with spike_detection and notifications."""
    alerts = load_alerts()
    assert isinstance(alerts, dict)
    assert "spike_detection" in alerts
    assert "notifications" in alerts


def test_load_trend_cycles_returns_dict():
    """load_trend_cycles returns a dict with planned and adhoc cycle info."""
    cycles = load_trend_cycles()
    assert isinstance(cycles, dict)
    assert "planned_cycles" in cycles
    assert "adhoc_slots" in cycles


def test_load_entity_aliases_covers_all_markets():
    """entity_aliases.yaml has an alias group for each market with real names."""
    aliases = load_entity_aliases()
    for market in ("za", "ng", "ke"):
        assert market in aliases
        assert isinstance(aliases[market], dict)
        assert len(aliases[market]) > 0


def test_load_entity_aliases_missing_file_returns_empty_dict(monkeypatch):
    """A checkout with no entity_aliases.yaml degrades to an empty map."""
    import src.utils.config_loader as config_loader

    monkeypatch.setattr(config_loader.Path, "exists", lambda self: False)
    assert load_entity_aliases() == {}


def test_load_yaml_empty_file_returns_empty_dict(tmp_path: Path):
    """load_yaml returns an empty dict when the file is empty."""
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert load_yaml(empty) == {}


def test_load_yaml_invalid_raises(tmp_path: Path):
    """load_yaml raises YAMLError on malformed input."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("key: : broken\n  : nope", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        load_yaml(bad)


def _collect_yaml_files() -> list[Path]:
    """Collect every yaml file under the configs directory."""
    return sorted(CONFIGS_DIR.rglob("*.yaml"))


@pytest.mark.parametrize("yaml_path", _collect_yaml_files(), ids=lambda p: p.name)
def test_all_config_yamls_parse_cleanly(yaml_path: Path):
    """Every yaml file under configs/ parses without error."""
    data = load_yaml(yaml_path)
    assert data is not None
    assert isinstance(data, dict)
