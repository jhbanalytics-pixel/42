import copy
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from core.agent.answer import validate_answer

EVAL = Path(__file__).resolve().parents[2] / "eval"
FIXTURE = EVAL / "fixtures" / "answer_complete.json"


@pytest.fixture
def answer():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def problems_text(answer):
    problems = validate_answer(answer)
    assert problems, "expected at least one problem"
    return "\n".join(problems)


def test_fixture_is_valid(answer):
    assert validate_answer(answer) == []


def test_fixture_meets_now01_floor(answer):
    questions = yaml.safe_load((EVAL / "questions.yaml").read_text(encoding="utf-8"))
    q = next(t["vars"] for t in questions if t["vars"]["id"] == "NOW-01")
    floor = q["min_evidence"]
    as_of = datetime.fromisoformat(answer["as_of"])
    start = as_of - timedelta(days=q["window_days"])
    records = {r["id"]: r for r in answer["evidence"]}
    cited = [records[i] for i in {i for c in answer["claims"] for i in c["evidence_ids"]}]
    assert all(start <= datetime.fromisoformat(r["posted_at"]) <= as_of for r in cited)
    assert all(r["market"] in q["markets"] for r in cited)
    assert len(cited) >= floor["posts"]
    assert len({r["platform"] for r in cited}) >= floor["platforms"]
    assert len({r["handle"].lower() for r in cited}) >= floor["creators"]
    assert all(c.get("quotes") for c in answer["claims"])
    assert any(c.get("numbers") for c in answer["claims"])


def test_unknown_top_level_key(answer):
    answer["tier"] = "deep"
    assert "tier" in problems_text(answer)


def test_bad_label(answer):
    answer["claims"][0]["label"] = "likely"
    assert "likely" in problems_text(answer)


def test_missing_evidence_ids(answer):
    del answer["claims"][0]["evidence_ids"]
    assert "evidence_ids" in problems_text(answer)


def test_empty_evidence_ids(answer):
    answer["claims"][0]["evidence_ids"] = []
    answer["claims"][0].pop("quotes", None)
    assert "evidence_ids" in problems_text(answer)


def test_evidence_id_does_not_resolve(answer):
    answer["claims"][1]["evidence_ids"].append("tt_missing")
    assert "tt_missing" in problems_text(answer)


def test_quote_not_verbatim(answer):
    quote = answer["claims"][0]["quotes"][0]
    quote["text"] = quote["text"] + " and it was the best one"
    assert "verbatim" in problems_text(answer)


def test_quote_from_uncited_record(answer):
    other = answer["claims"][1]["evidence_ids"][0]
    answer["claims"][0]["quotes"][0]["evidence_id"] = other
    assert other in problems_text(answer)


def test_quote_with_curly_quotes_and_extra_whitespace_passes(answer):
    records = {r["id"]: r for r in answer["evidence"]}
    claim = answer["claims"][0]
    rid = next(q["evidence_id"] for q in claim["quotes"] if "'" in records[q["evidence_id"]]["text"])
    source = records[rid]["text"]
    start = source.index("'") - 4
    fragment = source[start : start + 30]
    messy = "  " + fragment.replace("'", "’").replace(" ", "   \n") + " "
    claim["quotes"].append({"evidence_id": rid, "text": messy})
    assert validate_answer(answer) == []


def test_bad_result_hash(answer):
    claim = next(c for c in answer["claims"] if c.get("numbers"))
    claim["numbers"][0]["result_hash"] = "sha256:abc123"
    assert "result_hash" in problems_text(answer) or "sha256" in problems_text(answer)


def test_proposal_without_falsifier(answer):
    claim = answer["claims"][2]
    claim["kind"] = "proposal"
    claim["basis"] = "Two cited posts reuse Heritage Day outfits"
    assert "falsifier" in problems_text(answer)


def test_proposal_with_basis_and_falsifier_passes(answer):
    claim = answer["claims"][2]
    claim["kind"] = "proposal"
    claim["basis"] = "Two cited posts reuse Heritage Day outfits"
    claim["falsifier"] = "Fewer than five #heritagefits rewear posts in the next seven days"
    assert validate_answer(answer) == []


def test_platform_google_trends_rejected(answer):
    answer["evidence"][0]["platform"] = "google_trends"
    assert "google_trends" in problems_text(answer)


def test_complete_with_no_claims(answer):
    answer["claims"] = []
    answer["so_what"] = []
    answer["watch_next"] = []
    assert "no claims" in problems_text(answer)


def test_duplicate_claim_id(answer):
    answer["claims"][1]["id"] = answer["claims"][0]["id"]
    assert "duplicate" in problems_text(answer)


def test_duplicate_evidence_id(answer):
    extra = copy.deepcopy(answer["evidence"][0])
    answer["evidence"].append(extra)
    assert "duplicate" in problems_text(answer)


def test_so_what_claim_id_does_not_resolve(answer):
    answer["so_what"][0]["claim_ids"].append("c99")
    assert "c99" in problems_text(answer)


def test_watch_next_claim_id_does_not_resolve(answer):
    answer["watch_next"][0]["claim_ids"] = ["c42"]
    assert "c42" in problems_text(answer)


def test_bad_posted_at(answer):
    answer["evidence"][0]["posted_at"] = "last Tuesday"
    assert "posted_at" in problems_text(answer)


def test_bad_as_of(answer):
    answer["as_of"] = "2026-09-28"
    assert "as_of" in problems_text(answer)


def test_run_metadata_rejected_in_evidence(answer):
    answer["evidence"][0]["credits"] = 12
    assert "credits" in problems_text(answer)


MEDIA = {
    "thumbnail_url": "https://example.com/tt_7431.jpg",
    "duration_s": 21.0,
    "transcript_span": {"start_s": 4.2, "end_s": 9.8, "text": "I can't be the only one"},
    "creator_tier": "micro",
}


def test_evidence_accepts_the_four_media_fields(answer):
    answer["evidence"][0].update(copy.deepcopy(MEDIA))
    assert validate_answer(answer) == []


def test_evidence_accepts_each_media_field_alone(answer):
    for key, value in MEDIA.items():
        record = copy.deepcopy(answer)
        record["evidence"][0][key] = copy.deepcopy(value)
        assert validate_answer(record) == [], key


@pytest.mark.parametrize(
    "key, value",
    [
        ("thumbnail_url", 42),
        ("thumbnail_url", "tt_7431.jpg"),
        ("duration_s", "21s"),
        ("duration_s", -1),
        ("duration_s", True),
        ("transcript_span", "4.2 to 9.8"),
        ("transcript_span", {"start_s": 4.2, "text": "I can't be the only one"}),
        ("transcript_span", {"start_s": 4.2, "end_s": 9.8, "text": "x", "speaker": "host"}),
        ("transcript_span", {"start_s": -1, "end_s": 9.8, "text": "x"}),
        ("creator_tier", 3),
        ("creator_tier", ""),
    ],
)
def test_evidence_media_field_with_wrong_type_rejected(answer, key, value):
    answer["evidence"][0].update(copy.deepcopy(MEDIA))
    answer["evidence"][0][key] = value
    assert key in problems_text(answer)


def test_fifth_media_field_still_rejected(answer):
    answer["evidence"][0].update(copy.deepcopy(MEDIA))
    answer["evidence"][0]["clip_url"] = "https://example.com/tt_7431.mp4"
    assert "clip_url" in problems_text(answer)


def test_normalise_maps_every_quote_mark_to_a_straight_quote():
    from core.agent.answer import normalise

    fancy = "\u00aba\u00bb \u201eb\u201f \u201cc\u201d \u2039d\u203a \u201ae\u201b \u2018f\u2019"
    assert normalise(fancy) == "\"a\" \"b\" \"c\" 'd' 'e' 'f'"
