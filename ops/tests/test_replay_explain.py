"""The explanation replay on the DuckDB harness with the fakes of test_brief_job: the plan and its token, the caps
before each model call, the read-only client, and what the output may name. No cloud client is created."""
import json
import re

import pytest
from google.cloud import bigquery

from core.brief import explain, job
from core.brief.tests.test_brief_job import Client, FakeCtx, FakeModel, add_item, world
from core.detect.tests import duck
from core.detect.tests.fixtures import D
from ops.runners import replay_explain as rx

POST_TEXT = "Dancing to the new sound"


@pytest.fixture(autouse=True)
def fixed_scope_and_cap(monkeypatch):
    monkeypatch.setattr(job, "read_market_scope", lambda client, row, d, market, *, core, agent: {
        "market_scope": "market", "market_posts7": 6, "total_posts7": 8, "market_share7": 0.75})
    monkeypatch.setattr(explain, "model_daily_usd", lambda: 20.0)
    monkeypatch.setattr(rx, "model_daily_usd", lambda: 20.0)


class LeakyCritic(FakeModel):
    """FakeModel whose critic quotes a post, names a handle and gives a link."""

    def complete_json(self, **kw):
        out, usage = super().complete_json(**kw)
        if "ruled_out" in kw["schema"]["properties"]:
            out = dict(out, reason=f'@za1_c1 wrote "{POST_TEXT}" at https://example.com/p/1, so it is local.')
        return out, usage


def run(con, argv, model=None, tmp_path=None):
    client = Client(con)
    model = model or FakeModel()
    argv = [D.isoformat(), *argv] + (["--out", str(tmp_path)] if tmp_path else [])
    code = rx.main(argv, client=client, model=model, build_ctx=FakeCtx(), core="core", agent="agent")
    return code, client, model


def plan_token(text):
    return re.search(r"plan token: ([0-9a-f]{16})", text).group(1)


def test_a_dry_run_prints_the_plan_its_cost_and_token_and_calls_no_model(capsys, tmp_path):
    code, client, model = run(world(n=3), ["--markets", "ZA,NG"], tmp_path=tmp_path)
    out = capsys.readouterr().out
    assert code == 0 and model.calls == []
    assert "confirm searches skipped" in out and "dry run: no model call made" in out
    plan = json.loads(next(tmp_path.glob("replay-*-plan-*.json")).read_text())["plan"]
    assert [(e["market"], e["item_id"]) for e in plan["judge"]] == [
        ("ZA", "za1"), ("NG", "ng1"), ("ZA", "za2"), ("NG", "ng2"), ("ZA", "za3"), ("NG", "ng3")]
    assert [e["rank"] for e in plan["judge"]] == [1, 1, 2, 2, 3, 3]
    assert all(e["posts"] == 3 and e["pattern"] in (0, 1, 2) and e["worst_usd"] > 0 for e in plan["judge"])
    assert plan["worst_usd"] == pytest.approx(sum(e["worst_usd"] for e in plan["judge"]))
    assert plan_token(out) == plan["token"] == rx.token(D, ("ZA", "NG"), 30, 5.0, [e["item_id"] for e in plan["judge"]],
                                                        plan["model"], plan["worst_usd"])
    assert rx.token(D, ("ZA", "NG"), 30, 5.0, [e["item_id"] for e in plan["judge"]], "other-model",
                    plan["worst_usd"]) != plan["token"]
    assert any("Also found by search" in line for line in plan["differences"]) and "differs from the live brief" in out
    assert not client.inserted


def test_a_token_that_does_not_match_the_plan_is_refused_before_any_model_call(capsys):
    code, _, model = run(world(n=2), ["--paid", "0" * 16])
    assert code == 1 and model.calls == []
    assert "does not match this plan's token" in capsys.readouterr().err


def test_the_approved_token_runs_the_writer_support_and_critic_and_reports_each_outcome(capsys, tmp_path):
    con = world(n=2, markets=("ZA",))
    run(con, ["--markets", "ZA"])
    token = plan_token(capsys.readouterr().out)
    code, client, model = run(con, ["--markets", "ZA", "--paid", token], tmp_path=tmp_path)
    assert code == 0
    assert len(model.writer_calls()) == 2 and sum(c["critic"] for c in model.calls) == 2
    report = json.loads(next(tmp_path.glob("replay-*-paid-*.json")).read_text())["run"]
    assert [(i["item_id"], i["outcome"]) for i in report["items"]] == [("za1", "published"), ("za2", "published")]
    assert report["items"][0]["support"] == {"claims": {"supported": 3}, "sentence": "supported", "before_repair": {}}
    assert report["items"][0]["critic"]["ruled_out"] is True
    assert report["summary"]["ZA"]["judged"] == 2 and report["summary"]["ZA"]["published"] == 2
    assert report["usd_reported_or_estimated"] == pytest.approx(0.01 * len(model.calls))
    assert not client.inserted


def test_a_critic_hold_is_reported_with_the_check_that_held_it(capsys, tmp_path):
    con = world(n=1, markets=("KE",))
    run(con, ["--markets", "KE"])
    token = plan_token(capsys.readouterr().out)
    run(con, ["--markets", "KE", "--paid", token], model=FakeModel(ruled_out=False), tmp_path=tmp_path)
    report = json.loads(next(tmp_path.glob("replay-*-paid-*.json")).read_text())["run"]
    [held] = report["items"]
    assert held["outcome"] == "held" and held["rule"] == "G10" and held["held_by"] == "Critic"
    assert held["critic"]["ruled_out"] is False
    assert report["summary"]["KE"]["held_by"] == {"Critic": 1}


def test_the_candidate_cap_stops_further_calls_and_more_than_30_is_refused(capsys):
    con = world(n=3, markets=("ZA",))
    run(con, ["--markets", "ZA", "--max-candidates", "2"])
    out = capsys.readouterr().out
    assert "1 Today-bound candidates are past --max-candidates" in out
    code, _, model = run(con, ["--markets", "ZA", "--max-candidates", "2", "--paid", plan_token(out)])
    assert code == 0 and len(model.writer_calls()) == 2
    assert "#za3" not in "\n".join(c["user"] for c in model.calls)
    code, _, model = run(con, ["--max-candidates", "31"])
    assert code == 2 and model.calls == []


def test_the_usd_cap_stops_before_the_call_that_would_pass_it(capsys, tmp_path):
    con = world(n=2, markets=("ZA",))
    run(con, ["--markets", "ZA", "--max-usd", "3"])
    token = plan_token(capsys.readouterr().out)
    code, _, model = run(con, ["--markets", "ZA", "--max-usd", "3", "--paid", token], model=FakeModel(usd=1.0),
                         tmp_path=tmp_path)
    assert code == 0
    # Writer and two claim checks at USD 1 each reach 3; the next check would pass it, and za2 never starts.
    assert len(model.calls) == 3
    assert "#za2" not in "\n".join(c["user"] for c in model.calls)
    report = json.loads(next(tmp_path.glob("replay-*-paid-*.json")).read_text())["run"]
    assert report["stop"] == "USD cap"
    assert [i["outcome"] for i in report["items"]] == ["stopped: model_cap", "not judged"]
    assert report["usd_reported_or_estimated"] == pytest.approx(3.0)


def test_the_meter_refuses_a_call_its_estimate_would_take_past_the_cap():
    model = FakeModel()
    meter = rx.Meter(model, max_usd=0.001)
    with pytest.raises(rx.Refused):
        meter.complete_json(system=explain.WRITER_SYSTEM, user="x", schema=explain.WRITER_SCHEMA,
                            model="gemini-3.8-flash", max_tokens=explain.WRITER_MAX_TOKENS)
    assert model.calls == [] and meter.refused


def test_the_client_refuses_a_non_select_an_insert_and_a_load_before_sending_them():
    inner = Client(world(n=1))
    reader = rx.ReadOnlyClient(inner)
    for sql in ("INSERT INTO agent.briefs (market) VALUES ('ZA')", "DELETE FROM agent.briefs WHERE TRUE",
                "SELECT 1; DROP TABLE agent.briefs", "-- name: x\nMERGE agent.briefs t USING s ON FALSE"):
        with pytest.raises(rx.Refused):
            reader.query(sql)
    for call in ("insert_rows_json", "insert_rows", "load_table_from_json", "load_table_from_file"):
        with pytest.raises(rx.Refused):
            getattr(reader, call)("agent.briefs", [{"market": "ZA"}])
    assert inner.sql == [] and inner.inserted == {}


def test_every_select_is_sent_with_the_byte_cap():
    seen = []

    class Spy(Client):
        def query(self, sql, job_config=None):
            seen.append(job_config.maximum_bytes_billed)
            return super().query(sql, job_config)

    reader = rx.ReadOnlyClient(Spy(world(n=1)))
    reader.query("-- a comment\nSELECT 1 x").result()
    reader.query("SELECT 1 x", job_config=bigquery.QueryJobConfig(maximum_bytes_billed=10**12)).result()
    assert seen == [rx.CAP, rx.CAP]


def test_the_output_names_no_handle_post_text_or_link(capsys, tmp_path):
    con = world(n=1, markets=("ZA",))
    run(con, ["--markets", "ZA"])
    token = plan_token(capsys.readouterr().out)
    run(con, ["--markets", "ZA", "--paid", token], model=LeakyCritic(), tmp_path=tmp_path)
    printed = capsys.readouterr().out
    written = next(tmp_path.glob("replay-*-paid-*.json")).read_text()
    for text in (printed, written):
        assert "za1_c" not in text and POST_TEXT not in text and "example.com" not in text
    assert "[quote]" in printed and "@[handle]" in printed


def test_a_folded_candidate_is_listed_with_its_card_and_never_judged(capsys, tmp_path):
    con = world(n=2, markets=("ZA",))
    add_item(con, "ZA", "za_dup", 0.93, posts=0)
    duck.load(con, "core.post_items", [{"post_id": p, "item_id": "za_dup", "via": "hashtag"}
                                       for p in ("za1_p1", "za1_p2", "za1_p3")])
    run(con, ["--markets", "ZA"], tmp_path=tmp_path)
    out = capsys.readouterr().out
    plan = json.loads(next(tmp_path.glob("replay-*-plan-*.json")).read_text())["plan"]
    assert [e["item_id"] for e in plan["judge"]] == ["za1", "za2"]
    assert plan["judge"][0]["folded_in"] == ["za_dup"]
    assert plan["folded"] == [{"market": "ZA", "item_id": "za_dup", "into": "za1", "title": "#za_dup",
                               "pattern": plan["folded"][0]["pattern"]}]
    assert "folded, not judged: #za_dup" in out
    _, _, model = run(con, ["--markets", "ZA", "--paid", plan_token(out)])
    assert "#za_dup" not in "\n".join(c["user"] for c in model.calls)
    assert len(model.writer_calls()) == 2


class Timeout(FakeModel):
    """FakeModel whose writer call times out with no usage, as a dropped request does."""

    def complete_json(self, **kw):
        if "ruled_out" not in kw["schema"]["properties"] and "verdict" not in kw["schema"]["properties"]:
            self.calls.append({"critic": False, "user": kw["user"], "timeout": True})
            raise TimeoutError("request timed out")
        return super().complete_json(**kw)


def test_a_timed_out_call_is_booked_at_its_estimate():
    estimate = explain._estimate_usd(explain.WRITER_SYSTEM, "x", explain.WRITER_MAX_TOKENS, "gemini-3.8-flash")
    meter = rx.Meter(Timeout(), max_usd=1.0)
    with pytest.raises(TimeoutError):
        meter.complete_json(system=explain.WRITER_SYSTEM, user="x", schema=explain.WRITER_SCHEMA,
                            model="gemini-3.8-flash", max_tokens=explain.WRITER_MAX_TOKENS)
    assert meter.usd == pytest.approx(estimate) and meter.calls[0]["usd"] == pytest.approx(estimate)



def test_a_call_the_meter_refuses_is_reported_as_the_usd_cap(capsys, tmp_path, monkeypatch):
    # With the daily room set wide, the explain step's own cap never fires and the meter alone stops the run.
    monkeypatch.setattr(rx, "model_daily_usd", lambda: 0.0)
    con = world(n=2, markets=("ZA",))
    run(con, ["--markets", "ZA", "--max-usd", "3"])
    token = plan_token(capsys.readouterr().out)
    code, _, model = run(con, ["--markets", "ZA", "--max-usd", "3", "--paid", token], model=FakeModel(usd=1.0),
                         tmp_path=tmp_path)
    assert code == 0 and len(model.calls) == 3
    report = json.loads(next(tmp_path.glob("replay-*-paid-*.json")).read_text())["run"]
    assert report["stop"] == "USD cap"
    assert [i["outcome"] for i in report["items"]] == ["stopped: USD cap", "not judged"]
    assert report["usd_reported_or_estimated"] == pytest.approx(3.0)


def test_patterns_are_kept_per_market_when_two_markets_share_an_item_id():
    class Row(dict):
        pass

    class Job:
        def __init__(self, rows):
            self.rows = rows

        def result(self):
            return self.rows

    class Inner:
        def __init__(self):
            self.n = 0

        def query(self, sql, job_config=None):
            self.n += 1
            return Job([Row(item_id="same", _selection_pattern=self.n - 1)])

    reader = rx.ReadOnlyClient(Inner())
    for market in ("ZA", "NG"):
        config = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("market", "STRING", market)])
        reader.query("SELECT 1", job_config=config).result()
    assert reader.patterns == {("ZA", "same"): 0, ("NG", "same"): 1}


def test_the_replay_model_sends_one_request_per_call(monkeypatch):
    import core.llm.provider as provider
    monkeypatch.setattr(provider, "provider", lambda: None)
    model = rx.replay_model()
    assert model.retries == 0 and model.timeout_s == job.GEMINI_TIMEOUT_S


def test_once_a_call_is_refused_no_smaller_call_is_sent():
    model = FakeModel()
    meter = rx.Meter(model, max_usd=0.001)
    with pytest.raises(rx.Refused):
        meter.complete_json(system=explain.WRITER_SYSTEM, user="x", schema=explain.WRITER_SCHEMA,
                            model="gemini-3.8-flash", max_tokens=explain.WRITER_MAX_TOKENS)
    meter.max_usd = 100.0
    with pytest.raises(rx.Refused):
        meter.complete_json(system=explain.SUPPORT_SYSTEM, user="x", schema=explain.SUPPORT_SCHEMA,
                            model="gemini-3.8-flash", max_tokens=explain.SUPPORT_MAX_TOKENS)
    assert model.calls == [] and not meter.room()


def test_an_unpriced_plan_is_refused_before_any_model_call(capsys, monkeypatch):
    con = world(n=1, markets=("ZA",))
    monkeypatch.setattr(rx, "default_model", lambda: (_ for _ in ()).throw(RuntimeError("no price")))
    run(con, ["--markets", "ZA"])
    token = plan_token(capsys.readouterr().out)
    code, _, model = run(con, ["--markets", "ZA", "--paid", token])
    assert code == 1 and model.calls == [] and "not priced" in capsys.readouterr().err
