"""Ask power, offline replay: a transcript cited in a real answer, and a follow-up that starts from its parent.

No network, no live model. The real code checks run (check=None selects them), so the span's quote goes through K1, K3,
K5 and K8 as any claim's does. The first Ask searches stored posts, counts them, fetches one transcript through a fake
SocialCrawl client and cites a spoken line from it. The follow-up is handed what the API hands the agent for that
parent (agent_app.parent_queries over the first run's receipts) and shows its cited posts re-read and its counts
re-run before any research turn.
"""

import copy

from core.agent import ask
from core.agent.tests.test_ask import COUNT_SQL, NOW, POSTS, WRITER_OUT, FakeModel, Harness
from core.agent.tests.test_ask_power_followup import Store
from core.agent.tools.sql_query import sql_query
from core.agent.tools.warehouse import search_posts
from core.agent.tools.enrich_tools import get_transcript
from core.api.agent_app import parent_queries

SPOKEN = [
    "Okay so this is the Sunday session in Soweto and honestly the log drum is everything",
    "My cousin brought the speaker and the whole street stopped to listen",
    "Nobody asked for it but here we are again",
]
QUOTE = "honestly the log drum is everything"


class TranscriptClient:
    def __init__(self, mode="live", run_id=None):
        self.calls = []

    def quote(self, route, params):
        return 10.0 if route.endswith("/transcript") else 1.0

    def call(self, route, params, *, lane, run_id, max_credits):
        self.calls.append(route)
        items = [{"start": i * 4.0, "end": i * 4.0 + 4.0, "text": t} for i, t in enumerate(SPOKEN)]
        return {"items": items, "next_cursor": None, "credits_charged": 10.0, "status": "ok", "cache_hit": False,
                "reason": ""}


def first_writer_out(span_id):
    out = copy.deepcopy(WRITER_OUT)
    out["claims"] = [c for c in out["claims"] if c["id"] in ("c1", "c3", "c4")]
    out["claims"].append({"id": "c5", "text": "A creator said in a video that the log drum is everything.",
                          "label": "single_source", "kind": "observation", "evidence_ids": [span_id],
                          "quotes": [{"evidence_id": span_id, "text": QUOTE}], "numbers": []})
    out["so_what"] = [{"text": "Amapiano is a shared soundtrack across two platforms.", "claim_ids": ["c1"]}]
    return out


def rows(h):
    return [row for _, inserted in h.tables.inserts for row in inserted]


def run_first():
    span_id = "tt_1_span_1"
    holder = {}

    def research(ctx, prompt, options, emit, should_stop):
        search_posts(ctx, options.warehouse, "amapiano")
        sql_query(ctx, options.warehouse, COUNT_SQL, purpose="posts, authors and platforms for amapiano",
                  params={"term": "amapiano"})
        holder["transcript"] = get_transcript(ctx, options.client.client, "tt_1")
        return {"note": "Reading: amapiano, South Africa, this week.", "tokens": {"input": 1000, "output": 300},
                "usd": 0.02}

    model = FakeModel()
    model.writer_out = first_writer_out(span_id)
    h = Harness(research=research, check=None, model=model)
    h.warehouse = h.deps.warehouse = Store()
    h.deps.socialcrawl = lambda mode, run_id: TranscriptClient(mode, run_id)
    out = h.run()
    return h, out, holder["transcript"]


def test_a_transcript_is_cited_in_the_answer_and_passes_every_code_check():
    h, out, fetched = run_first()
    span_id = "tt_1_span_1"
    assert fetched["evidence_ids"][0] == span_id and fetched["credits_spent"] == 10.0
    answer = out["answer"]
    claim = next(c for c in answer["claims"] if c["id"] == "c5")
    assert claim["evidence_ids"] == [span_id] and claim["quotes"][0]["text"] == QUOTE
    shown = next(e for e in answer["evidence"] if e["id"] == span_id)
    assert shown["handle"] == POSTS[0]["handle"] and shown["market"] == "ZA"
    assert shown["transcript_span"] == {"start_s": 0.0, "end_s": 12.0, "text": " ".join(SPOKEN)}
    checks_for_claim = {r["rule"]: r["verdict"] for r in rows(h) if r["claim_id"] == "c5"}
    assert checks_for_claim["K1"] == "pass" and checks_for_claim["K3"] == "pass" and checks_for_claim["K8"] == "pass"
    assert out["run"]["credits"] == 10.0
    assert out["run"]["posts"] == len(POSTS)  # the span is not a post read


def test_the_writer_was_shown_the_span_as_spoken_words():
    h, out, _ = run_first()
    writer_user = next(c["user"] for c in h.model.calls if c["schema"] is ask.WRITER_SCHEMA)
    assert '"transcript_of": "tt_1"' in writer_user and QUOTE in writer_user


def test_a_follow_up_starts_from_its_parents_posts_and_counts():
    h1, first, _ = run_first()
    parent = {"question": "What is behind amapiano in South Africa this week?", "answer": first["answer"],
              "window": first["run"]["window"], "market": "ZA",
              "queries": parent_queries(first["answer"], first["query_receipts"])}
    assert [q["sql"] for q in parent["queries"]] == [COUNT_SQL]  # the count c1's numbers rest on; params ride along
    assert parent["queries"][0]["params"] == {"term": "amapiano"}

    seen = {}

    def research(ctx, prompt, options, emit, should_stop):
        seen.update(prompt=prompt, evidence=set(ctx.evidence), queries=copy.deepcopy(ctx.queries),
                    credits=ctx.credits_spent)
        return {"note": "", "tokens": {"input": 10, "output": 5}, "usd": 0.001}

    h2 = Harness(research=research, check=None)
    h2.warehouse = h2.deps.warehouse = Store()
    second = h2.run(question="Which creators are driving it?", parent=parent, market="ZA")

    assert h2.warehouse.fetches[0]["ids"] == ["tt_1", "tt_2", "tt_3", "x_1", "x_2"]  # the span reads as its post
    assert seen["evidence"] == {"tt_1", "tt_2", "tt_3", "x_1", "x_2"} and seen["credits"] == 0.0
    assert ask.REUSE_NOTE in seen["prompt"] and ask.OPENING_NOTE not in seen["prompt"]
    rerun = [q for q in seen["queries"].values() if q["sql"] == COUNT_SQL]
    assert len(rerun) == 1 and rerun[0]["params"] == {"term": "amapiano"}
    assert rerun[0]["purpose"].startswith(ask.REUSE_PURPOSE)
    assert second["run"]["window"] == first["run"]["window"]
    assert (h2.warehouse.fetches[0]["since"].isoformat(), h2.warehouse.fetches[0]["until"].isoformat()) == (
        first["run"]["window"]["from"], first["run"]["window"]["to"])
    assert second["run"]["credits"] == 0.0


def test_the_same_follow_up_in_another_window_or_market_reuses_less():
    h1, first, _ = run_first()
    base = {"question": "q", "answer": first["answer"], "window": first["run"]["window"], "market": "ZA",
            "queries": parent_queries(first["answer"], first["query_receipts"])}
    for question, market, parent_edit, reads_posts, reruns in (
            ("Which creators drove it over the last 10 days?", "ZA", {}, True, False),
            ("Which creators are driving it?", "NG", {}, False, False),
            ("Which creators are driving it?", "ZA", {"market": "KE"}, False, False),
            ("Which creators are driving it?", "ZA", {}, True, True)):
        seen = {}

        def research(ctx, prompt, options, emit, should_stop):
            seen.update(fetched=len(options.warehouse.fetches), prompt=prompt,
                        reran=any(q["sql"] == COUNT_SQL for q in ctx.queries.values()))
            return {"note": "", "tokens": {"input": 1, "output": 1}, "usd": 0.0}

        h = Harness(research=research, check=None)
        h.warehouse = h.deps.warehouse = Store()
        h.run(question=question, parent={**base, **parent_edit}, market=market)
        assert (seen["fetched"] == 1) is reads_posts and (ask.REUSE_NOTE in seen["prompt"]) is reads_posts
        assert seen["reran"] is reruns
