"""Per-question state shared by the agent's tools (AGENT.md, Tools)."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import date, datetime

# Per-question budgets by effort tier (AGENT.md, Effort tiers). At T2 max_turns is per researcher and max_budget_usd
# is the whole ask's research budget, which ask.py splits across the researchers (ask.researcher_usd).
TIERS = {
    "T0": {"credits": 10, "calls": 8, "max_turns": 12, "max_budget_usd": 0.50},
    "T1": {"credits": 60, "calls": 20, "max_turns": 30, "max_budget_usd": 2.00},
    "T2": {"credits": 300, "calls": 60, "max_turns": 40, "max_budget_usd": 6.00},
    "T3": {"credits": 600, "calls": 120, "max_turns": 20, "max_budget_usd": None},
}


# The tool mark on the whole-store count queries Ask runs before the writer (tools/warehouse.py store_breadth); the
# writer puts those queries first in its pack.
STORE_TOOL = "store_breadth"


class Refused(Exception):
    """A guardrail refused a tool call. The message is shown to the agent."""


def result_hash(rows) -> str:
    """sha256 over the canonical JSON of a query result, as pinned in every answer number."""
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class RunContext:
    run_id: str
    tier: str
    as_of: datetime
    market: str | None = None
    # The ask's window as Johannesburg dates, set by run_ask. search_posts and fetch_posts store no post outside it.
    window_start: date | None = None
    window_end: date | None = None
    credits_spent: float = 0.0
    model_usd_extra: float = 0.0  # model spend inside tools, such as query embeddings in search_posts
    research_usd: float = 0.0  # the research model's own spend so far in this context's loop (gemini_research)
    model_budget: object | None = field(default=None, repr=False, compare=False)
    timings: object | None = field(default=None, repr=False, compare=False)  # the run's timing recorder (timings.py)
    calls_made: int = 0
    queries: dict = field(default_factory=dict)   # query_id -> {"sql", "params", "rows", "result_hash", "purpose"}
    evidence: dict = field(default_factory=dict)  # post_id -> evidence record (rubric.md section 1 shape)
    events: list = field(default_factory=list)    # step events for the API's server-sent events
    # {"route", "params", "status"} per SocialCrawl client call, search and enrichment, in call order. Never streamed:
    # the params hold model-written query text, so they stay out of events. The gap checks read them.
    sc_calls: list = field(default_factory=list)
    enrich_credits_spent: float = 0.0              # credits get_comments and get_transcript spent, inside credits_spent
    enriched: set = field(default_factory=set)     # evidence ids get_comments or get_transcript has paid to enrich
    # evidence_id -> {"status", "segments": [{"start_s", "end_s", "text"}]}, raw text; only non-empty segments
    transcripts: dict = field(default_factory=dict)
    forecasts: dict = field(default_factory=dict)    # forecast_id -> {"forecast_id", "statement", "evidence_ids", ...}
    # Read by the checks, never exported with a replay record, so a replay re-reads promotion (forecast_promotion):
    # forecast_id -> its stored row and publishable line, and the ids already read.
    promoted_forecasts: dict = field(default_factory=dict, repr=False)
    promotion_read: set = field(default_factory=set, repr=False)
    native_statuses: dict = field(default_factory=dict, repr=False)
    native_languages: dict = field(default_factory=dict, repr=False)
    native_review_loaded: bool = field(default=False, repr=False)
    native_review_available: bool = field(default=False, repr=False)
    native_language_attempted: set = field(default_factory=set, repr=False)
    # A T2 researcher's own budget in place of its tier's (lane). An optional credit_cap in it holds credits_spent
    # below credits, so the enrichment share stays 20% of credits while part of them waits for the gap round.
    limits: dict | None = None
    # save_finding's rows, held until the answer's trust gate has run (warehouse.commit_findings). Lanes share it.
    pending_findings: list = field(default_factory=list, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def budget(self) -> dict:
        return self.limits or TIERS[self.tier]

    def credits_left(self) -> float:
        return max(0.0, self.budget.get("credit_cap", self.budget["credits"]) - self.credits_spent)

    def calls_left(self) -> int:
        return max(0, self.budget["calls"] - self.calls_made)

    def record_query(self, sql: str, params: dict | None, rows: list, purpose: str) -> tuple[str, str]:
        digest = result_hash(rows)
        with self.lock:  # researchers running at once share queries, so each id is taken under the lock
            query_id = f"q_{len(self.queries) + 1}"
            self.queries[query_id] = {"sql": sql, "params": params or {}, "rows": rows, "result_hash": digest,
                                      "purpose": purpose}
        return query_id, digest

    def lane(self, limits: dict) -> RunContext:
        """A context for one T2 researcher running in its own thread. It shares queries, held findings and their
        lock, so query ids stay unique, and keeps its own spend counters, evidence, events and calls, so no other
        thread writes them. merge folds it back once the researchers have finished."""
        return RunContext(run_id=self.run_id, tier=self.tier, as_of=self.as_of, market=self.market,
                          window_start=self.window_start, window_end=self.window_end,
                          queries=self.queries, pending_findings=self.pending_findings, limits=limits,
                          lock=self.lock, model_budget=self.model_budget, timings=self.timings)

    def merge(self, lane: RunContext) -> None:
        """Add a finished lane's spend and finds to this context, on one thread, lanes in a fixed order so events and
        the client's call counts stay aligned. A post already here keeps its first record."""
        self.credits_spent += lane.credits_spent
        self.calls_made += lane.calls_made
        self.model_usd_extra += lane.model_usd_extra
        self.enrich_credits_spent += lane.enrich_credits_spent
        for eid, record in lane.evidence.items():
            self.evidence.setdefault(eid, record)
        self.events += lane.events
        self.sc_calls += lane.sc_calls
        self.enriched |= lane.enriched
        self.transcripts.update(lane.transcripts)
        self.forecasts.update(lane.forecasts)

    def emit(self, step: str, **detail) -> None:
        self.events.append({"step": step, **detail})
