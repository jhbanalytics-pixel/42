"""A ranked answer whose lead was written over a story that lost every claim has its lead withheld at read time.

The code never blanks, rewrites or rebuilds the lead. At write time it only marks: when a ranked ask lost a whole story and
the final lead is not a kept rewrite, one gap and one K10 downgrade row are appended after every check and after the rewrite
decision, so nothing downstream reads the marker and no model call can follow from it. At read time answer_view.present
replaces the lead with briefing.LEAD_WITHHELD, and dossiers.build stores no summary. The stored record keeps the writer's
text and the typed summary state is whatever the tree before the lane wrote (dispatch_parity_062690d.json, a real run of
every scenario in the tree at 062690d)."""
import copy
import hashlib
import inspect
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from core.agent import answer_state, answer_view, briefing, checks, plain, writer
from core.agent.tests import lead_replay as lr
from core.api import dossiers, export, privacy, smoke
from core.agent.tools.socialcrawl import creator_key

HERE = Path(__file__).resolve().parent
PARITY = json.loads((HERE / "fixtures" / "lead_cut" / "dispatch_parity_062690d.json").read_text(encoding="utf-8"))
SOURCE_HASHES = json.loads((HERE / "fixtures" / "lead_cut" / "writer_source_hashes_062690d.json").read_text(encoding="utf-8"))
SCENARIOS = {s["id"]: s for s in lr.scenarios()}
NOBODY = (set(), set(), set())
DEFAULT_QUESTION = "What are people in South Africa posting about most this week?"


@pytest.fixture(scope="module")
def rows():
    return {sid: lr.run(scn) for sid, scn in SCENARIOS.items()}


def marked(row):
    return briefing.lead_withheld(row["result"]["answer"])


def stored_record(row):
    meta = row["result"]["answer_meta"]
    question = row["result"].get("vars", {}).get("question", DEFAULT_QUESTION)
    return {"ask_id": meta["ask_id"], "status": "complete", "question": question, "answer": row["result"]["answer"],
            "run": row["result"]["run"], "answer_meta": meta}


def wire_record(row):
    stored = stored_record(row)
    return {**stored, "answer_meta": answer_state.meta_view(stored)}


# 1 and 2: the dispatch counts by schema, and the typed summary state, equal the tree before the lane's, on every scenario.

def test_the_golden_is_a_real_run_of_every_scenario():
    assert set(PARITY) == set(SCENARIOS) and len(SCENARIOS) == 70
    assert not [sid for sid, v in PARITY.items() if v["error"] or v["smoke_ok"] is not True]


@pytest.mark.parametrize("sid", sorted(SCENARIOS))
def test_every_scenario_dispatches_the_calls_and_stores_the_state_of_the_tree_before_the_lane(sid, rows):
    got = lr.parity_view(rows[sid])
    want = {k: v for k, v in PARITY[sid].items() if k != "smoke_ok"}
    assert got == want  # calls by schema, the stored lead, status, claims, summary state, removals, rewrite, state check


def test_the_pinned_dispatches(rows):
    rewrites = {sid: row["calls"].get("headline_rewrite", 0) for sid, row in rows.items()}
    assert rewrites["variant/V1-EIGHT-UNNAMED"] == 0 and rewrites["variant/V8-CROSS-CLAIM"] == 0
    assert rewrites["variant/V4-RESTATED"] == 1 and rewrites["ranked/TOP-ZA-SUPPORTCUT"] == 1
    assert all(n == 0 for sid, n in rewrites.items() if sid.startswith("critic/"))
    assert rewrites["variant/R3-T1-FOUR"] == 0
    assert all(rows[sid]["calls"].get("critic", 0) == 1 for sid in rows if sid.startswith("critic/"))


def test_no_record_stores_an_unattributed_removal_and_every_state_verifies(rows):
    for sid, row in rows.items():
        assert row["meta_problem"] is None, sid
        assert all(r["cause"] != "unattributed" for r in row["summary"]["removals"]), sid


# 3: the release smoke, on the wire form after the privacy projection and the reader view.

@pytest.mark.parametrize("sid", sorted(SCENARIOS))
def test_the_release_smoke_passes_on_the_record_a_reader_gets(sid, rows):
    shown = answer_view.present(privacy.project_record(wire_record(rows[sid]), None, hidden=NOBODY))
    ok, why = smoke.check_ask_record(shown)
    assert ok, why


# 4: the marker.

def test_a_story_lost_to_the_code_checks_marks_the_lead_and_leaves_the_stored_state_alone(rows):
    for sid in ("ranked/TOP-ZA-K1CUT", "lead/K1CUT-COUNT"):
        row = rows[sid]
        assert marked(row) and row["lead"] == PARITY[sid]["lead"]
        assert row["summary"] == {"state": "shown", "removals": [], "rewrite": "not_attempted"}
        assert row["calls"].get("headline_rewrite", 0) == 0


def test_the_marker_is_one_gap_and_one_k10_downgrade_row_that_name_no_story(rows):
    result = rows["ranked/TOP-ZA-K1CUT"]["result"]
    answer = result["answer"]
    whats = [g["what"] for g in answer["gaps"] if g["what"] == briefing.LEAD_WITHHELD_GAP["what"]]
    assert len(whats) == 1
    lead_rows = [r for r in result["claim_checks"] if r["claim_id"] == "short_answer" and r["rule"] == "K10"
                 and r["reason"] == checks.LEAD_STORY_REASON]
    assert [r["verdict"] for r in lead_rows] == ["downgrade"]
    assert "Forbes" not in json.dumps(answer["gaps"]) and "Forbes" not in checks.LEAD_STORY_REASON


def test_a_kept_rewrite_is_never_marked_and_still_costs_its_one_rewrite(rows):
    row = rows["ranked/TOP-ZA-SUPPORTCUT"]
    assert row["calls"]["headline_rewrite"] == 1 and not marked(row)
    assert row["summary"] == PARITY["ranked/TOP-ZA-SUPPORTCUT"]["summary"]
    assert row["summary"]["state"] == "shown_rewritten"


def test_the_review3_state_the_lead_names_three_stories_the_fourth_is_cut_and_the_third_flagged(rows):
    row = rows["variant/R3-T1-FOUR"]
    assert row["calls"].get("headline_rewrite", 0) == 0 and marked(row)
    assert row["summary"]["state"] == "shown" and row["lead"].startswith("Three stories led")


@pytest.mark.parametrize("sid", [s for s in SCENARIOS if s.startswith("critic/")])
def test_the_critic_path_marks_the_lead_pays_for_no_rewrite_and_keeps_the_state(sid, rows):
    row = rows[sid]
    # At T2 the ask is a ranked ask. At T3 the skill is the investigation's, which is not given the ranked rule, so the
    # ask is not a ranked ask and nothing is marked. That is the builder's own reading of `ranked`, not a cost the ruling
    # accepted, and it waits for an erratum (notes.md). The dispatches and the state are the tree before the lane's either way.
    assert marked(row) is sid.startswith("critic/T2/") and row["calls"].get("headline_rewrite", 0) == 0
    assert row["summary"] == PARITY[sid]["summary"] and row["lead"] == PARITY[sid]["lead"]


def test_both_sites_firing_append_the_gap_and_the_row_once(rows):
    result = rows["critic/T2/TOP-ZA-K1CUT/c4-c5-c6"]["result"]
    gaps = [g for g in result["answer"]["gaps"] if g["what"] == briefing.LEAD_WITHHELD_GAP["what"]]
    lead_rows = [r for r in result["claim_checks"] if r["reason"] == checks.LEAD_STORY_REASON]
    assert len(gaps) == 1 and len(lead_rows) == 1


def test_a_story_that_loses_one_of_two_claims_is_not_marked(rows):
    for cid in ("c1", "c2", "c3", "c5", "c6", "c8"):
        assert not marked(rows[f"flag/TOP-ZA/{cid}"]), cid
    assert marked(rows["flag/TOP-ZA/c7"])  # the trial had one claim, and the support check cut it without clearing the lead


def test_a_question_that_is_no_ranked_question_is_not_marked(rows):
    assert not marked(rows["lead/K1CUT-UNRANKED"])
    assert rows["lead/K1CUT-UNRANKED"]["lead"] == PARITY["lead/K1CUT-UNRANKED"]["lead"]


def test_the_same_cut_under_a_skill_with_its_own_answer_shape_is_not_marked():
    row = lr.run(SCENARIOS["ranked/TOP-ZA-K1CUT"], skill="context-pack")
    assert row["error"] is None and not marked(row)


def test_a_marker_never_sits_beside_a_kept_rewrite_on_the_gate_path(rows):
    for sid, row in rows.items():
        if not sid.startswith("critic/"):
            assert not (marked(row) and row["summary"]["state"] == "shown_rewritten"), sid


def test_the_marker_comes_from_the_final_pass_only(rows):
    # Each gate pass marks from its own draft and answer; the pass that is stored is the one whose answer carries the gap.
    stored = rows["ranked/TOP-ZA-K1CUT"]["result"]["answer"]
    assert sum(g["what"] == briefing.LEAD_WITHHELD_GAP["what"] for g in stored["gaps"]) == 1


# REVIEW5 I1: a marker the gate wrote does not survive a critic that then blanks the lead or leaves fewer than two claims.

AFTER_MARK = sorted(sid for sid in SCENARIOS if sid.startswith("after-mark/"))


def lead_rows(result):
    return [r for r in result["claim_checks"] if r["reason"] == checks.LEAD_STORY_REASON]


def test_the_two_critic_after_mark_states_are_in_the_set():
    assert len(AFTER_MARK) == 2 and AFTER_MARK[0].split("/")[2] == "S1" and AFTER_MARK[1].split("/")[2] == "S2"


@pytest.mark.parametrize("sid", AFTER_MARK)
def test_a_critic_that_blanks_the_lead_or_leaves_fewer_than_two_claims_drops_the_gates_marker(sid, rows):
    row = rows[sid]
    answer = row["result"]["answer"]
    assert not marked(row) and not lead_rows(row["result"])
    assert briefing.LEAD_WITHHELD_GAP["what"] not in [g["what"] for g in answer["gaps"]]
    assert row["calls"].get("headline_rewrite", 0) == 0 and row["calls"].get("critic", 0) == 1


def test_s1_the_fixed_text_is_what_the_reader_sees_and_the_state_is_the_one_before_the_lane(rows):
    row = rows[next(sid for sid in AFTER_MARK if "/S1/" in sid)]
    assert row["status"] == "insufficient_evidence" and row["summary"]["state"] == "fixed_text"
    shown = answer_view.present(privacy.project_record(wire_record(row), None, hidden=NOBODY))
    assert shown["answer"]["short_answer"] == checks.INSUFFICIENT == row["lead"]
    assert answer_state.check_wire(shown) is None
    assert smoke.check_ask_record(shown)[0]


def test_s2_the_blank_lead_stays_blank_with_its_removal_and_the_state_is_the_one_before_the_lane(rows):
    row = rows[next(sid for sid in AFTER_MARK if "/S2/" in sid)]
    assert row["status"] == "partial" and row["lead"] == "" and row["summary"]["state"] == "removed"
    assert [r["cause"] for r in row["summary"]["removals"]] == ["claim_cut"]
    shown = answer_view.present(privacy.project_record(wire_record(row), None, hidden=NOBODY))
    assert shown["answer"]["short_answer"] != briefing.LEAD_WITHHELD
    assert answer_state.check_wire(shown) is None
    ok, why = smoke.check_ask_record(shown)
    assert ok, why
    assert briefing.LEAD_WITHHELD not in export.render_answer_html(shown)


def test_unmark_unshown_lead_drops_the_gap_only_when_the_lead_is_blank_or_the_fixed_text():
    marker = dict(checks.LEAD_STORY_GAP)
    other = {"what": "x", "searched": "y", "why": "z"}
    for lead in ("", "   ", None, checks.INSUFFICIENT):
        out = checks.unmark_unshown_lead(answer_of(lead, [A], gaps=[other, marker]))
        assert out["gaps"] == [other], lead
    kept = answer_of("A lead.", [A, B], gaps=[other, marker])
    assert checks.unmark_unshown_lead(kept) is kept
    plain_blank = answer_of("", [A], gaps=[other])
    assert checks.unmark_unshown_lead(plain_blank) is plain_blank


def test_the_reader_view_never_writes_the_withheld_sentence_over_a_blank_lead_or_the_fixed_text():
    marker = dict(checks.LEAD_STORY_GAP)
    for lead, status in (("", "partial"), (checks.INSUFFICIENT, "insufficient_evidence")):
        record = {"answer": {**answer_of(lead, [A], gaps=[marker]), "status": status}}
        assert answer_view.present(record)["answer"]["short_answer"] == lead
    record = {"answer": {**answer_of("A lead.", [A, B], gaps=[marker]), "status": "partial"}}
    assert answer_view.present(record)["answer"]["short_answer"] == briefing.LEAD_WITHHELD


# REVIEW5 M3: where only the critic site marks, it writes one K10 row and one gap.

@pytest.mark.parametrize("sid", ["critic/T2/TOP-ZA/c4-c5-c6", "critic/T2/TOP-ZA/c7"])
def test_the_critic_site_alone_writes_one_gap_and_one_k10_row(sid, rows):
    result = rows[sid]["result"]
    assert [r["verdict"] for r in lead_rows(result)] == ["downgrade"] and lead_rows(result)[0]["rule"] == "K10"
    assert sum(g["what"] == briefing.LEAD_WITHHELD_GAP["what"] for g in result["answer"]["gaps"]) == 1


# REVIEW5 M2: a gap round. The marker comes from the pass that is stored, whichever pass lost the story.

GAP_ROUND = {"id": "gap-round", "folder": "ranked", "qid": "TOP-ZA-K1CUT", "tier": "T2", "pending": ["c8"]}
GAP_ROUND_CALLS = {"critic": 2, "fields": 2, "support": 14, "writer": 2}


@pytest.mark.parametrize("name,drop,is_marked", [("lost in the first pass only", {"2": "c7"}, False),
                                                 ("lost in the second pass only", {"1": "c7"}, True),
                                                 ("lost in both passes", {}, True)])
def test_a_gap_round_marks_from_the_final_pass_only(name, drop, is_marked):
    row = lr.run({**GAP_ROUND, "id": f"gap-round/{name}", "writer_drop": drop})
    assert row["error"] is None and row["calls"] == GAP_ROUND_CALLS and row["meta_problem"] is None
    assert marked(row) is is_marked
    result = row["result"]
    assert len(lead_rows(result)) == (1 if is_marked else 0)
    assert sum(g["what"] == briefing.LEAD_WITHHELD_GAP["what"] for g in result["answer"]["gaps"]) == (1 if is_marked else 0)
    assert row["smoke"][0] is True


# The helpers.

def story(name, rest="a line.", cid=None, kind="observation"):
    return {"id": cid or name[:3], "text": f"{name}: {rest}", "kind": kind}


A, B, C = story("Soweto unrest"), story("Cup final"), story("Murder trial")


def test_lost_story_is_true_when_a_story_of_two_or_more_has_no_claim_after():
    assert checks.lost_story([A, B, C], [A, B]) is True
    assert checks.lost_story([A, B, C], [A]) is True


def test_lost_story_is_false_when_every_story_keeps_a_claim_or_there_is_one_story_or_none():
    second = story("Murder trial", "another line.", "mt2")
    assert checks.lost_story([A, B, C, second], [A, B, second]) is False
    assert checks.lost_story([A, B], [A, B]) is False
    assert checks.lost_story([A], []) is False and checks.lost_story([], []) is False
    assert checks.lost_story([{"id": "x", "text": "No label here.", "kind": "observation"}, A], []) is False


def test_lost_story_compares_names_by_the_readers_key_and_counts_only_story_claims():
    curly, straight = story("Nando’s trial", cid="n1"), story("Nando's trial", "second.", "n2")
    assert checks.lost_story([A, curly, straight], [A, straight]) is False
    assert checks.lost_story([A, story("SOWETO UNREST", "again.", "s2"), B], [story("soweto unrest", "x.", "s3")]) is True
    recommendation = story("Cup final", kind="recommendation")
    assert checks.lost_story([A, B, recommendation], [A, recommendation]) is True  # a recommendation is no story claim
    assert checks.lost_story([A, recommendation], [A]) is False


def answer_of(lead, claims, gaps=()):
    return {"status": "partial", "short_answer": lead, "claims": list(claims), "gaps": list(gaps)}


def test_mark_lead_withheld_appends_the_gap_once_and_one_row_and_changes_nothing_else():
    answer = answer_of("A lead.", [A, B])
    before = copy.deepcopy(answer)
    out, row = checks.mark_lead_withheld(answer, [A, B, C], ranked=True)
    assert answer == before  # the input is not changed
    assert out["gaps"] == [checks.LEAD_STORY_GAP] and {k: v for k, v in out.items() if k != "gaps"} == {
        k: v for k, v in before.items() if k != "gaps"}
    assert row == {"claim_id": "short_answer", "rule": "K10", "verdict": "downgrade", "reason": checks.LEAD_STORY_REASON,
                   "checker": "code"}
    again, second = checks.mark_lead_withheld(out, [A, B, C], ranked=True)
    assert again["gaps"] == [checks.LEAD_STORY_GAP] and second is None


@pytest.mark.parametrize("lead,ranked,before", [("A lead.", False, [A, B, C]), ("", True, [A, B, C]), ("   ", True, [A, B, C]),
                                               (checks.INSUFFICIENT, True, [A, B, C]), ("A lead.", True, [A, B]),
                                               (None, True, [A, B, C])])
def test_mark_lead_withheld_marks_nothing_for_an_unranked_ask_a_blank_lead_or_a_fixed_text_or_no_lost_story(lead, ranked, before):
    answer = answer_of(lead, [A, B])
    out, row = checks.mark_lead_withheld(answer, before, ranked=ranked)
    assert row is None and out == answer


# 5: the readers.

def test_the_producers_gap_and_the_readers_gap_cannot_drift():
    plain_gap = plain.gap(checks.LEAD_STORY_GAP)
    assert plain_gap["what"] == briefing.LEAD_WITHHELD_GAP["what"] and plain_gap["why"] == briefing.LEAD_WITHHELD_GAP["why"]
    assert checks.LEAD_STORY_GAP == {**briefing.LEAD_WITHHELD_GAP, "what": briefing.LEAD_WITHHELD_GAP["what"] + " (K10)"}
    assert "(K10)" not in briefing.LEAD_WITHHELD_GAP["what"] and briefing.LEAD_WITHHELD_GAP["searched"] == "the short answer text"


def test_the_default_copy():
    assert briefing.LEAD_WITHHELD_GAP["what"] == "Short answer withheld: a story it was written over had every claim cut"
    assert briefing.LEAD_WITHHELD_GAP["why"] == ("the short answer may be shown only when every story it was written over "
                                                 "still has a claim that passed the checks")
    assert checks.LEAD_STORY_REASON == "the short answer was written over a story whose claims were all cut, so it is withheld"
    assert briefing.LEAD_WITHHELD == ("The one-line summary is not shown because a story it was written over did not pass the "
                                      "checks. The stories that did are below.")


def test_lead_withheld_reads_the_gap_with_or_without_its_code_and_nothing_else():
    assert briefing.lead_withheld({"gaps": [dict(briefing.LEAD_WITHHELD_GAP)]}) is True
    assert briefing.lead_withheld({"gaps": [dict(checks.LEAD_STORY_GAP)]}) is True
    assert briefing.lead_withheld({"gaps": [{**briefing.LEAD_WITHHELD_GAP, "what": "Short answer withheld: something else"}]}) is False
    assert briefing.lead_withheld({"gaps": [{**briefing.LEAD_WITHHELD_GAP, "why": "another reason"}]}) is False
    for bad in ({}, {"gaps": []}, {"gaps": None}, {"gaps": ["x", 3]}, None, "text"):
        assert briefing.lead_withheld(bad) is False


def test_present_replaces_the_lead_when_the_marker_is_there_and_leaves_it_otherwise(rows):
    marked_view = answer_view.present(wire_record(rows["ranked/TOP-ZA-K1CUT"]))
    assert marked_view["answer"]["short_answer"] == briefing.LEAD_WITHHELD
    plain_view = answer_view.present(wire_record(rows["ranked/TOP-ZA"]))
    assert plain_view["answer"]["short_answer"] == rows["ranked/TOP-ZA"]["lead"]


def test_present_is_idempotent_with_the_marker_and_the_briefing_still_renders_beneath(rows):
    once = answer_view.present(wire_record(rows["ranked/TOP-ZA-K1CUT"]))
    assert answer_view.present(once) == once
    stories = [s["name"] for s in once["run"]["briefing"]["stories"]]
    assert stories == ["Soweto unrest", "Pirates vs Sundowns cup final"]
    assert len(once["answer"]["claims"]) == 7


def test_a_gap_with_different_wording_does_nothing(rows):
    record = wire_record(rows["ranked/TOP-ZA-K1CUT"])
    record["answer"] = copy.deepcopy(record["answer"])
    record["answer"]["gaps"] = [g if g["what"] != briefing.LEAD_WITHHELD_GAP["what"] else {**g, "what": "Short answer: nothing"}
                                for g in record["answer"]["gaps"]]
    assert answer_view.present(record)["answer"]["short_answer"] == rows["ranked/TOP-ZA-K1CUT"]["lead"]


def test_the_reader_view_never_changes_the_stored_record_or_the_state(rows):
    record = wire_record(rows["ranked/TOP-ZA-K1CUT"])
    before = copy.deepcopy(record)
    shown = answer_view.present(record)
    assert record == before and shown["answer_meta"] == before["answer_meta"]


def test_the_export_prints_the_sentence_in_the_short_answer_slot_and_no_lead_text(rows):
    html = export.render_answer_html(wire_record(rows["ranked/TOP-ZA-K1CUT"]))
    assert briefing.LEAD_WITHHELD in html and rows["ranked/TOP-ZA-K1CUT"]["lead"] not in html
    assert "Forbes" not in html
    ordinary = export.render_answer_html(wire_record(rows["ranked/TOP-ZA"]))
    assert rows["ranked/TOP-ZA"]["lead"] in ordinary and briefing.LEAD_WITHHELD not in ordinary


def test_a_dossier_built_from_a_withheld_lead_stores_no_summary(rows):
    def body(row):
        record = stored_record(row)
        sel = dossiers.selection({}, record, None)
        return dossiers.build(record, sel, dossier_id="d_0123456789ab", version=1, created_at="2026-10-11T08:00:00+02:00",
                              source={"ask_id": record["ask_id"]})

    assert body(rows["ranked/TOP-ZA-K1CUT"])["summary"] is None
    assert body(rows["ranked/TOP-ZA"])["summary"] == rows["ranked/TOP-ZA"]["lead"]


def test_the_page_fixture_holds_the_view_the_reader_gets_today(rows):
    page = HERE.parents[2] / "app" / "frontend" / "src" / "ui" / "__tests__" / "fixtures" / "ask42_ranked_lead_withheld.json"
    held = json.loads(page.read_text(encoding="utf-8"))
    record = copy.deepcopy(held)
    record["answer"]["short_answer"] = rows["ranked/TOP-ZA-K1CUT"]["lead"]
    record["run"].pop("briefing", None)
    assert answer_view.present(record) == held
    assert held["answer"]["short_answer"] == briefing.LEAD_WITHHELD


# 6: privacy.

def test_the_projection_carries_the_marker_gap_through_masking_byte_for_byte(rows):
    record = wire_record(rows["ranked/TOP-ZA-K1CUT"])
    out = privacy.project_record(record, None, hidden=({creator_key("tiktok", "@kasi_football_hub")}, set(), set()))
    assert checks.LEAD_STORY_GAP in record["answer"]["gaps"] or briefing.LEAD_WITHHELD_GAP in record["answer"]["gaps"]
    carried = [g for g in out["answer"]["gaps"] if g["what"] == briefing.LEAD_WITHHELD_GAP["what"]]
    assert carried == [g for g in record["answer"]["gaps"] if g["what"] == briefing.LEAD_WITHHELD_GAP["what"]]


def test_a_withheld_claim_still_gives_the_privacy_sentence_and_a_verified_state_through_both_hops(rows):
    record = wire_record(rows["ranked/TOP-ZA-K1CUT"])
    out = privacy.project_record(record, None, hidden=({creator_key("tiktok", "@kasi_football_hub")}, set(), set()))
    assert out["answer"]["short_answer"] == privacy.SUMMARY_WITHHELD
    assert out["answer_meta"] == record["answer_meta"] and out["answer_meta"]["check"] == "verified"
    assert answer_state.check_wire(out) is None
    after_present = answer_view.present(out)
    assert after_present["answer"]["short_answer"] == briefing.LEAD_WITHHELD  # accepted: no lead shows either way
    assert answer_state.check_wire(after_present) is None


# 7: Release C readers on every lead-cut, variant and critic record.

C_READER = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from core.agent import answer, answer_state, answer_view
from core.api import export
bad = []
for record in json.load(open(sys.argv[2], encoding="utf-8")):
    plain = {k: v for k, v in record.items() if k not in ("stored_meta", "label")}
    stored = {**plain, "answer_meta": record["stored_meta"]}
    problems = answer.validate_answer(record["answer"])
    shown = answer_view.present(plain)
    html = export.render_answer_html(plain)
    if problems or answer_state.verify_stored(stored) is not None or not html or shown["answer"]["claims"] != record["answer"]["claims"]:
        bad.append(record["label"])
print(json.dumps({"checked": len(json.load(open(sys.argv[2], encoding="utf-8"))), "bad": bad}))
"""


def test_release_c_reads_every_lead_cut_variant_and_critic_record(rows, tmp_path):
    git = subprocess.run(["git", "cat-file", "-e", "d0aebe7^{commit}"], capture_output=True, cwd=HERE.parents[2])
    if git.returncode != 0:
        # d0aebe7 is not an ancestor of this branch, so a checkout can lack it. A run that must prove the old reader sets
        # the switch and fails here instead of showing only in the skip count.
        if os.environ.get("F42_REQUIRE_RELEASE_C"):
            pytest.fail("the release C commit d0aebe7 is not in this checkout")
        pytest.skip("the release C commit d0aebe7 is not in this checkout")
    archive = subprocess.run(["git", "archive", "d0aebe7", "core"], capture_output=True, cwd=HERE.parents[2], check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(tmp_path / "release_c", filter="data")
    chosen = [sid for sid in rows if marked(rows[sid]) or sid.startswith(("variant/", "critic/", "lead/"))]
    records = []
    for sid in chosen:
        wire = wire_record(rows[sid])
        records.append({**wire, "label": sid, "stored_meta": rows[sid]["result"]["answer_meta"]})
    (tmp_path / "records.json").write_text(json.dumps(records), encoding="utf-8")
    out = subprocess.run([sys.executable, "-I", "-c", C_READER, str(tmp_path / "release_c"), str(tmp_path / "records.json")],
                         capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr[-600:]
    verdict = json.loads(out.stdout.strip().splitlines()[-1])
    assert verdict["checked"] == len(chosen) and verdict["bad"] == []


# 9: the writer's own functions are the tree before the lane's.

@pytest.mark.parametrize("name", sorted(SOURCE_HASHES))
def test_the_writer_functions_the_rewrite_rests_on_are_byte_for_byte_the_tree_before_the_lane(name):
    source = inspect.getsource(getattr(writer, name)).replace("\r\n", "\n")
    assert hashlib.sha256(source.encode("utf-8")).hexdigest() == SOURCE_HASHES[name]


def test_the_functions_the_ruling_drops_are_gone():
    for name in ("cut_story_in_lead", "replace_cut_story_lead", "_story_names", "fold", "_STORY_COUNT", "_COUNTS"):
        assert not hasattr(checks, name), name
    assert not hasattr(writer, "names_cut_story")
