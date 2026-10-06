"""Every evidence shape observed in the R3 window resolves to one approved vendor and channel pair."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from src.analysis.open_intelligence import candidates, predictions, readiness
from src.analysis.open_intelligence.candidates import (
    APPROVED_SOURCE_IDENTITY_PAIRS,
    QUALIFYING_SOURCE_FAMILIES,
    resolve_source_identity,
)
from src.analysis.open_intelligence.graph import wave1_pair_is_independent

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "docs" / "contracts" / "open_intelligence_v2.md"

# Shapes read back from enriched_content for 14 to 27 August 2026 that the resolver refused,
# with the pair each must resolve to. Endpoints are null on every historical row.
OBSERVED_SHAPES = (
    (("brand24", "news", "mention"), ("brand24", "brand24")),
    (("brand24", "tiktok", "mention"), ("brand24", "brand24")),
    (("brand24", "web", "topic"), ("brand24", "brand24")),
    (("brand24", "linkedin", "mention"), ("brand24", "brand24")),
    (("brand24", "social", "trending_hashtag"), ("brand24", "brand24")),
    (("brand24", "facebook", "top_author"), ("brand24", "brand24")),
    (("brand24", "twitter", "trending_link"), ("brand24", "brand24")),
    (("brand24", "bluesky", "mention"), ("brand24", "brand24")),
    (("brand24", "instagram", "top_author"), ("brand24", "brand24")),
    (("socialcrawl", "twitter", "twitter/user_tweets"), ("socialcrawl", "twitter")),
    (("socialcrawl", "threads", "threads/search"), ("socialcrawl", "threads")),
    (("socialcrawl", "threads", "threads/user_posts"), ("socialcrawl", "threads")),
    (("socialcrawl", "news", "news/socialcrawl"), ("socialcrawl", "news")),
    (("Google Trends", "google_search", "search_term"), ("google_trends", "search")),
    (("bigquery_trends", "google_search", "top_term"), ("google_trends", "search")),
    (("google_trends_rss", "google_search", "trending_search"), ("google_trends", "search")),
    (("apple_music", "apple_music", "chart_track"), ("apple_music", "music")),
    (("punchng.com", "news", "gdelt_gkg"), ("gdelt", "news")),
    (("citizen.co.za", "news", "gdelt_gkg"), ("gdelt", "news")),
)


@pytest.mark.parametrize(("shape", "expected"), OBSERVED_SHAPES)
def test_every_observed_window_shape_resolves_to_its_approved_pair(shape, expected):
    source, platform, content_type = shape
    resolved = resolve_source_identity(
        source=source, platform=platform, content_type=content_type, endpoint=None
    )
    assert resolved == expected
    assert resolved in APPROVED_SOURCE_IDENTITY_PAIRS


def test_existing_shapes_keep_their_pairs():
    assert resolve_source_identity(source="rss", platform="web", content_type="article") == (
        "rss",
        "news",
    )
    assert resolve_source_identity(source="gdelt_events", platform="news") == ("gdelt", "news")
    assert resolve_source_identity(source="youtube", platform="youtube") == (
        "google_youtube",
        "youtube",
    )
    assert resolve_source_identity(
        source="socialcrawl", platform="tiktok", endpoint="/v1/tiktok/song"
    ) == ("socialcrawl", "short_video")
    assert resolve_source_identity(source="socialcrawl", platform="reddit") == (
        "socialcrawl",
        "reddit",
    )


def test_unknown_shapes_still_refuse_instead_of_guessing():
    with pytest.raises(ValueError, match="source identity is unavailable"):
        resolve_source_identity(source="mystery_vendor", platform="carrier_pigeon")
    with pytest.raises(ValueError, match="source identity is unavailable"):
        resolve_source_identity(source="socialcrawl", platform="linkedin")
    with pytest.raises(ValueError, match="unsupported"):
        candidates.validate_source_identity("brand24", "news")


def test_new_channels_qualify_but_one_vendor_never_manufactures_independence():
    assert {"twitter", "threads"} <= QUALIFYING_SOURCE_FAMILIES
    assert candidates.SOURCE_FAMILY_MAP_VERSION == "channel_family_v2"
    assert wave1_pair_is_independent("socialcrawl", "twitter", "socialcrawl", "threads") is False
    assert wave1_pair_is_independent("socialcrawl", "twitter", "socialcrawl", "news") is False
    assert wave1_pair_is_independent("socialcrawl", "twitter", "gdelt", "news") is True
    assert wave1_pair_is_independent("google_trends", "search", "apple_music", "music") is True


def test_brand24_is_legacy_on_both_axes_and_never_active():
    assert ("brand24", "brand24") in APPROVED_SOURCE_IDENTITY_PAIRS
    assert "brand24" not in readiness._ACTIVE_V2_FAMILIES
    assert "brand24" not in predictions._ACTIVE_FAMILIES
    assert wave1_pair_is_independent("brand24", "brand24", "brand24", "brand24") is False
    assert candidates.validate_source_identity("brand24", "brand24") == ("brand24", "brand24")


def test_contract_table_lists_exactly_the_approved_pairs():
    text = CONTRACT.read_text(encoding="utf-8")
    section = text[text.index("#### 2.3.1 Source identity pairs") : text.index("### 2.4 ")]
    documented = set(re.findall(r"^\| `([a-z_0-9]+)` \| `([a-z_0-9]+)` \|", section, re.MULTILINE))
    assert documented == set(APPROVED_SOURCE_IDENTITY_PAIRS)
    families = set(
        re.findall(
            r"`([a-z_0-9]+)`",
            section.split("The qualifying channel family set is", 1)[1].split(";")[0],
        )
    )
    assert families == set(QUALIFYING_SOURCE_FAMILIES)
