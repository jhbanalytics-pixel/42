"""Ask speed, fix 2 (ASK-LATENCY.md): the rows a research call hands the model are narrower than the rows it records.

search_posts hands the model every field of every post it stores, including a url and an evidence_id that repeats the
post's id. Those ride in the history of every later turn. The model gets the narrower view; ctx.evidence, ctx.queries
and so the receipts, result hashes and cited evidence ids keep the full rows. These tests run the same scripted ask
twice, once with the narrowing switched off (which is what the code did before) and once as shipped, and hold the two to
the same record while the model's input falls.
"""

import copy
import json

import pytest

from core.agent import gemini_research, toolset
from core.agent.tests.test_ask import NOW, POSTS, FakeWarehouse, Harness, post
from core.agent.tests.test_ask_model_budget import configure_gemini
from core.agent.tests.test_ask_timings import research_turns, through_gemini
from core.agent.tests.test_gemini_research_budget import FakeClient
from core.agent.tools import warehouse as warehouse_tools
from core.agent.writer import _input_token_upper_bound

MANY = 40


class ManyPostsWarehouse(FakeWarehouse):
    """The Harness warehouse, but every post search finds MANY posts (the six the writer fixture cites and clones)."""

    def run(self, sql, params, max_bytes_billed):
        rows = super().run(sql, params, max_bytes_billed)
        if "COUNT(" in sql:
            return rows
        extra = [post(f"tt_x{i}", "tiktok", f"@clone_{i}", f"amapiano clone post {i} from the weekend", 22 + i % 5)
                 for i in range(MANY - len(rows))]
        return [*rows, *extra]


class MeasuringClient(FakeClient):
    """The scripted provider, noting how many tool-result bytes each call carried at the moment it was made (the
    history list is shared and grows, so a later look would count later turns too)."""

    def __init__(self, *responses):
        super().__init__(*responses)
        self.sent = []
        self.search_output = None

    def generate_content(self, **kwargs):
        self.sent.append(tool_payload_bytes(kwargs))
        for content in kwargs["contents"]:
            for part in content.parts:
                response = getattr(part, "function_response", None)
                if response is not None and self.search_output is None and "evidence" in str(response.response)[:40]:
                    self.search_output = response.response["output"]
        return super().generate_content(**kwargs)


def run_once(monkeypatch, narrowed):
    configure_gemini(monkeypatch)
    if not narrowed:
        monkeypatch.setattr(toolset, "search_view", lambda result: result)
    client = MeasuringClient(*research_turns(cached=0))
    h = Harness(research=through_gemini(monkeypatch, client))
    h.warehouse = h.deps.warehouse = ManyPostsWarehouse()
    return h.run(), client


def normalised(out):
    text = json.dumps({"answer": out["answer"], "receipts": out["query_receipts"],
                       "run": {k: v for k, v in out["run"].items() if k not in ("timings", "seconds", "phase_seconds")}},
                      sort_keys=True, default=str)
    return json.loads(text.replace(out["run"]["run_id"], "RUN"))


def tool_payload_bytes(call):
    """The bytes of tool results the model received in one call: the repo's own input upper bound, over the results."""
    sent = 0
    for content in call["contents"]:
        for part in content.parts:
            response = getattr(part, "function_response", None)
            if response is not None:
                sent += _input_token_upper_bound("", json.dumps(response.response, ensure_ascii=False), {})
    return sent


def test_the_model_gets_narrower_rows_and_the_record_is_identical(monkeypatch):
    before, before_client = run_once(monkeypatch, narrowed=False)
    monkeypatch.undo()
    after, after_client = run_once(monkeypatch, narrowed=True)

    assert normalised(after) == normalised(before)  # answer, receipts, result hashes, cited ids, counts, spend
    assert after["query_receipts"] and all(r["result_hash"].startswith("sha256:") for r in after["query_receipts"].values())
    cited = {e for c in after["answer"]["claims"] for e in c["evidence_ids"]}
    assert cited and cited == {e for c in before["answer"]["claims"] for e in c["evidence_ids"]}
    assert {r["id"] for r in after["answer"]["evidence"]} >= cited
    assert all(r.get("url") for r in after["answer"]["evidence"])  # the full record keeps its url
    assert len(after_client.calls) == len(before_client.calls) == 3  # same turns: no model call added or dropped
    assert after_client.sent[1] < before_client.sent[1]  # and the two runs really did hand the model different rows


def test_input_per_turn_falls_after_a_search_and_the_cut_is_what_was_dropped(monkeypatch):
    _, before_client = run_once(monkeypatch, narrowed=False)
    monkeypatch.undo()
    _, after_client = run_once(monkeypatch, narrowed=True)

    assert before_client.sent[0] == after_client.sent[0] == 0  # the first turn carries no tool result
    for turn in (1, 2):  # every turn after the search re-sends its result
        was, now = before_client.sent[turn], after_client.sent[turn]
        assert now < was * 0.85, (turn, was, now)
    records = json.loads(after_client.search_output)["evidence"]
    assert len(records) >= 20
    assert all("url" not in r and "evidence_id" not in r for r in records)
    assert all(r["id"] and r["text"] and r["platform"] and r["handle"] and "posted_at" in r and "engagement" in r
               for r in records)  # what the model reads a post by is still there
    assert not any(r.get("flags") == [] for r in records)  # an empty flag list says nothing


def test_a_flag_and_a_source_market_the_model_could_use_are_kept():
    full = {"id": "tt_9", "platform": "TikTok", "handle": "@a", "url": "https://example.test/tt_9",
            "posted_at": "2026-09-22T19:40:00+02:00", "market": "ZA", "text": "<fenced>", "engagement": {"views": 5},
            "flags": ["market_assumed"], "source_market": "KE", "evidence_id": "tt_9"}
    result = {"evidence": [full], "query_id": "q_1", "query_ids": ["q_1", "q_2"], "skipped_outside_window": 2}
    original = copy.deepcopy(result)

    narrow = warehouse_tools.search_view(result)

    assert result == original  # the full result is not touched
    assert narrow["evidence"] == [{k: v for k, v in full.items() if k not in ("url", "evidence_id")}]
    assert {k: v for k, v in narrow.items() if k != "evidence"} == {k: v for k, v in result.items() if k != "evidence"}
    bare = warehouse_tools.search_view({"evidence": [{**full, "flags": [], "source_market": None}], "query_id": "q_1"})
    assert "flags" not in bare["evidence"][0] and "source_market" not in bare["evidence"][0]


@pytest.mark.parametrize("other", [[{"evidence_id": "tt_1"}], {"error": "x"}, {"evidence": None}, None])
def test_anything_that_is_not_a_search_result_passes_through_as_it_is(other):
    assert warehouse_tools.search_view(other) is other
