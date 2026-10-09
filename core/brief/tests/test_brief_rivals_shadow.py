"""The code-found rivals, recorded beside the critic and changing nothing (METHOD-GAPS Gap 7, part three, shadow).

For each of the seven simpler explanations the brief computes a boolean in code from the pack's pinned detect values
and records it with any disagreement with the critic's ruled_out. It holds, drops and changes no card, and adds no
model call. Thresholds are the ones core/detect/sql/state.sql already uses for share_flags (near_duplicates at 0.30
on line 129, burst at 0.35 and concentrated at 0.60 with 10 or more posts on lines 130 and 131), the calendar window
of its cal CTE (line 81 to 83: three days back to fourteen days ahead) and the 0.5 sponsored share of METHOD-GAPS.
"""

import copy

import pytest

from core.brief import rivals
from core.brief.tests.test_brief_explain import RULED_OUT, FakeModel, good, make_pack, run


def number(field, value):
    return {"value": value, "unit": f"u {field}", "query_id": f"q_{field}", "run_id": "r_1",
            "result_hash": "sha256:00", "rival_field": field}


def pack_with(**values):
    """make_pack plus rival numbers (numeric values) and pinned rows (everything else)."""
    pack = make_pack()
    pack["numbers"] += [number(f, v) for f, v in values.items() if isinstance(v, (int, float)) and v is not None]
    pins = [number(f, v) for f, v in values.items() if v is None or not isinstance(v, (int, float))]
    if pins:
        pack["pinned"] = pins
    return pack


QUIET = dict(posts7=20, burst_share=0.1, top3_share=0.2, near_dup_share=0.05, sponsored_share=0.0, moment=None)


def quiet(**over):
    return pack_with(**{**QUIET, **over})


def found(**values):
    record = rivals.code_rivals(quiet(**values))
    return record["found"], record["not_assessed"]


# Thresholds, each on its boundary


@pytest.mark.parametrize("field,rival,at,below", [
    ("sponsored_share", "sponsored", 0.5, 0.49), ("burst_share", "burst", 0.35, 0.34),
    ("top3_share", "concentrated", 0.6, 0.59), ("near_dup_share", "near_duplicates", 0.30, 0.29)])
def test_each_share_rival_is_found_at_its_threshold_and_not_below(field, rival, at, below):
    assert found(**{field: at})[0] == [rival]
    assert found(**{field: below})[0] == []


@pytest.mark.parametrize("field,value", [("burst_share", 1.0), ("top3_share", 1.0)])
def test_burst_and_concentration_need_ten_posts_as_share_flags_do(field, value):
    assert found(**{field: value, "posts7": 9})[0] == []
    assert found(**{field: value, "posts7": 10})[0] != []


def test_near_duplicates_need_no_post_floor():
    assert found(near_dup_share=0.5, posts7=3)[0] == ["near_duplicates"]


def test_a_calendar_moment_is_a_rival_and_no_moment_is_not():
    assert found(moment="Heritage Day")[0] == ["calendar_moment"]
    assert found(moment=None)[0] == []


def test_a_news_outlet_leading_is_the_earliest_post_in_the_pack_being_a_news_post():
    pack = quiet()
    assert rivals.code_rivals(pack)["found"] == []
    news = copy.deepcopy(pack)
    news["evidence"][0]["platform"] = "news"          # tt_1 is the one earliest post
    record = rivals.code_rivals(news)
    assert record["found"] == ["news_leading"]
    tied = copy.deepcopy(news)
    tied["evidence"][1]["posted_at"] = tied["evidence"][0]["posted_at"]
    assert "news_leading" in rivals.code_rivals(tied)["not_assessed"]


def test_the_route_regime_break_rival_is_never_assessed_because_detect_computes_no_such_value():
    record = rivals.code_rivals(quiet())
    assert "regime_break" in record["not_assessed"] and "regime_break" not in record["found"]


def test_a_missing_input_is_not_assessed_and_never_counted_as_absent():
    record = rivals.code_rivals(pack_with(posts7=20, burst_share=0.5))
    assert record["found"] == ["burst"]
    assert {"sponsored", "concentrated", "near_duplicates", "calendar_moment"} <= set(record["not_assessed"])
    short = rivals.code_rivals(pack_with(burst_share=0.5))       # posts7 missing: burst cannot be judged
    assert "burst" in short["not_assessed"] and short["found"] == []


def test_a_pack_with_no_detect_rival_values_gives_no_record():
    assert rivals.code_rivals(make_pack()) is None


def test_the_record_names_each_input_it_used_with_its_query_id():
    record = rivals.code_rivals(quiet(burst_share=0.5))
    assert record["inputs"]["burst_share"] == {"value": 0.5, "query_id": "q_burst_share"}
    assert record["inputs"]["posts7"] == {"value": 20, "query_id": "q_posts7"}


def test_the_record_carries_its_version_and_the_thresholds_it_judged_by():
    record = rivals.code_rivals(quiet())
    assert record["record_version"] == 2     # 1 is the unversioned record that read near_dup_share over sized posts only
    assert record["thresholds"] == {"sponsored": 0.5, "burst": 0.35, "concentrated": 0.6, "near_duplicates": 0.3,
                                    "min_posts7": 10}


def test_the_recorded_thresholds_are_the_ones_the_verdicts_use(monkeypatch):
    monkeypatch.setattr(rivals, "BURST_AT", 0.05)
    monkeypatch.setattr(rivals, "MIN_POSTS7", 25)
    record = rivals.code_rivals(quiet(burst_share=0.1))
    assert record["thresholds"]["burst"] == 0.05 and record["thresholds"]["min_posts7"] == 25
    assert "burst" not in record["found"]                       # 20 posts is under the floor of 25


# In explain_trend: recorded on the critic row and answer, nothing else moves


def comparable(result):
    """Everything a decision or a card reads, leaving out only what the shadow adds."""
    out = copy.deepcopy(result)
    for row in out["checks"]:
        row.pop("code_rivals", None)
    if out["critic"]:
        out["critic"].pop("code_rivals", None)
    return out


def test_a_rival_the_critic_ruled_out_is_recorded_as_a_disagreement_and_the_card_is_unchanged():
    base_model, rival_model = FakeModel([good()], critic=RULED_OUT), FakeModel([good()], critic=RULED_OUT)
    base = run(base_model)
    result = run(rival_model, pack=quiet(burst_share=0.5))
    assert base["reason"] is None and result["reason"] is None
    assert result["explanation"] == base["explanation"] and result["numbers_only"] is False
    assert comparable(result) == comparable(base)
    [row] = [r for r in result["checks"] if r["rule"] == "critic"]
    [base_row] = [r for r in base["checks"] if r["rule"] == "critic"]
    assert row["verdict"] == base_row["verdict"] == "pass" and row["detail"] == base_row["detail"]
    record = row["code_rivals"]
    assert record["found"] == ["burst"] and record["critic_ruled_out"] is True and record["disagreement"] is True
    assert record["record_version"] == 2 and record["thresholds"]["burst"] == 0.35   # stored with the critic's answer
    assert result["critic"]["code_rivals"] == record
    assert [r for r in base["checks"] if "code_rivals" in r] == []


def test_the_same_card_publishes_with_and_without_the_rival_in_the_pack():
    base = run(FakeModel([good()], critic=RULED_OUT))
    result = run(FakeModel([good()], critic=RULED_OUT), pack=quiet(burst_share=0.9, sponsored_share=0.8))
    assert [(r["rule"], r["verdict"], r["claim_id"]) for r in result["checks"]] == [
        (r["rule"], r["verdict"], r["claim_id"]) for r in base["checks"]]
    assert result["reason"] == base["reason"] and result["explanation_claim_ids"] == base["explanation_claim_ids"]
    assert [c["id"] for c in result["claims"]] == [c["id"] for c in base["claims"]]
    assert [c["label"] for c in result["claims"]] == [c["label"] for c in base["claims"]]
    assert result["news_driven"] == base["news_driven"] and result["specificity"] == base["specificity"]


def test_a_critic_that_did_not_rule_out_records_the_rival_without_a_disagreement():
    held = {"non_cultural_explanation": "a paid campaign", "ruled_out": False, "local_why_now": True,
            "reason": "sponsored flags cannot be excluded"}
    result = run(FakeModel([good()], critic=held), pack=quiet(sponsored_share=0.7))
    [row] = [r for r in result["checks"] if r["rule"] == "critic"]
    assert row["verdict"] == "cut"                       # the critic held it, as before
    assert row["code_rivals"]["found"] == ["sponsored"] and row["code_rivals"]["disagreement"] is False


def test_values_that_show_no_rival_are_recorded_on_the_row_and_in_the_audit_answer_too():
    base = run(FakeModel([good()], critic=RULED_OUT))
    result = run(FakeModel([good()], critic=RULED_OUT), pack=quiet())
    [row] = [r for r in result["checks"] if r["rule"] == "critic"]
    assert row["code_rivals"]["found"] == [] and row["code_rivals"]["disagreement"] is False
    assert row["code_rivals"]["read"] == "ok" and row["code_rivals"]["critic_ruled_out"] is True
    record = result["critic"].pop("code_rivals")
    assert record == row["code_rivals"] and result["critic"] == base["critic"]


def test_a_failed_or_partial_rival_read_is_recorded_not_left_as_a_missing_record():
    for status, values in (("failed", {}), ("cutoff_missing", dict(sponsored_share=0.1))):
        pack = pack_with(**values)
        pack["rival_read"] = status
        result = run(FakeModel([good()], critic=RULED_OUT), pack=pack)
        record = result["critic"]["code_rivals"]
        assert record["read"] == status and record["found"] == [] and record["disagreement"] is False
        assert "burst" in record["not_assessed"]


def test_an_exception_in_the_shadow_cannot_escape_explain_trend(monkeypatch):
    base = run(FakeModel([good()], critic=RULED_OUT))

    def boom(pack):
        raise TypeError("not a pack")

    monkeypatch.setattr(rivals, "code_rivals", boom)
    result = run(FakeModel([good()], critic=RULED_OUT), pack=quiet())
    assert result["reason"] is None and result["explanation"] == base["explanation"]
    [row] = [r for r in result["checks"] if r["rule"] == "critic"]
    assert row["code_rivals"] == {"error": "TypeError"} and result["critic"]["code_rivals"] == {"error": "TypeError"}


def test_nothing_is_recorded_when_the_pack_has_no_detect_rival_values():
    result = run(FakeModel([good()], critic=RULED_OUT))
    assert all("code_rivals" not in r for r in result["checks"]) and "code_rivals" not in result["critic"]


def test_no_model_call_is_added():
    plain, rival = FakeModel([good()], critic=RULED_OUT), FakeModel([good()], critic=RULED_OUT)
    run(plain)
    run(rival, pack=quiet(burst_share=0.9, sponsored_share=0.8, moment="Heritage Day"))
    assert (len(rival.writer_calls()), len(rival.support_calls()), len(rival.critic_calls())) == (
        len(plain.writer_calls()), len(plain.support_calls()), len(plain.critic_calls()))
    assert len(rival.calls) == len(plain.calls)


def test_the_critic_still_sees_the_rival_numbers_in_its_prompt():
    model = FakeModel([good()], critic=RULED_OUT)
    run(model, pack=quiet(burst_share=0.5))
    [call] = model.critic_calls()
    assert "q_burst_share" in call["user"]
    assert "q_burst_share" in model.writer_calls()[0]["user"]
