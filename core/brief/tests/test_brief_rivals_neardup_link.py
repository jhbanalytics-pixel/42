"""The near duplicate step's counts, as the detect job stores them, are what the rival window reads (R6).

The window query takes near_dup_share as measured only when the detect run's stored counts hold
near_dup.near_dup_posts (core/brief/sql/evidence.sql, JSON_VALUE(r.counts, '$.near_dup.near_dup_posts')). The other
rival tests write those counts by hand, so a renamed key in either place would leave them green while every
near_dup_share read NULL. Here the counts come out of detect's own job.run, with the real near duplicate step and
every other step stubbed, are stored the way the chain stores them (json.dumps of the dict), and are read back through
the rival_window query. The near duplicate step lives on the detect branch, so this module runs where it is present.
"""

import importlib
import json
from types import SimpleNamespace

import pytest

if importlib.util.find_spec("core.detect.neardup") is None:    # absent, not broken: a broken import still fails
    pytest.skip("the near duplicate step (N21) is not on this branch", allow_module_level=True)

from core.detect import neardup  # noqa: E402
from core.brief.tests.test_brief_evidence import DETECT, build  # noqa: E402
from core.brief.tests.test_brief_rivals_evidence import rival_world, window_row  # noqa: E402
from core.detect import job as detect_job  # noqa: E402
from core.detect.tests import duck  # noqa: E402
from core.detect.tests.fixtures import D  # noqa: E402

CAPTION = "Capetonians are going mad for the new shaya step challenge this weekend"
OTHERS = [
    "My gran learned the new school dance in one afternoon", "Road closures on the N2 near the airport this morning",
    "Loadshedding schedule changed again for the northern suburbs", "Best bunny chow spots in Durban ranked honestly",
    "Matric results are out and the whole street is celebrating", "Taxi fares are going up from the first of the month",
    "Sunset over Table Mountain from the cable car queue", "Braai day plans with the whole family this Sunday",
    "Small business owners share how they survived the winter"]


class Chain:
    """What detect's job needs of the chain: a run id to begin, and the counts stored on finish the way
    core/collect/chain.py stores them."""

    def __init__(self, con):
        self.con = con

    def begin(self, stage, d):
        return SimpleNamespace(run_id=DETECT)

    def finish(self, run, status, counts, error=None):
        self.con.execute("UPDATE agent.runs SET counts = ? WHERE run_id = ?", [json.dumps(counts), run.run_id])

    def start_next(self, stage, d):
        pass


def stub_every_step_but_neardup(monkeypatch):
    monkeypatch.setattr(detect_job, "run_coaction_step", lambda *a, **k: {})
    monkeypatch.setattr(detect_job, "run_state", lambda *a, **k: 0)
    for name in ("run_breakout_step", "run_watch_step", "run_seeds_step", "run_forecast_step", "run_centroids_step",
                 "run_locality_step",
                 "apply_spread_step", "apply_agent_views_step", "apply_news_step"):
        monkeypatch.setattr(detect_job, name, lambda *a, **k: {})
    monkeypatch.setattr(detect_job.sqlrun, "apply_views", lambda *a, **k: None)
    monkeypatch.setattr(detect_job, "apply_waves", lambda *a, **k: None)
    monkeypatch.setattr(detect_job, "_step", lambda *a, **k: {"series_test": 0})
    monkeypatch.setattr(detect_job.sqlrun, "query", lambda *a, **k: [])
    monkeypatch.setattr(detect_job, "topics_failed_today", lambda *a, **k: None)


def captioned_world():
    """The 14 post rival world, its hand written sizes and counts removed, with 5 posts carrying one caption and the
    other 9 each a caption of their own. Nothing but detect's job writes the sizes and the counts now."""
    con = rival_world()
    con.execute("DELETE FROM core.post_enrichment")
    con.execute("UPDATE agent.runs SET counts = NULL WHERE run_id = ?", [DETECT])
    for n in range(14):
        con.execute("UPDATE core.posts SET text = ? WHERE post_id = ?", [CAPTION if n < 5 else OTHERS[n - 5], f"p{n}"])
    return con


def near_dup(pack):
    return next((e for e in pack["numbers"] if e.get("rival_field") == "near_dup_share"), None)


def test_the_counts_detects_job_stores_make_the_rival_window_measure_near_dup_share(monkeypatch):
    con = captioned_world()
    assert near_dup(build(con)[0]) is None            # no counts yet: the step has not run for the window
    stub_every_step_but_neardup(monkeypatch)
    counts = detect_job.run(duck.Client(con), D, chain=Chain(con), core="core", agent="agent")
    # the step also carries its wall time (corroborated), which is a measurement and not a count
    assert isinstance(counts["near_dup"].pop("seconds"), float)
    assert counts["near_dup"] == {"posts": 14, "near_dup_posts": 5, "written": 5}
    share = near_dup(build(con)[0])
    assert share is not None, "the stored detect counts do not hold what the rival window reads"
    assert share["value"] == 5 / 14 == window_row(con)["near_dup_share"]


def test_a_near_duplicate_step_that_failed_is_stored_as_not_run_and_the_window_reads_no_share(monkeypatch):
    con = captioned_world()
    stub_every_step_but_neardup(monkeypatch)

    def boom(*a, **k):
        raise ImportError("No module named datasketch")
    monkeypatch.setattr(neardup, "run_neardup", boom)
    counts = detect_job.run(duck.Client(con), D, chain=Chain(con), core="core", agent="agent")
    assert counts["near_dup"]["status"] == "skipped"
    assert near_dup(build(con)[0]) is None
