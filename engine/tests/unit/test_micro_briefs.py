"""Unit tests for src.analysis.micro_briefs.select_micro_briefs.

Pure function: no BQ, no network. Aggregates seed_graph row-grain rows per
term for one market, then applies the micro-brief selection rules (term type,
frequency band, recency, safety, config/stoplist exclusion) and ranks the
survivors by frequency times recency.
"""

import datetime

from src.analysis.micro_briefs import select_micro_briefs

TREND_DATE = datetime.date(2026, 7, 3)


def _row(
    term,
    term_type="hashtag",
    platform="tiktok",
    row_count=1,
    event_date=TREND_DATE,
    topic_groups=None,
    near_topics=None,
    sample_row_ids=None,
    market="za",
):
    return {
        "market": market,
        "term": term,
        "term_type": term_type,
        "platform": platform,
        "trend_date": TREND_DATE,
        "event_date": event_date,
        "row_count": row_count,
        "avg_genz_score": 0.5,
        "slang_row_share": 0.2,
        "topic_groups": topic_groups if topic_groups is not None else ["music_amapiano"],
        "near_topics": near_topics or [],
        "sample_row_ids": sample_row_ids or ["r1"],
    }


def _rows_for_term(term, n, **kwargs):
    """n row-grain rows for one term so frequency (row count) lands at n."""
    return [_row(term, sample_row_ids=[f"{term}-{i}"], **kwargs) for i in range(n)]


def test_selects_eligible_hashtag_term():
    rows = _rows_for_term("bucketlisthustle", 10)
    out = select_micro_briefs(rows, "za", TREND_DATE, config_terms=set(), stoplist=frozenset())
    assert len(out) == 1
    item = out[0]
    assert item["term"] == "bucketlisthustle"
    assert item["frequency"] == 10
    assert item["platforms"] == ["tiktok"]
    assert item["topics"] == ["music_amapiano"]
    assert item["sample_row_ids"]


def test_filters_out_plain_token_term_type():
    rows = _rows_for_term("plainword", 10, term_type="token")
    out = select_micro_briefs(rows, "za", TREND_DATE, config_terms=set(), stoplist=frozenset())
    assert out == []


def test_allows_music_and_channel_term_types():
    rows_music = _rows_for_term("amapianoclassics", 8, term_type="music")
    rows_channel = _rows_for_term("newscastnow", 8, term_type="channel")
    out = select_micro_briefs(
        rows_music + rows_channel, "za", TREND_DATE, config_terms=set(), stoplist=frozenset()
    )
    terms = {item["term"] for item in out}
    assert terms == {"amapianoclassics", "newscastnow"}


def test_frequency_below_band_excluded():
    rows = _rows_for_term("toolow", 4)
    out = select_micro_briefs(rows, "za", TREND_DATE, config_terms=set(), stoplist=frozenset())
    assert out == []


def test_frequency_above_band_excluded():
    rows = _rows_for_term("toohigh", 51)
    out = select_micro_briefs(rows, "za", TREND_DATE, config_terms=set(), stoplist=frozenset())
    assert out == []


def test_frequency_band_boundaries_included():
    rows_low = _rows_for_term("lowbound", 5)
    rows_high = _rows_for_term("highbound", 50)
    out = select_micro_briefs(
        rows_low + rows_high, "za", TREND_DATE, config_terms=set(), stoplist=frozenset()
    )
    terms = {item["term"] for item in out}
    assert terms == {"lowbound", "highbound"}


def test_stale_first_seen_excluded():
    old_date = TREND_DATE - datetime.timedelta(days=4)
    rows = _rows_for_term("staleitem", 10, event_date=old_date)
    out = select_micro_briefs(rows, "za", TREND_DATE, config_terms=set(), stoplist=frozenset())
    assert out == []


def test_first_seen_within_three_days_included():
    recent = TREND_DATE - datetime.timedelta(days=3)
    rows = _rows_for_term("recentitem", 10, event_date=recent)
    out = select_micro_briefs(rows, "za", TREND_DATE, config_terms=set(), stoplist=frozenset())
    assert len(out) == 1


def test_config_term_excluded():
    rows = _rows_for_term("configged", 10)
    out = select_micro_briefs(
        rows, "za", TREND_DATE, config_terms={"configged"}, stoplist=frozenset()
    )
    assert out == []


def test_stoplisted_term_excluded():
    rows = _rows_for_term("stopped", 10)
    out = select_micro_briefs(
        rows, "za", TREND_DATE, config_terms=set(), stoplist=frozenset({"stopped"})
    )
    assert out == []


def test_no_topic_or_near_topic_excluded():
    rows = _rows_for_term("notopic", 10, topic_groups=[], near_topics=[])
    out = select_micro_briefs(rows, "za", TREND_DATE, config_terms=set(), stoplist=frozenset())
    assert out == []


def test_near_topic_alone_is_sufficient():
    rows = _rows_for_term("neartopiconly", 10, topic_groups=[], near_topics=["sports_football"])
    out = select_micro_briefs(rows, "za", TREND_DATE, config_terms=set(), stoplist=frozenset())
    assert len(out) == 1
    assert out[0]["topics"] == ["sports_football"]


def test_unsafe_term_excluded():
    # A term that is itself flagged as a risk marker by check_safety.
    rows = _rows_for_term("scandalstory", 10)
    out = select_micro_briefs(rows, "za", TREND_DATE, config_terms=set(), stoplist=frozenset())
    assert out == []


def test_wrong_market_rows_ignored():
    rows = _rows_for_term("otherterm", 10, market="ng")
    out = select_micro_briefs(rows, "za", TREND_DATE, config_terms=set(), stoplist=frozenset())
    assert out == []


def test_limit_caps_output_and_ranks_by_frequency_then_recency():
    rows = []
    rows += _rows_for_term("termhigh", 40)
    rows += _rows_for_term("termmid", 20)
    rows += _rows_for_term("termlow", 6)
    rows += _rows_for_term("termnewer", 6, event_date=TREND_DATE)
    out = select_micro_briefs(
        rows, "za", TREND_DATE, config_terms=set(), stoplist=frozenset(), limit=2
    )
    assert len(out) == 2
    assert out[0]["term"] == "termhigh"


def test_platforms_top_two_by_count():
    rows = (
        _rows_for_term("multiplat", 3, platform="tiktok")
        + _rows_for_term("multiplat", 5, platform="instagram")
        + _rows_for_term("multiplat", 1, platform="threads")
    )
    out = select_micro_briefs(rows, "za", TREND_DATE, config_terms=set(), stoplist=frozenset())
    assert len(out) == 1
    assert out[0]["platforms"] == ["instagram", "tiktok"]


def test_sample_row_ids_capped_at_three():
    rows = _rows_for_term("manysamples", 10)
    out = select_micro_briefs(rows, "za", TREND_DATE, config_terms=set(), stoplist=frozenset())
    assert len(out[0]["sample_row_ids"]) <= 3


def test_topics_capped_at_two():
    rows = _rows_for_term(
        "manytopics",
        10,
        topic_groups=["music_amapiano", "sports_football", "genz_slang"],
    )
    out = select_micro_briefs(rows, "za", TREND_DATE, config_terms=set(), stoplist=frozenset())
    assert len(out[0]["topics"]) <= 2


def test_empty_rows_returns_empty_list():
    out = select_micro_briefs([], "za", TREND_DATE, config_terms=set(), stoplist=frozenset())
    assert out == []
