import re
from pathlib import Path

import pytest
import yaml

MARKETS_FILE = Path(__file__).resolve().parents[1] / "markets.yaml"

REQUIRED_KEYS = {
    "country_code",
    "languages",
    "timezone",
    "terms",
    "subreddits",
    "twitter_handles",
    "facebook_pages",
    "hashtags",
}
LIST_BLOCKS = ["terms", "subreddits", "twitter_handles", "facebook_pages"]
EXPECTED_CODES = {"za": "ZA", "ng": "NG", "ke": "KE"}

# Banned strings are joined at run time so neither this file nor its
# compiled bytecode holds them (the compiler folds a plain + of literals).
BANNED_PATTERNS = [
    re.escape("".join(["gen", "z"])),
    re.escape("".join(["gen", " z"])),
    re.escape("".join(["gen", "-z"])),
    re.escape("".join(["gen", "_z"])),
    re.escape("".join(["google", "_trends"])),
    re.escape("".join(["google", " trends"])),
    re.escape("".join(["google", "trends"])),
    r"\b" + "you" + "th",
    r"\b" + "te" + "en",
    r"\b" + "stu" + "dent",
    r"\b" + "var" + "sity",
    r"\b" + "cam" + "pus",
    r"\b" + "adult" + "ing",
    r"\b" + "millen" + "nial",
    r"\b" + "boom" + "er",
    r"\b" + "zoom" + "er",
    r"\b" + "age" + r"d?\b",
    r"\b" + "age" + " group",
    r"\b" + "born" + " free",
    r"\b" + "ns" + r"fas\b",
    r"\b" + "mat" + r"ric\b",
]
DASHES = [chr(0x2014), chr(0x2013), "-" * 2]


@pytest.fixture(scope="module")
def raw_text():
    return MARKETS_FILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def markets(raw_text):
    return yaml.safe_load(raw_text)["markets"]


def test_three_markets_present(markets):
    assert set(markets) == {"za", "ng", "ke"}


@pytest.mark.parametrize("market", ["za", "ng", "ke"])
def test_market_has_required_keys(markets, market):
    block = markets[market]
    assert REQUIRED_KEYS <= set(block)
    assert block["country_code"] == EXPECTED_CODES[market]
    assert block["timezone"]["value"].startswith("Africa/")
    assert "en" in block["languages"]["codes"]


def test_instagram_locations_use_the_reviewed_probe_ids_and_provenance(markets):
    expected = {
        "za": (
            "instagram/search/location query Soweto in probe run probe-20260928T191308Z (8a)",
            ["320191774775153", "137545546357317", "143217589108151", "1491469801081593", "306914259384281"],
        ),
        "ng": (
            "instagram/search/location results reviewed from probe run probe-ig-20260929T190632Z",
            ["104481218205279", "213703378694790", "700978393", "110423998980756", "498032420"],
        ),
        "ke": (
            "instagram/search/location results reviewed from probe run probe-ig-20260929T190632Z",
            ["100485515208886", "110776560676783", "342842389725871", "254991622"],
        ),
    }
    for market, (source, ids) in expected.items():
        locations = markets[market]["instagram_locations"]
        assert locations["source"] == source
        assert locations["values"] == ids
        assert all(type(value) is str for value in locations["values"])


@pytest.mark.parametrize("market", ["za", "ng", "ke"])
def test_every_block_names_its_source(markets, market):
    block = markets[market]
    for key in ["languages", "timezone", *LIST_BLOCKS, "hashtags"]:
        assert block[key]["source"].strip(), f"{market}.{key} has no source"
    for key in LIST_BLOCKS:
        assert block[key]["source"].startswith("engine/configs/")


@pytest.mark.parametrize("market", ["za", "ng", "ke"])
def test_blocks_are_non_empty_string_lists(markets, market):
    block = markets[market]
    for key in LIST_BLOCKS:
        values = block[key]["values"]
        assert values, f"{market}.{key} is empty"
        assert all(isinstance(v, str) and v for v in values)
        assert len(values) == len(set(values)), f"{market}.{key} has duplicates"
    for platform in ["tiktok", "instagram"]:
        tags = block["hashtags"][platform]
        assert tags and all(isinstance(t, str) and t for t in tags)
        assert all(not t.startswith("#") for t in tags)


@pytest.mark.parametrize("market", ["za", "ng", "ke"])
def test_facebook_page_ids_are_numeric(markets, market):
    assert all(v.isdigit() for v in markets[market]["facebook_pages"]["values"])


def test_no_banned_term(raw_text):
    lowered = raw_text.lower()
    hits = [p for p in BANNED_PATTERNS if re.search(p, lowered)]
    assert hits == []


def test_no_dashes(raw_text):
    assert [d for d in DASHES if d in raw_text] == []


def test_test_file_holds_no_banned_literal():
    own = Path(__file__).read_text(encoding="utf-8").lower()
    assert ("".join(["gen", "z"])) not in own
    assert ("".join(["google", "_trends"])) not in own
    assert [d for d in DASHES if d in own] == []
