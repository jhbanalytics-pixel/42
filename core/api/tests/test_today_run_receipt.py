"""Today's run_receipt: the counts behind the morning, read from what collect and the brief stored."""
import json
from copy import deepcopy

from core.api import today
from core.api.store import FixtureStore

D30 = "2026-09-30"


class Patched(FixtureStore):
    def __init__(self, **overrides):
        super().__init__()
        for name, fn in overrides.items():
            setattr(self, name, fn)


def _collect(*rows):
    def runs(stage, run_date):
        return [dict(r, stage=stage, run_date=run_date) for r in rows] if stage == "collect" else []
    return runs


def _row(run_id, status, finished_at, counts):
    return {"run_id": run_id, "status": status, "started_at": finished_at, "finished_at": finished_at,
            "counts": counts, "error": None}


def _screen_counts(t):
    shown = sum(len(m["cards"]) + len(m["more"]) for m in t["markets"])
    held = sum(m["held_back"]["count"] for m in t["markets"])
    return shown, held


def test_receipt_reads_posts_from_the_latest_ok_collect_run_and_board_counts_from_the_brief():
    store = Patched(runs=_collect(
        _row("collect-early", "ok", "2026-09-30 01:00:00", {"posts": 900}),
        _row("collect-late", "ok", "2026-09-30 02:10:00", {"posts": 2536}),
        _row("collect-failed", "failed", "2026-09-30 03:00:00", {"posts": 50}),
    ))
    t = today.build_today(store, D30)
    shown, held = _screen_counts(t)
    assert t["run_receipt"] == {"posts": 2536, "markets": ["South Africa", "Nigeria", "Kenya"],
                                "shown": shown, "held": held, "collect_run_id": "collect-late"}
    assert shown > 0 and held > 0


def test_receipt_accepts_counts_stored_as_json_text():
    store = Patched(runs=_collect(_row("collect-ok", "ok", "2026-09-30 02:10:00", json.dumps({"posts": 12}))))
    assert today.build_today(store, D30)["run_receipt"]["posts"] == 12


def test_receipt_is_left_out_when_the_fixture_collect_run_stores_no_post_count():
    assert "run_receipt" not in today.build_today(FixtureStore(), D30)


def test_receipt_is_left_out_without_an_ok_collect_run():
    store = Patched(runs=_collect(_row("collect-failed", "failed", "2026-09-30 02:10:00", {"posts": 40})))
    assert "run_receipt" not in today.build_today(store, D30)


def test_receipt_is_left_out_when_the_post_count_is_not_a_whole_number():
    for counts in ({"posts": None}, {"posts": True}, {"posts": -1}, {"posts": 2.5}, {"posts": "2536"}, {},
                   None, "not json", "[1]"):
        store = Patched(runs=_collect(_row("collect-ok", "ok", "2026-09-30 02:10:00", counts)))
        assert "run_receipt" not in today.build_today(store, D30), counts


def test_receipt_is_left_out_when_a_market_has_no_brief_row():
    base = FixtureStore()

    def briefs(date):
        return [r for r in deepcopy(base.briefs(date)) if r["market"] != "KE"]

    store = Patched(briefs=briefs, runs=_collect(_row("collect-ok", "ok", "2026-09-30 02:10:00", {"posts": 7})))
    assert "run_receipt" not in today.build_today(store, D30)
