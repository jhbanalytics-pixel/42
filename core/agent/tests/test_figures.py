"""Reported figures for quantitative questions (core/agent/figures.py), the live item shapes Ask reads
(tools/socialcrawl.py flat_item) and article reading (tools/article.py). Offline: DuckDB for the warehouse, fakes for the
client, robots.txt and the model.

The case is the 16 October 2026 question behind Albert's screenshot: "Can 42 fill this gap: Exact quantitative
percentage breakdowns for all parties across Gauteng's local election". The answer read 194 posts, cited seven and
gave three findings saying polls were discussed, with no figure, while its cited posts named the polls."""

from datetime import date, datetime

import pytest

from core.agent import figures
from core.agent.context import SWEEP_TOOL, Refused, RunContext
from core.agent.tests.test_ask_depth_replay import ReplayWarehouse, posts_table
from core.agent.tools import article as article_tool
from core.agent.tools.sc_adapter import page_markdown
from core.agent.tools.socialcrawl import flat_item, socialcrawl_call

QUESTION = ("Can 42 fill this gap: Exact quantitative percentage breakdowns for all parties across Gauteng's local "
            "election?")
WINDOW = (date(2026, 9, 17), date(2026, 10, 16))
POLL = ("New poll puts parties virtually neck and neck in Gauteng: the ANC on 33.5% and the DA on 31%, with the EFF "
        "at 11% and ActionSA at 8%, according to the eNCA/MarkData survey of 1,500 registered voters conducted "
        "between 1 and 9 October.")


def make_ctx(**extra):
    return RunContext(run_id="r_fig", tier="T2", as_of=datetime(2026, 10, 16, 14, 0), market="ZA",
                      window_start=WINDOW[0], window_end=WINDOW[1], **extra)


def record(ctx, eid, text, *, platform="x", handle="News24", posted="2026-10-13T08:00:00"):
    ctx.evidence[eid] = {"id": eid, "platform": platform, "handle": handle, "url": f"https://src.example/{eid}",
                         "posted_at": posted, "market": "ZA", "text": text, "engagement": {"views": 10},
                         "flags": [], "source_market": None}


def fig(eid, quote, subject, value, **extra):
    base = {"evidence_id": eid, "quote": quote, "subject": subject, "value": value, "measure": "", "source": "",
            "fieldwork": "", "sample": "", "population": ""}
    return {**base, **extra}


# Intent and terms

@pytest.mark.parametrize("question", [
    QUESTION, "What share of the vote did the DA get in the Joburg by-election?", "Latest Kenya presidential poll",
    "Ipsos survey results on amapiano listeners", "what % of posts mention load shedding",
])
def test_a_question_that_asks_for_figures_is_quantitative(question):
    assert figures.quantitative_question(question)


@pytest.mark.parametrize("question", [
    "What are people in South Africa posting about this week?", "Which sounds lead on TikTok in Nigeria?",
    "Why is #gqomchallenge rising?",
])
def test_other_questions_are_not(question):
    assert not figures.quantitative_question(question)


def test_the_sweep_matches_the_question_words_names_first():
    terms = figures.question_terms(QUESTION)
    assert terms[0] == "Gauteng"
    assert "election" in terms and "percentage" not in terms and "parties" not in terms
    assert len(terms) <= figures.MAX_TERMS


# The figure sweep reads the whole store, on the SQL that ships

def poll_store():
    import duckdb

    con = duckdb.connect()
    posts_table(con)
    rows = []

    def add(pid, text, day=date(2026, 10, 12), market="ZA", engagement=10):
        rows.append((pid, "x", f"https://p.example/{pid}", f"c_{pid}", day, market, text, engagement))

    add("poll1", POLL, engagement=900)
    add("poll2", "Gauteng election: ANC 33.5% DA 31% in the eNCA/MarkData poll", engagement=50)
    add("words", "Gauteng election campaign heats up in Soweto")  # the topic, no percentage
    add("other", "Petrol price up 4% this month")  # a percentage, not the topic
    add("old", POLL, day=date(2026, 8, 1))  # outside the window
    add("ng", "Lagos election poll: APC 40%", market="NG")  # another market
    for i in range(400):
        add(f"noise{i}", "amapiano weekend vibes")
    con.executemany("INSERT INTO intelligence_42_core.posts VALUES (?, ?, ?, ?, NULL, ?, ?, 'geotag', ?, 10, 1, "
                    "NULL, NULL, ?, [])", rows)
    return con


def test_the_figure_sweep_stores_every_window_post_that_matches_and_carries_a_percentage():
    ctx = make_ctx()
    out = figures.figure_sweep(ctx, ReplayWarehouse(poll_store()), QUESTION)
    assert out["failed"] is None, out
    assert sorted(ctx.evidence) == ["poll1", "poll2"]
    assert out["posts"] == 2 and out["matched"] == 2
    query = next(iter(ctx.queries.values()))
    assert query["tool"] == SWEEP_TOOL and query["params"]["market"] == "ZA"


def test_a_failed_sweep_reports_and_does_not_raise():
    class Broken(ReplayWarehouse):
        def run(self, sql, params, max_bytes_billed):
            raise RuntimeError("warehouse down")

    ctx = make_ctx()
    out = figures.figure_sweep(ctx, Broken(poll_store()), QUESTION)
    assert out["failed"] and not ctx.evidence


# Verification in code: nothing reaches the reader that its source does not say

def test_figures_that_their_source_states_are_kept_with_its_fields():
    ctx = make_ctx()
    record(ctx, "p1", POLL)
    kept, cut = figures.verify([
        fig("p1", "the ANC on 33.5%", "ANC", 33.5, source="eNCA/MarkData", sample="1,500 registered voters",
            fieldwork="between 1 and 9 October", population="Gauteng", measure="poll"),
        fig("p1", "the DA on 31%", "DA", 31),
    ], ctx)
    assert cut == {}
    anc = kept[0]
    assert (anc["subject"], anc["value"], anc["unit"]) == ("ANC", 33.5, "%")
    assert anc["source"] == "eNCA/MarkData" and anc["sample"] == "1,500 registered voters"
    assert anc["url"] == "https://src.example/p1" and anc["handle"] == "News24"


@pytest.mark.parametrize("bad, reason", [
    (fig("p1", "the ANC on 35%", "ANC", 35), "quote not verbatim"),  # a figure the post does not hold
    (fig("p1", "the ANC on 33.5%", "ANC", 35), "value not in quote"),  # a value its quote does not say
    (fig("p1", "the ANC on 33.5%", "IFP", 33.5), "subject not in quote"),  # a party the quote does not name
    (fig("p1", "the ANC on 33.5% and the DA on 31%", "ANC", 3.5), "value not in quote"),  # part of a figure
    (fig("missing", "the ANC on 33.5%", "ANC", 33.5), "record not in this run"),
    (fig("p1", "the ANC on 33.5%", "ANC", True), "no value"),
])
def test_a_figure_its_source_does_not_state_is_cut(bad, reason):
    ctx = make_ctx()
    record(ctx, "p1", POLL)
    kept, cut = figures.verify([bad], ctx)
    assert kept == [] and cut == {reason: 1}


def test_fields_the_source_does_not_state_read_as_not_stated():
    ctx = make_ctx()
    record(ctx, "p1", POLL)
    kept, _ = figures.verify([fig("p1", "the DA on 31%", "DA", 31, source="Ipsos", sample="2,000 adults",
                                  measure="national vote share")], ctx)
    assert kept[0]["source"] == "" and kept[0]["sample"] == "" and kept[0]["measure"] == ""


def test_per_cent_and_a_decimal_comma_are_read_as_written():
    ctx = make_ctx()
    record(ctx, "p1", "Die ANC staan op 33,5% en die DA op 31 per cent in Gauteng.")
    kept, cut = figures.verify([fig("p1", "ANC staan op 33,5%", "ANC", 33.5),
                                fig("p1", "DA op 31 per cent", "DA", 31)], ctx)
    assert [k["value"] for k in kept] == [33.5, 31.0] and cut == {}


def test_a_value_is_kept_only_beside_its_own_subject():
    # Independent review, 16 October: both quotes are verbatim and name the DA, but neither value is the DA's.
    ctx = make_ctx()
    record(ctx, "p1", "Poll: the ANC on 33.5% and the DA on 31% in Gauteng; turnout was 58%.")
    kept, cut = figures.verify([fig("p1", "the ANC on 33.5% and the DA on 31%", "DA", 33.5),
                                fig("p1", "the DA on 31% in Gauteng; turnout was 58%", "DA", 58),
                                fig("p1", "the ANC on 33.5% and the DA on 31%", "DA", 31)], ctx)
    assert [(k["subject"], k["value"]) for k in kept] == [("DA", 31.0)]
    assert cut == {"value not written next to its subject": 2}


def test_a_figure_written_before_its_subject_or_far_from_it_is_cut():
    ctx = make_ctx()
    record(ctx, "p1", "33% for the ANC. The DA said in a long statement that turnout was 58%.")
    kept, cut = figures.verify([fig("p1", "33% for the ANC", "ANC", 33),
                                fig("p1", "The DA said in a long statement that turnout was 58%", "DA", 58)], ctx)
    assert kept == [] and cut == {"value not written next to its subject": 2}


def test_one_subject_given_two_values_in_one_record_shows_neither():
    ctx = make_ctx()
    record(ctx, "p1", "The DA on 31% here and the DA on 28% there.")
    kept, cut = figures.verify([fig("p1", "DA on 31%", "DA", 31), fig("p1", "DA on 28%", "DA", 28)], ctx)
    assert kept == [] and cut == {"one subject given two values": 2}


def test_the_figures_prompt_never_passes_the_input_the_hold_reserves():
    # Independent review, 16 October: 60 long records made a prompt three times the reserved input, so the call was
    # refused at reservation and stopped a finished answer.
    ctx = make_ctx()
    for i in range(60):
        record(ctx, f"p{i}", ("Ipsos poll: the ANC on 33.5% and the DA on 31%. " + "ë" * 200 + " ") * 30)
    user = figures.figures_user(QUESTION, figures.figure_records(ctx))
    assert figures.input_bound(user) <= figures.FIGURES_INPUT_TOKENS
    assert user.count("<untrusted_content>") >= 1


# Tables, the lead line and what is never combined

def test_one_table_per_source_newest_first_and_a_repeat_is_folded_in():
    ctx = make_ctx()
    record(ctx, "p1", POLL, posted="2026-10-13T08:00:00")
    record(ctx, "p2", POLL, handle="TimesLIVE", posted="2026-10-11T08:00:00")
    record(ctx, "p3", "An older Ipsos poll had the ANC at 36% and the DA at 26% in Gauteng.",
           handle="SundayTimesZA", posted="2026-10-01T08:00:00")
    listed = [fig(e, q, s, v, source="eNCA/MarkData") for e in ("p1", "p2")
              for q, s, v in (("the ANC on 33.5%", "ANC", 33.5), ("the DA on 31%", "DA", 31))]
    listed += [fig("p3", "the ANC at 36%", "ANC", 36, source="Ipsos"),
               fig("p3", "the DA at 26%", "DA", 26, source="Ipsos")]
    kept, _ = figures.verify(listed, ctx)
    built = figures.tables(kept)
    assert [t["source"] for t in built] == ["eNCA/MarkData", "Ipsos"]
    assert [r["subject"] for r in built[0]["rows"]] == ["ANC", "DA"]
    assert built[0]["also_reported_by"][0]["publisher"] == "TimesLIVE"
    assert figures.lead(built) == "eNCA/MarkData, published 13 October 2026: ANC 33.5%, DA 31%."


def test_shares_that_add_past_a_whole_carry_a_note():
    ctx = make_ctx()
    record(ctx, "p1", "Approval: Lesufi 61%, Makhura 58% in Gauteng surveys.")
    kept, _ = figures.verify([fig("p1", "Lesufi 61%", "Lesufi", 61), fig("p1", "Makhura 58%", "Makhura", 58)], ctx)
    assert figures.tables(kept)[0]["note"] == figures.SUM_NOTE


def test_a_table_with_no_named_source_says_who_posted_it():
    ctx = make_ctx()
    record(ctx, "p1", POLL)
    kept, _ = figures.verify([fig("p1", "the ANC on 33.5%", "ANC", 33.5)], ctx)
    assert figures.lead(figures.tables(kept)).startswith("A post by News24, published 13 October 2026: ANC 33.5%")


def test_extraction_reads_only_records_that_carry_a_percentage():
    ctx = make_ctx()
    record(ctx, "p1", POLL)
    record(ctx, "p2", "Gauteng election campaign heats up")
    seen = {}

    class Model:
        def complete_json(self, *, system, user, schema, model, max_tokens):
            seen["user"] = user
            return {"figures": [fig("p1", "the DA on 31%", "DA", 31)]}, {"input_tokens": 1, "output_tokens": 1,
                                                                        "usd": 0.0}

    listed, _, dispatched = figures.extract_figures(Model(), QUESTION, ctx, "m")
    assert dispatched and listed[0]["subject"] == "DA"
    assert "[p1]" in seen["user"] and "[p2]" not in seen["user"]
    assert "<untrusted_content>" in seen["user"]
    empty = make_ctx()
    assert figures.extract_figures(Model(), QUESTION, empty, "m")[2] is False


# Live items: SocialCrawl's envelopes keep their text, author and time

class Client:
    def __init__(self, items=None, page=None, status="ok"):
        self.items, self.page, self.status, self.calls = items or [], page, status, []

    def quote(self, route, params):
        return 1.0

    def call(self, route, params, *, lane, run_id, max_credits):
        self.calls.append((route, dict(params)))
        return {"items": self.items, "page": self.page, "next_cursor": None, "credits_charged": 1.0,
                "status": self.status, "cache_hit": False, "reason": ""}


class NoSuppressions:
    """The suppression read, with nobody suppressed (a live post 42 cannot check is withheld)."""

    def run(self, sql, params, max_bytes_billed):
        return []


NEWS_ITEM = {"article": {"title": "New poll puts parties virtually neck and neck in Gauteng",
                         "url": "https://www.timeslive.co.za/politics/poll", "source": "timeslive.co.za",
                         "domain": "timeslive.co.za", "snippet": "The ANC is on 33.5% and the DA on 31%.",
                         "published_at": "2026-10-11 07:30:00 +00:00"}}
POST_ITEM = {"post": {"id": "123", "platform": "tiktok", "url": "https://www.tiktok.com/@encanews/video/123",
                      "author": {"username": "encanews"}, "content": {"text": "The eNCA/MarkData 2026 election poll"},
                      "published_at": "2026-10-13T10:00:00Z", "engagement": {"views": 5000}}}


def test_a_news_result_becomes_a_citable_record():
    flat = flat_item(NEWS_ITEM)
    assert flat["platform"] == "news" and flat["handle"] == "timeslive.co.za"
    assert flat["text"] == ("New poll puts parties virtually neck and neck in Gauteng. "
                            "The ANC is on 33.5% and the DA on 31%.")
    assert flat["posted_at"] == "2026-10-11T07:30:00+00:00"


def test_a_post_envelope_keeps_its_author_text_and_time():
    flat = flat_item(POST_ITEM)
    assert (flat["handle"], flat["text"], flat["posted_at"]) == (
        "encanews", "The eNCA/MarkData 2026 election poll", "2026-10-13T10:00:00Z")
    assert flat_item({"text": "flat", "handle": "a"}) == {"text": "flat", "handle": "a"}


def test_live_search_stores_envelope_items_with_text_and_takes_keyword_for_news():
    ctx = make_ctx()
    client = Client(items=[NEWS_ITEM, POST_ITEM])
    out = socialcrawl_call(ctx, client, "google_news", "search",
                           {"query": "Gauteng poll", "location_name": "South Africa"}, max_credits=5,
                           warehouse=NoSuppressions())
    assert client.calls[0][1] == {"keyword": "Gauteng poll", "location_name": "South Africa"}
    stored = [ctx.evidence[e] for e in out["evidence_ids"]]
    assert all(r["text"] and r["handle"] and r["posted_at"] for r in stored)
    assert {r["platform"] for r in stored} == {"news", "tiktok"}


# Articles

class Robots:
    def __init__(self, allow=True):
        self.allow = allow

    def allowed(self, url):
        return self.allow


ARTICLE = ("# New poll puts parties virtually neck and neck\n\n![photo](https://img.example/a.jpg)\n\n"
           "The [ANC](https://x.example/anc) is on **33.5%**, the DA on 31%, the EFF on 11%.\n\nMore text.")


def news_ctx():
    ctx = make_ctx()
    socialcrawl_call(ctx, Client(items=[NEWS_ITEM]), "google_news", "search", {"keyword": "Gauteng poll"},
                     max_credits=5, warehouse=NoSuppressions())
    return ctx, next(iter(ctx.evidence))


def test_an_article_is_read_into_a_citable_record_with_its_results_publisher():
    ctx, eid = news_ctx()
    client = Client(page=ARTICLE)
    out = article_tool.read_article(ctx, client, eid, robots=Robots())
    assert out["status"] == "ok" and client.calls == [("web/scrape", {"url": NEWS_ITEM["article"]["url"],
                                                                     "only_main_content": "true"})]
    stored = ctx.evidence[out["evidence_id"]]
    assert "The ANC is on 33.5%, the DA on 31%, the EFF on 11%." in stored["text"]
    assert "img.example" not in stored["text"] and stored["handle"] == "timeslive.co.za"
    assert ctx.credits_spent == 2.0  # the search and the page
    kept, _ = figures.verify([fig(out["evidence_id"], "the EFF on 11%", "EFF", 11)], ctx)
    assert kept and kept[0]["url"] == NEWS_ITEM["article"]["url"]
    again = article_tool.read_article(ctx, client, eid, robots=Robots())
    assert again["already_read"] and len(client.calls) == 1


def test_only_a_news_result_from_this_run_can_be_opened():
    ctx, _ = news_ctx()
    record(ctx, "p1", POLL)
    with pytest.raises(Refused):
        article_tool.read_article(ctx, Client(page=ARTICLE), "p1", robots=Robots())
    with pytest.raises(Refused):
        article_tool.read_article(ctx, Client(page=ARTICLE), "https://anything.example", robots=Robots())


def test_a_page_robots_txt_forbids_is_not_fetched():
    ctx, eid = news_ctx()
    client = Client(page=ARTICLE)
    out = article_tool.read_article(ctx, client, eid, robots=Robots(allow=False))
    assert out["status"] == "robots_refused" and client.calls == [] and out["evidence_id"] is None


def test_articles_per_question_are_capped():
    ctx, eid = news_ctx()
    for i in range(article_tool.MAX_ARTICLES):
        ctx.evidence[f"article_{i}"] = {"id": f"article_{i}"}
    with pytest.raises(Refused):
        article_tool.read_article(ctx, Client(page=ARTICLE), eid, robots=Robots())


def test_the_adapter_reads_a_scraped_pages_markdown():
    assert page_markdown({"data": {"page": {"status_code": 200, "content": {"markdown": "# Hi"}}}}) == "# Hi"
    assert page_markdown({"data": {"page": {"status_code": 404, "content": {"markdown": "gone"}}}}) is None
    assert page_markdown({"data": {}}) is None


# Through run_ask: the figures travel beside the answer, inside the question's hold

class FiguresModel:
    """test_ask's FakeModel, plus the figure listing: one figure the record states and one it does not."""

    def __init__(self, fail=False):
        from core.agent.tests.test_ask import FakeModel

        self.inner, self.fail, self.figure_calls = FakeModel(), fail, []

    def complete_json(self, *, system, user, schema, model, max_tokens):
        if schema is figures.FIGURES_SCHEMA:
            self.figure_calls.append({"user": user, "max_tokens": max_tokens})
            if self.fail:
                raise RuntimeError("model down")
            return {"figures": [fig("poll_post", "the ANC on 33.5%", "ANC", 33.5, source="eNCA/MarkData"),
                                fig("poll_post", "the DA on 31%", "DA", 31, source="eNCA/MarkData"),
                                fig("poll_post", "the IFP on 40%", "IFP", 40)]}, \
                {"input_tokens": 100, "output_tokens": 20, "usd": 0.0006}
        return self.inner.complete_json(system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)


def poll_research(seen):
    from core.agent.tests.test_ask import make_research

    inner = make_research()

    def research(ctx, prompt, options, emit, should_stop):
        seen["prompt"] = prompt
        out = inner(ctx, prompt, options, emit, should_stop)
        record(ctx, "poll_post", POLL)
        return out

    return research


def ask_harness(model, seen):
    from core.agent.tests.test_ask import Harness

    return Harness(research=poll_research(seen), model=model)


def test_a_quantitative_ask_carries_checked_figures_beside_its_answer():
    from core.agent.answer import validate_answer

    seen, model = {}, FiguresModel()
    out = ask_harness(model, seen).run(question="Gauteng local election poll: party percentage breakdowns")
    assert ask_module().QUANT_NOTE in seen["prompt"]
    payload = out["run"]["figures"]
    assert payload["status"] == "ok" and payload["checked"]["figures_cut"] == {"quote not verbatim": 1}
    table = payload["tables"][0]
    assert [(r["subject"], r["value"]) for r in table["rows"]] == [("ANC", 33.5), ("DA", 31.0)]
    assert table["url"] == "https://src.example/poll_post" and table["source"] == "eNCA/MarkData"
    assert payload["lead"] == "eNCA/MarkData, published 13 October 2026: ANC 33.5%, DA 31%."
    assert "figures" not in out["answer"] and validate_answer(out["answer"]) == []
    assert len(model.figure_calls) == 1


def test_the_figures_call_is_inside_the_questions_hold():
    ask = ask_module()
    model = "gemini-3.8-flash"
    assert ask.figures_usd(model) == ask.call_usd(model, figures.FIGURES_INPUT_TOKENS, figures.FIGURES_MAX_TOKENS)
    assert ask.figures_usd(model) > 0


def test_a_failed_figures_call_shows_none_and_says_so():
    seen = {}
    out = ask_harness(FiguresModel(fail=True), seen).run(question="Latest Gauteng election poll percentages")
    assert out["run"]["figures"]["status"] == "failed" and out["run"]["figures"]["tables"] == []
    assert ask_module().FIGURES_FAILED in out["run"]["notices"]


def test_a_question_that_asks_for_no_figures_makes_no_figures_call():
    seen, model = {}, FiguresModel()
    out = ask_harness(model, seen).run()
    assert "figures" not in out["run"] and model.figure_calls == []
    assert ask_module().QUANT_NOTE not in seen["prompt"]


def test_a_quantitative_t2_question_sends_a_news_researcher_first():
    ask = ask_module()
    assert ask.platform_groups(["ZA"], requested_platforms=["news"])[0] == "news"
    assert "read_article" in ask._lane_prompt("q", "news", 4, 10) and "keyword" in ask._lane_prompt("q", "news", 4, 10)
    assert "read_article" not in ask._lane_prompt("q", "tiktok", 4, 10)


def ask_module():
    from core.agent import ask

    return ask
