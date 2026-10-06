"""Unit tests for market detection logic."""

import pytest
from src.utils import market_detector
from src.utils.market_detector import _cached_market_keywords, detect_market

# Minimal keyword stubs per market. Used to isolate the detector from
# the real configs so tests stay hermetic and fast.
_STUB_KEYWORDS = {
    "za": {
        "regional_markers": [
            "south africa",
            "mzansi",
            "jozi",
            "soweto",
            "amapiano",
            "load shedding",
            "kasi",
            "kota",
        ],
    },
    "ng": {
        "regional_markers": [
            "nigeria",
            "naija",
            "lagos",
            "abuja",
            "afrobeats",
            "japa",
        ],
    },
    "ke": {
        "regional_markers": [
            "kenya",
            "nairobi",
            "mombasa",
            "matatu",
            "gengetone",
            "mpesa",
        ],
    },
}


@pytest.fixture(autouse=True)
def _isolate_detector(monkeypatch):
    """Swap the cached keyword loader for an in-memory stub and clear the cache."""
    _cached_market_keywords.cache_clear()
    monkeypatch.setattr(
        market_detector,
        "get_active_markets",
        lambda: ["za", "ng", "ke"],
    )

    def _stub(market: str):
        return _STUB_KEYWORDS[market]

    # Wrap with an lru_cache so the cache-behavior test can still read cache_info.
    import functools

    stubbed = functools.lru_cache(maxsize=8)(_stub)
    monkeypatch.setattr(market_detector, "_cached_market_keywords", stubbed)
    yield
    _cached_market_keywords.cache_clear()


def test_detect_market_za():
    """Content with ZA-specific markers classifies as za."""
    text = "Jozi nightlife and amapiano are booming during load shedding"
    assert detect_market(text) == "za"


def test_detect_market_ng():
    """Content with NG-specific markers classifies as ng."""
    text = "Lagos afrobeats artists dominate the naija charts this week"
    assert detect_market(text) == "ng"


def test_detect_market_ke():
    """Content with KE-specific markers classifies as ke."""
    text = "Nairobi matatu culture meets gengetone on the streets of Mombasa"
    assert detect_market(text) == "ke"


def test_detect_market_ambiguous_falls_back():
    """Ambiguous content with no regional markers returns empty string."""
    text = "A generic news article about the weather today"
    assert detect_market(text) == ""


def test_detect_market_empty_string():
    """Empty input returns empty string without raising."""
    assert detect_market("") == ""
    assert detect_market(None) == ""


def test_detect_market_explicit_region_wins():
    """source_region takes priority over keyword content."""
    text = "Jozi nightlife and amapiano everywhere"
    assert detect_market(text, source_region="Kenya") == "ke"


def test_detect_market_platform_region_maps_codes():
    """Short country codes on platform_region map to markets."""
    assert detect_market("", platform_region="ZA") == "za"
    assert detect_market("", platform_region="NG") == "ng"
    assert detect_market("", platform_region="KE") == "ke"


def test_detect_market_handle_matching():
    """Regional markers can match on the author handle alone."""
    assert detect_market(text="", author_handle="jozi_lifestyle") == "za"


def test_detect_market_cached():
    """The stubbed keyword loader is hit once per market, then cached."""
    stubbed = market_detector._cached_market_keywords
    stubbed.cache_clear()

    for text in (
        "jozi amapiano",
        "lagos afrobeats",
        "nairobi gengetone",
        "jozi kasi",
        "lagos naija",
    ):
        detect_market(text)

    info = stubbed.cache_info()
    # Each call iterates 3 markets; the first round fills 3 misses, every
    # subsequent round is all hits.
    assert info.misses == 3, f"expected 3 misses, got {info.misses}"
    assert info.hits >= 12, f"expected >=12 hits, got {info.hits}"
