"""Live staging, 4 October: the TikTok sounds answer showed raw ids for sounds, vague claims, table names and a check
code under "What we do not know", warehouse steps with no description, and three failed attempts to save a finding.
"""

import pytest

from core.agent import ask, plain, toolset, writer
from core.agent.skills import load_skill
from core.agent.tests.test_warehouse_tools import GOOD, FakeWriter, ctx, passed, primed  # noqa: F401
from core.agent.tools.sql_query import WAREHOUSE_EXAMPLE, check_sql
from core.agent.tools.warehouse import commit_findings, save_finding


# What we do not know

@pytest.mark.parametrize("raw, shown", [
    ("watch_next item 0 removed: a forecast before it has beaten persistence (K9)",
     "A line on what to watch next was removed: a forecast that has not outperformed a simple no-change forecast"),
    ("so_what item 2 removed: a number with no query behind it (K2)",
     "A line on why it matters was removed: a number with no query behind it"),
    ("Claim c3 cut: a quote not found in its posts (K8)", "A claim was cut: a quote not found in its posts"),
    ("Cross-referenced item_daily, rising_topics, and cultural_map tables.",
     "Cross-referenced daily item counts, rising topics, and the item map tables."),
    ("the watch_next text", "the what to watch next text"),
    ("intelligence_42_core.post_items", "post links to items"),
])
def test_gap_text_names_no_table_field_or_check_code(raw, shown):
    assert plain.text(raw) == shown


def test_ids_and_routes_in_a_gap_stay_as_they_are():
    assert plain.text("stored evidence tt_7431, p_2") == "stored evidence tt_7431, p_2"
    assert plain.text("tiktok/search, last 7 days") == "tiktok/search, last 7 days"


def test_the_answer_a_reader_sees_has_plain_gaps_and_keeps_its_claims():
    shown = {"claims": [{"id": "c1", "text": "Claim withheld by the trust gate (K6)"}],
             "gaps": [{"what": "watch_next item 0 removed: a forecast before it has beaten persistence (K9)",
                       "searched": "the watch_next text", "why": "forecasts remain held"}]}
    out = plain.answer(shown)
    assert out["claims"] == shown["claims"]
    assert "(K9)" not in out["gaps"][0]["what"] and "watch_next" not in out["gaps"][0]["searched"]


@pytest.mark.parametrize("raw, shown", [
    ("numeric query scope unavailable for a referenced claim",
     "the stored counts could not be matched to a claim"),
    ("forecasts remain held until they beat the persistence baseline",
     "forecasts are not shown until they outperform a simple no-change forecast"),
    ("every evidence id must resolve to a stored post, and every quote must be verbatim in it",
     "every citation must lead to a stored post, and every quote must match its words exactly"),
    ("every number must come from a query recorded in this run and come back the same on re-run",
     "every number must come from a saved count for this answer and match when checked again"),
])
def test_unknowns_explain_the_missing_proof_in_plain_words(raw, shown):
    assert plain.gap({"what": "A claim was removed", "searched": "q_4, tt_73", "why": raw}) == {
        "what": "A claim was removed", "searched": "q_4, tt_73", "why": shown,
    }


def test_plain_unknowns_keep_checked_claims_numbers_citations_and_quotes_identical():
    from copy import deepcopy

    claim = {"id": "c1", "text": "51 posts, 3 creators: the sound appeared in their clips.",
             "numbers": [{"value": 51, "unit": "posts", "query_id": "q_4", "result_hash": "sha256:kept"}],
             "evidence_ids": ["tt_73"], "quotes": [{"evidence_id": "tt_73", "text": "the sound appeared"}]}
    shown = {"claims": [claim], "evidence": [{"id": "tt_73", "text": "the sound appeared"}],
             "gaps": [{"what": "A number was removed", "searched": "q_4", "why": "numeric query scope unavailable for a referenced claim"}]}
    frozen = deepcopy(shown)
    out = plain.answer(shown)
    assert out["claims"] == frozen["claims"] and out["evidence"] == frozen["evidence"]
    assert shown == frozen
    assert out["gaps"][0]["why"] == "the stored counts could not be matched to a claim"


def test_writer_receives_finding_first_and_usage_only_sound_instructions(ctx):  # noqa: F811
    from core.agent.tests.test_writer import FakeModel

    draft = {"short_answer": "", "claims": [], "so_what": [], "watch_next": [], "gaps": [], "context": ""}
    model = FakeModel(draft)
    writer.write_answer(model, question="Which sounds appeared?", as_of=ctx.as_of, market="ZA",
                        window="2026-09-22 to 2026-09-28", ctx=ctx)
    system = model.calls[0]["system"]
    assert "South African football, including the Premier Soccer League, appeared in 17 posts across 4 platforms" in system
    assert "Do not prefix a finding with a count list and a colon" in system
    assert "original sound used by @handle" in system
    assert "a handle from a cited post that uses the sound" in system
    assert "does not establish who made the sound or who first used it" in system
    assert "first used in the window by" not in system


def test_writer_receives_week_on_week_only_for_exact_comparable_query_windows(ctx):  # noqa: F811
    from core.agent.tests.test_writer import FakeModel

    draft = {"short_answer": "", "claims": [], "so_what": [], "watch_next": [], "gaps": [], "context": ""}
    model = FakeModel(draft)
    writer.write_answer(model, question="Which sounds appeared?", as_of=ctx.as_of, market="ZA",
                        window="2026-09-22 to 2026-09-28", ctx=ctx)
    system = model.calls[0]["system"]
    assert "Say week on week only when both query windows cover seven consecutive days" in system
    assert "same item, platform, market and count definition" in system
    assert "Do not calculate a difference or percentage unless a recorded query returns it" in system


def test_run_ask_returns_gaps_and_followups_in_plain_words():
    from core.agent.tests.test_ask import GateTextModel, Harness

    out = Harness(check=None, model=GateTextModel()).run()
    for gap in out["answer"]["gaps"]:
        for field in ("what", "searched", "why"):
            assert "(K" not in gap[field] and not plain._NAME.search(gap[field]), gap
    assert any(g["what"].startswith("A claim was") for g in out["answer"]["gaps"])


# Warehouse steps

@pytest.mark.parametrize("purpose", ["top 20 sounds", "", "sounds since 1200 posts"])
def test_a_warehouse_step_whose_purpose_cannot_show_says_what_it_counts_from(purpose):
    sql = ("SELECT p.sound_id FROM intelligence_42_core.posts p JOIN intelligence_42_core.post_items i "
           "USING (post_id)")
    _, text, _ = ask.describe_tool("mcp__f42__sql_query", {"sql": sql, "purpose": purpose}, None)
    assert text == "Counting in the warehouse from stored posts and post links to items"


def test_a_warehouse_step_purpose_shows_tables_in_plain_words():
    _, text, _ = ask.describe_tool("mcp__f42__sql_query", {"sql": "SELECT 1", "purpose": "Check item_state for sounds"},
                                   None)
    assert text == "Counting in the warehouse: Check item trends for sounds"


# Sound titles, hashtags and counts

def test_the_example_query_reads_each_sounds_stored_title_and_passes_the_guard():
    check_sql(WAREHOUSE_EXAMPLE)
    assert "m.label" in WAREHOUSE_EXAMPLE and "CONCAT(p.platform, ':', p.sound_id)" in WAREHOUSE_EXAMPLE
    described = toolset.DESCRIPTIONS["sql_query"]
    assert "a label equal to the sound id, means 42 holds no title" in described and "UNNEST(p.hashtags)" in described
    assert "IF(m.label = p.sound_id, NULL, m.label)" in WAREHOUSE_EXAMPLE  # the item map stores an unnamed sound by id


def test_the_sound_title_join_matches_how_collect_keys_a_sound():
    from core.detect.items import canonical_key
    assert canonical_key("sound", "7412345", "tiktok") == "tiktok:7412345"


def test_the_writer_names_hashtags_and_sounds_with_counts_and_never_a_raw_sound_id():
    assert "Name the specific hashtags, sounds and creators" in writer.WRITER_SYSTEM
    assert "never write the id" in writer.WRITER_SYSTEM
    assert "Keep the hashtags, sound titles and handles the posts name." in writer.K4_REWRITE_SYSTEM


def test_the_researcher_leaves_the_top_hashtags_and_sounds_to_the_code_counts():
    # Code counts them for every ask since the store-breadth change (tools/warehouse.py store_breadth); a live T1 ask
    # on 4 October spent a turn of nine warehouse counts repeating them.
    skill = load_skill("culture-read")
    assert "the top hashtags and sounds by name" in skill and "do not repeat those counts" in skill
    assert "title is not in 42's data" in skill


# Saving a finding

@pytest.mark.parametrize("label, stored", [("Observed", "observed"), ("Single source", "single_source"),
                                           (" single-source ", "single_source"), ("INFERRED", "inferred")])
def test_save_finding_takes_the_labels_as_the_skill_writes_them(primed, label, stored):  # noqa: F811
    ctx, q1, _ = primed
    w = FakeWriter()
    save_finding(ctx, w, evidence_ids=["p1"], query_ids=[q1], **{**GOOD, "label": label})
    commit_findings(ctx, w, [passed(GOOD["claim"], "corroborated", ["p1"])])
    assert w.inserts[0][1][0]["claims"][0]["label"] == stored


def test_save_finding_still_refuses_an_unknown_label(primed):  # noqa: F811
    from core.agent.context import Refused
    ctx, q1, _ = primed
    with pytest.raises(Refused):
        save_finding(ctx, FakeWriter(), evidence_ids=["p1"], query_ids=[q1], **{**GOOD, "label": "confirmed"})


def test_save_finding_tells_the_model_which_labels_and_item_ids_it_takes():
    schema = toolset.SCHEMAS["save_finding"]["properties"]
    assert "single_source" in schema["label"]["description"]
    assert "history, analogues or rising_topics" in schema["item_ids"]["description"]
    assert "sql_query rows are refused" in schema["item_ids"]["description"]


# Search terms joined by OR (live staging: 'sound OR challenge OR trend' searched for the word OR)

def test_or_between_search_terms_matches_any_of_them_and_is_never_searched_for(ctx):  # noqa: F811
    from core.agent.tests.test_warehouse_tools import FakeWarehouse
    from core.agent.tools.warehouse import search_posts

    wh = FakeWarehouse([])
    search_posts(ctx, wh, "sound OR challenge | trend gqom")
    sql, params = wh.runs[0][0], wh.runs[0][1]
    terms = {k: v for k, v in params.items() if k.startswith("term_")}
    assert sorted(terms.values()) == ["challenge", "gqom", "sound", "trend"]
    assert "((CONTAINS_SUBSTR(p.text, @term_0)) OR (CONTAINS_SUBSTR(p.text, @term_1)) OR " \
           "(CONTAINS_SUBSTR(p.text, @term_2) AND CONTAINS_SUBSTR(p.text, @term_3)))" in sql
    check_sql(sql)


def test_plain_terms_are_still_all_required(ctx):  # noqa: F811
    from core.agent.tests.test_warehouse_tools import FakeWarehouse
    from core.agent.tools.warehouse import search_posts

    wh = FakeWarehouse([])
    search_posts(ctx, wh, "amapiano AND braai")
    assert "(CONTAINS_SUBSTR(p.text, @term_0) AND CONTAINS_SUBSTR(p.text, @term_1))" in wh.runs[0][0]
    assert "AND" not in [v for k, v in wh.runs[0][1].items() if k.startswith("term_")]


@pytest.mark.parametrize("query", ["OR", "OR | AND"])
def test_a_query_of_only_joining_words_is_refused(ctx, query):  # noqa: F811
    from core.agent.context import Refused
    from core.agent.tests.test_warehouse_tools import FakeWarehouse
    from core.agent.tools.warehouse import search_posts

    with pytest.raises(Refused):
        search_posts(ctx, FakeWarehouse([]), query)


def test_recall_findings_takes_or_too(ctx):  # noqa: F811
    from core.agent.tests.test_warehouse_tools import FakeWarehouse
    from core.agent.tools.warehouse import recall_findings

    wh = FakeWarehouse([])
    recall_findings(ctx, wh, "amapiano OR gqom")
    assert sorted(v for k, v in wh.runs[0][1].items() if k.startswith("term_")) == ["amapiano", "gqom"]
    check_sql(wh.runs[0][0])


def test_the_purpose_hint_asks_for_plain_words_without_digits():
    assert "no digits" in toolset.SCHEMAS["sql_query"]["properties"]["purpose"]["description"]
    assert "OR between alternatives" in toolset.DESCRIPTIONS["search_posts"]


# Breadth (Albert, 4 October: answers drew on about 20 accounts while the store held thousands of posts)

def test_the_researcher_counts_the_whole_store_per_platform_before_reading_posts():
    skill = load_skill("culture-read")
    assert "Count the whole store first" in skill and "Cover every platform unless the question limits it" in skill
    assert "COUNT(DISTINCT p.creator_id) grouped by p.platform" in toolset.DESCRIPTIONS["sql_query"]


def test_claims_lead_with_store_totals_and_cite_posts_as_examples():
    assert "Give each claim the totals the queries give" in writer.WRITER_SYSTEM
    assert "Never open a claim with its figures" in writer.WRITER_SYSTEM
    writer.WRITER_SYSTEM.format(days=7)


def test_post_reads_and_query_rows_shown_are_wider():
    import inspect

    from core.agent.tools import warehouse
    assert warehouse.MAX_POSTS == 100 and writer.ROWS_SHOWN == 50
    assert inspect.signature(warehouse.search_posts).parameters["limit"].default == 50


def test_the_writer_sees_at_most_rows_shown_rows_of_a_query():
    block = writer._query_block("q_1", {"purpose": "p", "rows": [{"n": i} for i in range(writer.ROWS_SHOWN + 10)]})
    assert f"rows shown: {writer.ROWS_SHOWN} of {writer.ROWS_SHOWN + 10}" in block


@pytest.mark.parametrize("sql", ["SELECT 1 FROM intelligence_42_core.posts_5000 p",
                                 "SELECT 1 FROM intelligence_42_core.teen_users t"])
@pytest.mark.parametrize("purpose", ["", "top 20 sounds"])
def test_a_table_name_with_a_figure_or_breach_never_reaches_the_step(sql, purpose):
    _, text, _ = ask.describe_tool("mcp__f42__sql_query", {"sql": sql, "purpose": purpose}, None)
    assert text == "Counting in the warehouse"


# Saving a finding that cites posts the run saw only in query rows (live staging: obs1_ ids refused)

def test_save_finding_stores_posts_seen_only_in_query_rows_then_saves(ctx):  # noqa: F811
    from datetime import date

    from core.agent.tests.test_warehouse_tools import FakeWarehouse
    from core.agent.tools.sql_query import sql_query

    ctx.window_start, ctx.window_end = date(2026, 9, 22), date(2026, 9, 28)
    listing = FakeWarehouse([{"post_id": "obs1_aa", "n": 3}])
    q = sql_query(ctx, listing, "SELECT post_id FROM intelligence_42_core.posts", purpose="listed posts")["query_id"]
    post = {"post_id": "obs1_aa", "platform": "tiktok", "handle": "mbeqerh", "url": "https://t.example/1",
            "text": "gqom challenge", "published_at": "2026-09-27T10:00:00+02:00", "post_date": "2026-09-27",
            "geo_market": "ZA", "engagement": 5, "creator_id": "c1"}
    w = FakeWriter()
    out = save_finding(ctx, w, evidence_ids=["obs1_aa"], query_ids=[q], warehouse=FakeWarehouse([post]),
                       **{**GOOD, "label": "Observed"})
    assert out["finding_id"].startswith("f_") and "obs1_aa" in ctx.evidence
    commit_findings(ctx, w, [passed(GOOD["claim"], "observed", ["obs1_aa"])])
    assert w.inserts[0][1][0]["claims"][0]["evidence_post_ids"] == ["obs1_aa"]


def test_save_finding_still_refuses_an_id_no_query_row_returned(ctx):  # noqa: F811
    from datetime import date

    from core.agent.context import Refused
    from core.agent.tests.test_warehouse_tools import FakeWarehouse

    ctx.window_start, ctx.window_end = date(2026, 9, 22), date(2026, 9, 28)
    wh = FakeWarehouse([])
    with pytest.raises(Refused, match="obs1_made_up"):
        save_finding(ctx, FakeWriter(), evidence_ids=["obs1_made_up"], query_ids=[], warehouse=wh, **GOOD)
    assert wh.runs == []  # nothing fetched for an id the run never saw
