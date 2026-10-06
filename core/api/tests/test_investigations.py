import datetime as dt
import json
from unittest import mock

import pytest
from google.api_core.exceptions import NotFound

from core.api import investigations as inv
from core.config import caps as caps_config

# Arithmetic against the cap as it stood until 3 October 2026 (conftest.py).
pytestmark = pytest.mark.usefixtures("old_model_cap_schedule")

DAY = "2026-09-29"


@pytest.fixture(autouse=True)
def caps(monkeypatch):
    monkeypatch.delenv("ASK_DAILY", raising=False)
    monkeypatch.delenv("MODEL_DAILY_USD", raising=False)

    class Clock:
        value = dt.datetime(2026, 10, 3, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))

        @classmethod
        def now(cls, tz=None):
            return cls.value.astimezone(tz) if tz else cls.value.replace(tzinfo=None)

    monkeypatch.setattr(caps_config, "datetime", Clock)
    inv.RESERVED.clear()
    yield Clock
    inv.RESERVED.clear()


def test_caps_default_to_setup_and_read_env(monkeypatch):
    assert inv.caps() == {"credits": 600.0, "model_usd": 20.0}
    monkeypatch.setenv("ASK_DAILY", "900")
    monkeypatch.setenv("MODEL_DAILY_USD", "35.5")
    assert inv.caps() == {"credits": 900.0, "model_usd": 20.0}
    monkeypatch.setenv("MODEL_DAILY_USD", "15.5")
    assert inv.caps() == {"credits": 900.0, "model_usd": 15.5}


def test_budget_reads_shared_model_cap_again_after_sast_expiry(caps):
    spent = {"credits": 0, "model_usd": 0}
    reserved = {"credits": 0, "model_usd": 0}
    caps.value = dt.datetime(2026, 10, 2, 21, 59, tzinfo=dt.timezone.utc)
    before = inv.budget_left(spent, reserved)
    caps.value = dt.datetime(2026, 10, 2, 22, 0, tzinfo=dt.timezone.utc)
    after = inv.budget_left(spent, reserved)
    assert (before["model_usd"], after["model_usd"]) == (80.0, 20.0)


def test_budget_left_subtracts_spend_and_reservations():
    left = inv.budget_left({"credits": 214.5, "model_usd": 1.25}, {"credits": 100, "model_usd": 3.0})
    assert left == {"credits": 285, "model_usd": 15.75}


def test_budget_left_subtracts_planner_and_running_reservations():
    inv.RESERVED.add("plan:i_1", {"credits": 0, "model_usd": inv.PLANNER_USD})
    inv.RESERVED.add("i_2", {"credits": 200, "model_usd": 4.0})
    left = inv.budget_left({"credits": 100, "model_usd": 2.0}, inv.RESERVED.total())
    assert left == {"credits": 300, "model_usd": 13.5}


def test_budget_left_never_goes_below_zero():
    assert inv.budget_left({"credits": 700, "model_usd": 25}, {"credits": 0, "model_usd": 0}) == \
        {"credits": 0, "model_usd": 0.0}


@pytest.mark.parametrize("spent", [{"credits": None, "model_usd": 1.0}, {"credits": 10, "model_usd": None}, None])
def test_unknown_spend_gives_unknown_budget(spent):
    assert inv.budget_left(spent, {"credits": 0, "model_usd": 0}) is None


def row(run_id, stage="ask", credits=None, model_usd=None, record_usd=None, day=DAY, counts=None):
    out = {"run_id": run_id, "stage": stage, "run_date": day, "credits": credits}
    if model_usd is not None:
        out["model_usd"] = model_usd
    out["record"] = json.dumps({"run": {"model_usd": record_usd}}) if record_usd is not None else None
    if counts is not None:
        out["counts"] = json.dumps(counts)
    return out


def test_spent_in_rows_sums_today_across_stages():
    rows = [row("r1", credits=214, record_usd=0.8),
            row("r2", stage="investigation", model_usd=0.04),
            row("r3", stage="brief", model_usd=2.0),
            row("r4", credits=50, record_usd=9.0, day="2026-09-28"),
            row("r5", credits=38)]
    assert inv.spent_in_rows(rows, DAY) == {"credits": 252, "model_usd": 2.84}


def test_spent_in_rows_counts_a_run_once():
    rows = [row("r1", stage="brief", model_usd=1.0), row("r1", stage="brief", model_usd=1.5)]
    assert inv.spent_in_rows(rows, DAY) == {"credits": 0, "model_usd": 1.5}


def test_spent_in_rows_counts_bookings_corrections_and_unbooked_remainder():
    rows = [
        row("r_parent", stage="understand", counts={"model_usd": 0.75, "booked_model_usd": 9.0}),
        row("r_booking", stage="understand_spend", counts={"model_usd": 1.25}),
        row("r_booking", stage="understand_spend", counts={"model_usd": 0.5}),
        row("r_correction", stage="understand_spend", counts={"model_usd": -0.5}),
        row("r_record", stage="brief", record_usd=0.2, counts={"model_usd": 8.0}),
        row("r_top", stage="brief", model_usd=0.1, record_usd=0.2, counts={"model_usd": 0.3}),
        row("r_old", stage="understand_spend", day="2026-09-28", counts={"model_usd": 99.0}),
    ]
    assert inv.spent_in_rows(rows, DAY) == {"credits": 0, "model_usd": 1.8}


class FakeClient:
    def __init__(self, columns=("run_id", "model_usd", "record"), credits=120.0, usd=2.5, missing=()):
        self.columns, self.credits, self.usd, self.missing = columns, credits, usd, missing
        self.calls = []

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        result = mock.MagicMock()
        if "credit_ledger" in sql:
            if "credit_ledger" in self.missing:
                raise NotFound("credit_ledger")
            result.result.return_value = [{"credits": self.credits}]
        elif "INFORMATION_SCHEMA" in sql:
            result.result.return_value = [{"column_name": c} for c in self.columns]
        else:
            if "runs" in self.missing:
                raise NotFound("runs")
            result.result.return_value = [{"usd": self.usd}]
        return result


def test_bigquery_spend_reads_ledger_and_runs_with_capped_parameterised_queries():
    client = FakeClient()
    assert inv.spent_in_bigquery(client, "p", DAY) == {"credits": 120.0, "model_usd": 2.5}
    ledger = next(sql for sql, _ in client.calls if "credit_ledger" in sql)
    runs = next(sql for sql, _ in client.calls if "intelligence_42_agent.runs" in sql)
    assert "`p.intelligence_42_core.credit_ledger`" in ledger
    assert "@d" in ledger and "@jobs" in ledger and DAY not in ledger
    assert "@d" in runs and DAY not in runs
    assert "r.model_usd" in runs and "$.run.model_usd" in runs
    for _, config in client.calls:
        assert config.maximum_bytes_billed == 2 * 1024 ** 3
    params = {p.name: p for _, c in client.calls for p in c.query_parameters}
    assert str(params["d"].value) == DAY
    assert set(params["jobs"].values) == {"ask", "investigation"}


def test_bigquery_spend_without_model_usd_column_reads_the_record():
    client = FakeClient(columns=("run_id", "record"))
    assert inv.spent_in_bigquery(client, "p", DAY)["model_usd"] == 2.5
    runs = next(sql for sql, _ in client.calls if "intelligence_42_agent.runs" in sql)
    assert "r.model_usd" not in runs and "$.run.model_usd" in runs


def test_bigquery_spend_reads_counts_after_top_level_and_record_values():
    client = FakeClient(columns=("run_id", "model_usd", "record", "counts"), usd=1.8)
    assert inv.spent_in_bigquery(client, "p", DAY)["model_usd"] == 1.8
    runs = next(sql for sql, _ in client.calls if "intelligence_42_agent.runs" in sql)
    assert "JSON_VALUE(r.counts, '$.model_usd')" in runs
    assert runs.index("r.model_usd") < runs.index("$.run.model_usd") < runs.index("$.model_usd")
    assert "MAX(COALESCE(" in runs
    assert "WHERE r.run_date = @d" in runs and "GROUP BY r.run_id" in runs and "SUM(u.usd)" in runs


def test_bigquery_spend_with_nothing_spent_today_is_zero():
    client = FakeClient(credits=None, usd=None)
    assert inv.spent_in_bigquery(client, "p", DAY) == {"credits": 0.0, "model_usd": 0.0}


@pytest.mark.parametrize("client, unknown", [
    (FakeClient(missing=("credit_ledger",)), "credits"),
    (FakeClient(columns=()), "model_usd"),
    (FakeClient(columns=("run_id", "stage")), "model_usd"),
    (FakeClient(missing=("runs",)), "model_usd"),
])
def test_bigquery_missing_table_or_column_reads_as_unknown(client, unknown):
    spent = inv.spent_in_bigquery(client, "p", DAY)
    assert spent[unknown] is None
    assert inv.budget_left(spent, {"credits": 0, "model_usd": 0}) is None


def test_bigquery_error_reads_as_unknown_not_as_money():
    client = mock.MagicMock()
    client.query.side_effect = RuntimeError("unreachable")
    assert inv.spent_in_bigquery(client, "p", DAY) == {"credits": None, "model_usd": None}


def test_reservations_add_up_and_release():
    inv.RESERVED.add("i_a", {"credits": 400, "model_usd": 3.0})
    inv.RESERVED.add("i_b", {"credits": 200, "model_usd": 1.5})
    assert inv.RESERVED.total() == {"credits": 600, "model_usd": 4.5}
    inv.RESERVED.release("i_a")
    inv.RESERVED.release("i_a")
    assert inv.RESERVED.total() == {"credits": 200, "model_usd": 1.5}


def plan(**change):
    out = inv.fixture_planner({"question": "What is behind #fixture?", "market": "ZA"})["plan"]
    out.update(change)
    return out


def test_ceilings_are_the_lower_of_plan_and_budget_left():
    left = {"credits": 250, "model_usd": 10.0}
    assert inv.ceilings(plan(max_credits=400, max_model_usd=3.0), left) == {"max_credits": 250, "max_model_usd": 3.0}
    assert inv.ceilings(plan(max_credits=100, max_model_usd=3.0), {"credits": 250, "model_usd": 1.2}) == \
        {"max_credits": 100, "max_model_usd": 1.2}


def test_refusal_names_whichever_budget_the_estimate_breaks():
    assert inv.refusal({"credits": 300, "model_usd": 1.5}, {"credits": 300, "model_usd": 1.5}) is None
    credits = inv.refusal({"credits": 301, "model_usd": 1.5}, {"credits": 300, "model_usd": 5})
    assert "301 credits" in credits and "300" in credits and "ASK_DAILY" in credits
    assert "MODEL_DAILY_USD" not in credits
    usd = inv.refusal({"credits": 10, "model_usd": 2.0}, {"credits": 300, "model_usd": 1.25})
    assert "USD 2.00" in usd and "USD 1.25" in usd and "MODEL_DAILY_USD" in usd and "ASK_DAILY" not in usd
    both = inv.refusal({"credits": 999, "model_usd": 99}, {"credits": 1, "model_usd": 1})
    assert "ASK_DAILY" in both and "MODEL_DAILY_USD" in both


def test_clamp_keeps_plan_ceilings_within_budget_left():
    clamped = inv.clamp(plan(max_credits=400, max_model_usd=3.0), {"credits": 120, "model_usd": 0.75})
    assert (clamped["max_credits"], clamped["max_model_usd"]) == (120, 0.75)


def test_fixture_planner_gives_three_sub_questions_an_estimate_and_small_spend():
    out = inv.fixture_planner({"question": "What is behind #fixture?", "market": "ZA"})
    assert len(out["plan"]["sub_questions"]) == 3
    assert inv.validate_plan(out["plan"])[1] is None
    assert set(out["estimate"]) == {"credits", "model_usd", "minutes"}
    assert 0 < out["run"]["model_usd"] < inv.PLANNER_USD
    assert out["run"]["run_id"].startswith("r_plan_")
    assert inv.fixture_estimate(out["plan"]) == out["estimate"]


def test_validate_plan_keeps_only_plan_keys_and_names_x():
    raw = plan(extra="dropped")
    raw["sub_questions"][0] = {**raw["sub_questions"][0], "platforms": ["twitter", "tiktok"], "note": "dropped"}
    clean, why = inv.validate_plan(raw)
    assert why is None
    assert set(clean) == {"sub_questions", "researchers", "gap_round", "max_credits", "max_model_usd"}
    assert set(clean["sub_questions"][0]) == {"id", "text", "platforms", "credits"}
    assert clean["sub_questions"][0]["platforms"] == ["x", "tiktok"]


@pytest.mark.parametrize("count", [1, 8])
def test_validate_plan_normalizes_researchers_to_sub_question_count(count):
    raw = plan()
    template = raw["sub_questions"][0]
    raw["sub_questions"] = [{**template, "id": f"q{i}"} for i in range(1, count + 1)]
    raw["researchers"] = 99

    clean, why = inv.validate_plan(raw)

    assert why is None
    assert clean["researchers"] == count


def test_validate_plan_refuses_nine_sub_questions():
    raw = plan()
    template = raw["sub_questions"][0]
    raw["sub_questions"] = [{**template, "id": f"q{i}"} for i in range(1, 10)]
    raw["researchers"] = 8

    clean, why = inv.validate_plan(raw)

    assert clean is None and why


@pytest.mark.parametrize("change", [
    {"sub_questions": []},
    {"sub_questions": [{"id": "q1", "text": "Where?", "platforms": ["tiktok"], "credits": 10}] * 13},
    {"sub_questions": [{"id": "q1", "text": "", "platforms": ["tiktok"], "credits": 10}]},
    {"sub_questions": [{"id": "q1", "text": "Where did it start?", "platforms": ["myspace"], "credits": 10}]},
    {"sub_questions": [{"id": "q1", "text": "Where did it start?", "platforms": [], "credits": 10}]},
    {"sub_questions": [{"id": "q1", "text": "Where did it start?", "platforms": ["tiktok"], "credits": -1}]},
    {"sub_questions": [{"id": "q1", "text": "Where?", "platforms": ["tiktok"], "credits": 1},
                       {"id": "q1", "text": "Why?", "platforms": ["x"], "credits": 1}]},
    {"gap_round": "yes"},
    {"max_credits": -5},
    {"max_model_usd": "3"},
])
def test_bad_plans_are_named(change):
    clean, why = inv.validate_plan(plan(**change))
    assert clean is None and isinstance(why, str) and why


def test_storage_row_holds_contract_columns_as_json():
    at = "2026-09-29T10:00:00+02:00"
    stored = inv.storage_row("i_0123456789ab", 2, at, "draft", "What is behind #fixture?", "ZA",
                             plan(), {"credits": 300, "model_usd": 1.5, "minutes": 6}, None, "r_plan_x")
    assert set(stored) == {"investigation_id", "version", "created_at", "who", "status", "question", "market",
                           "plan", "estimate", "ask_id", "run_id"}
    assert json.loads(stored["plan"]) == plan()
    assert inv.view(stored)["plan"] == plan()


# The plan contract the agent runs (core/agent/investigate.py validate_plan is authoritative).

def test_platform_groups_are_the_agents_groups():
    from core.agent import ask
    assert inv.PLAN_PLATFORMS == tuple(ask.PLATFORM_GROUPS)
    assert set(inv.PLATFORM_LABELS) == set(ask.GROUP_LABELS)


def real_planner_plan(planned=None):
    """A plan from the real planner on fakes: fractional credits and the agent's platform group ids."""
    from core.agent.tests import test_investigate as agent_tests
    model = agent_tests.PlannerModel({"c3": {"verdict": "needs_evidence"}}, planned=planned)
    return agent_tests.draft(agent_tests.harness(model=model))


PLANNED_WITH_EVERY_GROUP = [
    {"text": "Which amapiano sounds are people dancing to on TikTok?", "platforms": ["tiktok"]},
    {"text": "What do people on X say about amapiano at taxi ranks?", "platforms": ["x", "facebook"]},
    {"text": "Which amapiano clips travel on Instagram reels?", "platforms": ["instagram", "youtube"]},
    {"text": "What do Reddit and Threads posts say about the log drum?", "platforms": ["reddit_threads"]},
    {"text": "What does the news say about amapiano this week?", "platforms": ["news"]},
    {"text": "Which amapiano DJs do people name most?", "platforms": ["tiktok", "x"]},
    {"text": "Where in Gauteng do people post amapiano parties from?", "platforms": ["instagram", "tiktok"]},
]


def test_the_real_planners_plan_is_accepted_as_it_comes():
    out = real_planner_plan(PLANNED_WITH_EVERY_GROUP)
    raw = out["plan"]
    assert any(isinstance(q["credits"], float) and q["credits"] != int(q["credits"]) for q in raw["sub_questions"])
    clean, why = inv.validate_plan(raw)
    assert why is None
    assert [q["platforms"] for q in clean["sub_questions"]] == [q["platforms"] for q in raw["sub_questions"]]
    assert [q["credits"] for q in clean["sub_questions"]] == [q["credits"] for q in raw["sub_questions"]]
    assert clean["max_credits"] == raw["max_credits"]


def test_the_plan_the_api_returns_runs_in_the_agent():
    from core.agent import ask, investigate
    from core.agent.tests.test_ask import NOW
    for planned in (None, PLANNED_WITH_EVERY_GROUP):
        clean, why = inv.validate_plan(real_planner_plan(planned)["plan"])
        assert why is None
        clamped = inv.clamp(clean, {"credits": 600, "model_usd": 20.0})
        assert investigate.validate_plan(clamped, ask.MODEL, now=NOW)["sub_questions"] == clamped["sub_questions"]
        assert inv.validate_plan({**clamped, "skin_id": "sk_0123456789ab"})[0] == clamped


@pytest.mark.parametrize("legacy, group", [("twitter", "x"), ("reddit", "reddit_threads"),
                                           ("threads", "reddit_threads")])
def test_legacy_platform_names_map_to_agent_groups(legacy, group):
    raw = plan()
    raw["sub_questions"][0] = {**raw["sub_questions"][0], "platforms": [legacy]}
    clean, why = inv.validate_plan(raw)
    assert why is None and clean["sub_questions"][0]["platforms"] == [group]


def test_reddit_and_threads_together_are_one_group():
    raw = plan()
    raw["sub_questions"][0] = {**raw["sub_questions"][0], "platforms": ["reddit", "threads", "reddit_threads"]}
    assert inv.validate_plan(raw)[0]["sub_questions"][0]["platforms"] == ["reddit_threads"]


@pytest.mark.parametrize("platform, word", [("telegram", "Telegram"), ("apple_music", "Apple Music")])
def test_platforms_the_agent_cannot_search_are_refused_in_plain_words(platform, word):
    raw = plan()
    raw["sub_questions"][0] = {**raw["sub_questions"][0], "platforms": ["tiktok", platform]}
    clean, why = inv.validate_plan(raw)
    assert clean is None
    assert word in why and "cannot" in why and "TikTok" in why


def test_credits_are_any_number_from_zero_kept_to_two_decimals():
    raw = plan(max_credits=399.999, max_model_usd=3.0)
    raw["sub_questions"][0] = {**raw["sub_questions"][0], "credits": 33.3333}
    raw["sub_questions"][1] = {**raw["sub_questions"][1], "credits": 0}
    clean, why = inv.validate_plan(raw)
    assert why is None
    assert clean["sub_questions"][0]["credits"] == 33.33 and clean["sub_questions"][1]["credits"] == 0
    assert clean["max_credits"] == 400.0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.01, "10", True, None])
def test_credits_must_be_a_finite_number_from_zero(bad):
    raw = plan()
    raw["sub_questions"][0] = {**raw["sub_questions"][0], "credits": bad}
    assert inv.validate_plan(raw)[0] is None
    assert inv.validate_plan(plan(max_credits=bad))[0] is None


def test_fixture_planner_uses_the_agents_groups():
    from core.agent import ask
    out = inv.fixture_planner({"question": "What is behind #fixture?", "market": "ZA"})
    assert all(p in ask.PLATFORM_GROUPS for q in out["plan"]["sub_questions"] for p in q["platforms"])


def test_angle_bounds_are_the_planners():
    from core.agent import investigate
    assert inv.ANGLE_CHARS == investigate.ANGLE_CHARS
    assert not inv.angle_ok("ab") and inv.angle_ok("abc") and inv.angle_ok("a" * 300) and not inv.angle_ok("a" * 301)
