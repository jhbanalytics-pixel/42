"""Unit tests for core/setup/watchdog.py. No cloud access: a fake store answers each rule's question, and a
fake BigQuery client records every query and its parameters."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from core.collect import chain
from core.detect.tests import duck
from core.setup import watchdog as wd

DAY = date(2026, 10, 1)
SAST = timezone(timedelta(hours=2))


def at(hh, mm=0, day=DAY):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=SAST).astimezone(timezone.utc)


class FakeStore:
    """A healthy day unless a test says otherwise."""

    def __init__(self, collect_ok=True, observations=None, published=("ZA", "NG", "KE"), usd=0.0, keys=None,
                 ask=(0, 0), runs=None, seeds=None, detect=None, briefs=None, understand=None, stages=None,
                 watchdog_at=None):
        self.runs = runs
        self._briefs = briefs
        self._understand = understand
        self._stages = stages or []
        self._watchdog_at = watchdog_at
        self._seeds = seeds
        self._detect = detect
        self._collect_ok = collect_ok
        self._observations = {"ZA": 120, "NG": 90, "KE": 75} if observations is None else observations
        self._published = set(published)
        self._usd = usd
        self._keys = keys or []
        self._ask = ask
        self.asked = []

    def collect_ok(self, d):
        self.asked.append(("collect_ok", d))
        return self._collect_ok

    def observations(self, d):
        self.asked.append(("observations", d))
        return dict(self._observations)

    def published_markets(self, d):
        self.asked.append(("published_markets", d))
        return set(self._published)

    def model_usd(self, d):
        self.asked.append(("model_usd", d))
        return self._usd

    def route_keys(self, d):
        self.asked.append(("route_keys", d))
        return list(self._keys)

    def ask_runs(self, d):
        self.asked.append(("ask_runs", d))
        return self._ask

    def seeds_latest(self, d):
        self.asked.append(("seeds_latest", d))
        return self._seeds

    def detect_latest(self, d):
        self.asked.append(("detect_latest", d))
        return self._detect

    def brief_state(self, d):
        self.asked.append(("brief_state", d))
        if self._briefs is not None:
            return dict(self._briefs)
        return {m: {"status": "published", "cards": 3, "held": 0} for m in self._published}

    def understand_latest(self, d):
        self.asked.append(("understand_latest", d))
        return self._understand

    def stage_latest(self, d):
        self.asked.append(("stage_latest", d))
        return list(self._stages)

    def watchdog_last(self, d):
        """When the watchdog last ran today. A healthy fake ran just now, so no test sees a gap unless it asks."""
        self.asked.append(("watchdog_last", d))
        if self._watchdog_at is not None:
            return self._watchdog_at
        rows = [r for r in (self.runs.rows if self.runs is not None else []) if r["stage"] == "watchdog"]
        if rows:
            return max(datetime.fromisoformat(r["finished_at"]) for r in rows)
        return datetime(2099, 1, 1, tzinfo=timezone.utc)

    def fired_today(self, d):
        """Read back from the watchdog rows main() appended, as BigQueryStore does from the runs table."""
        rows = self.runs.rows if self.runs is not None else []
        return {n for r in rows if r["stage"] == "watchdog" and r["run_date"] == d.isoformat()
                for n in (r["counts"] or {}).get("fired", [])}


def names(alerts):
    return [a.name for a in alerts]


@pytest.mark.parametrize("stage", ["seeds", "detect"])
@pytest.mark.parametrize("with_skip", [False, True])
def test_watchdog_effective_run_ignores_a_later_skipped_duplicate(stage, with_skip):
    con = duck.connect(views=False)
    start = at(2)
    rows = [
        {"run_id": "real", "stage": stage, "run_date": DAY, "status": "running", "started_at": start},
        {"run_id": "real", "stage": stage, "run_date": DAY, "status": "failed", "started_at": start,
         "finished_at": start, "counts": '{"agent_views":{"status":"failed"}}'},
    ]
    if with_skip:
        rows.append({"run_id": "duplicate", "stage": stage, "run_date": DAY, "status": "skipped_duplicate",
                     "started_at": at(3), "finished_at": at(3)})
    duck.load(con, "agent.runs", rows)
    sql = wd.SQL[f"{stage}_latest"].replace(f"`{wd.AGENT}.runs`", "{agent}.runs")
    rows = duck.query(con, sql, {"d": DAY})

    assert len(rows) == 1 and rows[0]["run_id"] == "real"
    if stage == "seeds":
        assert rows[0]["status"] == "failed"
        assert "seeds_failed" in names(wd.check(at(3), FakeStore(seeds=rows[0])))
    else:
        assert rows[0]["agent_views_status"] == "failed"
        assert "agent_views_failed" in names(wd.check(at(3), FakeStore(detect=rows[0])))


def keyrow(route, key, today, prior, today_total, prior_total):
    return {"route": route, "key": key, "today_rows": today, "prior_rows": prior,
            "today_total": today_total, "prior_total": prior_total}


STABLE = [keyrow("tiktok/trending", k, 10, 70, 10, 70) for k in ("success", "data", "credits_used")]


def test_a_healthy_morning_fires_nothing():
    assert wd.check(at(7, 0), FakeStore(keys=STABLE)) == []


@pytest.mark.parametrize("hh,mm,fires", [(3, 45, False), (4, 0, True), (7, 30, True)])
def test_collection_missing_fires_from_0400_sast_without_an_ok_collect(hh, mm, fires):
    alerts = wd.check(at(hh, mm), FakeStore(collect_ok=False, observations={}, published=()))
    assert ("collection_missing" in names(alerts)) is fires


def test_collection_missing_stays_quiet_when_collect_ran_ok():
    assert "collection_missing" not in names(wd.check(at(5, 0), FakeStore()))


def test_zero_rows_fires_for_an_ok_collect_with_a_market_that_has_no_observations():
    alerts = wd.check(at(4, 30), FakeStore(observations={"ZA": 120, "KE": 0}))
    assert names(alerts) == ["zero_rows"]
    assert "NG" in alerts[0].reason and "KE" in alerts[0].reason and "ZA" not in alerts[0].reason


def test_zero_rows_stays_quiet_when_every_market_has_rows_or_collect_is_not_ok():
    assert "zero_rows" not in names(wd.check(at(4, 30), FakeStore()))
    assert "zero_rows" not in names(wd.check(at(3, 0), FakeStore(collect_ok=False, observations={})))


@pytest.mark.parametrize("hh,mm,fires", [(6, 15, False), (6, 29, False), (6, 30, True), (9, 0, True)])
def test_brief_late_fires_from_0630_sast_for_a_market_without_a_published_brief(hh, mm, fires):
    alerts = wd.check(at(hh, mm), FakeStore(published=("ZA", "KE")))
    assert ("brief_late" in names(alerts)) is fires
    if fires:
        reason = [a for a in alerts if a.name == "brief_late"][0].reason
        assert "NG" in reason and "ZA" not in reason


def test_brief_late_stays_quiet_when_every_market_published():
    assert "brief_late" not in names(wd.check(at(8, 0), FakeStore()))


@pytest.fixture
def old_model_cap_schedule(monkeypatch, tmp_path):
    """MODEL_DAILY_USD as it stood until 3 October 2026 (USD 20, USD 80 on 1 and 2 October SAST), for the tests
    of the window and its expiry; the live core/config/caps.yaml has no window now."""
    from core.config import caps as caps_config

    path = tmp_path / "caps.yaml"
    path.write_text('MODEL_DAILY_USD:\n  default: 20\n  temporary:\n    amount: 80\n'
                    '    starts_on: "2026-10-01"\n    ends_on: "2026-10-02"\n', encoding="utf-8")
    monkeypatch.setattr(wd, "model_daily_usd", lambda *, now=None: caps_config.model_daily_usd(path, now=now))


@pytest.mark.parametrize("now,usd,fires", [
    (at(12, day=date(2026, 10, 3)), 40.0, False),
    (at(12, day=date(2026, 10, 3)), 40.01, True),
])
def test_model_spend_fires_over_80_percent_of_the_live_daily_cap(now, usd, fires):
    alerts = wd.check(now, FakeStore(usd=usd))
    assert ("model_spend" in names(alerts)) is fires


@pytest.mark.usefixtures("old_model_cap_schedule")
@pytest.mark.parametrize("now,usd,fires", [
    (at(12, day=date(2026, 10, 1)), 64.0, False),
    (at(12, day=date(2026, 10, 1)), 64.01, True),
    (at(12, day=date(2026, 10, 3)), 16.0, False),
    (at(12, day=date(2026, 10, 3)), 16.01, True),
])
def test_model_spend_fires_over_80_percent_of_the_active_daily_cap(now, usd, fires):
    alerts = wd.check(now, FakeStore(usd=usd))
    assert ("model_spend" in names(alerts)) is fires


@pytest.mark.usefixtures("old_model_cap_schedule")
def test_model_spend_uses_the_cap_at_check_time_across_sast_midnight():
    store = FakeStore(usd=20.0)
    before_expiry = datetime(2026, 10, 2, 21, 59, 59, tzinfo=timezone.utc)
    after_expiry = datetime(2026, 10, 2, 22, 0, tzinfo=timezone.utc)
    assert "model_spend" not in names(wd.check(before_expiry, store))
    assert "model_spend" in names(wd.check(after_expiry, store))


def test_schema_drift_fires_on_a_new_top_level_key():
    keys = STABLE + [keyrow("tiktok/trending", "warning", 3, 0, 10, 70)]
    alerts = wd.check(at(7, 0), FakeStore(keys=keys))
    assert names(alerts) == ["schema_drift"]
    assert "tiktok/trending" in alerts[0].reason and "warning" in alerts[0].reason


def test_schema_drift_fires_when_a_key_every_prior_response_carried_is_gone():
    keys = [keyrow("x/search", "success", 5, 40, 5, 40), keyrow("x/search", "data", 0, 40, 5, 40)]
    alerts = wd.check(at(7, 5), FakeStore(keys=keys))
    assert names(alerts) == ["schema_drift"]
    assert "x/search" in alerts[0].reason and "data" in alerts[0].reason


def test_schema_drift_stays_quiet_on_a_sometimes_key_a_new_route_or_a_route_not_called_today():
    keys = STABLE + [
        keyrow("tiktok/trending", "next_cursor", 0, 30, 10, 70),  # only some prior responses carried it
        keyrow("youtube/new", "items", 4, 0, 4, 0),  # first day of a new route: nothing to compare with
        keyrow("reddit/hot", "posts", 0, 50, 0, 50),  # not called today: collection rules cover that
    ]
    assert wd.check(at(7, 0), FakeStore(keys=keys)) == []


@pytest.mark.parametrize("hh,mm,checked", [(6, 45, False), (7, 0, True), (7, 14, True), (7, 15, False), (13, 0, False)])
def test_schema_drift_runs_once_a_day_in_the_0700_run(hh, mm, checked):
    store = FakeStore(keys=STABLE + [keyrow("tiktok/trending", "warning", 3, 0, 10, 70)])
    alerts = wd.check(at(hh, mm), store)
    assert ("schema_drift" in names(alerts)) is checked
    assert (("route_keys", DAY) in store.asked) is checked


@pytest.mark.parametrize("ask,fires", [((19, 19), False), ((20, 1), False), ((20, 2), True), ((100, 5), False),
                                       ((100, 6), True), ((0, 0), False)])
def test_agent_error_rate_fires_over_5_percent_once_there_are_20_runs(ask, fires):
    alerts = wd.check(at(15, 0), FakeStore(ask=ask))
    assert ("agent_error_rate" in names(alerts)) is fires


def seeds(status, run_id="seeds-20261001-abc123"):
    return {"run_id": run_id, "status": status}


def test_seeds_failed_fires_when_the_days_latest_seeds_row_failed():
    [alert] = wd.check(at(3, 0), FakeStore(seeds=seeds("failed")))
    assert alert.name == "seeds_failed"
    assert "2026-10-01" in alert.reason and "seeds-20261001-abc123" in alert.reason and "failed" in alert.reason


@pytest.mark.parametrize("latest", [None, seeds("ok")])
def test_seeds_failed_stays_quiet_without_a_seeds_row_or_when_the_latest_is_ok(latest):
    assert wd.check(at(7, 0), FakeStore(keys=STABLE, seeds=latest)) == []


def test_seeds_failed_writes_the_shared_alert_line_and_fires_once_a_day(capsys):
    runs = chain.MemoryRunsStore()
    store = FakeStore(seeds=seeds("failed"), runs=runs)
    assert wd.main(now=at(3, 0), store=store, runs=runs) == 0
    [line] = lines(capsys.readouterr().out)
    assert line["severity"] == "ERROR" and line["alert"] == "seeds_failed"
    assert line["message"].startswith("42 ALERT seeds_failed: ")
    assert wd.main(now=at(3, 15), store=store, runs=runs) == 0
    [again] = lines(capsys.readouterr().out)
    assert again["severity"] == "INFO" and "seeds_failed" in again["message"]
    assert [r["counts"]["fired"] for r in runs.rows if r["status"] != "running"] == [["seeds_failed"], []]


def test_bigquery_store_reads_the_latest_seeds_row_of_the_day():
    client = FakeClient({"stage = 'seeds'": [{"run_id": "seeds-20261001-b", "status": "failed"}]})
    store = wd.BigQueryStore(client)
    assert store.seeds_latest(DAY) == {"run_id": "seeds-20261001-b", "status": "failed"}
    [(sql, params)] = client.queries
    assert params == {"d": DAY}
    assert "`ogilvy-trends-v2.intelligence_42_agent.runs`" in sql
    assert "run_date = @d" in sql and "stage = 'seeds'" in sql
    assert "ORDER BY COALESCE(finished_at, started_at) DESC" in sql and "LIMIT 1" in sql
    for word in ("INSERT", "UPDATE", "MERGE", "DELETE", "CREATE", "DROP"):
        assert word not in sql.upper().split()
    assert wd.BigQueryStore(FakeClient({"stage = 'seeds'": []})).seeds_latest(DAY) is None


def detect(status, error=None, run_id="detect-20261001-def456"):
    return {"run_id": run_id, "agent_views_status": status, "agent_views_error": error}


def test_agent_views_failed_fires_with_the_error_when_the_days_latest_detect_row_failed_the_views():
    [alert] = wd.check(at(3, 0), FakeStore(detect=detect("failed", "BadRequest: 400 Unrecognized name: gate")))
    assert alert.name == "agent_views_failed"
    assert "2026-10-01" in alert.reason and "detect-20261001-def456" in alert.reason
    assert alert.reason.endswith(": BadRequest: 400 Unrecognized name: gate")


def test_agent_views_failed_fires_without_error_text_when_counts_carries_none():
    [alert] = wd.check(at(3, 0), FakeStore(detect=detect("failed")))
    assert alert.name == "agent_views_failed" and "detect-20261001-def456" in alert.reason
    assert "None" not in alert.reason


@pytest.mark.parametrize("latest", [None, detect("ok"), detect(None)])
def test_agent_views_failed_stays_quiet_without_a_detect_row_when_ok_or_when_counts_has_no_agent_views(latest):
    assert wd.check(at(7, 0), FakeStore(keys=STABLE, detect=latest)) == []


def test_agent_views_failed_writes_the_shared_alert_line_and_fires_once_a_day(capsys):
    runs = chain.MemoryRunsStore()
    store = FakeStore(detect=detect("failed", "NotFound: 404 v_item_gate_current"), runs=runs)
    assert wd.main(now=at(3, 0), store=store, runs=runs) == 0
    [line] = lines(capsys.readouterr().out)
    assert line["severity"] == "ERROR" and line["alert"] == "agent_views_failed"
    assert line["message"].startswith("42 ALERT agent_views_failed: ")
    assert line["message"].endswith("NotFound: 404 v_item_gate_current")
    assert wd.main(now=at(3, 15), store=store, runs=runs) == 0
    [again] = lines(capsys.readouterr().out)
    assert again["severity"] == "INFO" and "agent_views_failed" in again["message"]
    assert [r["counts"]["fired"] for r in runs.rows if r["status"] != "running"] == [["agent_views_failed"], []]
    store.asked.clear()
    wd.main(now=at(3, 30), store=store, runs=runs)
    assert ("detect_latest", DAY) not in store.asked


def test_bigquery_store_reads_the_agent_views_outcome_of_the_latest_detect_row_of_the_day():
    row = {"run_id": "detect-20261001-b", "agent_views_status": "failed", "agent_views_error": "NotFound: x"}
    client = FakeClient({"stage = 'detect'": [row]})
    store = wd.BigQueryStore(client)
    assert store.detect_latest(DAY) == row
    [(sql, params)] = client.queries
    assert params == {"d": DAY}
    assert "`ogilvy-trends-v2.intelligence_42_agent.runs`" in sql
    assert "run_date = @d" in sql and "stage = 'detect'" in sql
    assert "JSON_VALUE(counts, '$.agent_views.status')" in sql and "JSON_VALUE(counts, '$.agent_views.error')" in sql
    assert "ORDER BY COALESCE(finished_at, started_at) DESC, finished_at IS NOT NULL DESC LIMIT 1" in sql
    for word in ("INSERT", "UPDATE", "MERGE", "DELETE", "CREATE", "DROP"):
        assert word not in sql.upper().split()
    assert wd.BigQueryStore(FakeClient({"stage = 'detect'": []})).detect_latest(DAY) is None


def test_every_rule_reads_the_sast_day_not_the_utc_day():
    store = FakeStore()
    wd.check(datetime(2026, 9, 30, 22, 30, tzinfo=timezone.utc), store)  # 00:30 SAST on 1 October
    assert {d for _, d in store.asked} == {DAY}


def test_run_date_in_the_environment_overrides_the_day(monkeypatch):
    monkeypatch.setenv("RUN_DATE", "2026-09-29")
    store = FakeStore()
    wd.check(at(9, 0), store)
    assert {d for _, d in store.asked} == {date(2026, 9, 29)}


def test_every_alert_name_is_one_of_the_rules():
    assert wd.ALERTS == ("collection_missing", "zero_rows", "brief_late", "model_spend", "schema_drift",
                         "agent_error_rate", "seeds_failed", "agent_views_failed")


def lines(out):
    return [json.loads(line) for line in out.strip().splitlines()]


def test_a_forced_zero_rows_run_writes_one_error_line_named_for_the_alert(capsys):
    store = FakeStore(observations={"ZA": 0, "NG": 90, "KE": 75})
    assert wd.main(now=at(4, 30), store=store, runs=chain.MemoryRunsStore()) == 0
    out = lines(capsys.readouterr().out)
    alerts = [o for o in out if o["severity"] == "ERROR"]
    assert len(alerts) == 1
    assert alerts[0]["message"].startswith("42 ALERT zero_rows: ")
    assert "ZA" in alerts[0]["message"]
    assert alerts[0]["alert"] == "zero_rows"


def test_one_line_per_firing_alert_and_an_info_line_when_all_is_clear(capsys):
    runs = chain.MemoryRunsStore()
    assert wd.main(now=at(7, 0), store=FakeStore(collect_ok=False, observations={}, published=()), runs=runs) == 0
    out = lines(capsys.readouterr().out)
    assert [o["alert"] for o in out if o["severity"] == "ERROR"] == ["collection_missing", "brief_late"]
    assert wd.main(now=at(7, 0), store=FakeStore(keys=STABLE), runs=chain.MemoryRunsStore()) == 0
    out = lines(capsys.readouterr().out)
    assert [o["severity"] for o in out] == ["INFO"]
    assert "42 ALERT" not in out[0]["message"]


class BrokenStore(FakeStore):
    def model_usd(self, d):
        raise RuntimeError("query failed")


def test_a_rule_that_cannot_run_does_not_stop_the_others_and_fails_the_job(capsys):
    store, runs = BrokenStore(published=("ZA",)), chain.MemoryRunsStore()
    assert wd.main(now=at(7, 0), store=store, runs=runs) == 1
    out = lines(capsys.readouterr().out)
    assert [o["alert"] for o in out if o.get("alert")] == ["brief_late"]
    assert any("model_spend" in o["message"] and "query failed" in o["message"] for o in out if o["severity"] == "ERROR"
               and not o.get("alert"))
    assert ("ask_runs", DAY) in store.asked
    [row] = runs.rows
    assert row["status"] == "failed" and "model_spend" in row["error"]
    assert row["counts"]["fired"] == ["brief_late"] and row["counts"]["errors"] == ["model_spend"]


def test_each_run_appends_one_watchdog_runs_row_listing_what_fired(capsys):
    runs = chain.MemoryRunsStore()
    store = FakeStore(observations={"ZA": 0, "NG": 90, "KE": 75}, runs=runs)
    assert wd.main(now=at(4, 30), store=store, runs=runs) == 0
    [row] = runs.rows
    assert (row["stage"], row["run_date"], row["status"]) == ("watchdog", "2026-10-01", "ok")
    assert row["run_id"].startswith("watchdog-20261001-")
    assert row["finished_at"] and row["error"] is None
    assert row["counts"]["fired"] == ["zero_rows"]


def test_an_alert_fires_at_most_once_a_day(capsys):
    runs = chain.MemoryRunsStore()
    store = FakeStore(observations={"ZA": 0, "NG": 90, "KE": 75}, published=("NG",), runs=runs)
    for hh, mm in ((4, 30), (4, 45), (6, 30), (6, 45), (9, 0)):
        assert wd.main(now=at(hh, mm), store=store, runs=runs) == 0
    fired = [o["alert"] for o in lines(capsys.readouterr().out) if o.get("alert")]
    assert fired == ["zero_rows", "brief_late"]
    assert [r["counts"]["fired"] for r in runs.rows] == [["zero_rows"], [], ["brief_late"], [], []]
    store.asked.clear()
    wd.main(now=at(9, 0), store=store, runs=runs)
    assert ("observations", DAY) not in store.asked and ("published_markets", DAY) not in store.asked


def test_the_same_alert_fires_again_the_next_day(capsys):
    runs = chain.MemoryRunsStore()
    store = FakeStore(observations={"ZA": 0, "NG": 90, "KE": 75}, runs=runs)
    wd.main(now=at(4, 30), store=store, runs=runs)
    wd.main(now=at(4, 30, day=DAY + timedelta(days=1)), store=store, runs=runs)
    fired = [o["alert"] for o in lines(capsys.readouterr().out) if o.get("alert")]
    assert fired == ["zero_rows", "zero_rows"]


class NoMemory(FakeStore):
    def fired_today(self, d):
        raise RuntimeError("runs unreadable")


def test_when_todays_rows_cannot_be_read_every_rule_still_runs_and_the_job_fails(capsys):
    runs = chain.MemoryRunsStore()
    assert wd.main(now=at(4, 30), store=NoMemory(observations={"ZA": 0}), runs=runs) == 1
    out = lines(capsys.readouterr().out)
    assert [o["alert"] for o in out if o.get("alert")] == ["zero_rows"]
    assert any("runs unreadable" in o["message"] for o in out if not o.get("alert"))
    assert runs.rows[0]["status"] == "failed"


class FakeRow(dict):
    def items(self):
        return super().items()


class FakeClient:
    def __init__(self, answers):
        self.answers = answers
        self.queries = []

    def query(self, sql, job_config=None):
        params = {p.name: p.value for p in job_config.query_parameters}
        self.queries.append((sql, params))
        for marker, rows in self.answers.items():
            if marker in sql:
                return FakeJob(rows)
        raise AssertionError(f"unexpected query {sql}")


class FakeJob:
    def __init__(self, rows):
        self.rows = rows

    def result(self):
        return [FakeRow(r) for r in self.rows]


def test_bigquery_store_asks_parameterised_questions_of_the_right_tables():
    client = FakeClient({
        "stage = 'watchdog'": [{"name": "zero_rows"}, {"name": "brief_late"}],
        "stage = 'collect'": [{"n": 1}],
        "post_observations": [{"market": "ZA", "n": 3}, {"market": "NG", "n": 0}],
        ".briefs": [{"market": "ZA"}, {"market": "KE"}],
        "model_usd": [{"usd": 4.5}],
        "raw_responses": [keyrow("tiktok/trending", "data", 1, 7, 1, 7)],
        "stage = 'ask'": [{"total": 21, "failed": 2}],
    })
    store = wd.BigQueryStore(client)
    assert store.collect_ok(DAY) is True
    assert store.observations(DAY) == {"ZA": 3, "NG": 0}
    assert store.published_markets(DAY) == {"ZA", "KE"}
    assert store.model_usd(DAY) == 4.5
    assert store.route_keys(DAY) == [keyrow("tiktok/trending", "data", 1, 7, 1, 7)]
    assert store.ask_runs(DAY) == (21, 2)
    assert store.fired_today(DAY) == {"zero_rows", "brief_late"}
    assert len(client.queries) == 7
    for sql, params in client.queries:
        assert params == {"d": DAY}
        assert "2026-10-01" not in sql
        assert "`ogilvy-trends-v2.intelligence_42_" in sql
        for word in ("INSERT", "UPDATE", "MERGE", "DELETE", "CREATE", "DROP"):
            assert word not in sql.upper().split()


def test_model_spend_sums_model_usd_over_every_stage_plus_ask_records():
    sql = wd.SQL["model_usd"]
    assert "stage IN" not in sql and "stage = 'brief'" not in sql
    assert "JSON_VALUE(r.counts, '$.model_usd')" in sql
    assert "r.stage = 'ask'" in sql and "JSON_VALUE(r.record, '$.run.model_usd')" in sql
    assert "GROUP BY r.run_id" in sql


def test_bigquery_store_reads_an_empty_spend_as_zero():
    client = FakeClient({"model_usd": [{"usd": None}], "stage = 'collect'": [{"n": 0}]})
    store = wd.BigQueryStore(client)
    assert store.model_usd(DAY) == 0.0
    assert store.collect_ok(DAY) is False


def test_job_and_schedule_constants_for_deploy_jobs_and_schedule():
    job = wd.JOB
    assert (job["name"], job["module"], job["identity"]) == ("f42-watchdog", "core.setup.watchdog", "f42-brief")
    assert job["retries"] == 0 and job["secret"] is False and job["timeout"] <= timedelta(minutes=10)
    crons = dict(wd.SCHEDULES)
    assert crons == {"f42-watchdog-quarter": "*/15 2-7 * * *", "f42-watchdog-hourly": "0 0,1,8-23 * * *"}


# N33 and the L2 watchdog rule: log-only signals. They write "42 ALERT <name>:" lines like the others but belong
# to no Cloud Monitoring policy, so they add no cloud resource.

def brief(status="published", cards=3, held=0):
    return {"status": status, "cards": cards, "held": held}


def severity_of(alerts, name):
    return [a.severity for a in alerts if a.name == name]


def test_log_only_signals_are_not_policy_alerts():
    from core.setup import monitoring

    assert wd.LOG_ONLY == ("brief_empty", "brief_all_held", "brief_data_issue", "understand_degraded", "stage_dead",
                           "watchdog_gap")
    assert set(wd.LOG_ONLY).isdisjoint(wd.ALERTS)
    assert set(wd.LOG_ONLY).isdisjoint(monitoring.ALERTS) and set(wd.LOG_ONLY).isdisjoint(monitoring.LOG_ALERTS)


@pytest.mark.parametrize("hh,mm,fires", [(6, 29, False), (6, 30, True), (9, 0, True)])
def test_brief_empty_fires_from_0630_for_a_published_market_with_no_cards_and_nothing_held(hh, mm, fires):
    briefs = {"ZA": brief(cards=0, held=0), "NG": brief(), "KE": brief()}
    alerts = wd.check(at(hh, mm), FakeStore(briefs=briefs))
    assert ("brief_empty" in names(alerts)) is fires
    if fires:
        [alert] = [a for a in alerts if a.name == "brief_empty"]
        assert "ZA" in alert.reason and "NG" not in alert.reason and "KE" not in alert.reason
        assert severity_of(alerts, "brief_empty") == ["WARNING"]


def test_brief_empty_covers_a_partial_brief_too():
    alerts = wd.check(at(7, 0), FakeStore(briefs={"NG": brief("partial", 0, 0), "ZA": brief(), "KE": brief()}))
    assert names(alerts) == ["brief_empty"] and "NG" in alerts[0].reason


def test_a_brief_with_cards_fires_none_of_the_empty_signals():
    assert wd.check(at(7, 0), FakeStore(keys=STABLE)) == []


def test_zero_cards_with_items_held_is_information_not_a_silent_failure():
    briefs = {"ZA": brief(cards=0, held=10), "NG": brief(), "KE": brief()}
    alerts = wd.check(at(7, 0), FakeStore(briefs=briefs))
    assert names(alerts) == ["brief_all_held"]
    assert severity_of(alerts, "brief_all_held") == ["INFO"]
    assert "ZA" in alerts[0].reason and "10" in alerts[0].reason


def test_a_data_issue_brief_is_named_at_any_hour_and_is_not_also_called_empty():
    briefs = {"ZA": brief("data_issue", 0, 0), "NG": brief("data_issue", 0, 0), "KE": brief()}
    alerts = wd.check(at(6, 20), FakeStore(briefs=briefs))
    assert names(alerts) == ["brief_data_issue"]
    assert "ZA" in alerts[0].reason and "NG" in alerts[0].reason and "KE" not in alerts[0].reason
    assert severity_of(alerts, "brief_data_issue") == ["WARNING"]
    assert "brief_empty" not in names(wd.check(at(9, 0), FakeStore(briefs=briefs)))


def understand(status="ok", degraded=(), run_id="understand-20261001-abc123"):
    return {"run_id": run_id, "status": status, "degraded": list(degraded)}


def test_understand_degraded_fires_for_an_ok_run_that_could_not_write_something():
    [alert] = wd.check(at(5, 0), FakeStore(understand=understand(degraded=["enrich", "cluster:ng"])))
    assert alert.name == "understand_degraded" and alert.severity == "WARNING"
    assert "understand-20261001-abc123" in alert.reason and "enrich" in alert.reason and "cluster:ng" in alert.reason


@pytest.mark.parametrize("latest", [None, understand(), understand("failed", ["enrich"]),
                                    understand("running", ["enrich"])])
def test_understand_degraded_stays_quiet_for_no_row_a_clean_run_or_a_run_that_is_not_ok(latest):
    assert "understand_degraded" not in names(wd.check(at(5, 0), FakeStore(understand=latest)))


def stage_row(stage, status, started, run_id=None):
    return {"stage": stage, "status": status, "started_at": started, "run_id": run_id or f"{stage}-20261001-aaa"}


@pytest.mark.parametrize("stage,age_min,fires", [
    ("collect", 3 * 60 + 1, True), ("collect", 3 * 60 - 1, False),
    ("understand", 2 * 60 + 1, True), ("understand", 2 * 60 - 1, False),
    ("detect", 61, True), ("detect", 59, False),
    ("brief", 61, True), ("brief", 59, False),
    ("reconcile", 16, True), ("reconcile", 14, False),
])
def test_stage_dead_fires_when_the_latest_row_is_running_past_its_chain_timeout(stage, age_min, fires):
    now = at(9, 0)
    rows = [stage_row(stage, "running", now - timedelta(minutes=age_min))]
    alerts = wd.check(now, FakeStore(stages=rows))
    assert ("stage_dead" in names(alerts)) is fires
    if fires:
        [alert] = [a for a in alerts if a.name == "stage_dead"]
        assert alert.severity == "WARNING" and f"{stage}-20261001-aaa" in alert.reason and stage in alert.reason


def test_stage_dead_uses_the_same_timeouts_chain_does():
    from core.collect import chain as chain_module

    now = at(12, 0)
    for stage, limit in chain_module.TIMEOUTS.items():
        old = [stage_row(stage, "running", now - limit - timedelta(seconds=1))]
        new = [stage_row(stage, "running", now - limit + timedelta(seconds=1))]
        assert "stage_dead" in names(wd.check(now, FakeStore(stages=old))), stage
        assert "stage_dead" not in names(wd.check(now, FakeStore(stages=new))), stage


def test_stage_dead_ignores_finished_blocked_and_unknown_rows_and_names_every_dead_stage():
    now = at(12, 0)
    long_ago = now - timedelta(hours=9)
    rows = [stage_row("collect", "ok", long_ago), stage_row("understand", "failed", long_ago),
            stage_row("detect", "blocked", long_ago), stage_row("ask", "running", long_ago),
            stage_row("brief", "running", long_ago), stage_row("reconcile", "running", long_ago)]
    [alert] = [a for a in wd.check(now, FakeStore(stages=rows)) if a.name == "stage_dead"]
    assert "brief" in alert.reason and "reconcile" in alert.reason
    for quiet in ("collect", "understand", "detect", "ask"):
        assert f"{quiet}-20261001" not in alert.reason


def test_stage_dead_reads_a_row_started_as_text_as_well_as_a_datetime():
    now = at(12, 0)
    rows = [stage_row("detect", "running", (now - timedelta(hours=2)).isoformat())]
    assert "stage_dead" in names(wd.check(now, FakeStore(stages=rows)))


@pytest.mark.parametrize("minutes,fires", [(20, False), (89, False), (91, True), (300, True)])
def test_watchdog_gap_fires_when_the_last_run_today_is_more_than_90_minutes_old(minutes, fires):
    now = at(9, 0)
    alerts = wd.check(now, FakeStore(watchdog_at=now - timedelta(minutes=minutes)))
    assert ("watchdog_gap" in names(alerts)) is fires
    if fires:
        [alert] = [a for a in alerts if a.name == "watchdog_gap"]
        assert alert.severity == "WARNING" and "watchdog" in alert.reason


def test_watchdog_gap_measures_from_midnight_when_it_has_not_run_today():
    store = FakeStore()
    store.watchdog_last = lambda d: None
    assert "watchdog_gap" not in names(wd.check(at(0, 30), store))
    assert "watchdog_gap" in names(wd.check(at(1, 31), store))


def test_watchdog_gap_is_not_judged_for_a_run_date_override(monkeypatch):
    monkeypatch.setenv("RUN_DATE", "2026-09-29")
    store = FakeStore()
    store.watchdog_last = lambda d: None
    assert "watchdog_gap" not in names(wd.check(at(9, 0), store))


def test_log_only_signals_write_warning_lines_once_a_day_and_stay_out_of_the_error_stream(capsys):
    runs = chain.MemoryRunsStore()
    now = at(7, 0)
    store = FakeStore(runs=runs, briefs={"ZA": brief("data_issue", 0, 0), "NG": brief(), "KE": brief()},
                      stages=[stage_row("collect", "running", now - timedelta(hours=5))])
    assert wd.main(now=now, store=store, runs=runs) == 0
    out = lines(capsys.readouterr().out)
    by_alert = {o["alert"]: o for o in out if o.get("alert")}
    assert set(by_alert) == {"brief_data_issue", "stage_dead"}
    assert all(o["severity"] == "WARNING" for o in by_alert.values())
    assert all(o["message"].startswith(f"42 ALERT {n}: ") for n, o in by_alert.items())
    assert [o for o in out if o["severity"] == "ERROR"] == []
    assert wd.main(now=at(7, 15), store=store, runs=runs) == 0
    [again] = lines(capsys.readouterr().out)
    assert again["severity"] == "INFO" and "stage_dead" in again["message"]


def test_a_log_only_rule_that_cannot_run_fails_the_job_like_any_other(capsys):
    class Broken(FakeStore):
        def stage_latest(self, d):
            raise RuntimeError("runs unreadable")

    runs = chain.MemoryRunsStore()
    assert wd.main(now=at(7, 0), store=Broken(runs=runs), runs=runs) == 1
    assert any("stage_dead" in o["message"] and "runs unreadable" in o["message"]
               for o in lines(capsys.readouterr().out) if o["severity"] == "ERROR")


def duck_agent_sql(name):
    return (wd.SQL[name].replace(f"`{wd.AGENT}.runs`", "{agent}.runs")
            .replace(f"`{wd.AGENT}.v_briefs_current`", "{agent}.v_briefs_current"))


def test_bigquery_store_reads_the_log_only_questions_with_parameterised_selects():
    client = FakeClient({
        "v_briefs_current": [{"market": "ZA", "status": "published", "cards": 0, "held": 0},
                             {"market": "NG", "status": "data_issue", "cards": None, "held": None}],
        "stage = 'understand'": [{"run_id": "understand-1", "status": "ok", "degraded": ["enrich"]}],
        "stage IN": [{"stage": "collect", "run_id": "collect-1", "status": "running", "started_at": at(2)}],
        "stage = 'watchdog'": [{"last_at": at(8)}],
    })
    store = wd.BigQueryStore(client)
    assert store.brief_state(DAY) == {"ZA": {"status": "published", "cards": 0, "held": 0},
                                      "NG": {"status": "data_issue", "cards": 0, "held": 0}}
    assert store.understand_latest(DAY) == {"run_id": "understand-1", "status": "ok", "degraded": ["enrich"]}
    assert store.stage_latest(DAY) == [{"stage": "collect", "run_id": "collect-1", "status": "running",
                                        "started_at": at(2)}]
    assert store.watchdog_last(DAY) == at(8)
    assert len(client.queries) == 4
    for sql, params in client.queries:
        assert params == {"d": DAY} and "2026-10-01" not in sql
        for word in ("INSERT", "UPDATE", "MERGE", "DELETE", "CREATE", "DROP"):
            assert word not in sql.upper().split()
    assert wd.BigQueryStore(FakeClient({"stage = 'understand'": []})).understand_latest(DAY) is None
    assert wd.BigQueryStore(FakeClient({"stage = 'watchdog'": [{"last_at": None}]})).watchdog_last(DAY) is None


def test_the_degraded_expression_is_the_one_coverage_reads():
    from core.api import store as api_store

    assert api_store.DEGRADED_SQL in wd.SQL["understand_latest"]


def test_stage_latest_sql_takes_the_newest_row_per_stage_and_skips_duplicates():
    con = duck.connect(views=False)
    a, b, c = at(2), at(2, 30), at(3)
    duck.load(con, "agent.runs", [
        {"run_id": "collect-old", "stage": "collect", "run_date": DAY, "status": "running", "started_at": a},
        {"run_id": "collect-old", "stage": "collect", "run_date": DAY, "status": "failed", "started_at": a,
         "finished_at": b},
        {"run_id": "collect-new", "stage": "collect", "run_date": DAY, "status": "running", "started_at": c},
        {"run_id": "collect-dup", "stage": "collect", "run_date": DAY, "status": "skipped_duplicate",
         "started_at": at(4), "finished_at": at(4)},
        {"run_id": "detect-1", "stage": "detect", "run_date": DAY, "status": "running", "started_at": a},
        {"run_id": "detect-1", "stage": "detect", "run_date": DAY, "status": "ok", "started_at": a, "finished_at": b},
        {"run_id": "ask-1", "stage": "ask", "run_date": DAY, "status": "running", "started_at": a},
        {"run_id": "collect-yesterday", "stage": "collect", "run_date": DAY - timedelta(days=1),
         "status": "running", "started_at": a},
    ])
    rows = {r["stage"]: r for r in duck.query(con, duck_agent_sql("stage_latest"), {"d": DAY})}
    assert set(rows) == {"collect", "detect"}
    assert (rows["collect"]["run_id"], rows["collect"]["status"]) == ("collect-new", "running")
    assert (rows["detect"]["run_id"], rows["detect"]["status"]) == ("detect-1", "ok")


def test_brief_state_sql_counts_cards_and_held_from_the_current_brief_of_each_market():
    con = duck.connect()
    t = at(6, 20)
    duck.load(con, "agent.runs", [
        {"run_id": "brief-1", "stage": "brief", "run_date": DAY, "status": "ok", "started_at": t, "finished_at": t},
        {"run_id": "brief-2", "stage": "brief", "run_date": DAY, "status": "ok", "started_at": at(8),
         "finished_at": at(8)},
    ])
    full = json.dumps({"cards": [{"id": 1}, {"id": 2}], "held_back": {"count": 4}})
    empty = json.dumps({"cards": [], "held_back": {"count": 0}})
    duck.load(con, "agent.briefs", [
        {"brief_date": DAY, "market": "ZA", "run_id": "brief-1", "published_at": t, "status": "data_issue",
         "payload": empty},
        {"brief_date": DAY, "market": "ZA", "run_id": "brief-2", "published_at": at(8), "status": "published",
         "payload": full},
        {"brief_date": DAY, "market": "NG", "run_id": "brief-2", "published_at": at(8), "status": "published",
         "payload": empty},
        {"brief_date": DAY, "market": "KE", "run_id": "brief-1", "published_at": t, "status": "published",
         "payload": full},
    ])
    rows = {r["market"]: r for r in duck.query(con, duck_agent_sql("brief_state"), {"d": DAY})}
    assert (rows["ZA"]["status"], rows["ZA"]["cards"], rows["ZA"]["held"]) == ("published", 2, 4)
    assert (rows["NG"]["status"], rows["NG"]["cards"], rows["NG"]["held"]) == ("published", 0, 0)
    assert "KE" not in rows  # its row belongs to brief-1, which brief-2 superseded as the day's good run
