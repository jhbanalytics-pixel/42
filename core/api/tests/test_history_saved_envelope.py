import copy
import json

import pytest

from core.api import history


def _saved_row(answer_status="complete"):
    claims = [
        {"text": "The first checked claim.", "label": "corroborated", "item_ids": [],
         "evidence_post_ids": ["p01", "p02"], "query_ids": ["q_first"], "run_ids": ["r_saved"],
         "result_hashes": ["sha256:" + "a" * 64]},
        {"text": "The second checked claim.", "label": "inferred", "item_ids": [],
         "evidence_post_ids": ["p01", "p02"], "query_ids": [], "run_ids": ["r_saved"], "result_hashes": []},
    ]
    texts = [claim["text"] for claim in claims]
    answer_text = "\n".join(texts)
    if answer_status == "partial":
        answer_text = "Partial answer: " + answer_text
    envelope = {
        "schema_version": "saved-ask-finding-v1",
        "answer_text": answer_text,
        "source_ask_id": "a_saved",
        "source_run_id": "r_saved",
        "parent_id": None,
        "answer_status": answer_status,
        "context": "South African scope for the saved Ask.",
        "market": "ZA",
        "window": {"from": "2026-09-21", "to": "2026-09-27"},
        "notices": ["One source returned no posts."],
        "gaps": [{"what": "No Instagram posts", "searched": "Instagram in the saved window", "why": "empty"}],
        "source_status": [{"platform": "instagram", "status": "empty", "items": 0}],
        "evidence": [
            {"id": "p01", "platform": "x", "handle": "@first", "url": "https://example.invalid/p01",
             "posted_at": "2026-09-22T10:00:00+02:00", "market": "ZA", "source_market": "NG",
             "text": "First source text.", "engagement": {}, "flags": []},
            {"id": "p02", "platform": "x", "handle": "@second", "url": "https://example.invalid/p02",
             "posted_at": "2026-09-23T10:00:00+02:00", "market": "ZA", "text": "Second source text.",
             "engagement": {}, "flags": []},
        ],
    }
    row = {"finding_id": "f_saved", "question": "What happened?",
           "answer": json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
           "as_of": "2026-09-28T06:10:00+02:00", "status": "current", "claims": claims}
    return row, envelope


class FindingStore:
    def __init__(self, row, posts=None):
        self.row = row
        self.posts = posts or []
        self.post_reads = []

    def findings(self, item_id=None):
        return [copy.deepcopy(self.row)]

    def suppressed_creators(self):
        return set()  # the suppression list exists and is empty

    def suppressions(self):
        return []  # and so is the table behind it

    def posts_by_id(self, post_ids):
        self.post_reads.append(list(post_ids))
        wanted = set(post_ids)
        return [copy.deepcopy(post) for post in self.posts if post["post_id"] in wanted]


def test_saved_envelope_returns_checked_text_context_and_exact_evidence_snapshot():
    row, envelope = _saved_row()
    store = FindingStore(row)

    finding = history.build_history_findings(store)["findings"][0]

    assert finding["answer"] == envelope["answer_text"]
    assert finding["claims"] == row["claims"]
    assert finding["evidence"] == envelope["evidence"]
    assert finding["evidence"][0]["flags"] == envelope["evidence"][0]["flags"]
    assert finding["evidence"][0]["source_market"] == "NG"
    assert finding["saved_context"] == {key: envelope[key] for key in (
        "schema_version", "source_ask_id", "source_run_id", "parent_id", "answer_status", "context", "market",
        "window", "notices", "gaps", "source_status")}
    assert store.post_reads == []


def test_partial_saved_envelope_keeps_the_producer_prefix():
    _, envelope = _saved_row(answer_status="partial")
    row, _ = _saved_row(answer_status="partial")

    finding = history.build_history_findings(FindingStore(row))["findings"][0]

    assert finding["answer"] == envelope["answer_text"]
    assert finding["answer"].startswith("Partial answer: ")


@pytest.mark.parametrize("mismatch", ["version", "text", "run", "evidence", "window", "source_status", "claims",
                                       "answer_status_type", "malformed"])
def test_malformed_or_mismatched_saved_envelope_fails_closed(mismatch):
    row, envelope = _saved_row()
    if mismatch == "version":
        envelope["schema_version"] = "saved-ask-finding-v2"
        row["answer"] = json.dumps(envelope)
    elif mismatch == "text":
        envelope["answer_text"] += " Extra unchecked text."
        row["answer"] = json.dumps(envelope)
    elif mismatch == "run":
        row["claims"][0]["run_ids"] = ["r_unrelated"]
    elif mismatch == "evidence":
        envelope["evidence"].pop()
        row["answer"] = json.dumps(envelope)
    elif mismatch == "window":
        envelope["window"] = {"from": "not-a-date", "to": "2026-09-27"}
        row["answer"] = json.dumps(envelope)
    elif mismatch == "source_status":
        envelope["source_status"] = {"platform": "x"}
        row["answer"] = json.dumps(envelope)
    elif mismatch == "answer_status_type":
        envelope["answer_status"] = []
        row["answer"] = json.dumps(envelope)
    elif mismatch == "claims":
        row["claims"] = {"text": "malformed"}
    else:
        row["answer"] = '{"schema_version":"saved-ask-finding-v1"'
    store = FindingStore(row)

    finding = history.build_history_findings(store)["findings"][0]

    assert finding["answer"] is None
    assert finding["claims"] == []
    assert finding["evidence"] == []
    assert isinstance(finding["unavailable_reason"], str) and finding["unavailable_reason"].strip()
    assert "saved_context" not in finding
    assert store.post_reads == []


def test_legacy_plain_finding_keeps_its_existing_answer_claims_and_evidence():
    row = {"finding_id": "f_legacy", "question": "What happened?", "answer": "A dance step from Soweto.",
           "as_of": "2026-09-29T10:00:40+02:00", "status": None,
           "claims": [{"text": "It started on TikTok in Soweto.", "evidence_post_ids": ["p01"]}]}
    post = {"post_id": "p01", "platform": "tiktok", "handle": "@fixture", "url": "https://example.invalid/p01",
            "published_at": "2026-09-29T08:00:00Z", "geo_market": "ZA", "text": "A local dance step.", "flags": []}
    store = FindingStore(row, [post])

    finding = history.build_history_findings(store)["findings"][0]

    assert finding["answer"] == row["answer"]
    assert finding["claims"] == row["claims"]
    assert finding["evidence"][0]["id"] == "p01"
    assert "saved_context" not in finding and "unavailable_reason" not in finding
    assert store.post_reads == [["p01"]]
