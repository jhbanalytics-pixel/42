"""log_forecast tells the model the one wording a promoted forecast can be published in (TRUST.md K9)."""

from core.agent.forecast_promotion import MARKET_NAMES, PERIODS, TEMPLATES, publishable_line
from core.agent.toolset import DESCRIPTIONS, SCHEMAS


def _statement_help():
    return SCHEMAS["log_forecast"]["properties"]["statement"].get("description", "")


def test_statement_field_gives_each_target_wording():
    text = _statement_help()
    for target, template in TEMPLATES.items():
        shown = template.format(label="<item name>", market="<market name>", period="<period>")
        assert shown in text, target


def test_statement_field_names_markets_and_periods():
    text = _statement_help()
    for name in list(MARKET_NAMES.values()) + list(PERIODS.values()):
        assert name in text


def test_tool_description_points_at_the_wording():
    assert "statement" in DESCRIPTIONS["log_forecast"]


def test_statement_written_from_the_field_text_matches_the_published_line():
    import re

    from core.agent.checks import _forecast_phrase
    text = _statement_help()
    row = {"item_id": "it_1", "label": "Amapiano Fridays", "market": "ZA", "target": "persist_50", "horizon": 14}
    shown = re.search(r'persist_50: "([^"]+)"', text).group(1)
    period = re.search(r'"([^"]+)" when horizon_days is 14', text).group(1)
    told = shown.replace("<item name>", "Amapiano Fridays").replace("<market name>", "South Africa") \
        .replace("<period>", period)
    assert _forecast_phrase(told) == _forecast_phrase(publishable_line(row))


def test_periods_are_quoted_without_day_counts():
    text = _statement_help()
    for days, period in PERIODS.items():
        assert f'"{period}" when horizon_days is {days}' in text
        assert f"{period} (" not in text
