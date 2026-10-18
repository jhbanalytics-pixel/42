"""Every document that restates a cap from the SETUP.md caps table states the value in core/config/caps.yaml."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
CAPS = yaml.safe_load((ROOT / "core" / "config" / "caps.yaml").read_text(encoding="utf-8"))


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _fmt(n):
    return f"{n:,}"


ENGINE = CAPS["ENGINE_DAILY"]["total"]
COLLECT = CAPS["ENGINE_DAILY"]["collect"]
MONTH = CAPS["MONTHLY"]["total"]


def test_operations_guide_caps_table_matches_caps_yaml():
    text = _read("docs/guide/operations.md")
    engine = re.search(r"^\| `ENGINE_DAILY` \| ([^|]+)\|", text, re.MULTILINE).group(1)
    month = re.search(r"^\| `MONTHLY` \| ([^|]+)\|", text, re.MULTILINE).group(1)
    assert engine.startswith(f"{_fmt(ENGINE)} credits: collect {_fmt(COLLECT)},"), engine
    assert month.startswith(f"{_fmt(MONTH)} credits"), month


def test_operations_guide_does_not_claim_its_copy_is_tested():
    text = _read("docs/guide/operations.md")
    assert "A test fails if the file drifts from the documented table." not in text


def test_spec_and_engine_restate_the_current_caps():
    for rel in ("docs/full-42/SPEC.md", "docs/full-42/ENGINE.md"):
        text = _read(rel)
        line = next(l for l in text.splitlines() if "One caps table in SETUP.md" in l or "Caps are set in one table in SETUP.md" in l)
        assert f"{_fmt(ENGINE)} credits a day" in line, rel
        assert f"collect {_fmt(COLLECT)}" in line or f"collect's share {_fmt(COLLECT)}" in line, rel
        assert f"month {_fmt(MONTH)}" in line, rel


def test_data_daily_budget_names_the_current_collect_share():
    text = _read("docs/full-42/DATA.md")
    line = next(l for l in text.splitlines() if l.startswith("Daily budget B ="))
    assert f"collect's share of ENGINE_DAILY ({_fmt(COLLECT)} credits, SETUP.md)" in line


def test_setup_monthly_note_does_not_claim_every_share_fits():
    b = CAPS["BUILD_DAILY"]["after_schedule"]
    need = 31 * (ENGINE + CAPS["ASK_DAILY"] + CAPS["VIDEO_DAILY"]["credits"] + CAPS["PULSE_DAILY"] + b)
    row = next(l for l in _read("docs/full-42/SETUP.md").splitlines() if l.startswith("| MONTHLY |"))
    if need > MONTH:
        assert "plus Ask, video, pulse and build at their own caps)" not in row, row
        assert "Ask is throttled" in row.split("|")[2], row
