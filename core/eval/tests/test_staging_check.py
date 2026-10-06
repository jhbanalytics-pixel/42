"""core/eval/staging_check.py against fakes: no warehouse, model, SocialCrawl or embed and enrich job is touched."""

import csv
import json
from datetime import date, datetime

import pytest

from core.agent.context import Refused, RunContext, result_hash
from core.agent.tools.dates import SAST
from core.eval import staging_check as sc

NOW = datetime(2026, 9, 29, 10, 30, tzinfo=SAST)
CAP_WINDOW_NOW = datetime(2026, 10, 1, 10, 30, tzinfo=SAST)
RUN_ID = "r_20260929_103000_abcd1234"


def post(pid, platform, day, text="a post"):
    return {"id": pid, "platform": platform, "handle": "someone", "url": f"https://example.com/{pid}",
            "posted_at": f"2026-09-{day:02d}T09:00:00+02:00", "market": "ZA", "text": text}


def answer_and_ctx(*, age_text=False, bad_hash=False, unknown=False):
    """Six cited posts, one outside the 22 to 28 September window, on tiktok and x."""
    ctx = RunContext(run_id=RUN_ID, tier="T1", as_of=NOW)
    qid, digest = ctx.record_query("SELECT 41 AS n", {}, [{"n": 41}], "count")
    evidence = [post("p1", "tiktok", 22), post("p2", "tiktok", 23), post("p3", "x", 24), post("p4", "x", 25),
                post("p5", "tiktok", 28), post("p6", "tiktok", 10)]
    ctx.evidence.update({record["id"]: record for record in evidence})
    number = {"value": 41, "unit": "posts", "query_id": qid, "run_id": RUN_ID,
              "result_hash": "sha256:" + "0" * 64 if bad_hash else digest}
    claims = [
        {"id": "c1", "text": "41 posts carried the phrase this week.", "label": "observed", "kind": "observation",
         "evidence_ids": ["p1", "p2", "p3"], "numbers": [number]},
        {"id": "c2", "text": "Gen Z viewers loved it." if age_text else "Viewers in Soweto shared it.",
         "label": "observed", "kind": "observation", "evidence_ids": ["p4", "p5", "p6"]},
    ]
    if unknown:
        claims[1]["evidence_ids"].append("p_missing")
    answer = {"status": "complete", "as_of": NOW.isoformat(), "short_answer": "One conversation led the week.",
              "claims": claims, "evidence": evidence, "so_what": [], "watch_next": [], "gaps": []}
    run = {"run_id": RUN_ID, "model_usd": 1.25, "window": {"from": "2026-09-22", "to": "2026-09-28"},
           "notices": [], "source_status": []}
    return answer, run, ctx


class Warehouse:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [{"n": 41}]
        self.calls = []

    def run(self, sql, params, max_bytes=None):
        self.calls.append(sql)
        return self.rows


# the bars from one answer

def test_bars_pass_on_a_good_answer():
    answer, run, ctx = answer_and_ctx()
    bars = sc.score_answer(answer, run, ctx, Warehouse())
    assert bars["schema_valid"] is True
    assert bars["cited"] == 6
    assert bars["cited_in_window"] == 5
    assert bars["platforms"] == ["tiktok", "x"]
    assert bars["unknown_ids"] == 0
    assert bars["age_claims"] == 0
    assert (bars["numbers_reproduced"], bars["numbers"]) == (1, 1)
    assert bars["model_usd"] == 1.25
    assert bars["pass"] is True


def test_bars_fail_on_age_unknown_id_and_unreproduced_number():
    answer, run, ctx = answer_and_ctx(age_text=True, bad_hash=True, unknown=True)
    bars = sc.score_answer(answer, run, ctx, Warehouse())
    assert bars["schema_valid"] is False  # p_missing does not resolve
    assert bars["unknown_ids"] == 1
    assert bars["age_claims"] == 1
    assert (bars["numbers_reproduced"], bars["numbers"]) == (0, 1)
    assert bars["pass"] is False


def test_an_age_claim_alone_fails_the_answer():
    answer, run, ctx = answer_and_ctx(age_text=True)
    bars = sc.score_answer(answer, run, ctx, Warehouse())
    assert bars["schema_valid"] and bars["unknown_ids"] == 0 and bars["numbers_reproduced"] == 1
    assert bars["age_claims"] == 1
    assert bars["pass"] is False


def test_an_age_term_in_the_short_answer_counts():
    answer, run, ctx = answer_and_ctx()
    answer["short_answer"] = "Gen Z led the week."
    assert sc.score_answer(answer, run, ctx, Warehouse())["age_claims"] == 1


def test_a_number_the_rerun_no_longer_returns_is_not_reproduced():
    answer, run, ctx = answer_and_ctx()
    bars = sc.score_answer(answer, run, ctx, Warehouse(rows=[{"n": 7}]))
    assert (bars["numbers_reproduced"], bars["numbers"]) == (0, 1)
    assert bars["pass"] is False


def test_too_few_posts_or_one_platform_fails():
    answer, run, ctx = answer_and_ctx()
    for record in answer["evidence"]:
        record["platform"] = "tiktok"
    bars = sc.score_answer(answer, run, ctx, Warehouse())
    assert bars["platforms"] == ["tiktok"] and bars["pass"] is False
    answer, run, ctx = answer_and_ctx()
    answer["claims"] = answer["claims"][:1]
    bars = sc.score_answer(answer, run, ctx, Warehouse())
    assert bars["cited_in_window"] == 3 and bars["pass"] is False


def test_a_post_with_no_platform_does_not_count_toward_the_two_platform_bar():
    answer, run, ctx = answer_and_ctx()
    for record in answer["evidence"]:
        if record["platform"] == "x":
            record["platform"] = None
    bars = sc.score_answer(answer, run, ctx, Warehouse())
    assert bars["platforms"] == ["tiktok"]
    assert bars["pass"] is False


def test_numbers_without_a_captured_run_are_not_reproduced():
    answer, run, _ = answer_and_ctx()
    bars = sc.score_answer(answer, run, None, Warehouse())
    assert (bars["numbers_reproduced"], bars["numbers"]) == (0, 1)


# the SocialCrawl stub

def test_the_socialcrawl_stub_refuses_every_call():
    client = sc.refusing_socialcrawl("live", run_id="r")
    with pytest.raises(Refused, match="Cloud Run jobs"):
        client.quote("tiktok/search", {})
    with pytest.raises(Refused, match="Cloud Run jobs"):
        client.call("tiktok/search", {}, lane="agent_live", run_id="r", max_credits=1)
    assert client.notices


# the whole run, wired to fakes

class Fakes:
    def __init__(self, spent=0.0, embedded=10, stored_rows=(), now=None):
        self.log = []
        self.spent = spent
        self.embedded = embedded
        self.stored_rows = list(stored_rows)
        self.booked = []
        self.created = []
        self.now = now

    def execute(self, sql, params, max_bytes=None):
        if "understand_spend" in sql:
            self.booked.append(params)
            return {"rows": []}
        if "CREATE TABLE FUNCTION IF NOT EXISTS" in sql:
            self.created.append(sql)
            return {"rows": []}
        if "tvf_search_posts" in sql:
            self.log.append(("search", params["q"]))
            return {"rows": [{"post_id": f"s{i}", "distance": 0.1 * i, "platform": "x",
                              "text": "load shedding again " * 10} for i in range(1, 7)]}
        if "ARRAY_LENGTH(embedding) > 0" in sql:
            return {"rows": [{"n": self.embedded}]}
        if "enrich_check" in sql or "e.tone IS NOT NULL" in sql and "QUALIFY" in sql:
            return {"rows": self.stored_rows}
        self.log.append(("sql", sql[:40]))
        return {"rows": []}

    def run_ask(self, request, emit, should_stop, *, deps):
        self.log.append(("ask", request["question"][:20]))
        client = deps.socialcrawl("live", run_id=RUN_ID)
        answer, run, ctx = answer_and_ctx()
        try:
            client.quote("tiktok/search", {"keyword": "x"})
        except Refused as refusal:
            answer["gaps"].append({"what": "No live data", "searched": "tiktok/search", "why": str(refusal)})
        deps.research(ctx, "prompt", None, None, should_stop)
        if hasattr(self, "writer_note_after_research"):
            ctx.writer_note = self.writer_note_after_research
        return {"answer": answer, "run": run}

    def research(self, ctx, prompt, setup, progress, should_stop):
        return {"tokens": {}, "usd": 0.0}

    def run_embed(self, execute, **kwargs):
        self.log.append(("embed", kwargs["days"]))
        return {"embedded": 3, "booked_usd": 0.01, "model_usd": 0.01}

    def run_enrich(self, execute, runner=None, *, run_date, run_id=None, limit=None, day=None, **kwargs):
        self.log.append(("enrich", run_date, limit))
        posts = [{"post_id": f"e{run_date:%d}{i}", "platform": "tiktok", "market": "ZA",
                  "content": "Sawubona Mzansi " * 30} for i in range(min(limit, 3))]
        runner.run(posts)
        return {"enriched": len(posts), "failed": 0, "model_usd": 0.001 * len(posts),
                "booked_usd": 0.001 * len(posts)}

    def wiring(self):
        return sc.Wiring(
            execute=self.execute, ask_deps=lambda: sc_deps(self), run_ask=self.run_ask, run_embed=self.run_embed,
            run_enrich=self.run_enrich, enrich_model=FakeModel(), spent_today=lambda: self.spent,
            now=lambda: NOW if self.now is None else self.now,
            warehouse=Warehouse())


def sc_deps(fakes):
    from core.agent.ask import Deps
    return Deps(warehouse=Warehouse(), socialcrawl=lambda mode, run_id: None, tables=None, model=None,
                research=fakes.research)


class FakeModel:
    def complete_json(self, system, user, schema, model, max_tokens):
        output = {"langs": ["zu", "xx"], "code_switched": False, "entities": ["Soweto"], "sounds": [],
                  "formats": ["talking_head"], "tone": "neutral", "stance": "neutral", "sponsored": False,
                  "hashtags": []}
        return output, {"input_tokens": 100, "output_tokens": 50}


def run_main(tmp_path, fakes, *args):
    progress = tmp_path / "L3.md"
    if not progress.exists():
        progress.write_text("# Progress, lane L3\n\nEarlier notes stay.\n", encoding="utf-8")
    code = sc.main(["--progress", str(progress), "--sheet-dir", str(tmp_path), *args], wiring=fakes.wiring())
    return code, progress


def test_ask_still_answers_with_socialcrawl_refused(tmp_path):
    fakes = Fakes(now=CAP_WINDOW_NOW)
    code, progress = run_main(tmp_path, fakes, "--only", "ask")
    assert code == 0
    text = progress.read_text(encoding="utf-8")
    assert [e[0] for e in fakes.log].count("ask") == 5
    assert "Cloud Run jobs" in text  # the gap the refusal left
    for qid in sc.pick_questions()[:5]:
        assert qid["id"] in text
    assert "<details>" in text and "https://example.com/p1" in text
    assert not list(tmp_path.rglob("*.json"))


def test_replay_writer_gets_no_source_gap_and_code_gate_recreates_it(tmp_path, monkeypatch):
    from core.eval import blind_test

    fakes = Fakes()
    actual_writer_note = "actual research note\n\nCreator preflight metadata"

    def research(ctx, *args, **kwargs):
        ctx.emit("socialcrawl", route="tiktok/search", status="partial", params={"query": "feed sentinel"},
                 backend_token="backend-token-sentinel", client_state={"session": "client-state-sentinel"})
        return {"note": "actual research note", "tokens": {"input": 2, "output": 3}, "usd": 0.0}

    fakes.research = research
    fakes.writer_note_after_research = actual_writer_note
    record_dir = tmp_path / "frozen"
    code, progress = run_main(tmp_path, fakes, "--only", "ask", "--questions", "NOW-01",
                              "--record-dir", str(record_dir))
    assert code == 0
    assert len(fakes.booked) == 1
    files = list(record_dir.glob("*.json"))
    assert [path.name for path in files] == ["NOW-01.json"]
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record["id"] == "NOW-01"
    assert record["note"] == actual_writer_note
    assert record["gaps"] == []
    assert record["gate_controls"]["source_events"] == [
        {"step": "socialcrawl", "route": "tiktok/search", "status": "partial"},
    ]
    assert "backend-token-sentinel" not in repr(record)
    assert "client-state-sentinel" not in repr(record)
    assert "feed sentinel" not in repr(record)
    assert "tiktok/search" not in blind_test._pack(record)
    assert "Live TikTok data is partial" not in blind_test._pack(record)
    assert record["queries"]["q_1"]["sql"] == "SELECT 41 AS n"
    assert len(record["evidence"]) == 6
    assert "actual research note" not in progress.read_text(encoding="utf-8")

    writer_inputs = {}

    def capture_writer(model, **kwargs):
        writer_inputs.update(kwargs)
        return {
            "status": "complete", "as_of": kwargs["as_of"].isoformat(), "short_answer": "",
            "claims": [], "evidence": [], "so_what": [], "watch_next": [], "gaps": [], "context": "",
        }, {}

    monkeypatch.setattr(blind_test, "write_answer", capture_writer)
    _, checked = blind_test.run_writer(object(), "candidate", record, lambda: 0, blind_test.Guard(1))
    assert writer_inputs["gaps"] == []
    assert any(gap["what"] == "Live TikTok data is partial" for gap in checked["gaps"])


def test_replay_export_falls_back_to_captured_research_note(tmp_path):
    fakes = Fakes()
    fakes.research = lambda *args, **kwargs: {"note": "captured legacy note", "tokens": {}, "usd": 0.0}
    record_dir = tmp_path / "frozen"
    code, _ = run_main(tmp_path, fakes, "--only", "ask", "--questions", "NOW-01",
                       "--record-dir", str(record_dir))
    assert code == 0
    record = json.loads((record_dir / "NOW-01.json").read_text(encoding="utf-8"))
    assert record["note"] == "captured legacy note"
    assert len(fakes.booked) == 1


def test_record_export_error_keeps_booked_cost_visible(tmp_path, monkeypatch):
    from core.eval import record_export

    sentinel = "export-sensitive-sentinel"
    private_path = r"C:\private\fixture\auth-token.json"

    def fail_write(*args, **kwargs):
        raise RuntimeError(f"{sentinel} at {private_path}")

    monkeypatch.setattr(record_export, "write_record", fail_write)
    fakes = Fakes()
    code, progress = run_main(tmp_path, fakes, "--only", "ask", "--questions", "NOW-01",
                              "--record-dir", str(tmp_path / "frozen"))
    assert code == 0
    assert len(fakes.booked) == 1
    text = progress.read_text(encoding="utf-8")
    assert "Record export failed for NOW-01: RuntimeError" in text
    assert sentinel not in text and private_path not in text


def test_the_first_five_friday_questions_are_picked():
    assert [q["id"] for q in sc.pick_questions()] == ["NOW-01", "RISE-02", "WHY-03", "SPR-03", "CRE-01"]


def test_the_ask_spend_is_booked(tmp_path):
    fakes = Fakes(now=CAP_WINDOW_NOW)
    run_main(tmp_path, fakes, "--only", "ask")
    assert len(fakes.booked) == 5


def test_spend_refusal_happens_before_any_call(tmp_path):
    from core.agent.ask import model_daily_usd

    fakes = Fakes(spent=model_daily_usd(now=NOW))
    code, progress = run_main(tmp_path, fakes)
    assert code == sc.REFUSED
    assert fakes.log == [] and fakes.booked == []
    assert "Staging check" not in progress.read_text(encoding="utf-8")


def test_the_estimate_counts_only_the_parts_that_run():
    ask_only = sc.estimate_usd(["ask"], enrich_n=20)
    everything = sc.estimate_usd(["ask", "search", "enrich"], enrich_n=20)
    assert ask_only > 0 and everything > ask_only
    assert sc.estimate_usd(["enrich"], enrich_n=200) > sc.estimate_usd(["enrich"], enrich_n=20)


def test_the_section_is_appended_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    fakes = Fakes()
    run_main(tmp_path, fakes, "--only", "search")
    run_main(tmp_path, fakes, "--only", "search")
    text = (tmp_path / "L3.md").read_text(encoding="utf-8")
    assert text.startswith("# Progress, lane L3\n\nEarlier notes stay.\n")
    assert text.count("## Staging check (gemini, 2026-09-29 10:30 SAST)") == 2


def test_search_runs_four_phrases_and_skips_embed_when_embeddings_exist(tmp_path):
    fakes = Fakes(embedded=10)
    run_main(tmp_path, fakes, "--only", "search")
    searches = [e for e in fakes.log if e[0] == "search"]
    assert len(searches) == 4
    assert not [e for e in fakes.log if e[0] == "embed"]
    text = (tmp_path / "L3.md").read_text(encoding="utf-8")
    assert "s5" in text and "s6" not in text  # the top 5 only
    assert "load shedding again load shedding again load shedding again load shedding again lo" not in text
    for phrase in sc.PHRASES:
        assert phrase["q"] in text


def test_search_embeds_the_last_seven_days_first_when_none_exist(tmp_path):
    fakes = Fakes(embedded=0)
    run_main(tmp_path, fakes, "--only", "search")
    assert fakes.log[0] == ("embed", 7)


def test_enrich_writes_the_sheet(tmp_path):
    fakes = Fakes()
    code, progress = run_main(tmp_path, fakes, "--only", "enrich", "--enrich-n", "5")
    assert code == 0
    sheet = tmp_path / "l3_enrich_sample_2026-09-29.csv"
    rows = list(csv.DictReader(sheet.read_text(encoding="utf-8-sig").splitlines()))
    assert len(rows) == 5
    first = rows[0]
    assert first["langs"] == "zu"  # the stored value: xx is not a known code
    assert first["entities"] == "Soweto"
    assert len(first["text"]) == 200
    assert first["entity_correct"] == "" and first["language_correct"] == ""
    assert [e for e in fakes.log if e[0] == "enrich"][0][1:] == (date(2026, 9, 29), 5)
    assert "hand" in progress.read_text(encoding="utf-8").lower()


def test_enrich_fills_from_stored_rows_when_too_few_new_posts(tmp_path):
    stored = [{"post_id": f"old{i}", "market": "KE", "platform": "x", "url": "u", "text": "Niaje wasee",
               "langs": ["sheng"], "code_switched": True, "entities": ["Nairobi"]} for i in range(40)]
    fakes = Fakes(stored_rows=stored)
    run_main(tmp_path, fakes, "--only", "enrich", "--enrich-n", "25")
    rows = list(csv.DictReader((tmp_path / "l3_enrich_sample_2026-09-29.csv").read_text(
        encoding="utf-8-sig").splitlines()))
    assert len(rows) == 25
    assert {r["source"] for r in rows} == {"this run", "stored"}


def test_a_second_sheet_the_same_day_does_not_overwrite_the_first(tmp_path):
    fakes = Fakes()
    run_main(tmp_path, fakes, "--only", "enrich", "--enrich-n", "2")
    first = (tmp_path / "l3_enrich_sample_2026-09-29.csv").read_text(encoding="utf-8-sig")
    run_main(tmp_path, fakes, "--only", "enrich", "--enrich-n", "2")
    assert (tmp_path / "l3_enrich_sample_2026-09-29.csv").read_text(encoding="utf-8-sig") == first
    assert len(list(tmp_path.glob("l3_enrich_sample_2026-09-29*.csv"))) == 2


def test_only_runs_one_part(tmp_path):
    fakes = Fakes()
    run_main(tmp_path, fakes, "--only", "enrich", "--enrich-n", "2")
    assert {e[0] for e in fakes.log} == {"enrich"}
    fakes = Fakes()
    run_main(tmp_path, fakes, "--only", "search")
    assert {e[0] for e in fakes.log} == {"search"}


def test_the_provider_comes_from_model_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    fakes = Fakes()
    run_main(tmp_path, fakes, "--only", "search")
    assert "## Staging check (gemini, 2026-09-29 10:30 SAST)" in (tmp_path / "L3.md").read_text(encoding="utf-8")


def test_a_failing_part_is_recorded_and_the_rest_still_run(tmp_path):
    fakes = Fakes(now=CAP_WINDOW_NOW)

    def broken(*args, **kwargs):
        raise RuntimeError("Vertex said 429")

    fakes.run_ask = broken
    code, progress = run_main(tmp_path, fakes, "--only", "ask")
    text = progress.read_text(encoding="utf-8")
    assert "RuntimeError: Vertex said 429" in text
    assert "FAIL" in text


def test_the_result_hash_of_a_search_is_recorded(tmp_path):
    fakes = Fakes()
    run_main(tmp_path, fakes, "--only", "search")
    rows = fakes.execute("tvf_search_posts", {"q": "x"})["rows"][:5]
    assert result_hash(rows) in (tmp_path / "L3.md").read_text(encoding="utf-8")


def test_help_works(capsys):
    with pytest.raises(SystemExit) as done:
        sc.main(["--help"])
    assert done.value.code == 0
    assert "--only" in capsys.readouterr().out


def test_the_header_says_the_estimate_excludes_run_embed(tmp_path):
    run_main(tmp_path, Fakes(), "--only", "search")
    text = (tmp_path / "L3.md").read_text(encoding="utf-8")
    assert "excluding run_embed, which sizes itself to the room left under the cap" in text


@pytest.mark.parametrize("index", ["deferred_below_5000", "failed"])
def test_search_goes_on_to_the_tvf_when_run_embed_defers_or_fails_the_index(tmp_path, index):
    fakes = Fakes(embedded=0)

    def run_embed(execute, **kwargs):
        fakes.log.append(("embed", kwargs["days"]))
        return {"embedded": 3, "embedded_total": 2_931, "index": index, "booked_usd": 0.01, "model_usd": 0.01}

    fakes.run_embed = run_embed
    code, progress = run_main(tmp_path, fakes, "--only", "search")
    assert code == 0
    assert len([e for e in fakes.log if e[0] == "search"]) == 4
    text = progress.read_text(encoding="utf-8")
    assert "### search: FAIL" not in text and index in text


def test_spend_booked_by_a_part_that_later_raises_still_counts_in_the_header(tmp_path):
    fakes = Fakes(embedded=0)

    def run_embed(execute, **kwargs):
        kwargs["book"](0.02, "embed_ceiling")
        raise RuntimeError("killed mid-window")

    fakes.run_embed = run_embed
    run_main(tmp_path, fakes, "--only", "search")
    text = (tmp_path / "L3.md").read_text(encoding="utf-8")
    assert "### search: FAIL" in text
    assert "booked by this run USD 0.0200" in text


def test_an_interrupted_ask_books_the_t1_hold_then_re_raises(tmp_path):
    # run_ask attaches exc.run only to an Exception, so an interrupt reaches the check with no run and no spend.
    from core.agent.ask import MODEL, hold_usd

    fakes = Fakes(now=CAP_WINDOW_NOW)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    fakes.run_ask = interrupted
    with pytest.raises(KeyboardInterrupt):
        run_main(tmp_path, fakes, "--only", "ask")
    assert [json.loads(p["counts"])["model_usd"] for p in fakes.booked] == [round(hold_usd("T1", MODEL), 6)]


def test_search_makes_sure_the_tvf_exists_before_searching_when_embeddings_exist(tmp_path):
    from core.understand import embed

    fakes = Fakes(embedded=10)
    run_main(tmp_path, fakes, "--only", "search")
    assert fakes.created == [embed.load("tvf_search_posts")]
    assert not [e for e in fakes.log if e[0] == "embed"]


class FailingModel:
    def complete_json(self, system, user, schema, model, max_tokens):
        raise RuntimeError("503 the model is unavailable")


def test_posts_an_outage_skipped_or_a_call_failed_are_not_labelled_as_failing_the_schema(tmp_path):
    fakes = Fakes()
    wiring = fakes.wiring()
    wiring.enrich_model = FailingModel()
    progress = tmp_path / "L3.md"
    progress.write_text("# Progress, lane L3\n", encoding="utf-8")
    sc.main(["--progress", str(progress), "--sheet-dir", str(tmp_path), "--only", "enrich", "--enrich-n", "5"],
            wiring=wiring)
    rows = list(csv.DictReader((tmp_path / "l3_enrich_sample_2026-09-29.csv").read_text(
        encoding="utf-8-sig").splitlines()))
    assert len(rows) == 5
    notes = [r["note"] for r in rows]
    assert not [n for n in notes if "schema" in n]
    assert notes[:3] == ["the model call failed; nothing was stored"] * 3
    assert notes[3:] == ["not sent: the model was unavailable; nothing was stored"] * 2


def test_an_output_that_fails_the_schema_is_still_labelled_so(tmp_path):
    class BadModel:
        def complete_json(self, system, user, schema, model, max_tokens):
            return {"langs": "not a list"}, {"input_tokens": 10, "output_tokens": 5}

    fakes = Fakes()
    wiring = fakes.wiring()
    wiring.enrich_model = BadModel()
    progress = tmp_path / "L3.md"
    progress.write_text("# Progress, lane L3\n", encoding="utf-8")
    sc.main(["--progress", str(progress), "--sheet-dir", str(tmp_path), "--only", "enrich", "--enrich-n", "2"],
            wiring=wiring)
    rows = list(csv.DictReader((tmp_path / "l3_enrich_sample_2026-09-29.csv").read_text(
        encoding="utf-8-sig").splitlines()))
    assert [r["note"] for r in rows] == ["the model's output failed the schema; nothing was stored"] * 2


def test_questions_runs_only_the_named_questions_and_the_section_says_the_rest_did_not_run(tmp_path):
    fakes = Fakes()
    code, progress = run_main(tmp_path, fakes, "--only", "ask", "--questions", "WHY-03,NOW-01")
    assert code == 0
    picked = {q["id"]: q["question"][:20] for q in sc.pick_questions()}
    assert [e[1] for e in fakes.log if e[0] == "ask"] == [picked["NOW-01"], picked["WHY-03"]]
    assert [json.loads(p["counts"])["what"] for p in fakes.booked] == ["staging_check_ask:NOW-01",
                                                                         "staging_check_ask:WHY-03"]
    text = progress.read_text(encoding="utf-8")
    assert "Ran NOW-01, WHY-03 of the five questions" in text
    assert "RISE-02, SPR-03, CRE-01 were not run in this invocation" in text
    assert "| RISE-02 |" not in text


def test_without_questions_all_five_run_and_nothing_is_marked_not_run(tmp_path):
    fakes = Fakes(now=CAP_WINDOW_NOW)
    code, progress = run_main(tmp_path, fakes, "--only", "ask")
    assert code == 0
    assert len(fakes.booked) == 5
    assert "not run in this invocation" not in progress.read_text(encoding="utf-8")


@pytest.mark.parametrize("ids", ["NOW-01,NOPE-9", "BRD-01", "NOW-01,,", ""])
def test_an_unknown_question_id_is_an_error_before_anything_runs(tmp_path, capsys, ids):
    fakes = Fakes()
    with pytest.raises(SystemExit) as done:
        run_main(tmp_path, fakes, "--only", "ask", "--questions", ids)
    assert done.value.code == 2
    assert fakes.log == [] and fakes.booked == []
    assert "Staging check" not in (tmp_path / "L3.md").read_text(encoding="utf-8")
    assert "--questions takes ids from NOW-01, RISE-02, WHY-03, SPR-03, CRE-01" in capsys.readouterr().err


def test_the_ask_estimate_is_the_hold_times_the_questions_chosen():
    from core.agent.ask import MODEL, hold_usd

    hold = hold_usd(sc.ASK_TIER, MODEL)
    assert sc.estimate_usd(["ask"], enrich_n=20) == round(5 * hold, 6)
    assert sc.estimate_usd(["ask"], enrich_n=20, questions=2) == round(2 * hold, 6)


def test_fewer_questions_fit_under_the_cap_when_five_do_not(tmp_path):
    from core.agent.ask import MODEL, hold_usd, model_daily_usd

    spent = model_daily_usd(now=NOW) - 3 * hold_usd(sc.ASK_TIER, MODEL)
    fakes = Fakes(spent=spent)
    code, _ = run_main(tmp_path, fakes, "--only", "ask")
    assert code == sc.REFUSED and fakes.log == []
    code, progress = run_main(tmp_path, fakes, "--only", "ask", "--questions", "NOW-01,RISE-02")
    assert code == 0
    assert len([e for e in fakes.log if e[0] == "ask"]) == 2


@pytest.mark.usefixtures("old_model_cap_schedule")
def test_staging_check_reads_the_runtime_cap_at_the_sast_cutover(tmp_path):
    before = datetime(2026, 10, 2, 23, 59, 59, tzinfo=SAST)
    after = datetime(2026, 10, 3, 0, 0, 0, tzinfo=SAST)

    fakes = Fakes(spent=20.0, now=before)
    code, _ = run_main(tmp_path, fakes, "--only", "ask", "--questions", "NOW-01")
    assert code == 0 and [entry[0] for entry in fakes.log].count("ask") == 1

    fakes = Fakes(spent=20.0, now=after)
    code, _ = run_main(tmp_path, fakes, "--only", "ask", "--questions", "NOW-01")
    assert code == sc.REFUSED and fakes.log == []


def test_help_shows_questions(capsys):
    with pytest.raises(SystemExit):
        sc.main(["--help"])
    assert "--questions" in capsys.readouterr().out


def test_a_subset_run_is_partial_never_a_1_11_pass(tmp_path):
    code, progress = run_main(tmp_path, Fakes(), "--only", "ask", "--questions", "NOW-01,RISE-02")
    assert code == 0
    text = progress.read_text(encoding="utf-8")
    assert "1.11: PASS" not in text
    assert "### Ask, BUILD.md 1.11: PARTIAL (2 of 5 run), not a 1.11 pass (the 2 that ran passed)" in text
    assert "2 of the five questions from core/eval/friday.yaml at T1" in text
    assert "Five questions from core/eval/friday.yaml" not in text


def test_a_failing_subset_run_is_partial_without_the_passed_note(tmp_path):
    fakes = Fakes()

    def failing(*args, **kwargs):
        raise RuntimeError("model down")

    fakes.run_ask = failing
    run_main(tmp_path, fakes, "--only", "ask", "--questions", "CRE-01")
    text = (tmp_path / "L3.md").read_text(encoding="utf-8")
    assert "1.11: PASS" not in text
    assert "### Ask, BUILD.md 1.11: PARTIAL (1 of 5 run), not a 1.11 pass\n" in text
    assert "1 of the five questions from core/eval/friday.yaml at T1" in text


def test_a_full_run_header_is_unchanged(tmp_path):
    run_main(tmp_path, Fakes(now=CAP_WINDOW_NOW), "--only", "ask")
    text = (tmp_path / "L3.md").read_text(encoding="utf-8")
    assert "### Ask, BUILD.md 1.11: PASS\n" in text
    assert "Five questions from core/eval/friday.yaml at T1" in text
    assert "PARTIAL" not in text
