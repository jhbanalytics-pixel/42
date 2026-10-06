"""Unit tests for the PULSE v2 email_render package.

Pure string-rendering tests: each renderer takes a brief dict, a display dict
and a brand dict and returns HTML. The binding contract is keep the art, bind
the data, so the tests assert two things everywhere: real fields appear, and
absent fields are omitted (never invented, never a fabricated quote).
"""

import datetime

import pytest
from src.alerts.email_render.brand import load_brand

# Brand fixtures ----------------------------------------------------------


@pytest.fixture
def ogilvy_brand():
    return load_brand("ogilvy")


@pytest.fixture
def run_date():
    return datetime.date(2026, 5, 29)


# Display fixtures --------------------------------------------------------


@pytest.fixture
def sample_display():
    return {
        "state": {"badge": "Day 4, holding No.1", "direction": "flat"},
        "phase": "Peaking",
        "window": "ride now, ~2 weeks",
        "in_market_pct": 88,
        "channels": [("tiktok", 4), ("threads", 2), ("reddit", 1), ("brand24", 1)],
        "confidence": "Confirmed, 4 channels",
        "search": "rising",
    }


@pytest.fixture
def display_new():
    return {
        "state": {"badge": "New on the board", "direction": "new"},
        "phase": "Emerging",
        "window": "wide, 1 to 2 weeks",
        "in_market_pct": 73,
        "channels": [("instagram", 3), ("tiktok", 3), ("threads", 2), ("reddit", 1)],
        "confidence": "Confirmed, 5 channels",
        "search": "flat",
    }


@pytest.fixture
def display_no_traj():
    # No channels, single-source confidence: the degraded path.
    return {
        "state": {"badge": "Day 2, accelerating", "direction": "up"},
        "phase": "Steady",
        "window": "build, 2 to 3 weeks",
        "in_market_pct": 40,
        "channels": [],
        "confidence": "single-source",
        "search": "no lift",
    }


# Brief fixtures ----------------------------------------------------------


@pytest.fixture
def sample_brief():
    return {
        "topic": "lifestyle_soft_life",
        "market": "za",
        "headline": "The soft-life economy goes mainstream",
        "trend_score": 0.55,
        "status_tag": "Key",
        "trend_synthesis": (
            "Soft life stopped being a flex and became the baseline. Comfort, "
            "ease, self-care over hustle-culture burnout."
        ),
        "cultural_context": (
            "Beauty or wellness brand x a Mzansi lifestyle creator on a "
            "My Soft Life, My Rules reel series. Position it as everyday "
            "self-respect, not aspiration."
        ),
        "sentiment_summary": "positive, steady",
        "b24_sentiment_trajectory": "holding positive over 7 days",
        "platforms": ["tiktok", "threads", "reddit", "brand24"],
        "platform_counts": ["tiktok | 40 items", "threads | 20 items"],
        "top_creators": [
            "@kasi_swenka | tiktok | 7 mentions",
            "@zekhy_m | tiktok | 7 mentions",
        ],
        "visual_anchor": "On The Feed: golden-hour township interior",
        "nano_banana_prompt": (
            "warm golden-hour township interior, single fresh flower, slow "
            "morning light, film grain"
        ),
        "lyria_prompt": "slowed amapiano, 100bpm, soft log-drum, lo-fi haze",
        "social_refs": ["https://example.com/x | tiktok | soft life reel"],
    }


@pytest.fixture
def brief_no_sentiment(sample_brief):
    b = dict(sample_brief)
    b.pop("sentiment_summary", None)
    b.pop("b24_sentiment_trajectory", None)
    return b


@pytest.fixture
def brief_no_comments(sample_brief):
    # The default brief has no comment_sentiment / comment_themes, so this is
    # the live pipeline shape today. Named for clarity in the room tests.
    b = dict(sample_brief)
    b.pop("comment_sentiment", None)
    b.pop("comment_themes", None)
    return b


@pytest.fixture
def brief_with_comments(sample_brief):
    b = dict(sample_brief)
    b["comment_sentiment"] = "warmer than the posts, money anxiety under the aspiration"
    b["comment_themes"] = [
        "anti-burnout 41%",
        "self-worth 28%",
        "cost-of-living tension 19%",
    ]
    return b


@pytest.fixture
def brief_no_headline(sample_brief):
    b = dict(sample_brief)
    b.pop("headline", None)
    return b


@pytest.fixture
def brief_no_creators(sample_brief):
    b = dict(sample_brief)
    b["top_creators"] = []
    return b


@pytest.fixture
def brief_no_nano(sample_brief):
    b = dict(sample_brief)
    b.pop("nano_banana_prompt", None)
    b.pop("lyria_prompt", None)
    return b


@pytest.fixture
def sample_briefs(sample_brief):
    ng = {
        "topic": "economy_sapa_hustle",
        "market": "ng",
        "headline": "Sapa turns being broke into a shared joke",
        "trend_score": 0.38,
        "status_tag": "Rising",
        "trend_synthesis": "Lagos creators are narrating the empty account out loud.",
        "cultural_context": "Fintech or value brand rides the humour with a budget-life creator reel.",
        "sentiment_summary": "mixed, defiant",
        "b24_sentiment_trajectory": "volatile, leaning negative",
        "platforms": ["instagram", "tiktok", "threads", "reddit", "news"],
        "platform_counts": ["instagram | 30 items"],
        "top_creators": ["@sapa.and.economic | tiktok | 5 mentions"],
        "visual_anchor": "On The Feed: Lagos danfo interior",
        "nano_banana_prompt": "Lagos danfo interior, empty wallet held to camera, bright street colour",
        "lyria_prompt": "street-hop, 110bpm, comedic horn stab",
        "social_refs": [],
    }
    ke = {
        "topic": "music_gengetone",
        "market": "ke",
        "headline": "Gengetone's nostalgia revival",
        "trend_score": 0.37,
        "status_tag": "Rising",
        "trend_synthesis": "Two waves at once: nostalgia for the founders and a fresh crop of sub-genres.",
        "cultural_context": "One drop, two eras. Pair an old-school anthem with a new act.",
        "sentiment_summary": "positive",
        "platforms": ["tiktok", "youtube", "apple_music"],
        "platform_counts": ["tiktok | 25 items"],
        "top_creators": ["@.kinanda | tiktok | 3 mentions"],
        "visual_anchor": "On The Feed: Nairobi matatu-art palette",
        "nano_banana_prompt": "Nairobi matatu-art colour palette, split-frame old tape vs new phone screen",
        "lyria_prompt": "gengetone, 105bpm, classic shouty hook over a current trap low-end",
        "social_refs": [],
    }
    return [sample_brief, ng, ke]


@pytest.fixture
def sample_displays(sample_display, display_new):
    ke_display = {
        "state": {"badge": "Day 2, accelerating", "direction": "up"},
        "phase": "Steady",
        "window": "build, 2 to 3 weeks",
        "in_market_pct": 81,
        "channels": [("tiktok", 4), ("youtube", 2), ("apple_music", 1)],
        "confidence": "Confirmed, 3 channels",
        "search": "rising",
    }
    return [sample_display, display_new, ke_display]


# Masthead ----------------------------------------------------------------


def test_masthead_carries_brand_and_four_dot_lockup(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    from src.alerts.email_render.masthead import render_masthead

    html = render_masthead(sample_briefs, sample_displays, ogilvy_brand, run_date)
    assert "PULSE" in html
    # The Google four-dot powered-by lockup renders its four brand colors and
    # the engine credit. The shift line + counts moved to the ticker / KPI cards.
    for color in ("#4285F4", "#EA4335", "#FBBC05", "#34A853"):
        assert f'bgcolor="{color}"' in html
    assert "Powered by Gemini on Vertex" in html
    assert "Ogilvy x Google intelligence" in html


def test_masthead_counts_markets_in_briefs_not_brand_config(sample_briefs):
    # The market count moved from the masthead provenance line to the KPI cards,
    # but the count helper stays in masthead.py as the single source of truth.
    # With briefs from only two markets the helper must read 2, not the brand's 3.
    from src.alerts.email_render.masthead import _count_markets

    two_market_briefs = [b for b in sample_briefs if b.get("market") in {"za", "ng"}]
    assert len({b["market"] for b in two_market_briefs}) == 2
    assert _count_markets(two_market_briefs) == 2
    assert _count_markets(sample_briefs) == 3


def test_masthead_wordmark_is_outlook_safe(sample_briefs, sample_displays, ogilvy_brand, run_date):
    """Regression: the wordmark must not use a sub-1.0 line-height.

    Outlook's Word engine renders line-height < 1 by overflowing the glyph
    upward, so the 58px wordmark bled into the lockup row above it (a recipient
    saw it "overlaid with the top banner"). The line box must be pinned with an
    exact rule, and the top gap must come from padding (Outlook ignores margin).
    """
    import re

    from src.alerts.email_render.masthead import render_masthead

    html = render_masthead(sample_briefs, sample_displays, ogilvy_brand, run_date)
    # No sub-1.0 line-height anywhere in the masthead (the overflow trigger).
    assert not re.search(r"line-height:0\.[0-9]", html)
    # The wordmark line box is pinned so Word reserves the full glyph height.
    assert "mso-line-height-rule:exactly" in html


# Ticker (replaces the lede; the standfirst is absorbed into hero + KPI + board)


def test_ticker_renders_shift_clauses(sample_briefs, sample_displays, ogilvy_brand):
    from src.alerts.email_render.ticker import render_ticker

    html = render_ticker(sample_briefs, sample_displays, ogilvy_brand)
    assert "Today's shift" in html
    # the shift clauses are built from the real short topic labels, so Sapa appears
    assert "Sapa" in html


def test_ticker_empty_when_no_briefs(ogilvy_brand):
    from src.alerts.email_render.ticker import render_ticker

    assert render_ticker([], [], ogilvy_brand) == ""


# Card --------------------------------------------------------------------


def test_card_renders_bound_values_no_invention(sample_brief, sample_display, ogilvy_brand):
    from src.alerts.email_render.card import render_card

    html = render_card(sample_brief, sample_display, ogilvy_brand)
    assert sample_brief["headline"] in html
    assert sample_display["state"]["badge"] in html
    assert sample_display["phase"] in html
    # the synthesis (the read) is bound
    assert "soft life" in html.lower()


def test_card_omits_mood_when_no_sentiment(brief_no_sentiment, display_no_traj, ogilvy_brand):
    from src.alerts.email_render.card import render_card

    html = render_card(brief_no_sentiment, display_no_traj, ogilvy_brand)
    assert "MOOD" not in html  # degrade gracefully, do not invent


def test_card_omits_seen_when_no_channels(brief_no_sentiment, display_no_traj, ogilvy_brand):
    from src.alerts.email_render.card import render_card

    html = render_card(brief_no_sentiment, display_no_traj, ogilvy_brand)
    assert "Seen on" not in html  # no platform_counts -> no bars block


def test_card_falls_back_to_humanized_slug_without_headline(
    brief_no_headline, sample_display, ogilvy_brand
):
    from src.alerts.email_render.card import render_card

    html = render_card(brief_no_headline, sample_display, ogilvy_brand)
    # humanize_topic('lifestyle_soft_life') -> 'Soft life'
    assert "Soft life" in html


def test_card_omits_drivers_when_no_creators(brief_no_creators, sample_display, ogilvy_brand):
    from src.alerts.email_render.card import render_card

    html = render_card(brief_no_creators, sample_display, ogilvy_brand)
    assert "Who is driving it" not in html


def test_card_renders_creator_handles(sample_brief, sample_display, ogilvy_brand):
    from src.alerts.email_render.card import render_card

    html = render_card(sample_brief, sample_display, ogilvy_brand)
    assert "@kasi_swenka" in html
    assert "7 mentions" in html


# The room (comment slot) -------------------------------------------------


def test_room_drops_entirely_without_comment_data(brief_no_comments, sample_display, ogilvy_brand):
    """Absent comment fields drop the slot (the binding rule). The caged
    "lands next week" promise is retired: it went stale once the producer
    flipped live, and on a recovery send it misread as a regression."""
    from src.alerts.email_render.card import render_card

    html = render_card(brief_no_comments, sample_display, ogilvy_brand)
    assert "What the room says" not in html
    assert "lands next week" not in html.lower()
    assert "NOT LIVE" not in html
    assert "<q>" not in html  # never fabricate a quote


def test_room_never_fabricates_quotes_even_with_themes(
    brief_with_comments, sample_display, ogilvy_brand
):
    from src.alerts.email_render.card import render_card

    html = render_card(brief_with_comments, sample_display, ogilvy_brand)
    # comment_sentiment + comment_themes are real, so the room renders them,
    # but the pipeline stores no quote strings so still no <q> tag is emitted
    assert "<q>" not in html
    assert "anti-burnout 41%" in html


# The hashtags slot (driving the conversation) ---------------------------


def test_hashtags_drops_entirely_without_data(sample_brief, sample_display, ogilvy_brand):
    """An absent driving_hashtags list drops the slot, mirroring the room."""
    from src.alerts.email_render.card import render_card

    b = dict(sample_brief)
    b.pop("driving_hashtags", None)
    html = render_card(b, sample_display, ogilvy_brand)
    assert "Driving the conversation" not in html
    assert "lands next week" not in html.lower()
    assert "NOT LIVE" not in html


def test_hashtags_render_live_tags_with_mood_and_share(sample_brief, sample_display, ogilvy_brand):
    from src.alerts.email_render.card import render_card

    b = dict(sample_brief)
    b["driving_hashtags"] = [
        {"tag": "#stokvel", "posts": 50, "share_pct": 60.0, "mood": "practical and supportive"},
        {"tag": "#sidehustle", "posts": 30, "share_pct": 25.0, "mood": "ambitious"},
    ]
    html = render_card(b, sample_display, ogilvy_brand)
    assert "#stokvel" in html
    assert "practical and supportive" in html
    assert "60% of tagged posts" in html
    assert "#sidehustle" in html
    assert "hashtag intelligence lands" not in html.lower()  # not caged when live


def test_hashtags_slot_is_dash_clean(sample_brief, sample_display, ogilvy_brand):
    from src.alerts.email_render.card import render_card

    b = dict(sample_brief)
    b["driving_hashtags"] = [
        {"tag": "#kasi", "posts": 10, "share_pct": 100.0, "mood": "proud kasi pride"},
    ]
    html = render_card(b, sample_display, ogilvy_brand)
    assert "#kasi" in html
    assert "proud kasi pride" in html
    # The digest auditor hard-fails on em/en dashes; the new slot must not add one.
    assert chr(0x2014) not in html
    assert chr(0x2013) not in html


# The kit -----------------------------------------------------------------


def test_kit_always_carries_exclusion(sample_brief, sample_display, ogilvy_brand):
    from src.alerts.email_render.kit import render_kit

    html = render_kit(sample_brief, sample_display, ogilvy_brand)
    assert "exclude" in html.lower()


def test_kit_renders_move_from_cultural_context(sample_brief, sample_display, ogilvy_brand):
    from src.alerts.email_render.kit import render_kit

    html = render_kit(sample_brief, sample_display, ogilvy_brand)
    assert "My Soft Life" in html  # the activation is the Move


def test_kit_omits_make_when_no_nano_prompt(brief_no_nano, sample_display, ogilvy_brand):
    from src.alerts.email_render.kit import render_kit

    html = render_kit(brief_no_nano, sample_display, ogilvy_brand)
    assert "Nano Banana" not in html
    assert "Lyria" not in html
    # but the Move (cultural_context) is always present
    assert "My Soft Life" in html


def test_kit_copy_has_no_verbatim_word(sample_brief, sample_display, ogilvy_brand):
    from src.alerts.email_render.kit import render_kit

    html = render_kit(sample_brief, sample_display, ogilvy_brand)
    assert "verbatim" not in html.lower()


def test_make_row_exclusion_appears_once(sample_brief, sample_display, ogilvy_brand):
    """The brand-safe exclusion list rides in the Nano Banana prompt; the Kit
    note must not re-list it (that printed the list twice). The marker phrase
    appears exactly once in the Make it cell.
    """
    from src.alerts.email_render.kit import render_kit

    b = dict(sample_brief)
    b["nano_banana_prompt"] = (
        "Photo-realistic vertical market scene. Created with Gemini. Exclude, as "
        "hard rules the model must not render: no brand logos, no competitor "
        "names or marks, no real-person likeness, no recognisable team kits, no "
        "real fonts or wordmarks."
    )
    html = render_kit(b, sample_display, ogilvy_brand)
    assert html.lower().count("no brand logos") == 1


def test_make_row_appends_exclusion_when_model_dropped_it(
    sample_brief, sample_display, ogilvy_brand
):
    """Brand-safety guarantee: a generated prompt missing the exclusion clause
    still ships brand-safe, the Kit appends the canonical clause.
    """
    from src.alerts.email_render.kit import render_kit

    b = dict(sample_brief)
    b["nano_banana_prompt"] = "Photo-realistic vertical market scene. Created with Gemini."
    html = render_kit(b, sample_display, ogilvy_brand)
    assert "no brand logos" in html.lower()


# Board (replaces radar; carries all ranks) -------------------------------


def test_board_renders_rows_from_briefs(sample_briefs, sample_displays, ogilvy_brand):
    from src.alerts.email_render.board import render_board

    html = render_board(sample_briefs, sample_displays, ogilvy_brand)
    assert "The board" in html
    assert "Gengetone" in html  # a real headline appears as a board row


def test_board_empty_when_no_briefs(ogilvy_brand):
    from src.alerts.email_render.board import render_board

    assert render_board([], [], ogilvy_brand) == ""


# Footer ------------------------------------------------------------------


def test_footer_lists_channels_from_real_counts(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    from src.alerts.email_render.footer import render_footer

    html = render_footer(sample_briefs, sample_displays, ogilvy_brand, run_date)
    assert "TikTok" in html  # platform name appears
    assert "WPP Open" in html or "Ogilvy" in html


# Guard -------------------------------------------------------------------


def test_guard_raises_on_unsourced_value():
    from src.alerts.email_render._guard import UnsourcedValueError, assert_sourced

    with pytest.raises(UnsourcedValueError):
        assert_sourced(None, "headline")
    with pytest.raises(UnsourcedValueError):
        assert_sourced("   ", "sentiment_summary")


def test_guard_passes_real_values():
    from src.alerts.email_render._guard import assert_sourced

    assert assert_sourced("hello", "x") == "hello"
    assert assert_sourced(0, "n") == 0  # 0 is real data, not absence


# Compose: render_pulse_html ---------------------------------------------


def test_render_pulse_html_full_email(sample_briefs, sample_displays, ogilvy_brand, run_date):
    from src.alerts.email_render import render_pulse_html

    html = render_pulse_html(sample_briefs, sample_displays, ogilvy_brand, run_date)
    assert "PULSE" in html
    assert "Today's shift" in html.replace("TODAY'S SHIFT", "Today's shift")
    # exactly one style block, the shared STYLE
    assert html.count("<style") == 1
    # the lead brief is the hero
    assert "Trend of the day" in html
    # a move card and a radar row both render
    assert "Today's moves" in html.replace("TODAY'S MOVES", "Today's moves")


def test_render_pulse_html_no_dashes_in_copy(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    from src.alerts.email_render import render_pulse_html

    html = render_pulse_html(sample_briefs, sample_displays, ogilvy_brand, run_date)
    assert "—" not in html
    assert "–" not in html  # noqa: RUF001  guarding against en dash


def test_render_pulse_html_strips_model_en_dash_from_brief_copy(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    """Real Gemini briefs in BigQuery carry dashes (a date range comes back
    with an en dash). The render-side sanitizer must strip them so the email
    is dash-free regardless of model output, not only when the prompt asked
    the model nicely.
    """
    from src.alerts.email_render import render_pulse_html

    em_dash = chr(0x2014)
    en_dash = chr(0x2013)
    # mutate the lead brief's synthesis to carry a real en dash date range
    dirty = [dict(b) for b in sample_briefs]
    dirty[0] = dict(dirty[0])
    dirty[0]["trend_synthesis"] = f"The soft-life run holds across the 2026{en_dash}2027 season."

    html = render_pulse_html(dirty, sample_displays, ogilvy_brand, run_date)
    assert em_dash not in html
    assert en_dash not in html
    # the surrounding copy still renders, just dash-free
    assert "2026" in html
    assert "2027" in html


def test_render_pulse_html_never_fabricates_quotes(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    from src.alerts.email_render import render_pulse_html

    html = render_pulse_html(sample_briefs, sample_displays, ogilvy_brand, run_date)
    # none of the sample briefs carry comment quotes, so no <q> anywhere
    assert "<q>" not in html


def test_render_pulse_html_second_brand_skins(sample_briefs, sample_displays, run_date):
    from src.alerts.email_render import render_pulse_html

    lumo = render_pulse_html(sample_briefs, sample_displays, load_brand("demo_lumo"), run_date)
    assert "LUMO" in lumo
    assert "PULSE" not in lumo


def test_render_pulse_html_empty_briefs_is_safe(ogilvy_brand, run_date):
    from src.alerts.email_render import render_pulse_html

    html = render_pulse_html([], [], ogilvy_brand, run_date)
    assert "PULSE" in html  # masthead still renders
    assert html.count("<style") == 1


# Desk verdict (daily_summary wired into PULSE) ---------------------------

_SAMPLE_SUMMARY = {
    "through_line": "Young Nigerians are turning the hustle into entertainment.",
    "summary_text": "Across the three markets, money pressure is fusing with cultural joy.",
    "call_to_action": "Brief creators to film the come-up, not the arrival.",
}


def test_render_verdict_surfaces_all_three_fields():
    from src.alerts.email_render.verdict import render_verdict

    html = render_verdict(_SAMPLE_SUMMARY)
    assert "The desk verdict" in html
    assert "turning the hustle into entertainment" in html
    assert "money pressure is fusing" in html
    assert "The move this week" in html
    assert "film the come-up" in html


def test_render_verdict_empty_when_no_summary_text():
    from src.alerts.email_render.verdict import render_verdict

    assert render_verdict(None) == ""
    assert render_verdict({}) == ""
    assert render_verdict({"through_line": "x", "summary_text": ""}) == ""


def test_render_verdict_strips_dashes():
    from src.alerts.email_render.verdict import render_verdict

    em_dash = chr(0x2014)
    en_dash = chr(0x2013)
    html = render_verdict(
        {
            "through_line": f"Hustle{em_dash}driven creativity is the throughline.",
            "summary_text": f"The 2026{en_dash}2027 wave is cross-market.",
            "call_to_action": f"Move now{em_dash}the window is short.",
        }
    )
    assert em_dash not in html
    assert en_dash not in html
    assert "throughline" in html


def test_render_verdict_humanizes_raw_topic_refs():
    from src.alerts.email_render.verdict import render_verdict

    html = render_verdict(
        {
            "through_line": "Sport is the unifier.",
            "summary_text": "Momentum is real this week.",
            "call_to_action": "Ride za/sports_rugby and ng/music_afrobeats now.",
        }
    )
    assert "za/sports_rugby" not in html
    assert "ng/music_afrobeats" not in html
    assert "Rugby" in html


def test_render_verdict_strips_raw_velocity_metric():
    from src.alerts.email_render.verdict import render_verdict

    html = render_verdict(
        {
            "through_line": "Sport unifies the markets.",
            "summary_text": "Momentum is real this week.",
            "call_to_action": "Ride the Rugby momentum (velocity +0.94) with grassroots content.",
        }
    )
    assert "velocity +0.94" not in html
    assert "Rugby momentum" in html


def test_ticker_dedups_same_label():
    from src.alerts.email_render.ticker import render_ticker

    briefs = [
        {"market": "ng", "topic": "diaspora_japa", "query_group": "diaspora_japa"},
        {"market": "ke", "topic": "economy_hustle", "query_group": "economy_hustle"},
        {"market": "ke", "topic": "economy_hustle", "query_group": "economy_hustle"},
    ]
    displays = [{"state": {"direction": d}} for d in ("flat", "up", "up")]
    line = render_ticker(briefs, displays, load_brand("ogilvy"))
    # The shift clause for a repeated label appears once, not twice.
    assert line.count("Hustle") == 1


def test_parse_creator_drops_numeric_user_id():
    from src.alerts.email_render._util import parse_creator

    assert parse_creator("@71446739959 | instagram | 2 mentions") is None
    assert parse_creator("526986282 | instagram | 1 mentions") is None
    kept = parse_creator("@254gang | tiktok | 3 mentions")
    assert kept is not None
    assert kept["handle"] == "@254gang"


def test_render_pulse_html_includes_verdict_when_summary_supplied(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    from src.alerts.email_render import render_pulse_html

    html = render_pulse_html(
        sample_briefs, sample_displays, ogilvy_brand, run_date, daily_summary=_SAMPLE_SUMMARY
    )
    assert "The desk verdict" in html
    assert "turning the hustle into entertainment" in html
    assert "The move this week" in html
    assert "—" not in html
    assert "–" not in html  # noqa: RUF001


def test_render_pulse_html_omits_verdict_without_summary(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    from src.alerts.email_render import render_pulse_html

    html = render_pulse_html(sample_briefs, sample_displays, ogilvy_brand, run_date)
    assert "The desk verdict" not in html


# Inbox summary (Task 12 stub) -------------------------------------------


def test_render_inbox_summary_is_table_based(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    from src.alerts.email_render import render_inbox_summary

    html = render_inbox_summary(
        sample_briefs, sample_displays, ogilvy_brand, run_date, full_url="https://example.com/full"
    )
    assert "<table" in html
    assert "https://example.com/full" in html
    assert "<style" not in html  # bulletproof: inline styles only


def test_render_inbox_summary_text_matches_path_a_shape(sample_briefs, sample_displays, run_date):
    from src.alerts.email_render import render_inbox_summary_text

    ds = {
        "through_line": "Rugby is carrying the week.",
        "summary_text": "Defiant optimism across markets.",
    }
    text = render_inbox_summary_text(
        sample_briefs,
        sample_displays,
        run_date,
        full_url="https://example.com/full",
        daily_summary=ds,
    )
    assert "Top trends today:" in text
    assert "Open the full read: https://example.com/full" in text
    assert "Rugby is carrying the week." in text


def test_render_inbox_summary_includes_desk_verdict(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    """The inbox teaser leads with the day's verdict (the line Jo opens for),
    web-safe only: the through_line + summary_text render in Georgia/Arial with
    no mono label, and the refs/internal metrics are cleaned the same way the
    rich verdict cleans them."""
    from src.alerts.email_render import render_inbox_summary

    ds = {
        "through_line": "za/sports_rugby is carrying the week (velocity +0.94).",
        "summary_text": "Across the three markets the mood is defiant optimism.",
        "call_to_action": "Ride it now.",
    }
    html = render_inbox_summary(
        sample_briefs,
        sample_displays,
        ogilvy_brand,
        run_date,
        full_url="https://example.com/full",
        daily_summary=ds,
    )
    assert "defiant optimism" in html  # summary_text rendered
    assert "The move this week: Ride it now." in html
    assert "za/sports_rugby" not in html  # raw ref humanised
    assert "velocity +0.94" not in html  # internal metric stripped
    assert "Courier" not in html  # no mono label in the inbox teaser


def test_render_inbox_summary_omits_verdict_when_absent(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    """No daily_summary -> the teaser still renders the headlines and the
    full-read button, no crash, no empty verdict furniture."""
    from src.alerts.email_render import render_inbox_summary

    html = render_inbox_summary(
        sample_briefs,
        sample_displays,
        ogilvy_brand,
        run_date,
        full_url="https://example.com/full",
        daily_summary=None,
    )
    assert "Open the full read" in html
    assert "<table" in html


# Hidden preheader / inbox preview text -----------------------------------


def test_render_inbox_summary_emits_preheader_from_through_line(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    """The inbox teaser carries the through_line as a hidden preheader span so
    Gmail / Apple Mail scrape the verdict for the inbox snippet, not the date."""
    from src.alerts.email_render import render_inbox_summary

    ds = {
        "through_line": "Young Nigerians are turning the hustle into entertainment.",
        "summary_text": "Money pressure is fusing with cultural joy.",
        "call_to_action": "Brief creators to film the come-up.",
    }
    html = render_inbox_summary(
        sample_briefs,
        sample_displays,
        ogilvy_brand,
        run_date,
        full_url="https://example.com/full",
        daily_summary=ds,
    )
    assert "display:none;font-size:0;line-height:0;max-height:0;mso-hide:all" in html
    assert "turning the hustle into entertainment" in html
    # zero-width non-joiner run guards against body-copy bleed into the preview.
    assert chr(0x200C) in html


def test_render_inbox_summary_preheader_is_esc_applied(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    """The through_line is Gemini text, so the preheader must be esc-wrapped: a
    raw angle bracket arrives as the &lt; / &gt; entities, never as live markup."""
    from src.alerts.email_render import render_inbox_summary

    ds = {
        "through_line": "Hustle <b>culture</b> & joy collide.",
        "summary_text": "x",
        "call_to_action": "y",
    }
    html = render_inbox_summary(
        sample_briefs,
        sample_displays,
        ogilvy_brand,
        run_date,
        full_url="https://example.com/full",
        daily_summary=ds,
    )
    assert "Hustle &lt;b&gt;culture&lt;/b&gt; &amp; joy collide." in html
    assert "<b>culture</b>" not in html


def test_render_inbox_summary_omits_preheader_when_through_line_empty(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    """No through_line -> no preheader span at all (no empty hidden element)."""
    from src.alerts.email_render import render_inbox_summary

    no_tl = render_inbox_summary(
        sample_briefs,
        sample_displays,
        ogilvy_brand,
        run_date,
        full_url="https://example.com/full",
        daily_summary={"through_line": "", "summary_text": "still here"},
    )
    none_ds = render_inbox_summary(
        sample_briefs,
        sample_displays,
        ogilvy_brand,
        run_date,
        full_url="https://example.com/full",
        daily_summary=None,
    )
    assert "mso-hide:all;overflow:hidden;opacity:0" not in no_tl
    assert "mso-hide:all;overflow:hidden;opacity:0" not in none_ds


def test_render_pulse_html_emits_preheader_from_through_line(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    """The full editorial email carries the same hidden preheader after <body>."""
    from src.alerts.email_render import render_pulse_html

    html = render_pulse_html(
        sample_briefs, sample_displays, ogilvy_brand, run_date, daily_summary=_SAMPLE_SUMMARY
    )
    assert "display:none;font-size:0;line-height:0;max-height:0;mso-hide:all" in html
    assert "turning the hustle into entertainment" in html
    assert chr(0x200C) in html


def test_render_pulse_html_omits_preheader_without_summary(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    from src.alerts.email_render import render_pulse_html

    html = render_pulse_html(sample_briefs, sample_displays, ogilvy_brand, run_date)
    assert "mso-hide:all;overflow:hidden;opacity:0" not in html


# Dark-mode CTA contrast --------------------------------------------------


def test_inbox_cta_fill_carries_lp_cta_not_lp_ab(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    """The inbox CTA fill carries lp-cta, not lp-ab. lp-ab flips to the dark
    band accent #3a63c4 in dark mode; the CTA opts out of that flip via lp-cta,
    which has no dark override, so the fill stays #2f5fd0 where white text is
    5.72:1 (AA). The light bgcolor is #2f5fd0."""
    from src.alerts.email_render import render_inbox_summary

    html = render_inbox_summary(
        sample_briefs, sample_displays, ogilvy_brand, run_date, full_url="https://example.com/full"
    )
    # The CTA td carries the opt-out fill class and the light accent bgcolor.
    assert '<td class="lp-cta" bgcolor="#2f5fd0"' in html
    # The CTA must not ride the hero band class that flips dark.
    assert '<td class="lp-ab"' not in html


def test_dark_style_block_does_not_flip_lp_cta(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    """lp-cta is absent from every dark override, so the CTA fill never flips to
    the dark accent. lp-ab (the hero band) stays in the dark block, untouched."""
    from src.alerts.email_render._style import STYLE

    assert "lp-cta" not in STYLE
    # The hero band override is still present, now the darker AA-passing fill.
    assert ".lp-ab{background-color:#3a63c4!important;}" in STYLE


# v3 (19 Jun 2026): the shift line moved to the ticker and dropped all flag
# emoji (judge blocker: inconsistent flag rendering). The ticker names topics
# via short labels and hides on mobile.

_KE_FLAG = "\U0001f1f0\U0001f1ea"
_NG_FLAG = "\U0001f1f3\U0001f1ec"
_ZA_FLAG = "\U0001f1ff\U0001f1e6"


def test_ticker_hidden_on_mobile_and_no_flag_emoji():
    from src.alerts.email_render.ticker import render_ticker

    briefs = [
        {"market": "ke", "topic": "economy_hustle"},
        {"market": "za", "topic": "genz_lifestyle"},
        {"market": "ng", "topic": "diaspora_japa"},
    ]
    displays = [
        {"state": {"direction": "up"}, "confidence": "5 channels agree"},
        {"state": {"direction": "down"}},
        {"state": {"direction": "up"}},
    ]
    line = render_ticker(briefs, displays, load_brand("ogilvy"))
    # the strip hides below 600px (the only nowrap width-floor element)
    assert "lp-hide-sm" in line
    assert "mso-hide:all" in line
    # no flag emoji of any market
    assert _KE_FLAG not in line
    assert _ZA_FLAG not in line
    assert _NG_FLAG not in line
    # the "N channels agree" lead binds the hero confidence
    assert "5 channels agree" in line


# Round-11 (30 May 2026): the first PULSE render off real briefs leaked raw
# topic slugs into the prose. Gemini wrote `The "economy_hustle" topic in Kenya
# is currently trending` and the slug rode clean_copy into the body. clean_copy
# now humanises any topic slug left in brief text.


def test_clean_copy_humanises_quoted_topic_slug():
    from src.alerts.email_render._util import clean_copy

    out = clean_copy('The "economy_hustle" topic in Kenya is currently trending.')
    assert "economy_hustle" not in out
    assert "Hustle" in out


def test_clean_copy_humanises_bare_topic_slug():
    from src.alerts.email_render._util import clean_copy

    assert "fashion_ankara_asoebi" not in clean_copy("Around fashion_ankara_asoebi today.")


def test_clean_copy_humanises_multi_segment_slug():
    from src.alerts.email_render._util import clean_copy

    out = clean_copy("The economy_sapa_hustle topic is loud today.")
    assert "economy_sapa_hustle" not in out
    assert "Sapa hustle" in out


def test_clean_copy_leaves_normal_prose_untouched():
    from src.alerts.email_render._util import clean_copy

    src = "Amapiano keeps the local feed warm this week."
    assert clean_copy(src) == src


def test_clean_copy_neutralises_banned_buzzwords():
    """Gemini copy that reaches for a banned house-style buzzword is rewritten in
    place (the 22-Jun digest carried leverage/seamless/elevate). Case is kept."""
    from src.alerts.email_render._util import clean_copy

    assert clean_copy("creators leverage advanced prompts") == "creators use advanced prompts"
    assert clean_copy("the conversation seamlessly blends") == "the conversation smoothly blends"
    assert clean_copy("Leveraging the trend") == "Using the trend"
    assert (
        clean_copy("a robust signal that elevates the brand")
        == "a strong signal that lifts the brand"
    )
    # an ordinary word that merely contains a buzzword substring is untouched.
    assert clean_copy("financial independence") == "financial independence"


def test_clean_copy_keeps_handles_and_urls_intact():
    from src.alerts.email_render._util import clean_copy

    out = clean_copy("Creator @scholah_meeme posts from https://x.com/a_b_c daily.")
    assert "@scholah_meeme" in out
    assert "a_b_c" in out


def test_clean_copy_humanises_any_category_slug():
    # The prefix allowlist missed education_matric_nsfas on the 30 May render;
    # the generic matcher must catch any lowercase word_word token.
    from src.alerts.email_render._util import clean_copy

    assert "education_matric_nsfas" not in clean_copy("The education_matric_nsfas topic.")
    assert "infra_power_eskom" not in clean_copy("Watching infra_power_eskom closely.")


def test_clean_copy_leaves_screaming_case_untouched():
    from src.alerts.email_render._util import clean_copy

    # An acronym-style token in caps is not a topic slug.
    assert "PAY_AS_YOU_GO" in clean_copy("The PAY_AS_YOU_GO plan is popular.")


def test_clean_copy_keeps_non_category_compound_whole():
    # A non-category underscored compound must be humanised WHOLE. The broad
    # de-slug regex still catches it (no leak), but the prefix is only dropped
    # for a real taxonomy category, so cost_of_living keeps all three words
    # instead of collapsing to "Living". Guards the over-match the prefix-drop
    # used to cause; a prefix allowlist on the regex was tried and reverted
    # because it under-matched and leaked education_matric_nsfas (30 May).
    from src.alerts.email_render._util import clean_copy, humanize_topic

    out = clean_copy("The cost_of_living squeeze is real.")
    assert "cost_of_living" not in out
    assert "Cost of living" in out
    # Real category prefixes still drop, so the headline fallback is unchanged.
    assert humanize_topic("cost_of_living") == "Cost of living"
    assert humanize_topic("economy_sapa_hustle") == "Sapa hustle"
    assert humanize_topic("music_amapiano") == "Amapiano"


def test_clean_copy_keeps_underscore_hashtag_intact():
    # An underscore hashtag is a client-facing tag, not a topic slug. The de-slug
    # regex must not eat the segment after the hash, so #money_moves survives.
    from src.alerts.email_render._util import clean_copy

    assert clean_copy("#money_moves") == "#money_moves"
    assert "#money_moves" in clean_copy("Ride the #money_moves wave this week.")


def test_clean_copy_dash_replacement_leaves_no_space_before_comma():
    # A spaced em or en dash must collapse to a clean ", " with no orphaned
    # space before the comma.
    from src.alerts.email_render._util import clean_copy

    assert clean_copy("ease " + chr(0x2014) + " softness") == "ease, softness"
    assert clean_copy("ease " + chr(0x2013) + " softness") == "ease, softness"
    assert " ," not in clean_copy("a " + chr(0x2014) + " b " + chr(0x2013) + " c")


def test_clean_copy_scrubs_double_hyphen():
    # A Gemini "--" reaches the email through clean_copy alone for several text
    # fields, and the auditor hard-fails on double hyphens, so clean_copy is the
    # last-mile guard for all three dash forms.
    from src.alerts.email_render._util import clean_copy

    assert "--" not in clean_copy("ease -- softness")
    assert clean_copy("ease -- softness") == "ease, softness"


# Round-11: the MOOD chip carried the whole sentiment sentence and overflowed
# the chip row. mood_label returns one word; the glyph stays.


# Live 2026-06-09 sentiment_summary strings the old first-substring scan mislabelled.
_LIVE_FINANCE_STOKVEL_SUMMARY = (
    "The sentiment is generally positive and constructive, with a neutral tone on "
    "technical credit score discussions, and no significant negative risk."
)
_LIVE_GENZ_LIFESTYLE_SUMMARY = (
    "The overall sentiment is highly positive and celebratory, with a neutral baseline "
    "for daily lifestyle discussions."
)


def test_mood_label_returns_single_word():
    from src.alerts.email_render._util import mood_label

    assert (
        mood_label("The average tone score of 0.49 indicates a largely neutral read") == "Neutral"
    )
    assert mood_label("The sentiment is predominantly positive") == "Positive"
    assert mood_label("slightly negative to neutral sentiment") == "Negative"
    assert mood_label("") == ""
    # Negation-aware: "no significant negative risk" must not read Negative.
    assert mood_label(_LIVE_FINANCE_STOKVEL_SUMMARY) == "Positive"
    # Position-aware: "highly positive ... neutral baseline" resolves to the head word.
    assert mood_label(_LIVE_GENZ_LIFESTYLE_SUMMARY) == "Positive"
    assert mood_label("no negative reaction, mostly positive") == "Positive"


def test_card_mood_chip_is_not_a_full_sentence(sample_briefs, sample_displays, ogilvy_brand):
    from src.alerts.email_render.card import render_card

    brief = dict(sample_briefs[0])
    brief["sentiment_summary"] = (
        "The average tone score of 0.49 indicates a largely neutral to slightly "
        "positive sentiment, reflecting the observational nature of the content."
    )
    html = render_card(brief, sample_displays[0], ogilvy_brand)
    assert "average tone score" not in html  # the sentence never reaches the chip


# Round-11: every radar row dumped the full two-paragraph synthesis. The row
# must carry one trimmed line.


def test_board_compact_row_trims_synthesis_to_one_line():
    from src.alerts.email_render.board import render_board

    long_synth = (
        "First sentence that sets the scene for the trend in plain language. "
        "Second sentence with a lot more detail that should never reach the row. "
        "Third sentence piling on even more text well past any reasonable cap."
    )
    briefs = [
        {
            "market": "ke",
            "topic": "music_gengetone",
            "headline": "Gengetone",
            "trend_synthesis": long_synth,
            "trend_score": 0.4,
        }
    ]
    displays = [{"state": {"direction": "down"}}]
    # start_rank 5 makes this a compact (rank >= 4) one-line index row.
    html = render_board(briefs, displays, load_brand("ogilvy"), start_rank=5)
    assert "First sentence that sets the scene for the trend in plain language." in html
    assert "Second sentence" not in html


# Round-12 (30 May 2026): the four stakeholder-driven differentiators were
# computed but never rendered, and no test asserted they reached the HTML, so
# 87 green tests hid four missing headline features. These positive render
# assertions are the gate that stops that recurring.


def test_card_renders_local_filter_chip(sample_brief, display_new, ogilvy_brand):
    from src.alerts.email_render.card import render_card

    html = render_card(sample_brief, display_new, ogilvy_brand)
    assert "LOCAL" in html
    assert f"{display_new['in_market_pct']}%" in html


def test_card_renders_search_only_on_rising(sample_brief, display_new, ogilvy_brand):
    from src.alerts.email_render.card import render_card

    # A real rising read earns the chip.
    d = dict(display_new)
    d["search"] = "rising"
    html = render_card(sample_brief, d, ogilvy_brand)
    assert "SEARCH" in html
    assert "rising" in html


def test_card_omits_search_when_flat_or_no_lift(sample_brief, display_new, ogilvy_brand):
    from src.alerts.email_render.card import render_card

    # "flat" (measured, no movement) and "no lift" (no data) are both omitted:
    # search is a signal only when it says something.
    for value in ("flat", "no lift", ""):
        d = dict(display_new)
        d["search"] = value
        assert "SEARCH" not in render_card(sample_brief, d, ogilvy_brand)


def test_card_confidence_never_says_confirmed(sample_brief, ogilvy_brand):
    from src.alerts.email_render.card import render_card

    display = {"state": {"direction": "up"}, "confidence": "5 channels agree"}
    html = render_card(sample_brief, display, ogilvy_brand)
    assert "Confirmed" not in html
    assert "5 channels agree" in html


def test_kit_renders_risk_line_when_flags_present(sample_brief, display_new, ogilvy_brand):
    from src.alerts.email_render.kit import render_kit

    brief = dict(sample_brief)
    brief["risk_flags"] = ["category overlaps a competitor, check before live"]
    html = render_kit(brief, display_new, ogilvy_brand)
    assert "Check" in html
    assert "overlaps a competitor" in html


def test_kit_omits_risk_line_when_no_flags(sample_brief, display_new, ogilvy_brand):
    from src.alerts.email_render.kit import render_kit

    brief = dict(sample_brief)
    brief["risk_flags"] = []
    html = render_kit(brief, display_new, ogilvy_brand)
    assert ">Check<" not in html


def test_card_foot_proof_links_to_real_evidence(sample_brief, display_new, ogilvy_brand):
    from src.alerts.email_render.card import render_card

    html = render_card(sample_brief, display_new, ogilvy_brand)
    assert 'href="#"' not in html
    assert "https://example.com/x" in html


def test_card_foot_omits_proof_when_no_refs(sample_brief, display_new, ogilvy_brand):
    from src.alerts.email_render.card import render_card

    brief = dict(sample_brief)
    brief["social_refs"] = []
    html = render_card(brief, display_new, ogilvy_brand)
    assert 'href="#"' not in html
    assert "Proof" not in html


def test_hero_leads_with_read_not_a_repeated_headline(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    """The hero leads with the read (no h1), so it does not reprint the top
    headline. The headline shows in the board rank-01 row and the strongest-
    confirmation line (both deliberate at-a-glance mentions), never as a stacked
    hero h1 (the 30 May double-print bug)."""
    from src.alerts.email_render import render_hero, render_pulse_html

    html = render_pulse_html(sample_briefs, sample_displays, ogilvy_brand, run_date)
    assert "<h1>" not in html
    # the hero block itself does not carry the headline string (it leads with the read)
    hero_html = render_hero(sample_briefs[0], sample_displays[0], ogilvy_brand)
    assert sample_briefs[0]["headline"] not in hero_html


def test_footer_credits_the_engine(sample_brief, display_new, ogilvy_brand):
    import datetime

    from src.alerts.email_render.footer import render_footer

    html = render_footer([sample_brief], [display_new], ogilvy_brand, datetime.date(2026, 5, 30))
    assert "Powered by Gemini on Vertex" in html


# Round-13: complete the Kit anatomy (Say it + Where) and surface the b24
# 7-day trajectory and the real search read.


def test_kit_renders_where_from_platforms(sample_brief, display_new, ogilvy_brand):
    from src.alerts.email_render.kit import render_kit

    html = render_kit(sample_brief, display_new, ogilvy_brand)
    assert "Where" in html
    assert "TikTok" in html


def test_kit_renders_say_it_from_visual_anchor(sample_brief, display_new, ogilvy_brand):
    from src.alerts.email_render.kit import render_kit

    html = render_kit(sample_brief, display_new, ogilvy_brand)
    assert "Say it" in html
    # the literal "On The Feed:" prefix is stripped from the anchor
    assert "On The Feed:" not in html.split("Say it", 1)[1][:120]


def test_kit_omits_where_when_no_platforms(sample_brief, display_new, ogilvy_brand):
    from src.alerts.email_render.kit import render_kit

    brief = dict(sample_brief)
    brief["platforms"] = []
    html = render_kit(brief, display_new, ogilvy_brand)
    assert ">Where<" not in html


def test_kit_where_keeps_longform_youtube_distinct_from_shorts(
    sample_brief, display_new, ogilvy_brand
):
    # Long-form YouTube and YouTube Shorts are distinct surfaces. Plain "youtube"
    # must read "YouTube", not relabel the whole trend onto Shorts.
    from src.alerts.email_render.kit import render_kit

    brief = dict(sample_brief)
    brief["platforms"] = ["youtube"]
    where = render_kit(brief, display_new, ogilvy_brand).split("Where", 1)[1][:300]
    assert "YouTube" in where
    assert "Shorts" not in where

    brief["platforms"] = ["youtube shorts"]
    where = render_kit(brief, display_new, ogilvy_brand).split("Where", 1)[1][:300]
    assert "YouTube Shorts" in where


def test_card_renders_7day_trajectory_chip(sample_brief, display_new, ogilvy_brand):
    from src.alerts.email_render.card import render_card

    # sample_brief carries "holding positive over 7 days"
    html = render_card(sample_brief, display_new, ogilvy_brand)
    assert "MARKET 7-DAY" in html
    assert "Holding Positive" in html


def test_card_omits_trajectory_when_absent(brief_no_sentiment, display_new, ogilvy_brand):
    from src.alerts.email_render.card import render_card

    html = render_card(brief_no_sentiment, display_new, ogilvy_brand)
    assert "MARKET 7-DAY" not in html


def test_hero_renders_market_7day_trajectory_chip(sample_brief, display_new, ogilvy_brand):
    # The hero (lead brief) renders the SAME market-level trajectory chip as the
    # move cards; its label must match ("MARKET 7-DAY"), not the old "7-DAY", or a
    # single email shows two different labels for the identical signal.
    from src.alerts.email_render import render_hero

    html = render_hero(sample_brief, display_new, ogilvy_brand)
    assert "MARKET 7-DAY" in html


def test_trajectory_label_title_cases_raw_fallback():
    # A bare value with no direction/tone word falls through the compact path.
    # The fallback must still title-case so the chip never renders lowercase.
    from src.alerts.email_render.card import _trajectory_label

    assert _trajectory_label({"b24_sentiment_trajectory": "stable"}) == "Stable"
    # A direction/tone word routes through the compact path, which was already
    # title-cased before this fix.
    assert _trajectory_label(
        {"b24_sentiment_trajectory": "holding positive over 7 days"}
    ).startswith("Holding")
    # A non-direction/non-tone phrase hits the raw fallback; it must title-case now.
    assert _trajectory_label({"b24_sentiment_trajectory": "improving sentiment"}).startswith(
        "Improving"
    )
    assert _trajectory_label({"b24_sentiment_trajectory": ""}) == ""


def test_trajectory_label_renders_the_producer_contract():
    # Regression, 17 Aug 2026. _b24_sentiment_trajectory_for_market emits exactly
    # four values: "stable", "improving (+Npp positive share)", "declining
    # (-Npp positive share)" and "". The renderer hunted for direction words
    # ("rising", "holding", "falling"...) that the producer NEVER emits, then fell
    # through to a tone word and matched the "positive" inside "positive share".
    # So a market getting WORSE rendered as "Positive", identical to one getting
    # better, on all 24 briefs every day. The direction is the whole signal.
    from src.alerts.email_render.card import _trajectory_label

    declining = _trajectory_label({"b24_sentiment_trajectory": "declining (-6pp positive share)"})
    improving = _trajectory_label({"b24_sentiment_trajectory": "improving (+8pp positive share)"})

    # The bug in one assertion: these two must never render the same string.
    assert declining != improving

    # Direction leads, and "positive share" is a unit, not a mood.
    assert declining.startswith("Declining")
    assert improving.startswith("Improving")
    assert "Positive" not in declining
    assert "Positive" not in improving

    # The magnitude the producer computed survives to the chip.
    assert "6pp" in declining
    assert "8pp" in improving

    # Double-digit and zero-pad shapes from the same f-string.
    assert _trajectory_label(
        {"b24_sentiment_trajectory": "declining (-12pp positive share)"}
    ).startswith("Declining")
    assert "12pp" in _trajectory_label(
        {"b24_sentiment_trajectory": "declining (-12pp positive share)"}
    )

    # The flat case carries no magnitude and must stay a bare word.
    assert _trajectory_label({"b24_sentiment_trajectory": "stable"}) == "Stable"


# Round-12 polish (30 May 2026): perfect the render before go-live.


def test_every_taxonomy_category_drops_its_prefix():
    # Every category in configs/topic_groups must map, so a headline never shows
    # a redundant prefix ("Transport matatu") or a raw "Genz". The flagline
    # category is the title; the headline is the remainder.
    from src.alerts.email_render._util import category_label, humanize_topic

    cases = {
        "genz_lifestyle": ("Culture", "Lifestyle"),
        "genz_sheng": ("Culture", "Sheng"),
        "transport_matatu": ("Transport", "Matatu"),
        "finance_stokvel": ("Finance", "Stokvel"),
        "culture_owambe": ("Culture", "Owambe"),
        "education_matric_nsfas": ("Education", "Matric nsfas"),
        "infra_power_eskom": ("Infrastructure", "Power eskom"),
    }
    for slug, (cat, head) in cases.items():
        assert category_label(slug) == cat, slug
        assert humanize_topic(slug) == head, slug


def test_platform_labels_are_clean():
    from src.alerts.email_render._util import _platform_label

    assert _platform_label("google_search") == "Google Search"
    assert _platform_label("linkedin") == "LinkedIn"
    # Unmapped underscore slug still renders spaced, not "Some_New_Source".
    assert _platform_label("some_new_source") == "Some New Source"


def test_parse_creator_drops_raw_youtube_channel_id():
    from src.alerts.email_render._util import parse_creator

    # An unresolved YouTube channel ID (UC + 22 chars) is noise, not a handle.
    assert parse_creator("@ucvco8xhpe3imwkxab0aaj4w | youtube | 2 mentions") is None
    assert parse_creator("UCvco8xHPe3iMWkXAb0AaJ4w | youtube | 2 mentions") is None
    # A real handle that merely starts with "uc" is kept.
    assert parse_creator("@uncle_waffles | tiktok | 9 mentions") is not None


def test_channel_count_matches_footer_and_masthead(sample_briefs):
    # The KPI channels card, the masthead count helper, and the footer channel
    # list must all count the same platform_counts keys (one source of truth).
    from src.alerts.email_render.footer import _channel_totals
    from src.alerts.email_render.masthead import _count_channels

    expected = len(_channel_totals(sample_briefs))
    assert expected > 0
    assert _count_channels(sample_briefs) == expected


# Email-client safety (31 May 2026): the v2 email shipped as a web page (flex +
# grid + a head <style> block), so Outlook on Windows (Word engine, no flex/grid,
# strips head <style>) collapsed it to unstyled stacked text. The layer was
# rebuilt to tables + inline styles. These tests are the guard that it stays
# email-safe: no flex, no grid, table layout, dark cells carry bgcolor, web-safe
# fonts inline, and the design survives the <style> block being deleted.

_CONTENT_MARKERS = (
    "PULSE",  # masthead wordmark
    "The board",  # board section divider
    "Trend of the day",  # hero band + board rank 01
    "The kit",  # kit header (hero)
    "READ THIS MORNING",  # footer channel line
    "Powered by Gemini on Vertex",  # engine credit
)


def test_render_pulse_html_has_no_flex_or_grid(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    from src.alerts.email_render import render_pulse_html

    html = render_pulse_html(
        sample_briefs, sample_displays, ogilvy_brand, run_date, daily_summary=_SAMPLE_SUMMARY
    )
    # The hard invariant: Outlook supports neither, so neither may appear.
    assert "display:flex" not in html
    assert "display:grid" not in html


def test_render_pulse_html_is_table_based(sample_briefs, sample_displays, ogilvy_brand, run_date):
    from src.alerts.email_render import render_pulse_html

    html = render_pulse_html(sample_briefs, sample_displays, ogilvy_brand, run_date)
    # Layout is presentation tables, lots of them, all marked role=presentation.
    assert "<table" in html
    assert 'role="presentation"' in html
    assert html.count("<table") > 10


def test_render_pulse_html_light_base_carries_bgcolor(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    from src.alerts.email_render import render_pulse_html

    html = render_pulse_html(sample_briefs, sample_displays, ogilvy_brand, run_date)
    # The inline base is now LIGHT (the paper skin). Outlook needs the bgcolor
    # ATTRIBUTE (it ignores the stripped stylesheet), so the canvas / hero / kit /
    # footer cells must set the LIGHT value in both the attribute form and the
    # inline background-color form.
    assert 'bgcolor="#f3efe6"' in html  # the light paper canvas
    assert "background-color:#f3efe6" in html
    # the blue accent hero band sets its on-paper accent bgcolor
    assert 'bgcolor="#2f5fd0"' in html
    # the dark midnight canvas value lives only in the @media override block,
    # never as an inline bgcolor attribute on the light base.
    assert 'bgcolor="#1b1714"' not in html


def test_render_pulse_html_has_dark_mode_overrides(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    """The <style> block carries the prefers-color-scheme dark override and flips

    the page-bg class to the midnight canvas. This is the layer Apple Mail and any
    query-honouring client use to switch the light inline base to dark.
    """
    from src.alerts.email_render import render_pulse_html

    html = render_pulse_html(sample_briefs, sample_displays, ogilvy_brand, run_date)
    assert "@media (prefers-color-scheme: dark)" in html
    # the page-bg class flips to the midnight canvas with !important so it beats
    # the inline light base
    assert ".lp-bg{background-color:#1b1714" in html
    # the accent class flips to the brighter on-dark accent
    assert ".lp-accent{color:#5e87f0" in html


def test_render_pulse_html_has_ogsc_overrides(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    """Outlook.com strips the media query but honours [data-ogsc] (foreground) and

    [data-ogsb] (background); both override sets must be present so Outlook.com
    dark mode flips the light base too.
    """
    from src.alerts.email_render import render_pulse_html

    html = render_pulse_html(sample_briefs, sample_displays, ogilvy_brand, run_date)
    assert "[data-ogsc] .lp-ink{color:#f4f1ea" in html
    assert "[data-ogsb] .lp-bg{background-color:#1b1714" in html


def test_render_pulse_html_light_base_survives_style_deletion(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    """Delete the head <style> block (what Outlook does) and the email must still

    read as the LIGHT paper base: the light canvas bgcolor + inline color are on
    the elements, not only in the stripped block, and no dark midnight value is
    left behind as an inline attribute.
    """
    import re

    from src.alerts.email_render import render_pulse_html

    html = render_pulse_html(
        sample_briefs, sample_displays, ogilvy_brand, run_date, daily_summary=_SAMPLE_SUMMARY
    )
    stripped = re.sub(r"<style>.*?</style>", "", html, flags=re.DOTALL)
    assert "<style" not in stripped
    # the light base reads correctly with the block gone
    assert 'bgcolor="#f3efe6"' in stripped  # light canvas inline
    assert "color:#2b2722" in stripped  # primary ink text inline
    # the dark midnight canvas value was only ever in the deleted block, so the
    # remaining body carries none of it inline (the dark page canvas never leaks
    # onto an element as an inline color). The dark header band keeps its own
    # fixed on-dark text tones in both themes, so those are expected to remain.
    assert "#1b1714" not in stripped


def test_render_pulse_html_web_safe_fonts_are_inline(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    from src.alerts.email_render import render_pulse_html

    html = render_pulse_html(sample_briefs, sample_displays, ogilvy_brand, run_date)
    # The web-safe stacks must be inline on elements, not only in the optional
    # <style> block, so the email reads when Outlook deletes that block. The v3
    # stacks lead with Newsreader / Hanken but keep Georgia + Segoe UI + Arial as
    # first-class fallbacks.
    assert "Georgia" in html  # serif fallback
    assert "Arial" in html  # body fallback
    assert "Segoe UI" in html  # sans fallback
    # and they appear inside style="..." attributes, not just the <style> tag
    body_only = html.split("</style>", 1)[-1]
    assert "Georgia" in body_only
    assert "Arial" in body_only


def test_render_pulse_html_survives_style_block_deletion(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    """The acid test: delete the head <style> block (what Outlook does) and the

    email must still be table-based, dash-free, and carry every content marker.
    Nothing load-bearing may live only in <style>.
    """
    import re

    from src.alerts.email_render import render_pulse_html

    html = render_pulse_html(
        sample_briefs, sample_displays, ogilvy_brand, run_date, daily_summary=_SAMPLE_SUMMARY
    )
    stripped = re.sub(r"<style>.*?</style>", "", html, flags=re.DOTALL)
    assert "<style" not in stripped
    assert "display:flex" not in stripped
    assert "display:grid" not in stripped
    assert "<table" in stripped
    for marker in _CONTENT_MARKERS:
        assert marker in stripped, marker
    # editorial rules still hold with the block gone
    assert "—" not in stripped
    assert "–" not in stripped  # noqa: RUF001  guarding against en dash


def test_render_pulse_html_markers_present_with_sample_fixtures(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    from src.alerts.email_render import render_pulse_html

    html = render_pulse_html(
        sample_briefs, sample_displays, ogilvy_brand, run_date, daily_summary=_SAMPLE_SUMMARY
    )
    for marker in _CONTENT_MARKERS:
        assert marker in html, marker
    # the drivers block renders off the sample fixtures; the room renders
    # only when a brief carries comment data (covered by the room tests)
    assert "Who is driving it" in html
    assert "Seen on" in html


def test_render_pulse_html_empty_briefs_is_email_safe(ogilvy_brand, run_date):
    from src.alerts.email_render import render_pulse_html

    html = render_pulse_html([], [], ogilvy_brand, run_date)
    # With no briefs the masthead still renders, still table-based, still
    # no flex/grid, exactly one optional style block. The ticker / KPI / board /
    # hero / moves all omit cleanly.
    assert "display:flex" not in html
    assert "display:grid" not in html
    assert "<table" in html
    assert html.count("<style") == 1
    assert "PULSE" in html  # masthead
    assert "The board" not in html  # board omitted on a dead day
    assert 'bgcolor="#f3efe6"' in html  # the light canvas cell


def test_render_inbox_summary_has_no_flex_or_grid(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    from src.alerts.email_render import render_inbox_summary

    html = render_inbox_summary(
        sample_briefs, sample_displays, ogilvy_brand, run_date, full_url="https://example.com/full"
    )
    assert "display:flex" not in html
    assert "display:grid" not in html
    assert "<style" not in html  # bulletproof: no style block at all
    assert "Georgia" in html  # web-safe serif inline


def test_mood_glyph_matches_resolved_label():
    """The MOOD chip glyph color must agree with the printed label. mood_glyph
    used to first-substring-match _MOOD_GLYPHS (positive first in dict order)
    while mood_label is negation- and position-aware, so a hedged sentence
    rendered a green positive dot beside the word Neutral. The glyph is now
    derived from the resolved label so they cannot disagree."""
    from src.alerts.email_render._util import _MOOD_GLYPHS, mood_glyph, mood_label

    for sentence in (
        "largely neutral to slightly positive sentiment",
        "no significant negative shift, broadly neutral",
    ):
        label = mood_label(sentence)
        assert label == "Neutral"
        assert mood_glyph(sentence) == _MOOD_GLYPHS[label.lower()]


# ---------------------------------------------------------------------------
# PULSE v3 redesign (19 Jun 2026): the board, KPI cards, pip strips, the NOT
# MEASURED footnote, the source-volume footer total, ACT BY, MSO ghosts, Gmail
# clip. The trust backbone (no per-trend mention count, one shared denominator)
# is load-bearing, so these are the gate that the redesign kept it.
# ---------------------------------------------------------------------------


def _board_brief(market="ke", topic="music_gengetone", headline="Gengetone", score=0.5):
    return {
        "market": market,
        "topic": topic,
        "headline": headline,
        "trend_score": score,
        "trend_synthesis": "A scene that became a baseline this week.",
        "sentiment_summary": "positive",
        "driving_hashtags": [{"tag": "#gengetone", "share_pct": 55.0}],
        "platform_counts": ["tiktok | 40 items", "youtube | 10 items"],
    }


def test_board_rich_rows_render_pip_strip(ogilvy_brand):
    from src.alerts.email_render.board import render_board

    briefs = [_board_brief(headline="One"), _board_brief(headline="Two", score=0.4)]
    displays = [
        {"state": {"direction": "up"}, "confidence": "7 channels agree"},
        {"state": {"direction": "flat"}, "confidence": "single-source"},
    ]
    html = render_board(briefs, displays, ogilvy_brand)
    # rank 01: 7 filled pips out of D, labelled "7 / D channels"
    assert "7 / 12 channels" in html
    # single-source row reads 1 pip out of D, never hot/corroborated
    assert "1 / 12 channels" in html


def test_board_pip_denominator_matches_channel_count(ogilvy_brand):
    from src.alerts.email_render.board import render_board
    from src.alerts.email_render.kpi import channel_denominator
    from src.alerts.email_render.masthead import _count_channels

    briefs = [_board_brief(headline="One")]
    displays = [{"state": {"direction": "up"}, "confidence": "2 channels agree"}]
    d = channel_denominator(briefs)
    assert d == max(12, _count_channels(briefs))
    html = render_board(briefs, displays, ogilvy_brand)
    # the pip "/ D" denominator and the footnote "out of D checked" agree
    assert f"/ {d} channels" in html
    assert f"out of {d} checked" in html


def test_board_long_tail_has_score_on_every_row_no_dash_filler(ogilvy_brand):
    from src.alerts.email_render.board import render_board

    briefs = [_board_brief(headline=f"Row {i}", score=0.5 - i * 0.02) for i in range(8)]
    displays = [
        {"state": {"direction": "flat"}, "confidence": "3 channels agree"} for _ in range(8)
    ]
    html = render_board(briefs, displays, ogilvy_brand)
    # ranks 04+ each carry a two-decimal score; no dash glyph used as filler
    assert "0.44" in html  # row index 3 (rank 04) score
    assert chr(0x2014) not in html
    assert chr(0x2013) not in html
    assert ">&mdash;<" not in html


def test_board_no_per_trend_mention_count(ogilvy_brand):
    from src.alerts.email_render.board import render_board

    briefs = [_board_brief(headline=f"Row {i}") for i in range(6)]
    displays = [{"state": {"direction": "up"}, "confidence": "4 channels agree"} for _ in range(6)]
    html = render_board(briefs, displays, ogilvy_brand)
    # No row prints a per-trend "N mentions" total (the hard contract rule).
    import re

    assert not re.search(r"\d+\s+mentions", html)


def test_not_measured_footnote_present_and_dash_free(ogilvy_brand):
    from src.alerts.email_render.board import render_board

    briefs = [_board_brief(headline="One")]
    displays = [{"state": {"direction": "up"}, "confidence": "5 channels agree"}]
    html = render_board(briefs, displays, ogilvy_brand)
    assert "A per-trend mention total is" in html
    assert "not measured" in html
    assert "so it is never shown" in html
    assert chr(0x2014) not in html
    assert chr(0x2013) not in html


def test_kpi_cards_bind_real_counts(sample_briefs, sample_displays, ogilvy_brand):
    from src.alerts.email_render.kpi import channel_denominator, render_kpi
    from src.alerts.email_render.masthead import _count_markets, _count_posts

    html = render_kpi(sample_briefs, sample_displays, ogilvy_brand)
    assert str(channel_denominator(sample_briefs)) in html
    assert str(_count_markets(sample_briefs)) in html
    assert f"{_count_posts(sample_briefs):,}" in html
    assert str(len(sample_briefs)) in html
    assert "Channels" in html
    assert "Trends tracked" in html


def test_kpi_omitted_when_no_briefs(ogilvy_brand):
    from src.alerts.email_render.kpi import render_kpi

    assert render_kpi([], [], ogilvy_brand) == ""


def test_footer_volume_sums_to_post_topic_reads(sample_briefs, sample_displays, ogilvy_brand):
    import datetime

    from src.alerts.email_render.footer import _channel_totals, render_footer
    from src.alerts.email_render.masthead import _count_posts

    # The visible per-platform list must sum exactly to the post-topic-reads total.
    total_from_list = sum(n for _, n in _channel_totals(sample_briefs))
    assert total_from_list == _count_posts(sample_briefs)
    html = render_footer(sample_briefs, sample_displays, ogilvy_brand, datetime.date(2026, 6, 18))
    assert f"{_count_posts(sample_briefs):,} post-topic reads" in html


def test_act_by_chip_replaces_window(sample_brief, sample_display, ogilvy_brand, run_date):
    from src.alerts.email_render import render_pulse_html

    html = render_pulse_html([sample_brief], [sample_display], ogilvy_brand, run_date)
    # The forward-horizon chip reads ACT BY; WINDOW only appears in the method
    # line (the 14-day lookback), never as a chip label.
    assert "ACT BY" in html
    assert ">WINDOW<" not in html


def test_render_pulse_html_has_mso_ghost_tables(
    sample_briefs, sample_displays, ogilvy_brand, run_date
):
    from src.alerts.email_render import render_pulse_html

    html = render_pulse_html(
        sample_briefs, sample_displays, ogilvy_brand, run_date, daily_summary=_SAMPLE_SUMMARY
    )
    # The outer 600 wrapper and the KPI four-across row both wrap in MSO ghosts.
    assert "<!--[if mso]>" in html
    assert 'width="600"' in html
    assert 'width="552"' in html  # the KPI ghost row


def test_render_pulse_html_under_gmail_clip(sample_briefs, sample_displays, ogilvy_brand, run_date):
    from src.alerts.email_digest import _guard_inline_body_size
    from src.alerts.email_render import render_pulse_html

    # A representative render (full board, hero, moves, verdict). When the rich
    # body ships inline (no hosted full read), the send path runs it through
    # _guard_inline_body_size first, so the body that actually goes out is what
    # must clear the Gmail 102,400-byte clip threshold. A heavy 24-brief render
    # can sit right at the edge of that limit; the inline guard is the production
    # safety net, so this test asserts the guarded body the way the send path does.
    big_briefs = []
    big_displays = []
    by_topic = {}
    for i in range(24):
        b = dict(sample_briefs[i % len(sample_briefs)])
        b["headline"] = f"Signal headline number {i} carrying a realistic length"
        b["trend_score"] = 0.6 - i * 0.01
        b["topic"] = f"{b['topic']}_{i}"
        big_briefs.append(b)
        big_displays.append(sample_displays[i % len(sample_displays)])
        by_topic[(b["market"], b["topic"])] = {**b, "display": big_displays[-1]}
    html = render_pulse_html(
        big_briefs, big_displays, ogilvy_brand, run_date, daily_summary=_SAMPLE_SUMMARY
    )
    guarded = _guard_inline_body_size(html, by_topic, run_date, _SAMPLE_SUMMARY, ceiling=102400)
    assert len(guarded.encode("utf-8")) < 102400, len(guarded.encode("utf-8"))


# Wave 1 (19 Jun 2026): two dark-on-ship EmailRender features built against the
# locked Wave 1 contract field names. Both are defensive: they read additive
# fields the engine does not write yet and stay byte-identical to today until
# the field is present AND the flag is on. The continuity badge reads
# continuity_state / continuity_day (CONTINUITY_BADGES_ENABLED); the platform
# heat-map aggregates the existing per-brief platform_counts
# (PLATFORM_HEATMAP_ENABLED). These tests are the gate that proves the no-op-off
# / render-on contract.


@pytest.fixture
def brief_with_continuity(sample_brief):
    b = dict(sample_brief)
    b["continuity_state"] = "day3plus"
    b["continuity_day"] = 5
    return b


# --- Continuity badge (card.py) ------------------------------------------


def test_continuity_badge_noop_when_flag_off_even_with_field(
    brief_with_continuity, sample_display, ogilvy_brand, monkeypatch
):
    """Today's state is the field present but the flag off (the engine may write
    continuity_state before the flag flips). The badge must not render: a
    flag-off card is byte-identical to one with no continuity field at all."""
    from src.alerts.email_render.card import render_card

    monkeypatch.delenv("CONTINUITY_BADGES_ENABLED", raising=False)
    with_field = render_card(brief_with_continuity, sample_display, ogilvy_brand)
    without_field = render_card(
        {k: v for k, v in brief_with_continuity.items() if not k.startswith("continuity_")},
        sample_display,
        ogilvy_brand,
    )
    assert "Day 5" not in with_field
    assert "Rebounding" not in with_field
    # No continuity text leaks; the flagline is identical with or without the field.
    assert with_field == without_field


def test_continuity_badge_noop_when_flag_on_but_field_absent(
    brief_no_comments, sample_display, ogilvy_brand, monkeypatch
):
    """Flag on, no field (the live state on day one of the flip, before the
    engine starts writing it). The badge no-ops: nothing renders, no crash."""
    from src.alerts.email_render.card import render_card

    monkeypatch.setenv("CONTINUITY_BADGES_ENABLED", "true")
    b = {k: v for k, v in brief_no_comments.items() if not k.startswith("continuity_")}
    html = render_card(b, sample_display, ogilvy_brand)
    assert "New today" not in html
    assert "Day 2" not in html
    assert "Rebounding" not in html
    # the rest of the card still renders
    assert b["headline"] in html


def test_continuity_badge_renders_day_count_when_present(
    brief_with_continuity, sample_display, ogilvy_brand, monkeypatch
):
    """Flag on + field present: day3plus with continuity_day 5 reads the real
    running count, not a flat 'Day 3+'."""
    from src.alerts.email_render.card import render_card

    monkeypatch.setenv("CONTINUITY_BADGES_ENABLED", "1")
    html = render_card(brief_with_continuity, sample_display, ogilvy_brand)
    assert "Day 5" in html


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("new", "New today"),
        ("day2", "Day 2"),
        ("day3plus", "Day 3+"),
        ("rebounding", "Rebounding"),
    ],
)
def test_continuity_badge_renders_each_locked_state(
    sample_brief, sample_display, ogilvy_brand, monkeypatch, state, expected
):
    """Every value in the locked contract set renders its label. day3plus with
    no continuity_day falls back to the static 'Day 3+'."""
    from src.alerts.email_render.card import render_card

    monkeypatch.setenv("CONTINUITY_BADGES_ENABLED", "true")
    b = dict(sample_brief)
    b["continuity_state"] = state
    html = render_card(b, sample_display, ogilvy_brand)
    assert expected in html


def test_continuity_badge_unknown_state_no_ops(
    sample_brief, sample_display, ogilvy_brand, monkeypatch
):
    """A value outside the locked set drops the badge (defensive against a
    future engine writing an unmapped state), even with the flag on."""
    from src.alerts.email_render.card import render_card

    monkeypatch.setenv("CONTINUITY_BADGES_ENABLED", "true")
    b = dict(sample_brief)
    b["continuity_state"] = "garbage_state"
    html = render_card(b, sample_display, ogilvy_brand)
    assert "garbage_state" not in html


def test_continuity_badge_day3plus_ignores_bad_day(
    sample_brief, sample_display, ogilvy_brand, monkeypatch
):
    """continuity_day that is not a positive int (None, 0, a bool, a string)
    falls back to the static 'Day 3+' rather than printing junk."""
    from src.alerts.email_render.card import render_card

    monkeypatch.setenv("CONTINUITY_BADGES_ENABLED", "true")
    for bad in (None, 0, True, "5"):
        b = dict(sample_brief)
        b["continuity_state"] = "day3plus"
        b["continuity_day"] = bad
        html = render_card(b, sample_display, ogilvy_brand)
        assert "Day 3+" in html
        assert "Day True" not in html


def test_continuity_badge_is_dash_clean(
    brief_with_continuity, sample_display, ogilvy_brand, monkeypatch
):
    """The badge must not introduce an em or en dash (the auditor hard-fails)."""
    from src.alerts.email_render.card import render_card

    monkeypatch.setenv("CONTINUITY_BADGES_ENABLED", "true")
    html = render_card(brief_with_continuity, sample_display, ogilvy_brand)
    assert chr(0x2014) not in html
    assert chr(0x2013) not in html


def test_continuity_badge_on_hero_when_present(
    brief_with_continuity, sample_display, ogilvy_brand, monkeypatch
):
    """The hero (lead brief) carries the same continuity badge as the move cards
    so a single email never shows the signal on a move card but not the lead."""
    from src.alerts.email_render import render_hero

    monkeypatch.setenv("CONTINUITY_BADGES_ENABLED", "true")
    b = dict(brief_with_continuity)
    b["continuity_state"] = "rebounding"
    html = render_hero(b, sample_display, ogilvy_brand)
    assert "Rebounding" in html


def test_full_email_continuity_byte_identical_when_off(
    sample_briefs, sample_displays, ogilvy_brand, run_date, monkeypatch
):
    """The whole email is byte-identical with the flag off whether or not a
    brief carries continuity_state. This is the dark-on-ship guarantee."""
    from src.alerts.email_render import render_pulse_html

    monkeypatch.delenv("CONTINUITY_BADGES_ENABLED", raising=False)
    plain = render_pulse_html(sample_briefs, sample_displays, ogilvy_brand, run_date)
    tagged = [dict(b) for b in sample_briefs]
    tagged[0]["continuity_state"] = "rebounding"
    tagged[0]["continuity_day"] = 7
    tagged[1]["continuity_state"] = "day2"
    with_field = render_pulse_html(tagged, sample_displays, ogilvy_brand, run_date)
    assert plain == with_field


# --- Platform heat-map (heatmap.py) --------------------------------------


def test_heatmap_noop_when_flag_off(sample_briefs, ogilvy_brand, monkeypatch):
    """Dark on ship: flag off returns no section even though every sample brief
    carries platform_counts."""
    from src.alerts.email_render.heatmap import render_heatmap

    monkeypatch.delenv("PLATFORM_HEATMAP_ENABLED", raising=False)
    assert render_heatmap(sample_briefs, ogilvy_brand) == ""


def test_heatmap_noop_when_flag_on_but_no_counts(ogilvy_brand, monkeypatch):
    """Flag on but no usable platform_counts (a degraded day) renders nothing,
    not empty furniture."""
    from src.alerts.email_render.heatmap import render_heatmap

    monkeypatch.setenv("PLATFORM_HEATMAP_ENABLED", "true")
    briefs = [{"market": "za", "topic": "music_amapiano"}]  # no platform_counts
    assert render_heatmap(briefs, ogilvy_brand) == ""
    # a present-but-malformed field also yields nothing
    assert render_heatmap([{"platform_counts": ["tiktok", "no-number"]}], ogilvy_brand) == ""


def test_heatmap_aggregates_counts_across_briefs(monkeypatch):
    """The rollup sums each platform's items across all briefs, not per brief."""
    from src.alerts.email_render.heatmap import _aggregate_platform_counts

    monkeypatch.setenv("PLATFORM_HEATMAP_ENABLED", "true")
    briefs = [
        {"platform_counts": ["tiktok | 40 items", "threads | 20 items"]},
        {"platform_counts": ["tiktok | 10 items", "reddit | 5 items"]},
    ]
    pairs = dict(_aggregate_platform_counts(briefs))
    assert pairs["tiktok"] == 50  # 40 + 10 summed across briefs
    assert pairs["threads"] == 20
    assert pairs["reddit"] == 5


def test_heatmap_orders_by_total_descending(monkeypatch):
    """The ranked rows lead with the platform that drove the most volume."""
    from src.alerts.email_render.heatmap import _aggregate_platform_counts

    briefs = [
        {"platform_counts": ["threads | 20 items", "tiktok | 40 items", "reddit | 40 items"]},
    ]
    pairs = _aggregate_platform_counts(briefs)
    # tiktok and reddit tie at 40; tie broken by slug, so reddit precedes tiktok,
    # and threads (20) is last.
    assert [p for p, _ in pairs] == ["reddit", "tiktok", "threads"]


def test_heatmap_shows_every_live_channel_including_the_smallest(ogilvy_brand, monkeypatch):
    """A channel we pay to ingest must reach the render, not fall off the cap.

    This section is the ONLY reader of platform mix in the digest, so a channel
    ranked below the cap is dead ingestion. These are the real 27 Jul 2026 brief
    totals with facebook at its measured first-day volume: under the old cap of
    six, instagram and facebook both fell off the bottom.
    """
    from src.alerts.email_render.heatmap import render_heatmap

    monkeypatch.setenv("PLATFORM_HEATMAP_ENABLED", "true")
    briefs = [
        {
            "platform_counts": [
                "tiktok | 1098 items",
                "youtube | 987 items",
                "web | 172 items",
                "reddit | 113 items",
                "google_search | 98 items",
                "threads | 93 items",
                "apple_music | 91 items",
                "facebook | 48 items",
                "instagram | 45 items",
            ]
        }
    ]
    html = render_heatmap(briefs, ogilvy_brand)
    for label in ("TikTok", "YouTube", "Reddit", "Threads", "Facebook", "Instagram"):
        assert label in html, f"{label} missing from the heat map"


def test_heatmap_renders_section_with_real_platforms(sample_briefs, ogilvy_brand, monkeypatch):
    """Flag on + real counts: the section renders the platform names and the
    eyebrow, with the share computed from the stored counts."""
    from src.alerts.email_render.heatmap import render_heatmap

    monkeypatch.setenv("PLATFORM_HEATMAP_ENABLED", "true")
    html = render_heatmap(sample_briefs, ogilvy_brand)
    assert html != ""
    assert "platform mix" in html.lower()
    # sample_briefs carry tiktok + threads + instagram counts; the display names
    # are the clean _platform_label forms.
    assert "TikTok" in html
    assert "Threads" in html
    assert "Instagram" in html
    # a percentage share is printed
    assert "%" in html


def test_heatmap_share_sums_to_real_total(monkeypatch):
    """The share column is each platform's percent of the day's total reads, so
    a clean 3:1 split reads 75% and 25%."""
    from src.alerts.email_render.heatmap import render_heatmap

    monkeypatch.setenv("PLATFORM_HEATMAP_ENABLED", "true")
    briefs = [{"platform_counts": ["tiktok | 75 items", "reddit | 25 items"]}]
    html = render_heatmap(briefs, load_brand("ogilvy"))
    assert "75%" in html
    assert "25%" in html


def test_heatmap_is_email_safe(sample_briefs, ogilvy_brand, monkeypatch):
    """The section is table-based with a bgcolor fill bar, no flex/grid, and
    dash-clean (Outlook + the auditor)."""
    from src.alerts.email_render.heatmap import render_heatmap

    monkeypatch.setenv("PLATFORM_HEATMAP_ENABLED", "true")
    html = render_heatmap(sample_briefs, ogilvy_brand)
    assert "display:flex" not in html
    assert "display:grid" not in html
    assert "<table" in html
    assert 'role="presentation"' in html
    assert "bgcolor=" in html  # the fill bar is an attribute-coloured cell
    assert chr(0x2014) not in html
    assert chr(0x2013) not in html


def test_full_email_heatmap_byte_identical_when_off(
    sample_briefs, sample_displays, ogilvy_brand, run_date, monkeypatch
):
    """Dark-on-ship guarantee for the full email: with the flag off the heat-map
    contributes nothing, so the email matches the pre-feature output."""
    from src.alerts.email_render import render_pulse_html

    monkeypatch.delenv("PLATFORM_HEATMAP_ENABLED", raising=False)
    monkeypatch.delenv("CONTINUITY_BADGES_ENABLED", raising=False)
    html = render_pulse_html(sample_briefs, sample_displays, ogilvy_brand, run_date)
    assert "platform mix" not in html.lower()
    assert "What drove the day" not in html


def test_full_email_includes_heatmap_when_on(
    sample_briefs, sample_displays, ogilvy_brand, run_date, monkeypatch
):
    """Flag on: the heat-map section appears in the composed email and the email
    stays dash-free and table-based."""
    from src.alerts.email_render import render_pulse_html

    monkeypatch.setenv("PLATFORM_HEATMAP_ENABLED", "true")
    html = render_pulse_html(sample_briefs, sample_displays, ogilvy_brand, run_date)
    assert "platform mix" in html.lower()
    assert "TikTok" in html
    assert "display:flex" not in html
    assert "display:grid" not in html
    assert "—" not in html
    assert "–" not in html  # noqa: RUF001  guarding against en dash


# Wave 2 (19 Jun 2026): two more dark-on-ship EmailRender sections built against
# the locked Wave 2 contract names. The tone split aggregates the social
# sentiment lexicon (sentiment_lexicon_score, carried onto each brief as a
# sentiment_lexicon_scores list) into a positive/neutral/negative bar, gated
# TONE_SPLIT_ENABLED. The pan-African section lists the stories rising in two or
# more markets from the pan_african_stories rows, gated PAN_AFRICAN_ENABLED. Both
# stay byte-identical to today until the data is present AND the flag is on.


@pytest.fixture
def briefs_with_tone(sample_briefs):
    # The lexicon scores ride on the briefs as the engine will attach them: a
    # clean 6 positive / 2 neutral / 2 negative split across the day's briefs.
    b = [dict(x) for x in sample_briefs]
    b[0]["sentiment_lexicon_scores"] = [0.6, 0.4, 0.2, 0.8, 0.16, 0.9]
    b[1]["sentiment_lexicon_scores"] = [-0.5, -0.3, 0.05]
    b[2]["sentiment_lexicon_scores"] = [0.0]
    return b


def test_tone_split_noop_when_flag_off(briefs_with_tone, ogilvy_brand, monkeypatch):
    """Dark on ship: flag off returns no section even though the briefs carry
    lexicon scores."""
    from src.alerts.email_render.tone_split import render_tone_split

    monkeypatch.delenv("TONE_SPLIT_ENABLED", raising=False)
    assert render_tone_split(briefs_with_tone, ogilvy_brand) == ""


def test_tone_split_noop_when_flag_on_but_no_scores(sample_briefs, ogilvy_brand, monkeypatch):
    """Flag on but no usable lexicon score (the column is NULL across the feed
    today) renders nothing, not empty furniture."""
    from src.alerts.email_render.tone_split import render_tone_split

    monkeypatch.setenv("TONE_SPLIT_ENABLED", "true")
    # sample_briefs carry no sentiment_lexicon_scores
    assert render_tone_split(sample_briefs, ogilvy_brand) == ""
    # a present-but-all-out-of-range field also yields nothing
    bad = [{"sentiment_lexicon_scores": [2.0, -5.0, "x", None, True]}]
    assert render_tone_split(bad, ogilvy_brand) == ""


def test_tone_split_bins_by_locked_thresholds():
    """The split bins each score by the locked +/-0.15 polarity bands."""
    from src.alerts.email_render.tone_split import _collect_scores, _split_counts

    briefs = [
        {"sentiment_lexicon_scores": [0.6, 0.15, 0.149, 0.0, -0.15, -0.9]},
    ]
    scores = _collect_scores(briefs)
    # 0.149 sits inside the neutral band; 0.15 and -0.15 are the polarity edges.
    pos, neu, neg = _split_counts(scores)
    assert pos == 2  # 0.6, 0.15
    assert neg == 2  # -0.15, -0.9
    assert neu == 2  # 0.149, 0.0


def test_tone_split_bins_full_distribution_across_briefs():
    """The split bins every passed score, with no internal truncation or sampling.

    The deterministic, source-unbiased sample is built upstream in the SQL
    (ORDER BY FARM_FINGERPRINT before the cap); the render must bin the true
    distribution of whatever list it is handed. This pins that every score across
    every brief is counted, so the shares reflect the real positive/neutral/
    negative balance rather than a sub-slice of the list.
    """
    from src.alerts.email_render.tone_split import _collect_scores, _split_counts

    briefs = [
        {"sentiment_lexicon_scores": [0.5] * 70},
        {"sentiment_lexicon_scores": [0.0] * 20},
        {"sentiment_lexicon_scores": [-0.5] * 10},
    ]
    scores = _collect_scores(briefs)
    assert len(scores) == 100
    pos, neu, neg = _split_counts(scores)
    assert (pos, neu, neg) == (70, 20, 10)


def test_tone_split_renders_bar_and_shares(briefs_with_tone, ogilvy_brand, monkeypatch):
    """Flag on + real scores: the section renders the eyebrow, a stacked bar and
    the three shares computed from the stored scores."""
    from src.alerts.email_render.tone_split import render_tone_split

    monkeypatch.setenv("TONE_SPLIT_ENABLED", "true")
    html = render_tone_split(briefs_with_tone, ogilvy_brand)
    assert html != ""
    assert "tone split" in html.lower()
    # 6 positive / 2 neutral / 2 negative of 10 scored -> 60 / 20 / 20.
    assert "Positive 60%" in html
    assert "Neutral 20%" in html
    assert "Negative 20%" in html
    assert "10 scored posts" in html


def test_tone_split_is_email_safe(briefs_with_tone, ogilvy_brand, monkeypatch):
    """The section is table-based with bgcolor segments, no flex/grid, dash-clean."""
    from src.alerts.email_render.tone_split import render_tone_split

    monkeypatch.setenv("TONE_SPLIT_ENABLED", "true")
    html = render_tone_split(briefs_with_tone, ogilvy_brand)
    assert "display:flex" not in html
    assert "display:grid" not in html
    assert "<table" in html
    assert 'role="presentation"' in html
    assert "bgcolor=" in html
    assert chr(0x2014) not in html
    assert chr(0x2013) not in html


@pytest.fixture
def pan_african_rows():
    # Two genuine cross-market stories and one single-market row that must drop.
    return [
        {
            "trend_date": "2026-05-29",
            "story_id": "amapiano_wave",
            "story_label": "Amapiano crosses into West Africa",
            "markets": ["za", "ng"],
            "topic_keys": ["music_amapiano"],
            "total_item_count": 420,
            "momentum_composite": 0.81,
        },
        {
            "trend_date": "2026-05-29",
            "story_id": "sapa_humour",
            "story_label": "The broke-economy joke spreads",
            "markets": ["ng", "ke", "za"],
            "topic_keys": ["economy_sapa_hustle"],
            "total_item_count": 260,
            "momentum_composite": 0.64,
        },
        {
            "trend_date": "2026-05-29",
            "story_id": "local_only",
            "story_label": "A single-market story",
            "markets": ["za"],
            "topic_keys": ["lifestyle_soft_life"],
            "total_item_count": 90,
            "momentum_composite": 0.9,
        },
    ]


def test_pan_african_noop_when_flag_off(pan_african_rows, ogilvy_brand, monkeypatch):
    """Dark on ship: flag off returns no section even with real story rows."""
    from src.alerts.email_render.pan_african import render_pan_african

    monkeypatch.delenv("PAN_AFRICAN_ENABLED", raising=False)
    assert render_pan_african(pan_african_rows, ogilvy_brand) == ""


def test_pan_african_noop_when_flag_on_but_no_stories(ogilvy_brand, monkeypatch):
    """Flag on but no cross-market story (the table is empty today, or every row
    is single-market) renders nothing, not empty furniture."""
    from src.alerts.email_render.pan_african import render_pan_african

    monkeypatch.setenv("PAN_AFRICAN_ENABLED", "true")
    assert render_pan_african([], ogilvy_brand) == ""
    single = [{"story_label": "Local", "markets": ["za"], "momentum_composite": 0.9}]
    assert render_pan_african(single, ogilvy_brand) == ""


def test_pan_african_enforces_two_market_rule(pan_african_rows, ogilvy_brand, monkeypatch):
    """Only stories spanning two or more markets surface; the single-market row
    drops, and the rest sort by momentum_composite descending."""
    from src.alerts.email_render.pan_african import _clean_stories

    monkeypatch.setenv("PAN_AFRICAN_ENABLED", "true")
    cleaned = _clean_stories(pan_african_rows)
    assert [s["label"] for s in cleaned] == [
        "Amapiano crosses into West Africa",
        "The broke-economy joke spreads",
    ]
    # the duplicate-market guard keeps distinct slugs in first-seen order
    assert cleaned[1]["markets"] == ["ng", "ke", "za"]


def test_pan_african_renders_section(pan_african_rows, ogilvy_brand, monkeypatch):
    """Flag on + cross-market rows: the section renders the eyebrow, the labels,
    the market names and the item counts."""
    from src.alerts.email_render.pan_african import render_pan_african

    monkeypatch.setenv("PAN_AFRICAN_ENABLED", "true")
    html = render_pan_african(pan_african_rows, ogilvy_brand)
    assert html != ""
    assert "pan-african" in html.lower()
    assert "Amapiano crosses into West Africa" in html
    assert "Moving in 2 markets" in html
    assert "Moving in 3 markets" in html
    assert "420 items" in html


def test_pan_african_handles_numpy_array_markets(ogilvy_brand, monkeypatch):
    """A BQ ARRAY<STRING> read via to_dataframe yields markets as a numpy array,
    not a list; the renderer must not trip on numpy truthiness. This is the
    22-Jun cron render_error: `for entry in raw or []` raised on a multi-element
    ndarray, so the whole email failed to render."""
    import numpy as np
    from src.alerts.email_render.pan_african import render_pan_african

    monkeypatch.setenv("PAN_AFRICAN_ENABLED", "true")
    stories = [
        {
            "story_label": "Amapiano crosses into West Africa",
            "markets": np.array(["za", "ng"]),
            "total_item_count": 420,
            "momentum_composite": 0.8,
        }
    ]
    html = render_pan_african(stories, ogilvy_brand)
    assert "Amapiano crosses into West Africa" in html
    assert "Moving in 2 markets" in html


def test_pan_african_is_email_safe(pan_african_rows, ogilvy_brand, monkeypatch):
    """The section is table-based, no flex/grid, dash-clean."""
    from src.alerts.email_render.pan_african import render_pan_african

    monkeypatch.setenv("PAN_AFRICAN_ENABLED", "true")
    html = render_pan_african(pan_african_rows, ogilvy_brand)
    assert "display:flex" not in html
    assert "display:grid" not in html
    assert "<table" in html
    assert 'role="presentation"' in html
    assert chr(0x2014) not in html
    assert chr(0x2013) not in html


def test_full_email_wave2_byte_identical_when_off(
    briefs_with_tone, sample_displays, ogilvy_brand, run_date, pan_african_rows, monkeypatch
):
    """Dark-on-ship guarantee for the full email: with both flags off the tone
    split and the pan-African section contribute nothing, even when the data is
    supplied."""
    from src.alerts.email_render import render_pulse_html

    monkeypatch.delenv("TONE_SPLIT_ENABLED", raising=False)
    monkeypatch.delenv("PAN_AFRICAN_ENABLED", raising=False)
    monkeypatch.delenv("PLATFORM_HEATMAP_ENABLED", raising=False)
    monkeypatch.delenv("CONTINUITY_BADGES_ENABLED", raising=False)
    html = render_pulse_html(
        briefs_with_tone,
        sample_displays,
        ogilvy_brand,
        run_date,
        pan_african_stories=pan_african_rows,
    )
    assert "tone split" not in html.lower()
    assert "pan-african" not in html.lower()
    assert "How the day felt" not in html
    assert "Across the continent" not in html


def test_full_email_includes_wave2_when_on(
    briefs_with_tone, sample_displays, ogilvy_brand, run_date, pan_african_rows, monkeypatch
):
    """Both flags on: the tone split and the pan-African section appear in the
    composed email and the email stays dash-free and table-based."""
    from src.alerts.email_render import render_pulse_html

    monkeypatch.setenv("TONE_SPLIT_ENABLED", "true")
    monkeypatch.setenv("PAN_AFRICAN_ENABLED", "true")
    html = render_pulse_html(
        briefs_with_tone,
        sample_displays,
        ogilvy_brand,
        run_date,
        pan_african_stories=pan_african_rows,
    )
    assert "tone split" in html.lower()
    assert "pan-african" in html.lower()
    assert "Amapiano crosses into West Africa" in html
    assert "display:flex" not in html
    assert "display:grid" not in html
    assert "—" not in html
    assert "–" not in html  # noqa: RUF001  guarding against en dash


# Wave 1 continuity badge --------------------------------------------------


def test_continuity_badge_off_when_flag_unset(
    sample_brief, sample_display, ogilvy_brand, monkeypatch
):
    """Flag off -> no continuity badge even when the field is present."""
    from src.alerts.email_render.card import render_card

    monkeypatch.delenv("CONTINUITY_BADGES_ENABLED", raising=False)
    b = dict(sample_brief)
    b["continuity_state"] = "day2"
    html = render_card(b, sample_display, ogilvy_brand)
    assert "Day 2" not in html


def test_continuity_badge_renders_day2_when_flag_on(
    sample_brief, sample_display, ogilvy_brand, monkeypatch
):
    """Flag on + state present -> the Day 2 badge appears."""
    from src.alerts.email_render.card import render_card

    monkeypatch.setenv("CONTINUITY_BADGES_ENABLED", "true")
    b = dict(sample_brief)
    b["continuity_state"] = "day2"
    html = render_card(b, sample_display, ogilvy_brand)
    assert "Day 2" in html


def test_continuity_badge_uses_running_day_count_for_day3plus(
    sample_brief, sample_display, ogilvy_brand, monkeypatch
):
    """day3plus with a real continuity_day -> the running count, not 'Day 3+'."""
    from src.alerts.email_render.card import render_card

    monkeypatch.setenv("CONTINUITY_BADGES_ENABLED", "true")
    b = dict(sample_brief)
    b["continuity_state"] = "day3plus"
    b["continuity_day"] = 5
    html = render_card(b, sample_display, ogilvy_brand)
    assert "Day 5" in html


def test_continuity_badge_drops_unknown_state(
    sample_brief, sample_display, ogilvy_brand, monkeypatch
):
    """An unknown continuity_state value -> no badge (allow-list)."""
    from src.alerts.email_render.card import render_card

    monkeypatch.setenv("CONTINUITY_BADGES_ENABLED", "true")
    b = dict(sample_brief)
    b["continuity_state"] = "bogus"
    html = render_card(b, sample_display, ogilvy_brand)
    assert "bogus" not in html.lower()


# Wave 1 lifecycle badge ---------------------------------------------------


def test_lifecycle_badge_off_when_flag_unset(
    sample_brief, sample_display, ogilvy_brand, monkeypatch
):
    """Flag off -> no lifecycle badge even when the field is present."""
    from src.alerts.email_render.card import render_card

    monkeypatch.delenv("LIFECYCLE_ENABLED", raising=False)
    b = dict(sample_brief)
    b["lifecycle_phase"] = "growth"
    html = render_card(b, sample_display, ogilvy_brand)
    assert "Growth" not in html


def test_lifecycle_badge_renders_growth_when_flag_on(
    sample_brief, sample_display, ogilvy_brand, monkeypatch
):
    """Flag on + phase present -> the Growth badge appears."""
    from src.alerts.email_render.card import render_card

    monkeypatch.setenv("LIFECYCLE_ENABLED", "true")
    b = dict(sample_brief)
    b["lifecycle_phase"] = "growth"
    html = render_card(b, sample_display, ogilvy_brand)
    assert "Growth" in html


def test_lifecycle_badge_drops_when_field_absent(
    sample_brief, sample_display, ogilvy_brand, monkeypatch
):
    """Flag on but no field -> no badge, render stays clean."""
    from src.alerts.email_render.card import render_card

    monkeypatch.setenv("LIFECYCLE_ENABLED", "true")
    b = dict(sample_brief)
    b.pop("lifecycle_phase", None)
    html = render_card(b, sample_display, ogilvy_brand)
    for label in ("Birth", "Growth", "Maturity", "Decline"):
        assert label not in html


def test_lifecycle_badge_drops_unknown_phase(
    sample_brief, sample_display, ogilvy_brand, monkeypatch
):
    """An unknown lifecycle_phase value -> no badge (allow-list)."""
    from src.alerts.email_render.card import render_card

    monkeypatch.setenv("LIFECYCLE_ENABLED", "true")
    b = dict(sample_brief)
    b["lifecycle_phase"] = "zombie"
    html = render_card(b, sample_display, ogilvy_brand)
    assert "zombie" not in html.lower()


def test_lifecycle_badge_all_phases_label_map(
    sample_brief, sample_display, ogilvy_brand, monkeypatch
):
    """Each contract phase maps to its title-case label."""
    from src.alerts.email_render.card import render_card

    monkeypatch.setenv("LIFECYCLE_ENABLED", "true")
    for phase, label in (
        ("birth", "Birth"),
        ("growth", "Growth"),
        ("maturity", "Maturity"),
        ("decline", "Decline"),
    ):
        b = dict(sample_brief)
        b["lifecycle_phase"] = phase
        html = render_card(b, sample_display, ogilvy_brand)
        assert label in html


def test_badges_render_on_hero(sample_briefs, sample_displays, ogilvy_brand, run_date, monkeypatch):
    """Both badges light on the hero (lead brief) when flags are on, and the
    composed email stays dash-free and table-based."""
    from src.alerts.email_render import render_pulse_html

    monkeypatch.setenv("CONTINUITY_BADGES_ENABLED", "true")
    monkeypatch.setenv("LIFECYCLE_ENABLED", "true")
    briefs = [dict(b) for b in sample_briefs]
    briefs[0]["continuity_state"] = "rebounding"
    briefs[0]["lifecycle_phase"] = "maturity"
    html = render_pulse_html(briefs, sample_displays, ogilvy_brand, run_date)
    assert "Rebounding" in html
    assert "Maturity" in html
    assert "—" not in html
    assert "–" not in html  # noqa: RUF001  guarding against en dash


def test_badges_byte_identical_when_flags_off(
    sample_briefs, sample_displays, ogilvy_brand, run_date, monkeypatch
):
    """With both flags off, adding the fields to a brief does not change the
    rendered email (additive, dark by default)."""
    from src.alerts.email_render import render_pulse_html

    monkeypatch.delenv("CONTINUITY_BADGES_ENABLED", raising=False)
    monkeypatch.delenv("LIFECYCLE_ENABLED", raising=False)
    base = render_pulse_html(sample_briefs, sample_displays, ogilvy_brand, run_date)
    tagged = [dict(b) for b in sample_briefs]
    for b in tagged:
        b["continuity_state"] = "day3plus"
        b["continuity_day"] = 4
        b["lifecycle_phase"] = "growth"
    with_fields = render_pulse_html(tagged, sample_displays, ogilvy_brand, run_date)
    assert base == with_fields


# --- SEED chip (Jo, 22 Jun) ---------------------------------------------


def test_card_renders_seed_chip_when_present(sample_brief, sample_display, ogilvy_brand):
    from src.alerts.email_render.card import render_card

    sample_brief["seed_score"] = 0.82
    html = render_card(sample_brief, sample_display, ogilvy_brand)
    assert "SEED" in html
    assert "82%" in html


def test_card_omits_seed_chip_when_absent(sample_brief, sample_display, ogilvy_brand):
    from src.alerts.email_render.card import render_card

    sample_brief.pop("seed_score", None)
    html = render_card(sample_brief, sample_display, ogilvy_brand)
    assert "SEED" not in html


def test_card_omits_seed_chip_when_zero(sample_brief, sample_display, ogilvy_brand):
    from src.alerts.email_render.card import render_card

    sample_brief["seed_score"] = 0.0
    html = render_card(sample_brief, sample_display, ogilvy_brand)
    assert "SEED" not in html


def test_seed_chip_hot_above_threshold_differs_from_muted(
    sample_brief, sample_display, ogilvy_brand
):
    """At/above 0.70 the chip renders hot (accent); below it renders muted."""
    from src.alerts.email_render.card import render_card

    hot_brief = dict(sample_brief)
    hot_brief["seed_score"] = 0.80
    muted_brief = dict(sample_brief)
    muted_brief["seed_score"] = 0.40
    hot_html = render_card(hot_brief, sample_display, ogilvy_brand)
    muted_html = render_card(muted_brief, sample_display, ogilvy_brand)
    assert "SEED" in hot_html
    assert "SEED" in muted_html
    assert hot_html != muted_html


# --- contrast fixes (25 Jun 2026) ---------------------------------------
#
# WCAG relative-luminance contrast, computed from the sRGB hex so the chosen
# palette values are checked, not hand-asserted. AA normal-size text is 4.5:1.


def _rel_luminance(hexc: str) -> float:
    hexc = hexc.lstrip("#")
    chans = [int(hexc[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in chans]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    la, lb = _rel_luminance(a), _rel_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def test_light_on_ink_dim_clears_aa_on_paper():
    """FIX 1: on_ink_dim carries footer / board-label / meta text on the light
    paper #f3efe6, which is what Gmail and Outlook desktop show (they strip the
    dark overrides). The value must clear WCAG AA 4.5:1 there."""
    from src.alerts.email_render._table import _DEFAULT_PALETTE

    paper = _DEFAULT_PALETTE["paper"]
    assert paper == "#f3efe6"
    assert _contrast(_DEFAULT_PALETTE["on_ink_dim"], paper) >= 4.5


def test_light_on_ink_mute_clears_aa_on_paper():
    """Footer KPI labels and teaser meta use on_ink_mute on the light paper."""
    from src.alerts.email_render._table import _DEFAULT_PALETTE

    paper = _DEFAULT_PALETTE["paper"]
    assert _contrast(_DEFAULT_PALETTE["on_ink_mute"], paper) >= 4.5


def test_dark_hero_band_fill_clears_aa_for_white_text():
    """FIX 2: the hero band paints white text on the dark accent fill lp-ab. The
    fill must clear AA 4.5:1 against white."""
    from src.alerts.email_render._style import _DARK_BG

    assert _contrast(_DARK_BG["lp-ab"], "#ffffff") >= 4.5


def test_dark_accent_text_still_clears_aa_on_canvas():
    """FIX 2 guard: the text-accent lp-accent stays light enough to clear AA as
    text on the dark canvas #1b1714. The darker band fill is a separate token."""
    from src.alerts.email_render._style import _DARK_BG, _DARK_FG

    assert _contrast(_DARK_FG["lp-accent"], "#1b1714") >= 4.5
    # The band fill and the text accent are deliberately different values.
    assert _DARK_BG["lp-ab"] != _DARK_FG["lp-accent"]


def test_dark_faint_text_clears_aa_on_canvas():
    """FIX 4: the faint dark text lp-ft must clear AA 4.5:1 on the dark canvas
    #1b1714 where it carries normal-size text."""
    from src.alerts.email_render._style import _DARK_FG

    assert _contrast(_DARK_FG["lp-ft"], "#1b1714") >= 4.5


# --- inline-fallback size guard (25 Jun 2026) ---------------------------


def _heavy_briefs_by_topic():
    """A synthetic 24-brief map whose full rich render exceeds the inline
    ceiling, so the size guard has something to trim."""
    long_synth = (
        "Creators across the market are narrating this shift in real time, and the "
        "volume keeps climbing across every channel the desk tracks this week. "
    ) * 16
    long_headline = (
        "Synthetic signal {i} carrying a realistic editorial length that the board "
        "index prints in full for every ranked brief on a heavy day"
    )
    out: dict[tuple[str, str], dict] = {}
    for i in range(24):
        market = ("ng", "ke", "za")[i % 3]
        topic = f"synthetic_topic_{i:02d}"
        out[(market, topic)] = {
            "topic": topic,
            "market": market,
            "headline": long_headline.format(i=i),
            "trend_score": 0.6 - i * 0.01,
            "status_tag": "Rising",
            "trend_synthesis": long_synth,
            "cultural_context": long_synth,
            "sentiment_summary": "mixed, defiant",
            "platforms": ["instagram", "tiktok", "threads", "reddit", "news"],
            "platform_counts": ["instagram | 30 items"],
            "top_creators": [f"@creator_{i} | tiktok | 5 mentions"],
            "social_refs": [],
            "display": {"state": {"badge": "Rising", "direction": "up"}},
        }
    return out


def test_inline_size_guard_trims_heavy_render_under_ceiling():
    """FIX 3: when the rich body would ship inline (no hosted URL) and exceeds
    the ceiling, the guard trims the board long tail until it clears. A test
    ceiling is passed so the trim path is exercised deterministically regardless
    of the exact production byte budget; the result must clear that ceiling and
    drop briefs off the bottom."""
    from src.alerts.email_digest import _guard_inline_body_size
    from src.alerts.email_render import render_pulse_html
    from src.alerts.email_render.brand import load_brand

    by_topic = _heavy_briefs_by_topic()
    run = datetime.date(2026, 6, 25)
    briefs = [{**b, "market": m, "topic": t} for (m, t), b in by_topic.items()]
    displays = [b["display"] for b in by_topic.values()]
    full = render_pulse_html(briefs, displays, load_brand("ogilvy"), run)
    test_ceiling = 80_000
    # The synthetic heavy render must actually breach the test ceiling, else the
    # test is not exercising the trim path.
    assert len(full.encode("utf-8")) > test_ceiling

    guarded = _guard_inline_body_size(full, by_topic, run, _SAMPLE_SUMMARY, ceiling=test_ceiling)
    assert len(guarded.encode("utf-8")) <= test_ceiling
    # Trimming the board long tail drops headlines off the bottom, so the trimmed
    # body is strictly shorter than the full inline render.
    assert len(guarded.encode("utf-8")) < len(full.encode("utf-8"))


def test_inline_size_guard_passes_through_small_render():
    """FIX 3: a body already under the ceiling is returned unchanged."""
    from src.alerts.email_digest import _guard_inline_body_size

    small = "<html><body>tiny</body></html>"
    run = datetime.date(2026, 6, 25)
    assert _guard_inline_body_size(small, {}, run, None) == small


# Dark-mode background inversion (2026-06-28 audit) -----------------------
#
# The inline base is light; dark mode flips each lp-* class to its midnight
# value via the @media block in _style.py. Three background surfaces were
# missing their dark override: the heatmap fill-bar track, and the three
# tone-split segment fills (lp-po / lp-nu / lp-ng were color-only). These
# tests pin that the classes are attached in the render AND that each has a
# background-color entry in the dark block, so a track or segment never renders
# light-on-dark in Apple Mail / Outlook dark mode.


def test_heatmap_fill_bar_carries_chip_line_dark_class(sample_briefs, ogilvy_brand, monkeypatch):
    """The heatmap fill-bar track carries the chip-line dark class so its light
    track fill inverts in dark mode (it was attribute-coloured with no class)."""
    from src.alerts.email_render._table import CLS
    from src.alerts.email_render.heatmap import render_heatmap

    monkeypatch.setenv("PLATFORM_HEATMAP_ENABLED", "true")
    html = render_heatmap(sample_briefs, ogilvy_brand)
    assert f'class="{CLS["chip_line"]}"' in html


def test_style_block_inverts_chip_line_background():
    """The chip-line role has a background-color override in the dark block, so
    the heatmap track flips to the midnight chip fill."""
    from src.alerts.email_render._style import STYLE
    from src.alerts.email_render._table import CLS

    assert f".{CLS['chip_line']}{{background-color:#2b2622!important;}}" in STYLE


def test_style_block_inverts_tone_split_segment_backgrounds():
    """The three tone-split segment BG classes (lp-pob / lp-nub / lp-ngb) carry a
    background-color override in the dark block, matching the dark pos/neu/neg
    values, so the stacked bar inverts instead of staying light-on-dark."""
    from src.alerts.email_render._style import STYLE

    assert ".lp-pob{background-color:#56c98a!important;}" in STYLE
    assert ".lp-nub{background-color:#9c958a!important;}" in STYLE
    assert ".lp-ngb{background-color:#e2654a!important;}" in STYLE


def test_style_block_never_paints_tone_text_backgrounds():
    """The tone TEXT classes (lp-po / lp-nu / lp-ng) must never receive a
    background-color override: sharing bg + fg on one class painted every
    tone-coloured text span as a solid same-colour block in dark mode (the
    10 Jul 2026 'random highlight' defect)."""
    from src.alerts.email_render._style import STYLE

    for cls in ("lp-po", "lp-nu", "lp-ng"):
        assert f".{cls}{{background-color" not in STYLE
        assert f"[data-ogsb] .{cls}{{" not in STYLE


def test_tone_split_segments_carry_dark_bg_classes(briefs_with_tone, ogilvy_brand, monkeypatch):
    """The rendered stacked bar attaches the three segment BG classes that
    invert in dark mode."""
    from src.alerts.email_render._table import CLS
    from src.alerts.email_render.tone_split import render_tone_split

    monkeypatch.setenv("TONE_SPLIT_ENABLED", "true")
    html = render_tone_split(briefs_with_tone, ogilvy_brand)
    for role in ("pos_bg", "neu_bg", "neg_bg"):
        assert f'class="{CLS[role]}"' in html


def test_move_card_carries_dark_surface_and_rule_classes(
    sample_brief, sample_display, ogilvy_brand
):
    """The move card renders dark (hardcoded dark=True), so its panel cell carries
    the surface + rule classes the dark block inverts."""
    from src.alerts.email_render._table import CLS
    from src.alerts.email_render.card import render_card

    html = render_card(sample_brief, sample_display, ogilvy_brand)
    assert CLS["surface"] in html
    assert CLS["rule"] in html


def test_heatmap_and_tone_split_dark_classes_in_full_email(
    briefs_with_tone, sample_displays, ogilvy_brand, run_date, monkeypatch
):
    """With both flags on, the composed email carries the heatmap track and the
    tone-split segment dark classes alongside the move-card surface class."""
    from src.alerts.email_render import render_pulse_html
    from src.alerts.email_render._table import CLS

    monkeypatch.setenv("PLATFORM_HEATMAP_ENABLED", "true")
    monkeypatch.setenv("TONE_SPLIT_ENABLED", "true")
    html = render_pulse_html(briefs_with_tone, sample_displays, ogilvy_brand, run_date)
    assert f'class="{CLS["chip_line"]}"' in html
    assert CLS["surface"] in html
    for role in ("pos", "neu", "neg"):
        assert CLS[role] in html


# --- Wave 3 micro-briefs ("early signals" strip), MICRO_BRIEFS_ENABLED -----
# Dark on ship. select_micro_briefs (src/analysis/micro_briefs.py) runs in
# run_rss_now.py and stashes results on daily_summary_dict; render_micro_briefs
# renders them under the existing brief cards. Both stay byte-identical to
# today until the data is present AND the flag is on.


@pytest.fixture
def micro_briefs_by_market():
    return {
        "za": [
            {
                "term": "bucketlisthustle",
                "frequency": 12,
                "platforms": ["tiktok", "instagram"],
                "topics": ["music_amapiano"],
                "sample_row_ids": ["r1", "r2"],
            },
            {
                "term": "kasilingo",
                "frequency": 7,
                "platforms": ["threads"],
                "topics": ["genz_slang"],
                "sample_row_ids": ["r3"],
            },
        ],
        "ng": [
            {
                "term": "japaeconomy",
                "frequency": 9,
                "platforms": ["twitter"],
                "topics": ["economy_japa"],
                "sample_row_ids": ["r4"],
            }
        ],
    }


def test_micro_briefs_noop_when_flag_off(
    micro_briefs_by_market, sample_briefs, sample_displays, ogilvy_brand, run_date, monkeypatch
):
    """Dark on ship: flag off returns no section even with real items, and the
    full email is byte-identical to a render with no micro_briefs_by_market at all."""
    from src.alerts.email_render import render_pulse_html
    from src.alerts.email_render.micro_briefs import render_micro_briefs

    monkeypatch.delenv("MICRO_BRIEFS_ENABLED", raising=False)
    assert render_micro_briefs(micro_briefs_by_market, ogilvy_brand) == ""

    with_data = render_pulse_html(
        sample_briefs,
        sample_displays,
        ogilvy_brand,
        run_date,
        micro_briefs_by_market=micro_briefs_by_market,
    )
    without_data = render_pulse_html(sample_briefs, sample_displays, ogilvy_brand, run_date)
    assert with_data == without_data


def test_micro_briefs_noop_when_flag_on_but_no_items(ogilvy_brand, monkeypatch):
    """Flag on but no market carries an item renders nothing, not empty furniture."""
    from src.alerts.email_render.micro_briefs import render_micro_briefs

    monkeypatch.setenv("MICRO_BRIEFS_ENABLED", "true")
    assert render_micro_briefs(None, ogilvy_brand) == ""
    assert render_micro_briefs({}, ogilvy_brand) == ""
    assert render_micro_briefs({"za": []}, ogilvy_brand) == ""


def test_micro_briefs_renders_section(micro_briefs_by_market, ogilvy_brand, monkeypatch):
    """Flag on + real items: the section renders the eyebrow, the terms, the
    platform/topic hints and the row counts."""
    from src.alerts.email_render.micro_briefs import render_micro_briefs

    monkeypatch.setenv("MICRO_BRIEFS_ENABLED", "true")
    html = render_micro_briefs(micro_briefs_by_market, ogilvy_brand)
    assert html != ""
    assert "early signals" in html.lower()
    assert "bucketlisthustle" in html
    assert "kasilingo" in html
    assert "japaeconomy" in html
    assert "12 rows" in html
    assert "7 rows" in html
    assert "9 rows" in html


def test_micro_briefs_present_in_full_email_when_flag_on(
    micro_briefs_by_market, sample_briefs, sample_displays, ogilvy_brand, run_date, monkeypatch
):
    """The full render_pulse_html composition carries the section when the flag
    is on and micro_briefs_by_market is supplied."""
    from src.alerts.email_render import render_pulse_html

    monkeypatch.setenv("MICRO_BRIEFS_ENABLED", "true")
    html = render_pulse_html(
        sample_briefs,
        sample_displays,
        ogilvy_brand,
        run_date,
        micro_briefs_by_market=micro_briefs_by_market,
    )
    assert "early signals" in html.lower()
    assert "bucketlisthustle" in html


def test_micro_briefs_is_email_safe(micro_briefs_by_market, ogilvy_brand, monkeypatch):
    """The section is table-based, no flex/grid, dash-clean."""
    from src.alerts.email_render.micro_briefs import render_micro_briefs

    monkeypatch.setenv("MICRO_BRIEFS_ENABLED", "true")
    html = render_micro_briefs(micro_briefs_by_market, ogilvy_brand)
    assert "display:flex" not in html
    assert "display:grid" not in html
    assert "<table" in html
    assert 'role="presentation"' in html
    assert chr(0x2014) not in html
    assert chr(0x2013) not in html


# --- M-4: Gmail clip guard at max load (every flagged section on and fed) ---
# Gmail clips a message over 102,400 decoded bytes and hides the tail behind
# "[Message clipped]". The live send path runs the rich body through
# _guard_inline_body_size before it can ship inline, so the guarded body is
# what must clear the clip. The existing clip test runs flags-off; this one
# pins the true maximum: 24 briefs plus heatmap, tone split, pan-African,
# micro-briefs and continuity badges all on and populated.


def test_full_render_at_max_load_clears_gmail_clip_after_guard(
    sample_briefs,
    sample_displays,
    pan_african_rows,
    micro_briefs_by_market,
    ogilvy_brand,
    run_date,
    monkeypatch,
):
    import src.analysis.pan_african as pan_african_mod
    from src.alerts.email_digest import _guard_inline_body_size
    from src.alerts.email_render import render_pulse_html

    monkeypatch.setenv("PLATFORM_HEATMAP_ENABLED", "true")
    monkeypatch.setenv("TONE_SPLIT_ENABLED", "true")
    monkeypatch.setenv("PAN_AFRICAN_ENABLED", "true")
    monkeypatch.setenv("MICRO_BRIEFS_ENABLED", "true")
    monkeypatch.setenv("CONTINUITY_BADGES_ENABLED", "true")
    # The guard re-reads pan-African stories when the flag is on; keep the
    # test off BigQuery by feeding it the same fixture rows.
    monkeypatch.setattr(pan_african_mod, "read_pan_african_stories", lambda _d: pan_african_rows)

    briefs = []
    displays = []
    by_topic = {}
    for i in range(24):
        b = dict(sample_briefs[i % len(sample_briefs)])
        b["topic"] = f"{b['topic']}_{i:02d}"
        b["trend_score"] = 0.6 - i * 0.01
        b["sentiment_lexicon_scores"] = [0.6, 0.0, -0.4, 0.2, 0.8]
        b["continuity_state"] = "returning"
        b["continuity_day"] = 3
        d = dict(sample_displays[i % len(sample_displays)])
        briefs.append(b)
        displays.append(d)
        by_topic[(b["market"], b["topic"])] = {**b, "display": d}
    daily_summary = {**_SAMPLE_SUMMARY, "micro_briefs_by_market": micro_briefs_by_market}

    rich = render_pulse_html(
        briefs,
        displays,
        ogilvy_brand,
        run_date,
        daily_summary=daily_summary,
        pan_african_stories=pan_african_rows,
        micro_briefs_by_market=micro_briefs_by_market,
    )
    # Every fed section actually rendered: this is the real max-load document.
    assert "tone split" in rich.lower()
    assert "early signals" in rich.lower()
    assert "pan-african" in rich.lower()

    guarded = _guard_inline_body_size(rich, by_topic, run_date, daily_summary)
    size = len(guarded.encode("utf-8"))
    assert size < 102400, f"guarded inline body is {size} bytes, over the Gmail clip"
    # The guard trims the board long tail but must keep the flagged sections.
    assert "tone split" in guarded.lower()
    assert "early signals" in guarded.lower()
