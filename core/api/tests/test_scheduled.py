"""Scheduled questions: which schedules are due, the money pre-check and the job loop (contract.md 14.2).

No SocialCrawl, model or BigQuery call happens here: run_ask, the spend reader and BigQuery are fakes."""
import json
from datetime import date
from unittest import mock

import pytest

from core.api import agent_app, scheduled

MONDAY = date(2026, 9, 28)
TUESDAY = date(2026, 9, 29)
PLENTY = {"scheduled": 0, "credits": 0, "model_usd": 0.0}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for name in ("F42_DATA", "F42_PROJECT", "ASK_DAILY", "MODEL_DAILY_USD", "F42_AGENT"):
        monkeypatch.delenv(name, raising=False)
    agent_app.SINK.clear()
    agent_app.SCHEDULES.clear()
    yield
    agent_app.SINK.clear()
    agent_app.SCHEDULES.clear()


def schedule(sid, cadence="daily", tier="T1", status="active", created_at="2026-09-20T08:00:00+02:00",
             market="ZA", question="What is behind #amapiano in South Africa this week?"):
    return {"schedule_id": sid, "created_at": created_at, "status_at": created_at, "who": "passcode",
            "question": question, "market": market, "tier": tier, "cadence": cadence, "deliver": ["in_app"],
            "status": status}


# ---------- due ----------

def test_daily_is_due_every_day_and_weekly_only_on_monday():
    rows = [schedule("s_daily"), schedule("s_weekly", cadence="weekly_monday")]
    assert [s["schedule_id"] for s in scheduled.due(rows, MONDAY)] == ["s_daily", "s_weekly"]
    assert [s["schedule_id"] for s in scheduled.due(rows, TUESDAY)] == ["s_daily"]


def test_paused_is_never_due():
    rows = [schedule("s_daily", status="paused"), schedule("s_weekly", cadence="weekly_monday", status="paused")]
    assert scheduled.due(rows, MONDAY) == []
    assert scheduled.due(rows, TUESDAY) == []


# ---------- precheck ----------

def test_tier_maxima_follow_agent_md():
    assert scheduled.TIERS["T0"]["credits"] == 10 and scheduled.TIERS["T1"]["credits"] == 60
    assert set(scheduled.TIERS) == {"T0", "T1"}
    assert all(t["model_usd"] > 0 for t in scheduled.TIERS.values())


def test_precheck_passes_when_every_budget_covers_the_tier(monkeypatch):
    monkeypatch.setenv("ASK_DAILY", "600")
    monkeypatch.setenv("MODEL_DAILY_USD", "20")
    assert scheduled.precheck("T1", {"scheduled": 60, "credits": 540, "model_usd": 18.0}, 120) is None
    assert scheduled.precheck("T0", {"scheduled": 110, "credits": 590, "model_usd": 19.5}, 120) is None


def test_precheck_scheduled_share_spent(monkeypatch):
    monkeypatch.setenv("ASK_DAILY", "600")
    assert scheduled.precheck("T1", {"scheduled": 61, "credits": 61, "model_usd": 0.0}, 120) == \
        "Scheduled share spent"
    assert scheduled.precheck("T0", {"scheduled": 111, "credits": 111, "model_usd": 0.0}, 120) == \
        "Scheduled share spent"


def test_precheck_daily_questions_spent(monkeypatch):
    monkeypatch.setenv("ASK_DAILY", "600")
    assert scheduled.precheck("T1", {"scheduled": 0, "credits": 541, "model_usd": 0.0}, 120) == \
        "Daily questions spent"


def test_precheck_model_budget_spent(monkeypatch):
    monkeypatch.setenv("MODEL_DAILY_USD", "20")
    usd = 20 - scheduled.TIERS["T1"]["model_usd"] + 0.01
    assert scheduled.precheck("T1", {"scheduled": 0, "credits": 0, "model_usd": usd}, 120) == "Model budget spent"


def test_precheck_never_treats_unknown_spend_as_money():
    for spent in (None, {"scheduled": 0, "credits": None, "model_usd": 0.0},
                  {"scheduled": 0, "credits": 0, "model_usd": None}, {"scheduled": None, "credits": 0, "model_usd": 0.0}):
        assert scheduled.precheck("T0", spent, 120) == scheduled.SPEND_UNKNOWN


# ---------- run ----------

class Ledger:
    """A spend reader over asks the fake has made: each ask spends its tier's full maximum."""

    def __init__(self, credits=0, model_usd=0.0):
        self.spent = {"scheduled": 0, "credits": credits, "model_usd": model_usd}
        self.reads = 0

    def read(self):
        self.reads += 1
        return dict(self.spent)

    def charge(self, tier):
        self.spent["scheduled"] += scheduled.TIERS[tier]["credits"]
        self.spent["credits"] += scheduled.TIERS[tier]["credits"]
        self.spent["model_usd"] += scheduled.TIERS[tier]["model_usd"]


def make_run(ledger, fail=()):
    asked, skipped = [], []

    def ask(s, run_id):
        asked.append((s["schedule_id"], run_id, ledger.reads))
        if s["schedule_id"] in fail:
            raise RuntimeError("the agent fell over")
        ledger.charge(s["tier"])

    def record_skip(s, run_id, reason, status="skipped"):
        skipped.append((s["schedule_id"], run_id, reason, status))

    return ask, record_skip, asked, skipped


def test_run_asks_oldest_first_and_stops_asking_when_the_share_runs_out(monkeypatch):
    monkeypatch.setenv("ASK_DAILY", "600")
    rows = [schedule("s_third", created_at="2026-09-22T08:00:00+02:00"),
            schedule("s_first", created_at="2026-09-20T09:00:00+03:00"),
            schedule("s_second", created_at="2026-09-21T08:00:00+02:00"),
            schedule("s_paused", status="paused", created_at="2026-09-01T08:00:00+02:00")]
    ledger = Ledger()
    ask, record_skip, asked, skipped = make_run(ledger)
    out = scheduled.run(rows, TUESDAY, ledger.read, ask, record_skip, 120)
    assert [a[0] for a in asked] == ["s_first", "s_second"]
    assert [a[1] for a in asked] == ["sched-2026-09-29-s_first", "sched-2026-09-29-s_second"]
    assert skipped == [("s_third", "sched-2026-09-29-s_third", "Scheduled share spent", "skipped")]
    assert [(o["schedule_id"], o["status"], o["reason"]) for o in out] == [
        ("s_first", "asked", None), ("s_second", "asked", None), ("s_third", "skipped", "Scheduled share spent")]


def test_run_rereads_spend_before_each_ask():
    rows = [schedule("s_a", tier="T0"), schedule("s_b", tier="T0", created_at="2026-09-21T08:00:00+02:00")]
    ledger = Ledger()
    ask, record_skip, asked, _ = make_run(ledger)
    scheduled.run(rows, TUESDAY, ledger.read, ask, record_skip, 120)
    assert [a[2] for a in asked] == [1, 2]


def test_run_records_every_skip_with_its_reason(monkeypatch):
    monkeypatch.setenv("ASK_DAILY", "600")
    monkeypatch.setenv("MODEL_DAILY_USD", "20")
    rows = [schedule("s_t1"), schedule("s_t0", tier="T0", created_at="2026-09-21T08:00:00+02:00")]
    ledger = Ledger(credits=560)
    ask, record_skip, asked, skipped = make_run(ledger)
    scheduled.run(rows, TUESDAY, ledger.read, ask, record_skip, 120)
    assert [a[0] for a in asked] == ["s_t0"]
    assert skipped == [("s_t1", "sched-2026-09-29-s_t1", "Daily questions spent", "skipped")]

    ledger = Ledger(model_usd=19.9)
    ask, record_skip, asked, skipped = make_run(ledger)
    scheduled.run(rows, TUESDAY, ledger.read, ask, record_skip, 120)
    assert asked == []
    assert [s[2] for s in skipped] == ["Model budget spent", "Model budget spent"]


def test_run_keeps_going_after_an_ask_raises():
    rows = [schedule("s_bad", tier="T0"), schedule("s_good", tier="T0", created_at="2026-09-21T08:00:00+02:00")]
    ledger = Ledger()
    ask, record_skip, asked, skipped = make_run(ledger, fail=("s_bad",))
    out = scheduled.run(rows, TUESDAY, ledger.read, ask, record_skip, 120)
    assert [a[0] for a in asked] == ["s_bad", "s_good"]
    assert [(s[0], s[3]) for s in skipped] == [("s_bad", "failed")]
    assert "RuntimeError" in skipped[0][2]
    assert [o["status"] for o in out] == ["failed", "asked"]


def test_run_survives_a_spend_reader_that_raises():
    rows = [schedule("s_a", tier="T0")]
    ask, record_skip, asked, skipped = make_run(Ledger())

    def broken():
        raise TimeoutError("unreachable")

    scheduled.run(rows, TUESDAY, broken, ask, record_skip, 120)
    assert asked == [] and [s[2] for s in skipped] == [scheduled.SPEND_UNKNOWN]


def test_dry_run_asks_nothing_and_records_nothing():
    rows = [schedule("s_a"), schedule("s_b", cadence="weekly_monday")]
    ledger = Ledger()
    ask, record_skip, asked, skipped = make_run(ledger)
    out = scheduled.run(rows, MONDAY, ledger.read, ask, record_skip, 120, dry=True)
    assert asked == [] and skipped == []
    assert [(o["schedule_id"], o["status"]) for o in out] == [("s_a", "would_ask"), ("s_b", "would_ask")]


def test_missing_share_is_always_a_dry_run():
    ledger = Ledger()
    ask, record_skip, asked, skipped = make_run(ledger)
    out = scheduled.run([schedule("s_a")], MONDAY, ledger.read, ask, record_skip, None)
    assert asked == [] and skipped == []
    assert out[0]["status"] == "would_ask"


# ---------- the scheduled share ----------

def test_scheduled_share_counts_only_ask_rows_with_a_sched_run_id():
    day = "2026-09-29"
    rows = [
        {"run_id": "sched-2026-09-29-s_a", "stage": "ask", "run_date": day, "credits": 40},
        {"run_id": "sched-2026-09-29-s_a", "stage": "ask", "run_date": day, "credits": 55},  # same run, higher
        {"run_id": "sched-2026-09-29-s_b", "stage": "ask", "run_date": day, "credits": 10},
        {"run_id": "sched-2026-09-29-s_c", "stage": "scheduled", "run_date": day, "credits": 99},
        {"run_id": "r_20260929_user", "stage": "ask", "run_date": day, "credits": 60},
        {"run_id": "sched-2026-09-28-s_a", "stage": "ask", "run_date": "2026-09-28", "credits": 60},
        {"run_id": "sched-2026-09-29-s_i", "stage": "investigation", "run_date": day, "credits": 30},
    ]
    assert scheduled.scheduled_in_rows(rows, day) == 65


def bq_answers(*values):
    client = mock.MagicMock()
    client.query.side_effect = [mock.MagicMock(**{"result.return_value": [{"credits": v}]}) for v in values]
    return client


def test_scheduled_share_in_bigquery_reads_the_ledger_for_ask_and_sched():
    client = bq_answers(65.0, 0)
    assert scheduled.scheduled_in_bigquery(client, "test-project", "2026-09-29") == 65.0
    ledger, runs = (" ".join(c.args[0].split()) for c in client.query.call_args_list)
    assert "`test-project.intelligence_42_core.credit_ledger` l" in ledger
    assert "l.job = 'ask'" in ledger and "STARTS_WITH(l.run_id, 'sched-')" in ledger
    assert "l.trend_date = @d" in ledger
    assert "`test-project.intelligence_42_agent.runs` r" in runs
    assert "r.stage = 'ask'" in runs and "STARTS_WITH(r.run_id, 'sched-')" in runs and "GROUP BY r.run_id" in runs
    for c in client.query.call_args_list:
        params = c.kwargs["job_config"].query_parameters
        assert [(p.name, str(p.value)) for p in params] == [("d", "2026-09-29")]


def test_scheduled_share_in_bigquery_counts_the_runs_rows_while_the_ledger_lacks_the_sched_run_id():
    # run_ask gives SocialCrawl its own run_id today, so the ledger reads 0 while the pinned runs rows hold the spend.
    assert scheduled.scheduled_in_bigquery(bq_answers(None, 120.0), "p", "2026-09-29") == 120.0
    assert scheduled.scheduled_in_bigquery(bq_answers(90.0, 60.0), "p", "2026-09-29") == 90.0


def test_a_schedule_already_asked_today_is_not_asked_again():
    ledger = Ledger()
    ask, record_skip, asked, skipped = make_run(ledger)
    rows = [schedule("s_done"), schedule("s_new", created_at="2026-09-21T08:00:00+02:00")]
    out = scheduled.run(rows, TUESDAY, ledger.read, ask, record_skip, 120,
                        asked={"sched-2026-09-29-s_done"})
    assert [a[0] for a in asked] == ["s_new"] and skipped == []
    assert [(o["schedule_id"], o["status"], o["reason"]) for o in out] == [
        ("s_done", "already_asked", "Already asked today"), ("s_new", "asked", None)]


def test_unreadable_runs_ask_nothing_and_record_why():
    ledger = Ledger()
    ask, record_skip, asked, skipped = make_run(ledger)
    scheduled.run([schedule("s_a")], TUESDAY, ledger.read, ask, record_skip, 120, asked=None)
    assert asked == []
    assert skipped == [("s_a", "sched-2026-09-29-s_a", "Today's runs could not be read", "skipped")]


def journal():
    """ask, record_skip and record_start fakes that write one shared log, so the order of writes shows."""
    writes = []

    def ask(s, run_id):
        writes.append(("ask", s["schedule_id"], run_id))

    def record_skip(s, run_id, reason, status="skipped"):
        writes.append((status, s["schedule_id"], run_id))

    def record_start(s, run_id):
        writes.append(("started", s["schedule_id"], run_id))

    return ask, record_skip, record_start, writes


def test_a_started_row_is_written_before_each_ask():
    rows = [schedule("s_a", tier="T0"), schedule("s_b", tier="T0", created_at="2026-09-21T08:00:00+02:00")]
    ask, record_skip, record_start, writes = journal()
    out = scheduled.run(rows, TUESDAY, Ledger().read, ask, record_skip, 120, record_start=record_start)
    assert writes == [("started", "s_a", "sched-2026-09-29-s_a"), ("ask", "s_a", "sched-2026-09-29-s_a"),
                      ("started", "s_b", "sched-2026-09-29-s_b"), ("ask", "s_b", "sched-2026-09-29-s_b")]
    assert [o["status"] for o in out] == ["asked", "asked"]


def test_a_skipped_schedule_writes_no_started_row(monkeypatch):
    monkeypatch.setenv("ASK_DAILY", "600")
    ask, record_skip, record_start, writes = journal()
    scheduled.run([schedule("s_a")], TUESDAY, Ledger(credits=560).read, ask, record_skip, 120,
                  record_start=record_start)
    assert writes == [("skipped", "s_a", "sched-2026-09-29-s_a")]


def test_a_failed_ask_writes_started_then_failed():
    rows = [schedule("s_bad", tier="T0")]
    ledger = Ledger()
    ask, record_skip, _, _ = make_run(ledger, fail=("s_bad",))
    writes = []

    def logged_ask(s, run_id):
        writes.append("ask")
        ask(s, run_id)

    scheduled.run(rows, TUESDAY, ledger.read, logged_ask,
                  lambda s, rid, reason, status="skipped": writes.append(status), 120,
                  record_start=lambda s, rid: writes.append("started"))
    assert writes == ["started", "ask", "failed"]


def test_an_unrecorded_start_asks_nothing():
    ask, record_skip, _, writes = journal()

    def broken_start(s, run_id):
        raise RuntimeError("the runs row was not written")

    out = scheduled.run([schedule("s_a", tier="T0")], TUESDAY, Ledger().read, ask, record_skip, 120,
                        record_start=broken_start)
    assert writes == [("failed", "s_a", "sched-2026-09-29-s_a")]
    assert out[0]["status"] == "failed" and out[0]["reason"] == scheduled.START_UNRECORDED


def test_dry_run_writes_no_started_row():
    rows = [schedule("s_a"), schedule("s_b", cadence="weekly_monday")]
    ask, record_skip, record_start, writes = journal()
    scheduled.run(rows, MONDAY, Ledger().read, ask, record_skip, 120, dry=True, record_start=record_start)
    scheduled.run(rows, MONDAY, Ledger().read, ask, record_skip, None, record_start=record_start)
    assert writes == []


def test_record_start_writes_a_scheduled_started_row_that_has_not_finished():
    scheduled.record_start(schedule("s_go"), "sched-2026-09-29-s_go")
    (row,) = agent_app.SINK
    assert row["run_id"] == "sched-2026-09-29-s_go" and row["stage"] == "scheduled"
    assert row["status"] == "started" and row["credits"] == 0 and row["run_date"] == "2026-09-29"
    assert row["started_at"] and row["finished_at"] is None
    assert json.loads(row["record"])["schedule_id"] == "s_go"


def test_a_retry_after_a_kill_mid_ask_asks_nothing():
    day = scheduled.today()
    rid = scheduled.run_id(day, "s_a")
    killed = []

    def killed_ask(s, run_id):
        killed.append(run_id)
        raise KeyboardInterrupt  # the job is killed mid-ask: no ask row, no failed row

    with pytest.raises(KeyboardInterrupt):
        scheduled.run([schedule("s_a", tier="T0")], day, Ledger().read, killed_ask, scheduled.record_skip, 120,
                      record_start=scheduled.record_start)
    assert killed == [rid]
    assert [(r["stage"], r["status"]) for r in agent_app.SINK] == [("scheduled", "started")]

    ledger = Ledger()
    ask, record_skip, asked, skipped = make_run(ledger)
    out = scheduled.run([schedule("s_a", tier="T0")], day, ledger.read, ask, record_skip, 120,
                        asked=scheduled.asked_today(day.isoformat()), record_start=scheduled.record_start)
    assert asked == [] and skipped == []
    assert out[0]["status"] == "already_asked"
    assert len(agent_app.SINK) == 1


def test_a_started_ask_that_never_finished_shows_as_started_in_get_schedules():
    day = scheduled.today()
    agent_app.SCHEDULES.append(agent_app.schedule_row(schedule("s_a")))
    scheduled.record_start(schedule("s_a"), scheduled.run_id(day, "s_a"))
    (listed,) = agent_app.list_schedules()["schedules"]
    assert listed["last_run"]["status"] == "started"
    assert listed["last_run"]["run_id"] == scheduled.run_id(day, "s_a")
    assert listed["skip_reason"] is None


def test_a_finished_ask_replaces_the_started_row_in_get_schedules(monkeypatch):
    def fake_run_ask(request, emit, should_stop):
        return {"answer": {"status": "complete"}, "run": {"credits": 5, "model_usd": 0.1}}

    monkeypatch.setattr(agent_app, "resolve_run_ask", lambda: fake_run_ask)
    day = scheduled.today()
    s = schedule("s_a", tier="T0")
    agent_app.SCHEDULES.append(agent_app.schedule_row(s))
    scheduled.run([s], day, Ledger().read, scheduled.live_ask, scheduled.record_skip, 120,
                  record_start=scheduled.record_start)
    assert [(r["stage"], r["status"]) for r in agent_app.SINK] == [("scheduled", "started"), ("ask", "complete")]
    (listed,) = agent_app.list_schedules()["schedules"]
    assert listed["last_run"]["status"] == "complete"


def test_asked_today_counts_started_rows():
    day = "2026-09-29"
    agent_app.SINK.extend([
        {"run_id": "sched-2026-09-29-s_a", "stage": "scheduled", "status": "started", "run_date": day},
        {"run_id": "sched-2026-09-29-s_b", "stage": "scheduled", "status": "skipped", "run_date": day},
        {"run_id": "sched-2026-09-29-s_c", "stage": "scheduled", "status": "failed", "run_date": day},
        {"run_id": "sched-2026-09-28-s_d", "stage": "scheduled", "status": "started", "run_date": "2026-09-28"},
        {"run_id": "r_other", "stage": "scheduled", "status": "started", "run_date": day},
    ])
    assert scheduled.asked_today(day) == {"sched-2026-09-29-s_a"}


def test_asked_today_in_bigquery_counts_ask_rows_and_started_rows(monkeypatch):
    monkeypatch.setenv("F42_DATA", "bigquery")
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [{"run_id": "sched-2026-09-29-s_a"}]
    monkeypatch.setattr(agent_app, "bigquery_client", lambda: client)
    monkeypatch.setattr(agent_app, "project", lambda: "test-project")
    assert scheduled.asked_today("2026-09-29") == {"sched-2026-09-29-s_a"}
    sql = " ".join(client.query.call_args.args[0].split())
    assert "`test-project.intelligence_42_agent.runs` r" in sql
    assert "(r.stage = 'ask' OR (r.stage = 'scheduled' AND r.status = 'started'))" in sql
    assert "STARTS_WITH(r.run_id, 'sched-')" in sql and "r.run_date = @d" in sql


def test_main_live_writes_a_started_row_before_the_ask(job):
    caps, asked = job
    caps.write_text("SCHEDULED_DAILY: 120\n", encoding="utf-8")
    assert scheduled.main(["--live"]) == 0
    rid = f"sched-{scheduled.today().isoformat()}-s_main"
    assert asked == [("s_main", rid)]
    assert [(r["run_id"], r["stage"], r["status"]) for r in agent_app.SINK] == [(rid, "scheduled", "started")]


def test_asked_today_in_memory_reads_sched_ask_rows_of_the_day():
    day = "2026-09-29"
    agent_app.SINK.extend([
        {"run_id": "sched-2026-09-29-s_a", "stage": "ask", "run_date": day},
        {"run_id": "sched-2026-09-29-s_b", "stage": "scheduled", "run_date": day},
        {"run_id": "r_other", "stage": "ask", "run_date": day},
        {"run_id": "sched-2026-09-28-s_c", "stage": "ask", "run_date": "2026-09-28"},
    ])
    assert scheduled.asked_today(day) == {"sched-2026-09-29-s_a"}


def test_read_spend_adds_the_scheduled_share_to_todays_spend(monkeypatch):
    day = scheduled.today().isoformat()
    agent_app.SINK.extend([
        {"run_id": f"sched-{day}-s_a", "stage": "ask", "run_date": day, "credits": 50,
         "record": json.dumps({"run": {"model_usd": 1.25}})},
        {"run_id": "r_user", "stage": "ask", "run_date": day, "credits": 20,
         "record": json.dumps({"run": {"model_usd": 0.5}})},
    ])
    assert scheduled.read_spend() == {"scheduled": 50, "credits": 70, "model_usd": 1.75}


# ---------- SCHEDULED_DAILY ----------

def test_share_is_read_from_caps_yaml(tmp_path):
    caps = tmp_path / "caps.yaml"
    caps.write_text("ASK_DAILY: 600\nSCHEDULED_DAILY: 120\n", encoding="utf-8")
    assert scheduled.read_share(caps) == 120
    caps.write_text("ASK_DAILY: 600\n", encoding="utf-8")
    assert scheduled.read_share(caps) is None
    assert scheduled.read_share(tmp_path / "missing.yaml") is None


# ---------- the live ask and the skip row ----------

def test_live_ask_runs_the_agent_and_writes_an_ask_record_with_the_schedule_id(monkeypatch):
    seen = []

    def fake_run_ask(request, emit, should_stop):
        seen.append(request)
        emit({"kind": "plan", "text": "Planning"})
        return {"answer": {"status": "complete"}, "run": {"run_id": "r_agent_own", "credits": 12, "model_usd": 0.3}}

    monkeypatch.setattr(agent_app, "resolve_run_ask", lambda: fake_run_ask)
    s = schedule("s_live", tier="T0")
    record = scheduled.live_ask(s, "sched-2026-09-29-s_live")
    req = seen[0]
    assert req["question"] == s["question"] and req["market"] == "ZA"
    assert req["tier"] == "T0" and req["mode"] == "live" and req["parent_id"] is None
    assert req["run_id"] == "sched-2026-09-29-s_live" and req["schedule_id"] == "s_live"
    assert record["schedule_id"] == "s_live" and record["status"] == "complete"
    (row,) = agent_app.SINK
    assert row["run_id"] == "sched-2026-09-29-s_live" and row["stage"] == "ask" and row["credits"] == 12
    assert json.loads(row["record"])["schedule_id"] == "s_live"


def test_live_ask_that_fails_keeps_the_sched_run_id_and_its_spend(monkeypatch):
    class Failed(RuntimeError):
        run = {"model_usd": 0.4, "credits": 8}

    def fake_run_ask(request, emit, should_stop):
        raise Failed("boom")

    monkeypatch.setattr(agent_app, "resolve_run_ask", lambda: fake_run_ask)
    record = scheduled.live_ask(schedule("s_fail"), "sched-2026-09-29-s_fail")
    assert record["status"] == "failed"
    (row,) = agent_app.SINK
    assert row["run_id"] == "sched-2026-09-29-s_fail" and row["status"] == "failed"
    assert json.loads(row["record"])["run"]["model_usd"] == 0.4


def test_live_ask_without_an_agent_raises(monkeypatch):
    monkeypatch.setattr(agent_app, "resolve_run_ask", lambda: None)
    with pytest.raises(RuntimeError):
        scheduled.live_ask(schedule("s_none"), "sched-2026-09-29-s_none")
    assert agent_app.SINK == []


def test_record_skip_writes_a_scheduled_runs_row():
    scheduled.record_skip(schedule("s_skip"), "sched-2026-09-29-s_skip", "Scheduled share spent")
    (row,) = agent_app.SINK
    assert row["run_id"] == "sched-2026-09-29-s_skip" and row["stage"] == "scheduled"
    assert row["status"] == "skipped" and row["outcome"] == "Scheduled share spent"
    assert row["run_date"] == "2026-09-29" and row["credits"] == 0
    assert json.loads(row["record"]) == {"schedule_id": "s_skip", "reason": "Scheduled share spent"}


# ---------- main ----------

@pytest.fixture
def job(monkeypatch, tmp_path):
    asked = []
    monkeypatch.setattr(scheduled, "CAPS_FILE", tmp_path / "caps.yaml")
    monkeypatch.setattr(scheduled, "read_spend", lambda: dict(PLENTY))
    monkeypatch.setattr(scheduled, "live_ask", lambda s, run_id: asked.append((s["schedule_id"], run_id)))
    agent_app.SCHEDULES.append(agent_app.schedule_row(schedule("s_main")))
    return tmp_path / "caps.yaml", asked


def test_main_defaults_to_a_dry_run(job, capsys):
    caps, asked = job
    caps.write_text("SCHEDULED_DAILY: 120\n", encoding="utf-8")
    assert scheduled.main([]) == 0
    assert asked == [] and agent_app.SINK == []
    out = capsys.readouterr().out
    assert "Dry run" in out and "s_main" in out
    # stdout lands in Cloud Logging, so no question text.
    assert "amapiano" not in out


def test_main_live_without_the_share_stays_a_dry_run(job, capsys):
    _, asked = job
    assert scheduled.main(["--live"]) == 0
    assert asked == [] and agent_app.SINK == []
    assert "SCHEDULED_DAILY" in capsys.readouterr().out


def test_main_live_with_the_share_asks(job):
    caps, asked = job
    caps.write_text("SCHEDULED_DAILY: 120\n", encoding="utf-8")
    assert scheduled.main(["--live"]) == 0
    assert asked == [("s_main", f"sched-{scheduled.today().isoformat()}-s_main")]


# ---------- a failed ask is reported as failed ----------

def test_a_failed_live_ask_is_reported_failed_with_its_stored_reason(monkeypatch):
    def failing_run_ask(request, emit, should_stop):
        raise RuntimeError("the agent broke")

    monkeypatch.setattr(agent_app, "resolve_run_ask", lambda: failing_run_ask)
    day = scheduled.today()
    rid = scheduled.run_id(day, "s_a")
    s = schedule("s_a", tier="T0")
    agent_app.SCHEDULES.append(agent_app.schedule_row(s))
    out = scheduled.run([s], day, Ledger().read, scheduled.live_ask, scheduled.record_skip, 120,
                        record_start=scheduled.record_start)
    assert out[0]["status"] == "failed"
    assert out[0]["reason"] == "The agent hit an error and could not finish (RuntimeError)."
    # The failed ask row is the record; no second failed row is written beside it.
    assert [(r["run_id"], r["stage"], r["status"]) for r in agent_app.SINK] == [
        (rid, "scheduled", "started"), (rid, "ask", "failed")]
    (listed,) = agent_app.list_schedules()["schedules"]
    assert listed["last_run"]["status"] == "failed"
    # A retry the same day still does not ask it again.
    again = scheduled.run([s], day, Ledger().read, scheduled.live_ask, scheduled.record_skip, 120,
                          asked=scheduled.asked_today(day.isoformat()), record_start=scheduled.record_start)
    assert again[0]["status"] == "already_asked" and len(agent_app.SINK) == 2


def test_an_ask_that_returns_a_failed_record_is_not_counted_as_asked():
    def ask(s, run_id):
        return {"status": "failed", "answer": None,
                "error": {"error": "model_unavailable", "message": agent_app.MODEL_UNAVAILABLE}}

    writes = []
    out = scheduled.run([schedule("s_a", tier="T0")], TUESDAY, Ledger().read, ask,
                        lambda s, rid, reason, status="skipped": writes.append(status), 120,
                        record_start=lambda s, rid: writes.append("started"))
    assert [(o["status"], o["reason"]) for o in out] == [("failed", agent_app.MODEL_UNAVAILABLE)]
    assert writes == ["started"]


def test_main_exits_1_when_an_ask_failed(job, monkeypatch, capsys):
    caps, _ = job
    caps.write_text("SCHEDULED_DAILY: 120\n", encoding="utf-8")
    monkeypatch.setattr(scheduled, "live_ask", lambda s, run_id: {
        "status": "failed", "error": {"error": "internal", "message": "The agent hit an error and could not finish (RuntimeError)."}})
    assert scheduled.main(["--live"]) == 1
    out = capsys.readouterr().out
    assert "1 failed" in out and "failed s_main (T1)" in out
    assert "asked s_main" not in out
