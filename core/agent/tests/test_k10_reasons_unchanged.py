"""The lead marker is one more K10 case, and every older K10 row reads as it did before it.

K10 labels several kinds of row: a claim the code checks cut or kept, a so_what or watch_next item resting only on cut
claims, a short answer the support or critic path blanked, and each rewrite outcome. The reason a claim cut by any rule
shows beside it in the live list, and the reason stored in claim_checks, are built from a table of one wording per rule.
A K10 entry in that table gave every one of those rows the wording of the new check, so the new wording belongs to the
lead marker alone.

k10_reasons_before_lead_cut.json holds the reason of every stored check row and every claim event of the ten benchmark
replays and the ranked replays, written by the tree before the lead marker was added. The replays whose lead the marker
now flags differ by the one K10 row on the short answer, and in nothing else."""
import json
from pathlib import Path

import pytest

from core.agent import checks
from core.eval import ask_replay

HERE = Path(__file__).resolve().parent
GOLDEN = json.loads((HERE / "fixtures" / "k10_reasons_before_lead_cut.json").read_text(encoding="utf-8"))
DIRS = [Path(ask_replay.__file__).parent / "ask_replay_fixtures", HERE / "fixtures" / "ranked_briefing"]
LEAD_CUT = {"TOP-ZA-K1CUT"}


@pytest.fixture(scope="module")
def replays():
    saved, found = ask_replay.FIXTURES, {}
    try:
        for folder in DIRS:
            ask_replay.FIXTURES = folder
            for path in sorted(folder.glob("*.json")):
                found[path.stem] = ask_replay.run_question(path.stem)
    finally:
        ask_replay.FIXTURES = saved
    return found


def seen(result, lead_cut):
    """Every stored check row and claim event reason. In a replay whose lead the marker flags, the one row it adds
    (the K10 row on the short answer with LEAD_STORY_REASON) is left out and every other row is compared."""
    rows = [[c["claim_id"], c["rule"], c["verdict"], c["reason"]] for c in result["claim_checks"]]
    events = [[e["claim"]["id"], e.get("check"), e.get("reason")] for e in result["events"] if e.get("event") == "claim"]
    if lead_cut:
        added = [r for r in rows if r[0] == "short_answer" and r[1] == "K10" and r[3] == checks.LEAD_STORY_REASON]
        assert len(added) == 1
        rows = [r for r in rows if r not in added]
    return {"rows": rows, "events": events}


def test_the_golden_covers_every_replay_there_is(replays):
    assert set(GOLDEN) == set(replays) and len(GOLDEN) == 16


@pytest.mark.parametrize("qid", sorted(GOLDEN))
def test_every_check_row_and_claim_event_reads_as_it_did_before_the_lead_cut(qid, replays):
    result = replays[qid]
    assert result["error"] is None
    assert seen(result, qid in LEAD_CUT) == GOLDEN[qid]


def test_the_older_k10_rows_are_in_the_golden_in_every_kind():
    reasons = {r[3] for v in GOLDEN.values() for r in v["rows"] if r[1] == "K10"}
    assert "claim survives every code check" in reasons
    assert "cut by K1; the answer is at most partial" in reasons
    assert "cut by the checks; evidence shown as [unresolved]" in reasons  # a short answer, so_what or watch_next row
    assert any(r.startswith("the short answer rested on a cut or narrowed claim and was blanked") for r in reasons)
    assert any(r.startswith("the support check narrowed a claim") for r in reasons)


def test_a_claim_cut_by_one_rule_names_that_rule_alone_in_its_reason(replays):
    reasons = {e[0]: e[2] for e in seen(replays["NOW-03"], False)["events"]}
    assert "(K1)" in reasons["c3"] and "(K10)" not in reasons["c3"]
    assert "named a story" not in json.dumps(GOLDEN["NOW-03"])


def test_k10_has_no_entry_in_the_table_that_words_every_cut_rule():
    assert "K10" not in checks.RULE_GAPS


def test_the_lead_cut_has_its_own_wording_in_its_own_constants(replays):
    assert "(K10)" in checks.LEAD_STORY_GAP["what"] and "story" in checks.LEAD_STORY_GAP["what"]
    assert checks.LEAD_STORY_GAP["searched"] == "the short answer text"
    assert "Forbes" not in checks.LEAD_STORY_REASON and "story" in checks.LEAD_STORY_REASON
    result = replays["TOP-ZA-K1CUT"]
    lead_rows = [r for r in result["claim_checks"] if r["claim_id"] == "short_answer" and r["rule"] == "K10"]
    assert [r["verdict"] for r in lead_rows] == ["downgrade"] and lead_rows[0]["reason"] == checks.LEAD_STORY_REASON
    gaps = [g for g in result["answer"]["gaps"] if "Short answer" in g["what"]]
    assert gaps and all("story" in g["what"] for g in gaps)
    assert checks.LEAD_STORY_GAP["what"].replace(" (K10)", "") in [g["what"] for g in gaps]
