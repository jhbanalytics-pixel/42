import pytest
from src.analysis.open_intelligence.general_question_context_geo import foreign_local_exclusion

URL = "https://www.reddit.com/r/VirginiaTech/comments/1w7hzi3/labor_day_weekend_rundown_of_local_fun_events/"


def row(text=None):
    return {
        "market": "za",
        "platform": "reddit",
        "url": URL,
        "regional_score": 0,
        "title": "Labor Day Weekend Rundown of Local Fun & Events",
        "text": text or "Virginia Tech events in Blacksburg this weekend include specials.",
    }


def test_corroborated_foreign_local_event_is_excluded_for_local_za_question():
    assert foreign_local_exclusion(
        row(), {"question": "What changed in South African shopping?"}, {"markets": ["za"]}
    )


def test_foreign_identity_alone_and_unknown_geography_are_not_rejected():
    assert not foreign_local_exclusion(
        row("Shopping specials are available."), {"question": "Shopping?"}, {"markets": ["za"]}
    )
    assert not foreign_local_exclusion(
        {**row(), "url": None}, {"question": "Shopping?"}, {"markets": ["za"]}
    )
    assert not foreign_local_exclusion(
        {**row(), "platform": "news"}, {"question": "Shopping?"}, {"markets": ["za"]}
    )


def test_explicit_target_relevance_and_foreign_comparison_survive():
    assert not foreign_local_exclusion(
        row("Virginia Tech local events in Blacksburg compare South African shopping specials."),
        {"question": "South African shopping?"},
        {"markets": ["za"]},
    )
    assert not foreign_local_exclusion(
        row(), {"question": "Compare US and South African shopping specials."}, {"markets": ["za"]}
    )
    assert foreign_local_exclusion(
        row(), {"question": "Tell us about South African shopping specials."}, {"markets": ["za"]}
    )


def test_comparative_followup_uses_validated_plan_and_local_reset_does_not_use_old_history():
    request = {
        "question": "What about the second example?",
        "history": [{"role": "user", "text": "Compare US and South African shopping."}],
    }
    plan = {
        "markets": ["za"],
        "intent": "comparison",
        "requirements": [{"question": "Compare US and South African shopping."}],
    }
    assert not foreign_local_exclusion(row(), request, plan)
    assert foreign_local_exclusion(row(), {"question": request["question"]}, plan)
    assert foreign_local_exclusion(
        row(),
        {
            **request,
            "history": [{"role": "assistant", "text": "Compare US and South African shopping."}],
        },
        plan,
    )
    reset = {
        "question": "Now only discuss South African shopping.",
        "history": [{"role": "user", "content": "Compare US and South African shopping."}],
    }
    assert foreign_local_exclusion(
        row(),
        reset,
        {
            "markets": ["za"],
            "intent": "discovery",
            "requirements": [{"question": "South African shopping only"}],
        },
    )
    assert foreign_local_exclusion(row(), reset, plan)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.reddit.com.evil.test/r/VirginiaTech/",
        "file:///r/VirginiaTech/",
        "https://[broken",
        "https://www.reddit.com/r/VirginiaTechOther/",
    ],
)
def test_unknown_or_spoof_source_identity_is_not_declared_foreign(url):
    assert not foreign_local_exclusion(
        {**row(), "url": url}, {"question": "Shopping?"}, {"markets": ["za"]}
    )


@pytest.mark.parametrize("markets", [["za"], ["ng"], ["ke"], ["ke", "ng", "za"]])
def test_corroborated_us_local_event_is_outside_each_configured_market(markets):
    assert foreign_local_exclusion(row(), {"question": "Local shopping?"}, {"markets": markets})


def test_second_observed_local_subreddit_uses_same_content_corroboration():
    value = {
        **row(),
        "url": "https://www.reddit.com/r/blacksburg/comments/1w7hz7m/labor_day_weekend_rundown_of_local_fun_events/",
    }
    assert foreign_local_exclusion(
        value, {"question": "South African shopping?"}, {"markets": ["za"]}
    )
    assert foreign_local_exclusion(
        row("Virginia Tech local events in Blacksburg cover a South Africanism phrase."),
        {"question": "Local shopping?"},
        {"markets": ["za"]},
    )
