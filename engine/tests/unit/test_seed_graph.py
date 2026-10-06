"""Unit tests for seed_graph builder (V3 Track A)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from src.analysis import seed_graph as sg


def _row(**kwargs):
    base = {
        "id": "row1",
        "market": "za",
        "platform": "tiktok",
        "source": "EnsembleData",
        "title": "",
        "text": "",
        "slang_terms": "",
        "topic_groups": [],
        "genz_score": 0.5,
        "slang_score": 0.0,
        "near_topic": None,
        "near_cosine": None,
        "published_at": "2026-07-01T10:00:00+00:00",
        "collected_at": "2026-07-01T12:00:00+00:00",
        "content_type": "post",
        "author_handle": "",
    }
    base.update(kwargs)
    return base


def test_schema_order_parity_importable():
    import tests.unit.test_schema_order_parity as parity

    parity.test_every_schema_file_in_schema_order()
    parity.test_schema_order_entries_exist_on_disk()


@pytest.mark.parametrize(
    "source,platform,content_type,expected",
    [
        ("EnsembleData", "tiktok", "post", "tiktok"),
        ("brand24", "web", "post", "web"),
        ("brand24", "tiktok", "post", "tiktok"),
        ("brand24", "link", "aggregate", None),
        ("rss", "web", "post", "news"),
        ("gdelt", "news", "gdelt_gkg", "news"),
        ("bigquery_trends", "google_search", "post", "search"),
        ("apple_music", "apple_music", "chart_track", "music"),
        ("reddit", "reddit", "post", "reddit"),
        ("youtube", "youtube", "post", "youtube"),
    ],
)
def test_normalize_seed_platform(source, platform, content_type, expected):
    assert sg.normalize_seed_platform(source, platform, content_type) == expected


def test_publisher_named_rss_article_produces_news_seed_rows():
    frame = pd.DataFrame(
        [
            _row(
                source="Example publication",
                platform="web",
                content_type="article",
                text="Local #amapiano events bring neighbours together",
            )
        ]
    )

    rows = sg.build_seed_graph_rows(frame, "za", date(2026, 7, 1))

    assert any(row["term"] == "amapiano" and row["platform"] == "news" for row in rows)
    assert sg.normalize_seed_platform("Example publication", "web", "post") == "other"
    assert sg.normalize_seed_platform("brand24", "web", "article") == "web"


@pytest.mark.parametrize("hash_seed", ["1", "2"])
def test_bounded_cooccurrence_and_output_ties_are_hash_seed_independent(hash_seed):
    script = r"""
import json,socket
def blocked(*args, **kwargs):
    raise AssertionError("network disabled")
socket.socket.connect=blocked
socket.create_connection=blocked
import pandas as pd
from datetime import date
from src.analysis import seed_graph as sg
from tests.unit.test_seed_graph import _row
sg.MAX_CO_OCCUR_KEYS=2
frame=pd.DataFrame([
    _row(id="a",slang_terms="amapiano,ceramics,zebras"),
    _row(id="b",slang_terms="amapiano,apricots,zebras"),
])
rows=sg.build_seed_graph_rows(frame,"za",date(2026,7,1))
anchor=next(row for row in rows if row["term"]=="amapiano")
sg.TERM_CAP_PER_MARKET=1
capped=sg.build_seed_graph_rows(
    pd.DataFrame([_row(text="#amapiano",slang_terms="amapiano")]),
    "za",date(2026,7,1)
)
print(json.dumps({"co_occurs":anchor["co_occur_terms"],"capped_type":capped[0]["term_type"]}))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "PYTHONHASHSEED": hash_seed},
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    actual = json.loads(result.stdout)
    assert actual["co_occurs"] == ["zebras", "apricots"]
    assert actual["capped_type"] == "hashtag"


def test_pii_mention_drops_token():
    df = pd.DataFrame([_row(text="love this @thandi_m fire amapiano vibes")])
    rows = sg.build_seed_graph_rows(df, "za", date(2026, 7, 1))
    terms = {r["term"] for r in rows}
    assert "thandi_m" not in terms
    assert "thandi" not in terms


def test_hashtag_extraction():
    df = pd.DataFrame([_row(text="Check #amapiano and #fyp today")])
    rows = sg.build_seed_graph_rows(df, "za", date(2026, 7, 1))
    tags = {r["term"] for r in rows if r["term_type"] == "hashtag"}
    assert "amapiano" in tags
    assert "fyp" not in tags


def test_slang_extraction():
    df = pd.DataFrame([_row(slang_terms="sapa, yebo")])
    rows = sg.build_seed_graph_rows(df, "za", date(2026, 7, 1))
    slang = {r["term"] for r in rows if r["term_type"] == "slang"}
    assert "sapa" in slang
    assert "yebo" in slang


def test_ensemble_title_equals_text_prefix_per_row_presence():
    text = "amapiano drop " * 3
    df = pd.DataFrame([_row(title=text[:100], text=text)])
    rows = sg.build_seed_graph_rows(df, "za", date(2026, 7, 1))
    amap = [r for r in rows if r["term"] == "amapiano"]
    assert amap
    assert amap[0]["row_count"] == 1


def test_near_topics_empty_when_flag_off_columns_null():
    df = pd.DataFrame([_row(text="random chatter")])
    rows = sg.build_seed_graph_rows(df, "za", date(2026, 7, 1))
    for r in rows:
        assert r["near_topics"] == []


def test_near_topics_populated_from_near_miss_band():
    df = pd.DataFrame(
        [
            _row(
                text="#amapiano heat",
                topic_groups=["music_amapiano"],
                near_topic="music_amapiano",
                near_cosine=0.55,
            )
        ]
    )
    rows = sg.build_seed_graph_rows(df, "za", date(2026, 7, 1))
    assert any("music_amapiano" in (r.get("near_topics") or []) for r in rows)


def test_gdelt_excluded_from_token_extraction():
    df = pd.DataFrame([_row(source="gdelt", platform="news", text="unclassified tokenword")])
    rows = sg.build_seed_graph_rows(df, "za", date(2026, 7, 1))
    assert not any(r["term_type"] == "token" for r in rows)


# GDELT V2Themes leak: news outlets (source != 'gdelt') whose enriched text is a
# raw theme dump were tokenised into wb_696_public_sector_management,
# crisislex_*, tax_fncact_* etc, which then outranked real slang on the board.
_GDELT_THEME_DUMP = (
    "CRIME_COMMON_ROBBERY,80 CRIME_COMMON_ROBBERY,2019 TAX_FNCACT_ROBBERS,80 "
    "WB_696_PUBLIC_SECTOR_MANAGEMENT,120 EPU_POLICY_GOVERNMENT,45 "
    "CRISISLEX_C07_SAFETY,12 UNGP_FORESTS_RIVERS_OCEANS,9"
)


def test_gdelt_theme_dump_detected():
    assert sg._looks_like_gdelt_theme_dump(_GDELT_THEME_DUMP)
    assert not sg._looks_like_gdelt_theme_dump("amapiano is the sound of the weekend braai")


def test_gdelt_theme_dump_row_yields_no_tokens():
    # A news outlet, not source='gdelt', whose text is only the theme dump.
    df = pd.DataFrame([_row(source="the-star.co.ke", platform="news", text=_GDELT_THEME_DUMP)])
    rows = sg.build_seed_graph_rows(df, "ke", date(2026, 7, 1))
    terms = {r["term"] for r in rows}
    for junk in ("wb_696_public_sector_management", "crisislex_c07_safety", "tax_fncact_robbers"):
        assert junk not in terms


@pytest.mark.parametrize(
    "term",
    [
        "ucgnt5exzvbnvu7f6insehyg",  # random base32-ish hash
        "8230",  # bare number
        "2026",  # bare year
        "2019",
        "misspeters0724728587",  # phone number in a username
        "wb_133_information_and_communication_technologies",  # gdelt theme code
        "crisislex_crisislexrec",
        "epu_policy_government",
        "tax_fncact_president",
    ],
)
def test_low_quality_term_rejected(term):
    assert sg._low_quality_term(term)


@pytest.mark.parametrize(
    "term",
    [
        "amapiano",
        "mtoto",
        "nonini",
        "nsfas",  # SSA acronym, low vowel ratio but real
        "mpesa",
        "sapa",
        "tiktokdance",
        "springboks",
        "gengetone",
        "afronation",
        "durbanjuly",
        "detty",
        "bafana",
    ],
)
def test_real_terms_survive_quality_gate(term):
    assert not sg._low_quality_term(term)


def test_phone_number_handle_not_extracted():
    df = pd.DataFrame(
        [_row(source="EnsembleData", author_handle="misspeters0724728587", text="new drop")]
    )
    rows = sg.build_seed_graph_rows(df, "ke", date(2026, 7, 1))
    handles = {r["term"] for r in rows if r["term_type"] == "handle"}
    assert "misspeters0724728587" not in handles


def test_build_seed_graph_rows_empty_published_at():
    """Empty published_at must not poison event_date min() with NaT."""
    df = pd.DataFrame([_row(text="Synthetic source text", published_at="", collected_at="")])
    rows = sg.build_seed_graph_rows(df, "za", date(2026, 7, 1))
    assert rows
    assert all(r.get("event_date") is None for r in rows)


@pytest.mark.parametrize(
    "topic_groups",
    [
        [],
        None,
        pd.NA,
        pytest.param(__import__("numpy").array([]), id="numpy_empty"),
    ],
)
def test_build_seed_graph_rows_empty_topic_groups(topic_groups):
    """Rows with null or empty topic_groups must not crash the builder."""
    df = pd.DataFrame([_row(text="Check #amapiano today", topic_groups=topic_groups)])
    rows = sg.build_seed_graph_rows(df, "za", date(2026, 7, 1))
    assert rows
    for r in rows:
        assert r["topic_groups"] == []


def test_build_seed_graph_rows_numpy_topic_groups():
    import numpy as np

    df = pd.DataFrame(
        [
            _row(
                text="#amapiano heat",
                topic_groups=np.array(["music_amapiano"]),
            )
        ]
    )
    rows = sg.build_seed_graph_rows(df, "za", date(2026, 7, 1))
    assert any("music_amapiano" in (r.get("topic_groups") or []) for r in rows)


def test_persist_delete_then_insert_idempotent():
    trend = date(2026, 7, 1)
    rows = [
        {
            "market": "za",
            "term": "amapiano",
            "term_type": "hashtag",
            "platform": "tiktok",
            "trend_date": trend.isoformat(),
            "event_date": trend.isoformat(),
            "row_count": 2,
            "avg_genz_score": 0.5,
            "slang_row_share": 0.0,
            "topic_groups": ["music_amapiano"],
            "near_topics": [],
            "co_occur_terms": [],
            "sample_row_ids": ["a"],
            "generated_at": "2026-07-01T00:00:00+00:00",
        }
    ]
    mock_client = MagicMock()
    mock_table = MagicMock()
    mock_table.schema = []
    mock_client.get_table.return_value = mock_table
    mock_client.get_table.return_value = mock_table
    mock_client.load_table_from_file.return_value.result.return_value = None

    with (
        patch("src.analysis.seed_graph.get_client", return_value=mock_client),
        patch("src.analysis.seed_graph.get_dataset", return_value="trends_v2_dev"),
    ):
        n1 = sg.persist_seed_graph(rows, trend)
        n2 = sg.persist_seed_graph(rows, trend)

    assert n1 == 1
    assert n2 == 1
    assert mock_client.load_table_from_file.call_count == 2
    dest = mock_client.load_table_from_file.call_args[0][1]
    assert dest.endswith(".seed_graph$20260701")


def test_is_enabled_default_false():
    import os

    old = os.environ.pop("SEED_GRAPH_ENABLED", None)
    try:
        assert sg.is_enabled() is False
    finally:
        if old is not None:
            os.environ["SEED_GRAPH_ENABLED"] = old


def test_v2gcam_in_enriched_columns():
    assert "v2gcam" in sg.ENRICHED_COLUMNS


def test_structured_capture_row_yields_terms_with_nan_gcam_elsewhere():
    payload = '__sc__:[{"type": "music", "value": "Song X"}, {"type": "handle", "value": "DJMax"}]'
    df = pd.DataFrame(
        [
            _row(id="sc1", v2gcam=payload, text="#amapiano heat"),
            _row(id="sc2", v2gcam=float("nan"), text="#amapiano heat"),
        ]
    )
    rows = sg.build_seed_graph_rows(df, "za", date(2026, 7, 1))
    pairs = {(r["term"], r["term_type"]) for r in rows}
    assert ("song x", "music") in pairs
    assert ("djmax", "handle") in pairs


def test_event_date_nat_returns_none():
    row = _row(published_at=pd.NaT, collected_at=pd.NaT)
    assert sg._event_date(row) is None


def test_event_date_nat_published_falls_back_to_collected():
    row = _row(published_at=pd.NaT, collected_at=pd.Timestamp("2026-07-01T12:00:00+00:00"))
    assert sg._event_date(row) == date(2026, 7, 1)


def test_mislabeled_foreign_script_row_with_topics_is_blocked():
    foreign = "ข่าวบันเทิงไทยวันนี้"
    df = pd.DataFrame(
        [
            _row(
                id="fx1",
                text=f"{foreign} #amapiano",
                topic_groups=["music_amapiano"],
            )
        ]
    )
    rows = sg.build_seed_graph_rows(df, "za", date(2026, 7, 1))
    assert not any(r["term_type"] == "token" for r in rows)


_TAGALOG_POST = (
    "[r/FlipTop] Zoning 22 review na medyo late kasi babiyahe pa ako pa-MOA. "
    "Bago magstart si Aric ng event, lagi siyang nag-aask if okay yung sound sa "
    "ibat ibang sides ng venue kaya medyo nahihirapan ang mga tao sa likod"
)
_INDIAN_ENGLISH_POST = (
    "[r/IndianFestivals] Adhik Maas has almost no festivals. That is intentional. "
    "The extra lunar month feels like a glitch in the calendar, an entire month "
    "inserted to resync the lunar and solar years through the sadhana tradition"
)
_REAL_NG_REDDIT_POST = (
    "[r/Nigeria] The fuel subsidy removal has made transport so expensive in Lagos. "
    "How are people coping with the new prices at the pump this month, the hardship "
    "is real and nobody in government seems to care about ordinary Nigerians"
)


def test_foreign_reddit_subreddit_detected():
    assert sg._foreign_reddit_sub(_INDIAN_ENGLISH_POST)
    assert sg._foreign_reddit_sub(_TAGALOG_POST)
    assert not sg._foreign_reddit_sub(_REAL_NG_REDDIT_POST)
    assert not sg._foreign_reddit_sub("no bracket here at all")


def test_reddit_tagalog_row_yields_no_tokens():
    # A foreign-LANGUAGE reddit post (r/FlipTop, Tagalog) that the global guard
    # lets through because tl is excluded for the Pidgin collision.
    df = pd.DataFrame([_row(source="Reddit", platform="reddit", text=_TAGALOG_POST)])
    rows = sg.build_seed_graph_rows(df, "za", date(2026, 7, 1))
    terms = {r["term"] for r in rows}
    assert "siyang" not in terms
    assert "kapag" not in terms


def test_reddit_foreign_english_sub_yields_no_tokens():
    # A foreign-SUBREDDIT reddit post written in English (r/IndianFestivals): no
    # language check can catch it, the subreddit name is the only signal.
    df = pd.DataFrame(
        [_row(market="ng", source="Reddit", platform="reddit", text=_INDIAN_ENGLISH_POST)]
    )
    rows = sg.build_seed_graph_rows(df, "ng", date(2026, 7, 1))
    terms = {r["term"] for r in rows}
    assert "sadhana" not in terms


def test_real_ng_reddit_english_row_still_extracts():
    # Standard-English SSA reddit content must survive the stricter gate.
    df = pd.DataFrame(
        [_row(market="ng", source="Reddit", platform="reddit", text=_REAL_NG_REDDIT_POST)]
    )
    rows = sg.build_seed_graph_rows(df, "ng", date(2026, 7, 1))
    terms = {r["term"] for r in rows}
    assert "subsidy" in terms or "transport" in terms


@pytest.mark.parametrize(
    "platform,content_type",
    [("reddit", "reddit_post"), ("reddit", "reddit_comment"), ("Reddit", "reddit_post")],
)
def test_socialcrawl_reddit_foreign_community_tokens_are_withheld(platform, content_type):
    frame = pd.DataFrame(
        [
            _row(
                source="socialcrawl",
                platform=platform,
                content_type=content_type,
                text="[r/IndianFestivals] Local festivities inspire intricate lantern designs",
            )
        ]
    )
    rows = sg.build_seed_graph_rows(frame, "za", date(2026, 7, 1))
    assert not any(row["term_type"] == "token" for row in rows)


def test_socialcrawl_reddit_nigerian_context_keeps_tokens():
    frame = pd.DataFrame(
        [
            _row(
                market="ng",
                source="socialcrawl",
                platform="reddit",
                content_type="reddit_comment",
                text="[r/Nigeria] Lagos commuters discuss transport fares this week",
            )
        ]
    )
    rows = sg.build_seed_graph_rows(frame, "ng", date(2026, 7, 1))
    assert ("commuters", "token") in {(row["term"], row["term_type"]) for row in rows}


def test_empty_day_still_truncates_partition():
    trend = date(2026, 7, 1)
    mock_client = MagicMock()
    mock_client.project = "proj"
    mock_client.query.return_value.to_dataframe.return_value = pd.DataFrame()
    mock_client.load_table_from_file.return_value.result.return_value = None

    with (
        patch("src.analysis.seed_graph.get_client", return_value=mock_client),
        patch("src.analysis.seed_graph.get_dataset", return_value="trends_v2_dev"),
    ):
        counts = sg.build_and_persist_seed_graph(trend)

    assert counts == {"za": 0, "ng": 0, "ke": 0}
    assert mock_client.load_table_from_file.call_count == 1
    dest = mock_client.load_table_from_file.call_args[0][1]
    assert dest.endswith(".seed_graph$20260701")
    job_config = mock_client.load_table_from_file.call_args.kwargs["job_config"]
    assert job_config.write_disposition == "WRITE_TRUNCATE"


@pytest.mark.parametrize("boundary", ["fetch", "build", "load"])
def test_seed_graph_failure_reaches_caller_with_original_cause(boundary, monkeypatch, caplog):
    error = RuntimeError(f"synthetic {boundary} failed")
    client = MagicMock()
    client.project = "synthetic"
    client.query.return_value.to_dataframe.return_value = pd.DataFrame()
    client.load_table_from_file.return_value.result.return_value = None
    monkeypatch.setattr(sg, "get_client", lambda: client)
    monkeypatch.setattr(sg, "get_dataset", lambda: "synthetic")
    if boundary == "fetch":
        client.query.side_effect = error
    elif boundary == "build":
        monkeypatch.setattr(sg, "build_seed_graph_rows", MagicMock(side_effect=error))
    else:
        client.load_table_from_file.return_value.result.side_effect = error
    with pytest.raises(RuntimeError) as raised:
        sg.build_and_persist_seed_graph(date(2026, 9, 6))
    assert raised.value is error
    assert "persisted" not in caplog.text
    if boundary != "load":
        client.load_table_from_file.assert_not_called()
