"""The read-only card outcome runner on a fake warehouse client: dry run first, byte caps, SELECT only, aggregate
only output."""

import datetime as dt
import json

import pytest

from ops import card_outcome_report as rep

GB = 1024 ** 3
START, END = dt.date(2026, 9, 24), dt.date(2026, 10, 1)


class Job:
    def __init__(self, rows, estimate, billed):
        self._rows, self.total_bytes_processed, self.total_bytes_billed = rows, estimate, billed
        self.job_id, self.cache_hit = "job-fixture", False

    def result(self, timeout=None):
        return iter(self._rows)


class Client:
    def __init__(self, estimates=None, states=("rising", "peaking", "rising")):
        self.calls = []
        self.params = {}
        self.estimates = estimates or {}
        self.states = states  # the card's state at t, its state at t + 7, the held item's state at t + 7

    def query(self, sql, job_config=None, timeout=None):
        name = next(n for n in ("cards_json", "main_lane_class", "SELECT DISTINCT g.run_date") if n in sql)
        dry = bool(job_config.dry_run)
        self.calls.append((name, dry, job_config.maximum_bytes_billed, sql))
        self.params = {q.name: q.value for q in job_config.query_parameters}
        estimate = self.estimates.get(name, 1000)
        if dry:
            return Job([], estimate, 0)
        rows = {
            "cards_json": [{"brief_date": dt.date(2026, 9, 24), "market": "ZA", "run_id": "r1", "status": "published",
                            "published_at": dt.datetime(2026, 9, 24, 5, tzinfo=dt.timezone.utc),
                            "cards_json": json.dumps([{"item_id": "A", "rank": 1, "state": self.states[0]}]),
                            "more_json": "[]",
                            "held_json": json.dumps([{"item_id": "H", "reason": "not_confirmed", "rule": "G3"}])}],
            "main_lane_class": [
                {"metric_date": dt.date(2026, 10, 1), "market": "ZA", "item_id": "A", "state": self.states[1],
                 "untested": False, "main_lane_class": "panel"},
                {"metric_date": dt.date(2026, 10, 1), "market": "ZA", "item_id": "H", "state": self.states[2],
                 "untested": False, "main_lane_class": "search_presence"}],
            "SELECT DISTINCT g.run_date": [{"run_date": dt.date(2026, 10, 1) + dt.timedelta(days=i - 7)} for i in range(0, 20)],
        }[name]
        return Job(rows, estimate, 10 * 1024 * 1024)


def test_dry_run_comes_first_and_every_real_query_is_capped(tmp_path):
    client = Client()
    rep.run(client, START, END, tmp_path)
    names = [(n, dry) for n, dry, _, _ in client.calls]
    assert names == [("cards_json", True), ("cards_json", False), ("main_lane_class", True), ("main_lane_class", False),
                     ("SELECT DISTINCT g.run_date", True), ("SELECT DISTINCT g.run_date", False)]
    assert all(cap == 5 * GB for _, dry, cap, _ in client.calls if not dry)


def test_writes_markdown_and_json_with_bytes_billed_and_no_item_rows(tmp_path):
    report = rep.run(Client(), START, END, tmp_path)
    md, js = tmp_path / "card_outcome_2026-09-24_2026-10-01.md", tmp_path / "card_outcome_2026-09-24_2026-10-01.json"
    assert md.exists() and js.exists()
    data = json.loads(js.read_text(encoding="utf-8"))
    assert [q["name"] for q in data["queries"]] == ["briefs", "states", "detect_days"]
    assert all(q["bytes_billed"] == 10 * 1024 * 1024 and q["maximum_bytes_billed"] == 5 * GB for q in data["queries"])
    assert data["total_bytes_billed"] == 30 * 1024 * 1024
    held7 = [g for g in data["horizons"]["7"] if g["kind"] == "published" and g["market"] == "ZA" and g["stratum"] == "all"
             and g["hold_reason"] is None][0]
    assert (held7["n"], held7["held"]) == (1, 1) and held7["text"] == "not enough data"
    hold = [g for g in data["horizons"]["7"] if g["kind"] == "held" and g["hold_reason"] == "not_confirmed"
            and g["market"] == "ZA" and g["stratum"] == "all"][0]
    assert (hold["n"], hold["unmeasured"]) == (0, 1)
    assert set(data["horizons"]) == {"3", "7", "14"}
    blob = md.read_text(encoding="utf-8") + js.read_text(encoding="utf-8")
    assert '"A"' not in blob and '"H"' not in blob and "item_id" not in blob
    assert report["total_bytes_billed"] == 30 * 1024 * 1024
    assert "not enough data" in md.read_text(encoding="utf-8")


def test_estimate_above_the_per_query_cap_stops_before_the_real_run(tmp_path):
    client = Client({"main_lane_class": 6 * GB})
    with pytest.raises(rep.Refused):
        rep.run(client, START, END, tmp_path)
    assert not any(n == "main_lane_class" and not dry for n, dry, _, _ in client.calls)
    assert not list(tmp_path.glob("*.json"))


def test_total_estimate_above_twenty_gb_stops(tmp_path):
    client = Client({"cards_json": 4 * GB, "main_lane_class": 4 * GB, "SELECT DISTINCT g.run_date": 4 * GB})
    rep.run(client, START, END, tmp_path, total_cap=20 * GB)
    client = Client({"cards_json": 4 * GB, "main_lane_class": 4 * GB, "SELECT DISTINCT g.run_date": 4 * GB})
    with pytest.raises(rep.Refused):
        rep.run(client, START, END, tmp_path, total_cap=10 * GB)


def test_refuses_anything_but_select(tmp_path, monkeypatch):
    monkeypatch.setattr(rep, "QUERIES", {"briefs": "DELETE FROM t WHERE TRUE"})
    client = Client()
    with pytest.raises(rep.Refused):
        rep.run(client, START, END, tmp_path)
    assert client.calls == []


def test_defaults_are_the_agreed_caps():
    assert rep.QUERY_CAP == 5 * GB and rep.TOTAL_CAP == 20 * GB


def test_unreadable_window_is_refused(tmp_path):
    with pytest.raises(rep.Refused):
        rep.run(Client(), END, START, tmp_path)


def test_report_carries_state_mix_persisting_count_and_skipped_briefs(tmp_path):
    report = rep.run(Client(), START, END, tmp_path)
    assert report["persisting_seen"] == 3  # A rising at t and peaking at t + 7, H rising at t + 7 on a search lane
    assert report["state_rows"] == {"total": 2, "untested": 0}
    assert report["briefs_skipped"] == 0
    mix = {(m["kind"], m["state_t"], m["state_later"], m["outcome"]): m["n"] for m in report["state_mix"]}
    assert mix[("published", "rising", "peaking", "held")] == 1
    assert mix[("held", None, "rising", "unmeasured")] == 1
    assert "No Emerging, Rising or Peaking state" not in (tmp_path / "card_outcome_2026-09-24_2026-10-01.md").read_text(encoding="utf-8")


def test_markdown_warns_when_the_window_holds_no_persisting_state(tmp_path):
    report = rep.run(Client(states=("spike", "on_the_boards", "on_the_boards")), START, END, tmp_path)
    assert report["persisting_seen"] == 0
    assert "No Emerging, Rising or Peaking state" in (tmp_path / "card_outcome_2026-09-24_2026-10-01.md").read_text(encoding="utf-8")


def test_report_prints_the_state_distribution_before_any_rate(tmp_path):
    report = rep.run(Client(), START, END, tmp_path)
    dist = {(d["kind"], d["when"], d["state"]): d["n"] for d in report["state_distribution"]}
    assert dist == {("published", "t", "rising"): 1, ("published", "t+3", "absent"): 1, ("published", "t+7", "peaking"): 1,
                    ("published", "t+14", "absent"): 1, ("held", "t", None): 1, ("held", "t+3", "absent"): 1,
                    ("held", "t+7", "rising"): 1, ("held", "t+14", "absent"): 1}
    md = (tmp_path / "card_outcome_2026-09-24_2026-10-01.md").read_text(encoding="utf-8")
    assert md.index("State at t") < md.index("held at 7 days")
    assert "item_id" not in json.dumps(report, default=str)


def test_parameters_cover_the_run_dates_and_fourteen_days_past_the_end(tmp_path):
    client = Client()
    rep.run(client, START, END, tmp_path)
    assert client.params == {"start": START, "end": END, "last": END + dt.timedelta(days=14)}


@pytest.mark.parametrize("word", ["INSERT", "UPDATE", "DELETE", "MERGE", "CREATE", "DROP", "ALTER", "TRUNCATE", "EXPORT",
                                  "CALL", "EXECUTE", "LOAD", "GRANT", "BEGIN", "DECLARE", "SET"])
def test_every_write_or_script_word_is_refused_before_any_call(word, tmp_path, monkeypatch):
    monkeypatch.setattr(rep, "QUERIES", {"briefs": f"SELECT 1 FROM t; {word.lower()} x"})
    client = Client()
    with pytest.raises(rep.Refused):
        rep.run(client, START, END, tmp_path)
    assert client.calls == []
