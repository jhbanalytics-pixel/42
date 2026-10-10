"""Ask speed, fix 2 (ASK-LATENCY.md, not ranked): a numeral the writer left unpinned is pinned in code when the answer
is plain, so the numeric repair model call (about 18 s when it fires) is skipped.

The pin is narrow on purpose. It adds a numbers entry only when one cell of a query the claim already cites holds the
whole number, in a column the claim names next to the figure ("5 creators", column creators). Anything else, a
percentage, a decimal, two columns holding the value, a bad entry already on the claim, goes to the repair call exactly
as before, with the draft the writer wrote. After a pin the same K2 numeral check that listed the issue must list none,
and the K2 check, the support check and every other rule still run on the pinned draft.
"""

import copy
from decimal import Decimal

import pytest

from core.agent import ask, writer
from core.agent.context import RunContext
from core.agent.tests.test_ask import (NOW, WRITER_OUT, FakeWarehouse, Harness, NumericRepairModel)
from core.agent.writer import WRITER_SCHEMA

COUNT_ROWS = [{"posts": 6, "creators": 5, "platforms": 2}]


class CreatorsWarehouse(FakeWarehouse):
    """The Harness warehouse whose count names its column the way the claim does."""

    def run(self, sql, params, max_bytes_billed):
        rows = super().run(sql, params, max_bytes_billed)
        return copy.deepcopy(COUNT_ROWS) if "COUNT(" in sql else rows


def unpinned_draft():
    """The writer's draft with the pin for "5 creators" left off."""
    draft = copy.deepcopy(WRITER_OUT)
    draft["claims"][0]["numbers"] = [draft["claims"][0]["numbers"][0]]
    return draft


def audit(h):
    return [row for _, inserted in h.tables.inserts for row in inserted]


def test_a_plain_unpinned_count_is_pinned_in_code_and_the_repair_call_is_skipped():
    model = NumericRepairModel([unpinned_draft(), copy.deepcopy(WRITER_OUT)])
    h = Harness(check=None, model=model)
    h.warehouse = h.deps.warehouse = CreatorsWarehouse()

    out = h.run()

    assert len([c for c in model.calls if c["schema"] is WRITER_SCHEMA]) == 1  # the repair call never happened
    claim = next(c for c in out["answer"]["claims"] if c["id"] == "c1")
    pinned = [n for n in claim["numbers"] if n["value"] == 5]
    assert len(pinned) == 1 and pinned[0]["unit"] == "creators" and pinned[0]["query_id"] == "q_3"
    assert pinned[0]["run_id"] == out["run"]["run_id"] and pinned[0]["result_hash"] == out["query_receipts"]["q_3"]["result_hash"]
    k2 = [r for r in audit(h) if r["claim_id"] == "c1" and r["rule"] == "K2"]
    assert k2 and all(r["verdict"] == "pass" for r in k2)  # the K2 check ran on the pinned draft and passed it
    assert "numeric_repair" not in [s["name"] for s in out["run"]["timings"]["spans"] if s["kind"] == "phase"]
    assert out["run"]["tokens"]["input"] == 1000 + sum(u["input_tokens"] for u in model.usages)  # nothing billed for it


def test_a_count_whose_column_the_claim_does_not_name_still_goes_to_the_repair_call():
    model = NumericRepairModel([unpinned_draft(), copy.deepcopy(WRITER_OUT)])
    h = Harness(check=None, model=model)  # the default count names its column authors, the claim says creators

    out = h.run()

    writer_calls = [c for c in model.calls if c["schema"] is WRITER_SCHEMA]
    assert len(writer_calls) == 2
    assert "Numeric repair instructions" in writer_calls[1]["user"]
    assert "numeral 5 has no pinned" not in repr(out["run"]["notices"])
    assert "numeric_repair" in [s["name"] for s in out["run"]["timings"]["spans"] if s["kind"] == "phase"]


def context_with_count(rows):
    ctx = RunContext(run_id="r_pin", tier="T1", as_of=NOW, market="ZA", window_start=NOW.date(), window_end=NOW.date())
    qid, _ = ctx.record_query("SELECT COUNT(*) AS posts FROM intelligence_42_core.posts p", {}, rows, "counts")
    return ctx, qid


class RowsWarehouse(FakeWarehouse):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def run(self, sql, params, max_bytes_billed):
        return copy.deepcopy(self.rows)


def draft_for(text, numbers, qid):
    return {"claims": [{"id": "c1", "text": text, "label": "observed", "kind": "observation", "evidence_ids": [],
                        "quotes": [], "numbers": [{**n, "query_id": qid} for n in numbers]}], "gaps": []}


def pin(text, numbers, rows):
    ctx, qid = context_with_count(rows)
    wh = RowsWarehouse(rows)
    draft = draft_for(text, numbers, qid)
    for number in draft["claims"][0]["numbers"]:
        number["run_id"], number["result_hash"] = ctx.run_id, ctx.queries[qid]["result_hash"]
    window = (NOW.date(), NOW.date())
    reruns = {}
    issues = writer.unpinned_claim_numerals(draft, ctx, wh, window=window, reruns=reruns)
    pinned = writer.pin_numerals_in_code(draft, issues, ctx, wh, window=window, reruns=reruns)
    still = (writer.unpinned_claim_numerals(pinned, ctx, wh, window=window, reruns=reruns)
             if pinned is not None else None)
    return draft, issues, pinned, still


SIX = {"value": 6, "unit": "posts"}


def test_the_pin_adds_one_entry_that_the_k2_numeral_check_accepts_and_changes_nothing_else():
    draft, issues, pinned, still = pin("6 posts came from 5 creators.", [SIX], COUNT_ROWS)

    assert issues and still == []
    assert pinned is not draft and draft["claims"][0]["numbers"] == [{**SIX, "query_id": "q_1", "run_id": "r_pin",
                                                                      "result_hash": draft["claims"][0]["numbers"][0]["result_hash"]}]
    added = pinned["claims"][0]["numbers"][1:]
    assert len(added) == 1 and added[0]["value"] == 5 and added[0]["unit"] == "creators"
    assert added[0]["query_id"] == "q_1" and added[0]["run_id"] == "r_pin" and added[0]["result_hash"].startswith("sha256:")
    assert {k: v for k, v in pinned["claims"][0].items() if k != "numbers"} == \
           {k: v for k, v in draft["claims"][0].items() if k != "numbers"}


@pytest.mark.parametrize("text, numbers, rows, why", [
    ("6 posts came from 5 authors.", [SIX], COUNT_ROWS, "the claim names a different word than the column"),
    ("6 posts, up 20% creators.", [SIX], [{"posts": 6, "creators": 20}], "a percentage is never pinned in code"),
    ("6 posts came from 5.5 creators.", [SIX], [{"posts": 6, "creators": 5.5}], "a decimal is never pinned in code"),
    ("6 posts came from 7 creators.", [SIX], COUNT_ROWS, "no cell holds the value"),
    ("6 posts came from 5 creators.", [SIX], [{"posts": 6, "creators": 5, "authors": 5}], "two columns hold the value"),
    ("6 posts came from 5 creators and 5 creators.", [SIX], COUNT_ROWS, "the figure is written twice"),
    ("6 posts came from 5 creators.", [], COUNT_ROWS, "the claim cites no query yet"),
    ("6 posts came from 5 or so more creators.", [SIX], COUNT_ROWS, "the column word is past the two words after the figure"),
    ("6 posts came from 5 or more creators.", [SIX], COUNT_ROWS, "the column word is the third word after the figure"),
    ("6 posts came from 1 creators.", [SIX], [{"posts": 6, "creators": True}], "a bool cell is not a count"),
    ("6 posts came from 5 creators.", [SIX], [{"posts": 6, "creators": Decimal(5)}], "only int and float cells are read"),
    ("6 posts came from 5 creators.", [{"value": 61, "unit": "posts"}], COUNT_ROWS, "an entry already on the claim is bad"),
    ("6 posts came from 5 creators.", [SIX, {"value": 61, "unit": "posts"}], COUNT_ROWS,
     "a good entry beside a bad one: the bad one stays for the repair call"),
])
def test_anything_the_pin_cannot_settle_beyond_doubt_is_left_to_the_repair_call(text, numbers, rows, why):
    draft, issues, pinned, still = pin(text, numbers, rows)

    assert issues, why
    assert pinned is None, why


def test_a_pin_that_would_leave_any_numeral_open_is_not_used_for_the_numerals_it_could_settle():
    draft, issues, pinned, _ = pin("6 posts came from 5 creators, up 20%.", [SIX], COUNT_ROWS)

    assert pinned is None  # all or nothing: the repair call gets the draft the writer wrote, with every issue


class DriftingWarehouse(CreatorsWarehouse):
    """The count the research read has the posts on one row and the creators on another; every read after it (the K2
    re-runs) says 4 creators. The 6 posts still reproduce, the 5 creators do not."""

    def run(self, sql, params, max_bytes_billed):
        rows = super().run(sql, params, max_bytes_billed)
        if "COUNT(" not in sql:
            return rows
        self.count_reads = getattr(self, "count_reads", 0) + 1
        return [{"platform": "tiktok", "posts": 6}, {"platform": "x", "creators": 5 if self.count_reads == 1 else 4}]


def test_a_pin_the_rerun_does_not_reproduce_is_not_used_and_the_repair_call_runs():
    model = NumericRepairModel([unpinned_draft(), copy.deepcopy(WRITER_OUT)])
    h = Harness(check=None, model=model)
    h.warehouse = h.deps.warehouse = DriftingWarehouse()

    h.run()

    assert len([c for c in model.calls if c["schema"] is WRITER_SCHEMA]) == 2  # K2 would have cut the pin, so repair


TAGS = [{"hashtag": "#amapiano", "posts": 6, "creators": 5}, {"hashtag": "#gqom", "posts": 9, "creators": 3}]
NINE = {"value": 9, "unit": "posts"}


def k2_verdicts(draft, ctx, wh):
    from core.agent import checks

    _, verdicts = checks.check_answer(copy.deepcopy(draft), ctx, wh, window=(NOW.date(), NOW.date()), markets=["ZA"])
    return [(v["verdict"], v["reason"] or "") for v in verdicts if v["rule"] == "K2"]


def pinned_for(rows, text, numbers):
    ctx, qid = context_with_count(rows)
    wh = RowsWarehouse(rows)
    draft = draft_for(text, numbers, qid)
    for number in draft["claims"][0]["numbers"]:
        number["run_id"], number["result_hash"] = ctx.run_id, ctx.queries[qid]["result_hash"]
    window, reruns = (NOW.date(), NOW.date()), {}
    issues = writer.unpinned_claim_numerals(draft, ctx, wh, window=window, reruns=reruns)
    pinned = writer.pin_numerals_in_code(draft, issues, ctx, wh, window=window, reruns=reruns)
    return draft, issues, pinned, ctx, wh, window, reruns


def test_a_figure_from_another_subjects_row_is_not_pinned_and_k2_still_cuts_it():
    """#gqom drew 3 creators; the writer wrote 5, which is #amapiano's. The pin must not bind it to that row."""
    draft, issues, pinned, ctx, wh, _, _ = pinned_for(TAGS, "#gqom drew 9 posts from 5 creators.", [NINE])

    assert issues and pinned is None
    assert any(v == "cut" and "numeral 5 has no pinned" in why for v, why in k2_verdicts(draft, ctx, wh))


def test_the_true_figure_from_the_subjects_own_row_is_pinned():
    _, _, pinned, ctx, wh, window, reruns = pinned_for(TAGS, "#gqom drew 9 posts from 3 creators.", [NINE])

    assert pinned is not None and pinned["claims"][0]["numbers"][-1]["value"] == 3
    assert writer.unpinned_claim_numerals(pinned, ctx, wh, window=window, reruns=reruns) == []


def test_a_value_held_by_more_than_one_row_the_claim_names_is_not_pinned():
    rows = [{"hashtag": "#amapiano", "posts": 6, "creators": 5}, {"hashtag": "#gqom", "posts": 9, "creators": 5}]

    _, issues, pinned, *_ = pinned_for(rows, "#gqom and #amapiano drew 6 posts from 5 creators.",
                                       [{"value": 6, "unit": "posts"}])

    assert issues and pinned is None


def shared(extra):
    return [{**extra, "hashtag": "#amapiano", "posts": 6, "creators": 5}, {**extra, "hashtag": "#gqom", "posts": 9, "creators": 3}]


@pytest.mark.parametrize("rows, text", [
    (TAGS, "#gqom outpaced #amapiano, drawing 9 posts from 5 creators."),
    (shared({"platform": "TikTok"}), "#gqom drew 9 posts from 5 creators on TikTok."),
    (shared({"market": "ZA"}), "In ZA, #gqom drew 9 posts from 5 creators."),
], ids=["claim-names-both-subjects", "rows-share-a-platform-cell", "rows-share-a-market-cell"])
def test_a_figure_is_pinned_only_from_the_row_that_holds_the_claims_own_good_number(rows, text):
    """The anchor row is the one row holding the existing good number (9 posts, #gqom's). The 5 sits on #amapiano's
    row, so naming both subjects, or sharing a platform or market cell, must not let it be pinned."""
    draft, issues, pinned, ctx, wh, _, _ = pinned_for(rows, text, [NINE])

    assert issues and pinned is None
    assert any(v == "cut" and "numeral 5 has no pinned" in why for v, why in k2_verdicts(draft, ctx, wh))


def test_a_figure_on_the_anchor_row_is_not_pinned_when_the_claim_names_both_subjects():
    """Flipped from a pin (lead ruling W3-1): one repair call saved is a convenience, and a claim naming a second
    subject's row may be giving the figure to that subject, so it goes to the repair call and K2."""
    _, issues, pinned, *_ = pinned_for(TAGS, "#gqom outpaced #amapiano, drawing 9 posts from 3 creators.", [NINE])

    assert issues and pinned is None


GQOM_FIRST = [{"hashtag": "#gqom", "posts": 9, "creators": 5}, {"hashtag": "#amapiano", "posts": 7, "creators": 6}]


@pytest.mark.parametrize("text", [
    "#gqom drew 9 posts, while #amapiano reached 5 creators.",
    "#gqom drew 9 posts and #amapiano drew 5 creators.",
    "#gqom drew 9 posts; on #amapiano 5 creators posted.",
], ids=["while", "and", "semicolon"])
def test_a_figure_the_sentence_gives_to_a_second_subject_is_not_pinned_from_the_anchor_row(text):
    """The 5 is #gqom's creators in the data, but each sentence gives it to #amapiano, whose count is 6. Pinning it
    from the anchor row would let K2 pass a wrong attribution."""
    draft, issues, pinned, ctx, wh, _, _ = pinned_for(GQOM_FIRST, text, [NINE])

    assert issues and pinned is None
    assert any(v == "cut" and "numeral 5 has no pinned" in why for v, why in k2_verdicts(draft, ctx, wh))


def test_a_claim_naming_only_the_anchor_subject_is_still_pinned_beside_a_row_it_does_not_name():
    _, _, pinned, ctx, wh, window, reruns = pinned_for(GQOM_FIRST, "#gqom drew 9 posts from 5 creators.", [NINE])

    assert pinned is not None and pinned["claims"][0]["numbers"][-1]["value"] == 5
    assert writer.unpinned_claim_numerals(pinned, ctx, wh, window=window, reruns=reruns) == []


def test_a_good_number_held_by_more_than_one_row_has_no_anchor_row_and_nothing_is_pinned():
    rows = [{"hashtag": "#amapiano", "posts": 6, "creators": 5}, {"hashtag": "#gqom", "posts": 6, "creators": 4}]

    _, issues, pinned, *_ = pinned_for(rows, "#amapiano drew 6 posts from 5 creators.", [SIX])

    assert issues and pinned is None


@pytest.mark.parametrize("numbers", [[SIX, NINE], [NINE, SIX]], ids=["amapiano-first", "gqom-first"])
def test_good_numbers_held_by_different_rows_have_no_single_anchor_row_and_nothing_is_pinned(numbers):
    rows = [{"hashtag": "#amapiano", "posts": 6, "creators": 5}, {"hashtag": "#gqom", "posts": 9, "creators": 3}]

    _, issues, pinned, *_ = pinned_for(rows, "#amapiano drew 6 posts and #gqom 9 posts, 5 creators.", numbers)

    assert issues and pinned is None


@pytest.mark.parametrize("figure", ["5", "3"], ids=["amapiano-figure", "gqom-figure"])
def test_the_figure_of_either_of_two_anchor_candidates_is_not_pinned(figure):
    rows = [{"hashtag": "#amapiano", "posts": 6, "creators": 5}, {"hashtag": "#gqom", "posts": 9, "creators": 3}]

    _, issues, pinned, *_ = pinned_for(rows, f"#amapiano drew 6 posts and #gqom 9 posts, {figure} creators.", [SIX, NINE])

    assert issues and pinned is None


def test_a_good_number_no_plain_cell_holds_leaves_no_anchor_row_whatever_the_other_good_numbers_hold():
    rows = [{"hashtag": "#amapiano", "posts": 6, "creators": 5}, {"hashtag": "#gqom", "posts": Decimal(9), "creators": 3}]

    _, issues, pinned, *_ = pinned_for(rows, "#amapiano drew 6 posts and #gqom 9 posts, 5 creators.", [SIX, NINE])

    assert issues and pinned is None


def test_a_row_the_claim_does_not_name_is_not_read_even_when_it_is_the_anchor():
    _, issues, pinned, *_ = pinned_for(TAGS, "9 posts came from 3 creators.", [NINE])

    assert issues and pinned is None


ONE = {"value": 1, "unit": "posts"}


def test_a_bool_cell_is_not_a_holder_so_it_cannot_make_a_second_anchor_row():
    """True == 1 in Python. #amapiano's flag must not count as a row holding the good number 1, or #gqom's row would
    stop being the one anchor row."""
    rows = [{"hashtag": "#gqom", "posts": 1, "creators": 3}, {"hashtag": "#amapiano", "posts": 6, "creators": 5, "live": True}]

    _, issues, pinned, ctx, wh, window, reruns = pinned_for(rows, "#gqom drew 1 posts from 3 creators.", [ONE])

    assert issues and pinned is not None and pinned["claims"][0]["numbers"][-1]["value"] == 3
    assert writer.unpinned_claim_numerals(pinned, ctx, wh, window=window, reruns=reruns) == []


def test_a_bool_cell_alone_is_never_the_anchor_row_and_nothing_is_pinned():
    rows = [{"hashtag": "#gqom", "live": True, "creators": 3}]
    ctx, qid = context_with_count(rows)

    assert writer._anchor_row([{**ONE, "query_id": qid}], ctx) is None


def test_a_row_that_is_not_a_dict_has_no_anchor_row_and_is_refused_without_an_error():
    rows = [{"hashtag": "#gqom", "posts": 6, "creators": 3}, 6]
    ctx, qid = context_with_count(rows)

    assert writer._anchor_row([{"value": 6, "unit": "posts", "query_id": qid}], ctx) is None


def test_a_query_with_a_row_that_is_not_a_dict_is_left_to_the_repair_call():
    rows = [{"hashtag": "#gqom", "posts": 6, "creators": 3}, 6]

    _, issues, pinned, *_ = pinned_for(rows, "#gqom drew 6 posts from 3 creators.", [SIX])

    assert pinned is None


GQ = {"hashtag": "#gqom", "posts": 9, "creators": 5}
AM = {"hashtag": "#amapiano", "posts": 7, "creators": 6}
KW = {"hashtag": "#kwaito", "posts": 4, "creators": 2}


def pinned_across(queries, text, numbers_in_first=True):
    """Each list of rows is one recorded query; the claim cites only the first, through its good number."""
    ctx = RunContext(run_id="r_pin", tier="T1", as_of=NOW, market="ZA", window_start=NOW.date(), window_end=NOW.date())
    by_sql, qids = {}, []
    for n, rows in enumerate(queries):
        sql = f"SELECT COUNT(*) AS posts FROM intelligence_42_core.posts p WHERE n = {n}"
        qid, _ = ctx.record_query(sql, {}, rows, "counts")
        qids.append(qid)
        by_sql[sql] = rows

    class Many(RowsWarehouse):
        def run(self, sql, params, max_bytes_billed):
            return copy.deepcopy(by_sql[sql])

    wh = Many([])
    draft = draft_for(text, [NINE], qids[0])
    for number in draft["claims"][0]["numbers"]:
        number["run_id"], number["result_hash"] = ctx.run_id, ctx.queries[qids[0]]["result_hash"]
    window, reruns = (NOW.date(), NOW.date()), {}
    issues = writer.unpinned_claim_numerals(draft, ctx, wh, window=window, reruns=reruns)
    pinned = writer.pin_numerals_in_code(draft, issues, ctx, wh, window=window, reruns=reruns)
    return draft, issues, pinned, ctx, wh, window, reruns


@pytest.mark.parametrize("queries, text", [
    ([[GQ], [AM]], "#gqom drew 9 posts, while #amapiano reached 5 creators."),
    ([[GQ]], "#gqom drew 9 posts and #amapiano drew 5 creators."),
    ([[GQ, KW], [AM]], "#gqom drew 9 posts; on #amapiano 5 creators posted."),
], ids=["second-subject-in-an-uncited-query", "second-subject-in-no-query", "cited-query-lacks-the-second-subject"])
def test_a_figure_is_not_pinned_when_the_claim_names_a_second_subject_wherever_that_subject_sits(queries, text):
    """Review of 7c6049b, R1 to R3: the 5 is #gqom's creators in the data, but each sentence gives it to #amapiano.
    The cited query shows no other row for the claim to name, yet pinning would let K2 pass the wrong attribution."""
    draft, issues, pinned, ctx, wh, _, _ = pinned_across(queries, text)

    assert issues and pinned is None
    assert any(v == "cut" and "numeral 5 has no pinned" in why for v, why in k2_verdicts(draft, ctx, wh))


def test_a_second_subject_in_an_uncited_query_the_claim_does_not_name_does_not_stop_the_pin():
    _, issues, pinned, ctx, wh, window, reruns = pinned_across([[GQ], [AM]], "#gqom drew 9 posts from 5 creators.")

    assert issues and pinned is not None and pinned["claims"][0]["numbers"][-1]["value"] == 5
    assert writer.unpinned_claim_numerals(pinned, ctx, wh, window=window, reruns=reruns) == []


@pytest.mark.parametrize("text", [
    "#gqom drew 9 posts from 5 creators, ahead of #nobody.",
    "#gqom drew 9 posts from 5 creators, as @someone noticed.",
    "#gqom drew 9 posts from 5 creators; @one and @two shared it.",
], ids=["second-tag", "handle", "two-handles"])
def test_a_tag_or_handle_in_the_text_that_is_a_second_subject_stops_the_pin(text):
    """The extra subject is in no query at all, so only the text can show it."""
    _, issues, pinned, *_ = pinned_across([[GQ]], text)

    assert issues and pinned is None


def test_the_same_tag_written_twice_in_other_case_is_one_subject_and_still_pins():
    _, _, pinned, *_ = pinned_across([[GQ]], "#gqom drew 9 posts from 5 creators, and #GQOM kept rising.")

    assert pinned is not None and pinned["claims"][0]["numbers"][-1]["value"] == 5


def test_a_platform_cell_the_claim_names_is_not_a_second_subject():
    row = {**GQ, "platform": "TikTok"}

    _, _, pinned, *_ = pinned_across([[row]], "#gqom drew 9 posts from 5 creators on TikTok.")

    assert pinned is not None and pinned["claims"][0]["numbers"][-1]["value"] == 5


def test_a_second_subject_written_without_a_hash_is_found_in_a_query_the_claim_does_not_cite():
    plain = [{"hashtag": "gqom", "posts": 9, "creators": 5}], [{"hashtag": "amapiano", "posts": 7, "creators": 6}]

    _, issues, pinned, *_ = pinned_across(list(plain), "gqom drew 9 posts, while amapiano reached 5 creators.")

    assert issues and pinned is None


def test_a_row_text_and_the_claim_tag_that_differ_only_in_case_are_one_subject():
    _, _, pinned, *_ = pinned_across([[{**GQ, "hashtag": "#Gqom"}]], "#gqom drew 9 posts from 5 creators.")

    assert pinned is not None and pinned["claims"][0]["numbers"][-1]["value"] == 5


def test_an_at_sign_inside_a_word_is_not_a_handle_and_not_a_second_subject():
    _, _, pinned, *_ = pinned_across([[GQ]], "#gqom drew 9 posts from 5 creators (ask ops@example.com).")

    assert pinned is not None and pinned["claims"][0]["numbers"][-1]["value"] == 5
