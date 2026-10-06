"""Checks the draft hub panels in core/config/hubs.yaml (BUILD.md task 0.9)."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
HUBS = ROOT / "core" / "config" / "hubs.yaml"

MARKETS = {"za", "ng", "ke"}
TARGETS = {"x": 8, "culture_desk": 12}
KINDS = {"entertainment", "gossip", "music", "sport", "news"}
PLATFORMS = {"x", "instagram", "tiktok", "facebook", "youtube", "threads"}
FIELDS = {"handle", "platform", "kind", "source"}

# Built by joining at run time so neither this file nor its compiled
# bytecode holds any of the banned words.
AGE_TERMS = [
    "".join(parts)
    for parts in [
        ["gen", "z"],
        ["gen", " z"],
        ["gen", "-z"],
        ["you", "th"],
        ["stu", "dent"],
        ["te", "en"],
        ["mill", "ennial"],
        ["gener", "ation"],
        ["boo", "mer"],
        ["you", "ng"],
        ["var", "sity"],
        ["adoles", "cent"],
    ]
]
DASHES = [chr(0x2014), chr(0x2013), "".join(["-", "-"])]


def _load():
    return yaml.safe_load(HUBS.read_text(encoding="utf-8"))


def _entries(doc):
    for market in MARKETS:
        for list_name in TARGETS:
            for entry in doc["markets"][market][list_name] or []:
                yield market, list_name, entry


def test_top_level_is_a_draft_for_colleagues():
    doc = _load()
    assert doc["status"] == "draft"
    assert doc["note"].strip()


def test_three_markets_each_unconfirmed_with_both_lists():
    doc = _load()
    assert set(doc["markets"]) == MARKETS
    for market in MARKETS:
        block = doc["markets"][market]
        assert "confirmed_by" in block and block["confirmed_by"] is None
        for list_name in TARGETS:
            assert isinstance(block[list_name], list)


def test_list_sizes_or_declared_shortfall():
    doc = _load()
    for market in MARKETS:
        block = doc["markets"][market]
        gaps = block.get("gaps") or {}
        for list_name, target in TARGETS.items():
            have = len(block[list_name])
            assert have <= target, f"{market}.{list_name} has {have}, over {target}"
            missing = (gaps.get(list_name) or {}).get("missing", 0)
            assert missing >= 0
            assert have + missing == target, (
                f"{market}.{list_name}: {have} entries and {missing} declared missing, "
                f"target {target}"
            )
            if missing:
                assert gaps[list_name].get("kinds"), f"{market}.{list_name} gap names no kind"
                assert set(gaps[list_name]["kinds"]) <= KINDS


def test_every_entry_has_the_fields_and_known_values():
    doc = _load()
    for market, list_name, entry in _entries(doc):
        assert FIELDS <= set(entry), f"{market}.{list_name}: {entry}"
        assert entry["kind"] in KINDS, f"{market}: {entry}"
        assert entry["platform"] in PLATFORMS, f"{market}: {entry}"
        if list_name == "x":
            assert entry["platform"] == "x", f"{market}: {entry}"


def test_every_source_exists_and_its_line_holds_the_handle():
    doc = _load()
    for market, _list_name, entry in _entries(doc):
        path_part, _, line_part = entry["source"].rpartition(":")
        assert path_part.startswith("engine/configs/"), entry["source"]
        path = ROOT / path_part
        assert path.is_file(), f"{market}: missing file {path_part}"
        lines = path.read_text(encoding="utf-8").splitlines()
        number = int(line_part)
        assert 1 <= number <= len(lines), f"{market}: {entry['source']} out of range"
        handle = re.escape(str(entry["handle"]))
        pattern = rf"(?<![A-Za-z0-9_.@]){handle}(?![A-Za-z0-9_.])"
        assert re.search(pattern, lines[number - 1]), (
            f"{market}: {entry['handle']} not on {entry['source']}: {lines[number - 1]!r}"
        )


def test_no_duplicate_handle_within_a_market():
    doc = _load()
    for market in MARKETS:
        seen = set()
        for list_name in TARGETS:
            for entry in doc["markets"][market][list_name]:
                key = str(entry["handle"]).lower()
                assert key not in seen, f"{market}: duplicate {entry['handle']}"
                seen.add(key)


def test_no_age_terms_in_hubs_yaml():
    text = HUBS.read_text(encoding="utf-8").lower()
    for term in AGE_TERMS:
        assert term not in text, f"age term {term!r} in hubs.yaml"


def test_no_dashes_in_prose():
    for path in (HUBS, Path(__file__)):
        text = path.read_text(encoding="utf-8")
        for dash in DASHES:
            assert dash not in text, f"{dash!r} in {path.name}"
