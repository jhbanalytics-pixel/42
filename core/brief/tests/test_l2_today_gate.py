import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[3] / "ops" / "l2_today_gate.py"
spec = importlib.util.spec_from_file_location("gate_after_brief_20261001", path)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class Param:
    def __init__(self, name, kind, value):
        self.name, self.kind, self.value = name, kind, value


class Config:
    def __init__(self, **values):
        self.__dict__.update(values)


class BQ:
    ScalarQueryParameter = Param
    QueryJobConfig = Config


class Job:
    statement_type = "SELECT"

    def __init__(self, name, rows=(), processed=1024, billed=1024):
        self.job_id, self.rows = name, list(rows)
        self.total_bytes_processed, self.total_bytes_billed = processed, billed

    def result(self, timeout=None):
        return iter(self.rows)


class Client:
    def __init__(self, rows=None, completion=None, scopes=None, estimates=None, bills=None):
        self.rows, self.scopes = rows, scopes or []
        self.completion = completion or {
            "run_id": "brief-20261001-a", "run_date": gate.DAY, "stage": "brief",
            "status": "ok", "started_at": dt.datetime(2026, 10, 1, 2, 5, tzinfo=gate.SAST),
            "finished_at": dt.datetime(2026, 10, 1, 2, 22, tzinfo=gate.SAST),
        }
        self.estimates, self.bills, self.calls = estimates or {}, bills or {}, []

    def query(self, sql, job_config):
        self.calls.append((sql, job_config))
        kind = "brief_terminal" if ".runs" in sql else "brief_rows" if ".v_briefs_current" in sql else "cards_more_scope"
        if job_config.dry_run:
            return Job("dry", processed=self.estimates.get(kind, 1024), billed=0)
        rows = [self.completion] if kind == "brief_terminal" and self.completion else (
            self.rows if kind == "brief_rows" else self.scopes)
        return Job("read", rows, self.estimates.get(kind, 1024), self.bills.get(kind, 1024))


def evidence(pid, flags=(), source=None):
    text = "Dancing to the new sound at home"
    return {"id": pid, "platform": "X", "handle": "@test",
            "url": f"https://example.test/{pid}", "posted_at": "2026-10-01T02:00:00+02:00",
            "market": "ZA", "flags": list(flags), "source_market": source,
            "text": text, "quote_text": text}


def card():
    explanation = "Recent posts show friends dancing to a new sound at home."
    posts = [evidence(f"p{i}") for i in range(1, 4)]
    quote = {"evidence_id": "p1", "text": "Dancing to the new sound"}
    claims = [{"id": "c1", "evidence_ids": ["p1", "p2"], "quotes": [quote]},
              {"id": "c2", "evidence_ids": ["p2", "p3"], "quotes": []}]
    basis = gate.specificity_basis(explanation=explanation, claims=claims,
        explanation_claim_ids=["c1", "c2"], evidence=posts, market="ZA")
    return {"item_id": "za-local", "title": "#local", "explained": True,
        "explanation_status": "explained", "explanation": explanation, "claims": claims,
        "explanation_claim_ids": ["c1", "c2"], "evidence": posts,
        "specificity": {"status": "pass", **basis}}


def brief_rows(cards, run_id="brief-20261001-a", held=None):
    rows = []
    for market in ("ZA", "NG", "KE"):
        payload = {"cards": cards if market == "ZA" else [], "more": [],
            "held_back": {"items": (held or []) if market == "ZA" else []}}
        rows.append({"brief_date": gate.DAY, "market": market, "run_id": run_id,
            "published_at": dt.datetime(2026, 10, 1, 2, 23, tzinfo=gate.SAST),
            "status": "published", "payload": json.dumps(payload)})
    return rows


def brief_rows_by_market(payloads, run_id="brief-20261001-a"):
    rows = []
    for market in ("ZA", "NG", "KE"):
        payload = payloads.get(market, {"cards": [], "more": [], "held_back": {"items": []}})
        rows.append({"brief_date": gate.DAY, "market": market, "run_id": run_id,
            "published_at": dt.datetime(2026, 10, 1, 2, 23, tzinfo=gate.SAST),
            "status": payload.get("status", "published"), "payload": json.dumps(payload)})
    return rows


def held_item(item_id, market, *, rule="G10", reason="explanation_failed", source_market=None):
    post = {"id": f"{item_id}-post", "market": None if source_market else market,
        "source_market": source_market, "flags": [], "text": "Dancing to the new sound",
        "quote_text": "Dancing to the new sound"}
    return {"item_id": item_id, "title": f"#{item_id}", "rule": rule, "reason": reason,
        "reason_text": "Explanation failed its checks", "evidence_ids": [post["id"]], "evidence": [post]}


def scope(index, total, local):
    return {"card_index": index, "total_posts7": total, "market_posts7": local}


def run(client, tmp_path, now=None):
    return gate.run_gate(now=now or dt.datetime(2026, 10, 1, 3, tzinfo=gate.SAST),
        report_path=tmp_path / "report.md", marker_path=tmp_path / "marker.json",
        client_factory=lambda: (client, BQ))


def report_data(path):
    text = path.read_text(encoding="utf-8")
    return json.loads(text[text.rfind("\n{") + 1:].strip())

def run_receipt(marker_path, run_id):
    return gate._run_paths(marker_path, run_id)[0]


def test_reader_requires_output_paths_before_client_creation():
    called = []

    result = gate.run_gate(client_factory=lambda: called.append(True))

    assert result == {"status": "blocked", "reason": "output_paths_required", "query_job_ids": []}
    assert not called


def test_receipt_base_follows_the_operator_checkout():
    expected = Path(gate.__file__).resolve().parents[1] / "test-results" / "l2" / (
        "gate_after_brief_20261001.marker.json")

    assert gate.PRIMARY_RECEIPT_BASE == expected


def test_date_paths_are_exact_and_october_first_keeps_its_receipt_base():
    oct1_report, oct1_receipt = gate._paths(dt.date(2026, 10, 1))
    oct2_report, oct2_receipt = gate._paths(dt.date(2026, 10, 2))

    assert oct1_report == Path(r"C:\Users\AlbertMeintjes\dev\42-inputs\BRIEF-ACCEPT-2026-10-01.md")
    assert oct1_receipt == gate.PRIMARY_RECEIPT_BASE
    assert oct2_report == Path(r"C:\Users\AlbertMeintjes\dev\42-inputs\MORNING-L2-2026-10-02.md")
    assert oct2_receipt.name == "morning_l2_20261002.marker.json"
    assert gate.PRIMARY_REPORT_PATH.name == "L2-GATE-2026-10-01.md"
    assert gate._validate_cli_paths(oct2_report, oct2_receipt, dt.date(2026, 10, 2)) == (
        oct2_report.resolve(), oct2_receipt.resolve())
    with pytest.raises(gate.Stop, match="output_paths_outside_allowlist"):
        gate._validate_cli_paths(oct1_report, oct1_receipt, dt.date(2026, 10, 2))


def test_public_pass_works_without_private_field_and_ignores_legacy_false_field():
    item = card()
    assert "local_why_now_checked" not in item and gate.public_reasons(item, "ZA") == []
    item["local_why_now_checked"] = False
    assert gate.public_reasons(item, "ZA") == []


@pytest.mark.parametrize("value", [None, "pass", {}, {"status": "pass", "reason": None}])
def test_legacy_or_malformed_specificity_is_held(value):
    item = card()
    item["specificity"] = value
    assert gate.public_reasons(item, "ZA")


def test_public_false_verdict_is_held():
    item = card()
    item["specificity"].update(status="fail", reason="local_why_now_not_checked")
    assert "specificity_status_not_pass" in gate.public_reasons(item, "ZA")


def test_wrong_public_quote_is_held_even_when_claim_quote_is_valid():
    item = card()
    item["specificity"]["quote"]["text"] = "Not in the source"
    assert "specificity_contract_mismatch" in gate.public_reasons(item, "ZA")


def test_assumed_only_records_fail_but_source_market_records_pass():
    item = card()
    item["evidence"] = [evidence(f"p{i}", ["market_assumed"]) for i in range(1, 4)]
    assert "specificity_basis_insufficient_local_evidence" in gate.public_reasons(item, "ZA")
    item = card()
    item["evidence"] = [evidence(f"p{i}", ["market_assumed"], "ZA") for i in range(1, 4)]
    basis = gate.specificity_basis(explanation=item["explanation"], claims=item["claims"],
        explanation_claim_ids=item["explanation_claim_ids"], evidence=item["evidence"], market="ZA")
    item["specificity"] = {"status": "pass", **basis}
    assert gate.public_reasons(item, "ZA") == []


def test_not_ready_does_not_consume_marker_and_ready_run_completes_once(tmp_path):
    running = {"run_id": "brief-20261001-a", "run_date": gate.DAY, "stage": "brief",
        "status": "running", "started_at": dt.datetime(2026, 10, 1, 2, 5), "finished_at": None}
    marker, report = tmp_path / "marker.json", tmp_path / "report.md"
    first = gate.run_gate(now=dt.datetime(2026, 10, 1, 3, tzinfo=gate.SAST),
        report_path=report, marker_path=marker, client_factory=lambda: (Client(completion=running), BQ))
    receipt = run_receipt(marker, running["run_id"])
    assert first["status"] == "not_ready" and first["query_job_ids"]
    assert not receipt.exists() and not report.exists()
    second_client = Client(brief_rows([]))
    second = gate.run_gate(now=dt.datetime(2026, 10, 1, 3, 5, tzinfo=gate.SAST),
        report_path=report, marker_path=marker, client_factory=lambda: (second_client, BQ))
    assert second["status"] == "completed" and receipt.exists() and report.exists()
    third_client = Client()
    third = gate.run_gate(now=dt.datetime(2026, 10, 1, 3, 10, tzinfo=gate.SAST),
        report_path=report, marker_path=marker, client_factory=lambda: (third_client, BQ))
    assert third["status"] == "already_used"
    assert third["source_report_path"] == str(report)
    assert third["report_path"] == str(report)
    assert all(".v_briefs_current" not in sql for sql, _ in third_client.calls)
    assert all(".runs" in sql for sql, _ in third_client.calls)


def test_cli_routes_only_to_exact_primary_report_and_receipt_base(monkeypatch, capsys, tmp_path):
    calls = []
    monkeypatch.setattr(gate, "run_gate", lambda **kwargs: (
        calls.append(kwargs) or {"status": "already_used", "marker_status": "completed",
            "receipt_path": "per-run-receipt"}))
    day = dt.date(2026, 10, 1)
    report, receipt_base = gate._paths(day)
    at = dt.datetime(2026, 10, 1, 18, tzinfo=gate.SAST)

    assert gate.main(["--date", day.isoformat(), "--report", str(report),
        "--receipts", str(receipt_base)], now=at) == 0
    assert calls[0]["day"] == day
    assert calls[0]["now"] == at
    assert calls[0]["report_path"] == report.resolve()
    assert calls[0]["marker_path"] == receipt_base.resolve()
    assert json.loads(capsys.readouterr().out)["status"] == "already_used"
    assert gate._run_paths(receipt_base, "brief-a")[0] != gate._run_paths(receipt_base, "brief-b")[0]
    with pytest.raises(SystemExit):
        gate.main(["--date", "2026-10-02", "--report", str(report),
            "--receipts", str(receipt_base)], now=dt.datetime(2026, 10, 2, 6, 30, tzinfo=gate.SAST))
    assert len(calls) == 1


def test_oct2_gate_records_date_and_reuses_same_run_receipt_without_card_reads(tmp_path):
    day = dt.date(2026, 10, 2)
    run_id = "brief-20261002-a"
    completion = {**Client().completion, "run_id": run_id, "run_date": day,
        "started_at": dt.datetime(2026, 10, 2, 2, 5, tzinfo=gate.SAST),
        "finished_at": dt.datetime(2026, 10, 2, 2, 22, tzinfo=gate.SAST)}
    rows = brief_rows([], run_id=run_id)
    for row in rows:
        row["brief_date"] = day
        row["published_at"] = dt.datetime(2026, 10, 2, 2, 23, tzinfo=gate.SAST)
    report, marker = tmp_path / "MORNING-L2-2026-10-02.md", tmp_path / "morning_l2_20261002.marker.json"
    at = dt.datetime(2026, 10, 2, 6, 30, tzinfo=gate.SAST)
    client = Client(rows, completion=completion)

    result = gate.run_gate(now=at, day=day, report_path=report, marker_path=marker,
        client_factory=lambda: (client, BQ))

    assert result["status"] == "completed"
    assert json.loads(run_receipt(marker, run_id).read_text(encoding="utf-8"))["date"] == day.isoformat()
    assert "### Recovered read only Today gate, 2 October 2026" in report.read_text(encoding="utf-8")
    date_params = [param.value for _, config in client.calls for param in config.query_parameters
        if param.name == "d"]
    assert date_params and set(date_params) == {day}
    before = report.read_bytes()
    repeat_client = Client(completion=completion)
    repeated = gate.run_gate(now=dt.datetime(2026, 10, 2, 6, 31, tzinfo=gate.SAST), day=day,
        report_path=report, marker_path=marker, client_factory=lambda: (repeat_client, BQ))
    assert repeated["status"] == "already_used"
    assert all(".v_briefs_current" not in sql for sql, _ in repeat_client.calls)
    assert report.read_bytes() == before


def test_oct2_cutoff_is_exclusive():
    assert gate._guard(dt.datetime(2026, 10, 2, 6, 59, 59, tzinfo=gate.SAST)).date() == dt.date(2026, 10, 2)
    with pytest.raises(gate.Stop, match="outside_acceptance_deadline"):
        gate._guard(dt.datetime(2026, 10, 2, 7, 0, tzinfo=gate.SAST))


def test_completed_run_receipts_are_scoped_and_preserve_legacy_marker(tmp_path):
    report, marker = tmp_path / "report.md", tmp_path / "marker.json"
    legacy = b'{"status":"completed","run_id":"legacy"}'
    marker.write_bytes(legacy)
    first_id, second_id = "brief-20261001-a", "brief-20261001-b"
    first_client = Client(brief_rows([], run_id=first_id),
        completion={**Client().completion, "run_id": first_id})
    first = gate.run_gate(now=dt.datetime(2026, 10, 1, 3, tzinfo=gate.SAST), report_path=report,
        marker_path=marker, client_factory=lambda: (first_client, BQ))
    first_report = report.read_bytes()
    second_client = Client(brief_rows([], run_id=second_id),
        completion={**Client().completion, "run_id": second_id})
    second = gate.run_gate(now=dt.datetime(2026, 10, 1, 4, tzinfo=gate.SAST), report_path=report,
        marker_path=marker, client_factory=lambda: (second_client, BQ))

    assert first["status"] == second["status"] == "completed"
    assert run_receipt(marker, first_id).exists() and run_receipt(marker, second_id).exists()
    assert run_receipt(marker, first_id) != run_receipt(marker, second_id)
    assert report.read_bytes().startswith(first_report)
    assert marker.read_bytes() == legacy
    assert report.read_text(encoding="utf-8").count("### Recovered read only Today gate") == 2


def test_empty_cards_preserves_already_held_outside_denominator(tmp_path):
    client = Client(brief_rows([], held=[{"item_id": "held", "reason": "too_few_creators"}]),
        scopes=[scope(0, 0, 0)])
    result = run(client, tmp_path)
    text = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert result["status"] == "completed"
    assert '"cards": 0' in text and "too_few_creators" in text
    assert len(client.calls) == 6


def test_current_rows_must_match_completed_run(tmp_path):
    result = run(Client(brief_rows([], run_id="brief-20260930-old")), tmp_path)
    assert result["reason"] == "current_briefs_not_same_completed_run"


@pytest.mark.parametrize("rows", [
    brief_rows([])[:2],
    lambda rows: [rows[0], rows[1], rows[1]],
])
def test_partial_or_duplicate_market_rows_are_not_ready_without_marker(tmp_path, rows):
    rows = rows(brief_rows([])) if callable(rows) else rows
    result = run(Client(rows=rows), tmp_path)
    assert result == {
        "status": "not_ready",
        "reason": "current_brief_markets_or_run_incomplete",
        "query_job_ids": ["read", "read"],
    }
    assert not run_receipt(tmp_path / "marker.json", "brief-20261001-a").exists()
    assert not (tmp_path / "report.md").exists()
    assert len(result["query_job_ids"]) == 2


@pytest.mark.parametrize("now", [
    dt.datetime(2026, 9, 30, 23, 59, 59, tzinfo=gate.SAST),
    dt.datetime(2026, 10, 2, 0, 0, tzinfo=gate.SAST),
])
def test_time_guard_precedes_marker_and_client(tmp_path, now):
    called = []
    def factory():
        called.append(True)
        raise AssertionError("client created outside gate")
    with pytest.raises(gate.Stop):
        gate.run_gate(now=now, report_path=tmp_path / "r.md",
            marker_path=tmp_path / "m.json", client_factory=factory)
    assert not called and not (tmp_path / "m.json").exists()


def test_gate_accepts_any_time_on_the_october_first_sast_date(tmp_path):
    client = Client(brief_rows([]))
    result = gate.run_gate(now=dt.datetime(2026, 10, 1, 23, 59, 59, tzinfo=gate.SAST),
        report_path=tmp_path / "report.md", marker_path=tmp_path / "marker.json",
        client_factory=lambda: (client, BQ))

    assert result["status"] == "completed"


def test_selects_are_dry_run_first_and_use_batched_market_scope(tmp_path):
    client = Client(brief_rows([card()]), scopes=[{"card_index": 0, "total_posts7": 4, "market_posts7": 3}])
    result = run(client, tmp_path)
    marker = json.loads(run_receipt(tmp_path / "marker.json", "brief-20261001-a").read_text(encoding="utf-8"))
    assert result["status"] == "completed"
    assert [cfg.dry_run for _, cfg in client.calls] == [True, False] * 3
    assert all(not cfg.use_query_cache and cfg.maximum_bytes_billed <= gate.TOTAL_CAP for _, cfg in client.calls)
    assert "INTERVAL 6 DAY" in client.calls[-1][0] and marker["billed"] == 3 * 1024


def test_aggregate_byte_guard_stops_scope_before_execution(tmp_path):
    client = Client(brief_rows([card()]), estimates={"cards_more_scope": 5 * 1024 * 1024},
        bills={"brief_terminal": 60 * 1024 * 1024, "brief_rows": 64 * 1024 * 1024})
    result = run(client, tmp_path)
    assert result["reason"] == "aggregate_read_budget_would_be_exceeded"
    assert [cfg.dry_run for _, cfg in client.calls] == [True, False, True, False, True]


@pytest.mark.parametrize("estimates,bills,reason,dry_runs", [
    ({"cards_more_scope": 64 * 1024 * 1024 + 1}, {}, "aggregate_read_budget_would_be_exceeded",
        [True, False, True, False, True]),
    ({}, {"cards_more_scope": 64 * 1024 * 1024 + 1}, "aggregate_read_budget_exceeded",
        [True, False, True, False, True, False]),
])
def test_scope_read_obeys_64mib_query_cap_while_aggregate_budget_remains(tmp_path, estimates, bills, reason, dry_runs):
    client = Client(brief_rows([card()]), estimates=estimates, bills=bills)
    result = run(client, tmp_path)

    assert result["reason"] == reason
    assert client.calls[-1][1].maximum_bytes_billed == gate.QUERY_CAP
    assert [cfg.dry_run for _, cfg in client.calls] == dry_runs


def test_global_discover_is_separate_from_g10_and_existing_holds(tmp_path):
    item = card()
    item["market_scope"] = "global"
    client = Client(brief_rows([item], held=[{"item_id": "held", "reason": "too_few_creators"}]),
        scopes=[scope(0, 4, 2), scope(1, 0, 0)])
    result = run(client, tmp_path)
    text = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert result["status"] == "completed"
    assert "Global Discover" in text and "G10 hold" not in text
    assert "too_few_creators" in text


def test_held_g10_uses_batched_scope_and_keeps_missing_verdicts_unavailable(tmp_path):
    held = held_item("held-g10", "ZA", source_market="ZA")
    client = Client(brief_rows([card()], held=[held]), scopes=[scope(0, 4, 3), scope(1, 4, 3)])
    result = run(client, tmp_path)
    data = report_data(tmp_path / "report.md")
    entry = data["held_results"][0]
    parameters = {p.name: p.value for p in client.calls[-1][1].query_parameters}

    assert result["status"] == "completed"
    assert len(client.calls) == 6
    assert parameters["item_id_0"] == "za-local" and parameters["item_id_1"] == "held-g10"
    assert entry["stored_hold"] == {"rule": "G10", "reason": "explanation_failed",
        "reason_text": "Explanation failed its checks"}
    assert entry["current_scope"] == "market" and entry["local"] == 3 and entry["total"] == 4
    assert entry["market_attributed_posts"] == [{"evidence_id": "held-g10-post", "basis": "source_market_feed"}]
    assert entry["evidence_gaps"] == {
        "claim_support_verdict": "unavailable_claims_not_persisted",
        "quote_verdict": "unavailable_claims_not_persisted",
        "why_now_verdict": "unavailable_explanation_not_persisted",
        "specificity_verdict": "unavailable_claims_explanation_and_specificity_not_persisted",
    }
    assert entry["basis_unit"] == "posts"


def test_per_market_public_and_stored_hold_counts_reconcile(tmp_path):
    za = card()
    za["item_id"] = "za-card"
    za_more = card()
    za_more["item_id"] = "za-more"
    ng = card()
    ng["item_id"] = "ng-card"
    ke = card()
    ke["item_id"] = "ke-card"
    payloads = {
        "ZA": {"cards": [za], "more": [za_more], "held_back": {"items": [held_item("za-held", "ZA", reason="too_few_creators")]}},
        "NG": {"cards": [ng], "more": [], "held_back": {"items": [held_item("ng-held", "NG", rule="G5", reason="paid_led")]}},
        "KE": {"cards": [ke], "more": [], "held_back": {"items": [held_item("ke-held", "KE", reason="too_few_creators")]}},
    }
    client = Client(brief_rows_by_market(payloads), scopes=[
        scope(0, 4, 3), scope(1, 4, 2), scope(2, 4, 1), scope(3, 4, 3),
        scope(4, 4, 3), scope(5, 4, 2), scope(6, 4, 0)])
    run(client, tmp_path)
    data = report_data(tmp_path / "report.md")

    assert set(data["market_counts"]) == {"ZA", "NG", "KE"}
    expected_public = {"ZA": (2, 1), "NG": (1, 1), "KE": (1, 1)}
    for market, (raw_count, held_count) in expected_public.items():
        counts = data["market_counts"][market]
        assert counts["public"]["raw_items"] == raw_count
        assert counts["public"]["classified_items"] == raw_count
        assert sum(counts["public"]["result_counts"].values()) == raw_count
        assert counts["public"]["reconciles"] is True
        assert counts["stored_held"]["raw_items"] == held_count
        assert counts["stored_held"]["reason_total"] == held_count
        assert sum(item["count"] for item in counts["stored_held"]["top_reason_totals"]) == held_count
        assert counts["stored_held"]["reconciles"] is True
        producer = counts["producer_placement"]
        assert producer["published_items"] == raw_count
        assert producer["held_items"] == held_count
        assert producer["reconciles"] is True


def test_published_card_audit_joins_quote_and_local_examples(tmp_path):
    client = Client(brief_rows([card()]), scopes=[scope(0, 4, 3)])
    run(client, tmp_path)
    audit = report_data(tmp_path / "report.md")["results"][0]["publication_evidence"]

    assert audit["quote"]["status"] == "pass"
    assert audit["quote"]["text"] == "Dancing to the new sound"
    assert audit["quote"]["evidence"]["url"] == "https://example.test/p1"
    assert [item["evidence_id"] for item in audit["local_examples"]] == ["p1", "p2", "p3"]
    assert all(item["status"] == "pass" for item in audit["local_examples"])
    assert audit["why_now"] == "Recent posts show friends dancing to a new sound at home."
    assert audit["checks"]["why_now_contract"] == "pass"
    assert audit["checks"]["critic_local_why_now_attestation"] == "unavailable_not_persisted"
    floor = audit["checks"]["producer_showable_post_floor"]
    assert (floor["status"], floor["showable_posts"], floor["local_posts"]) == ("pass", 3, 3)
    assert floor["three_day_main_platform_validity"] == "unavailable_not_persisted"


@pytest.mark.parametrize("mutation,expected_reason", [
    ("missing", "quote_missing_or_malformed"),
    ("dangling", "quote_evidence_dangling"),
    ("forged", "quote_not_verbatim"),
])
def test_published_card_audit_explicitly_fails_unjoined_quotes(mutation, expected_reason):
    item = card()
    if mutation == "missing":
        item["specificity"]["quote"] = None
    elif mutation == "dangling":
        item["specificity"]["quote"]["evidence_id"] = "missing"
    else:
        item["specificity"]["quote"]["text"] = "Different words from the source"

    audit = gate.published_evidence_audit(item, "ZA")

    assert audit["checks"]["quote"] == "fail"
    assert audit["quote"]["status"] == "fail"
    assert expected_reason in audit["quote"]["reasons"]


def test_published_card_audit_marks_dangling_local_examples_as_fail():
    item = card()
    item["specificity"]["local_evidence_ids"] = ["p1", "missing"]

    audit = gate.published_evidence_audit(item, "ZA")

    assert audit["checks"]["local_examples"] == "fail"
    assert audit["local_examples"][1] == {
        "evidence_id": "missing", "status": "fail", "reason": "evidence_missing"}


@pytest.mark.parametrize("evidence_id", ["p1", "p2"])
@pytest.mark.parametrize("url", [
    None, "", "ftp://example.test/post", "https:///missing-host",
    "https://user:pass@example.test/post",
    "https://example.test/post?access_token=FAKE_ACCESS_TOKEN_SENTINEL",
    "https://example.test/post#access_token=FAKE_ACCESS_TOKEN_SENTINEL",
    "https://example.test/post#/callback?signature=FAKE_SIGNATURE_SENTINEL",
])
def test_published_quote_and_local_example_require_public_http_links(evidence_id, url):
    item = card()
    next(record for record in item["evidence"] if record["id"] == evidence_id)["url"] = url

    audit = gate.published_evidence_audit(item, "ZA")

    example = next(record for record in audit["local_examples"] if record["evidence_id"] == evidence_id)
    assert example["status"] == "fail"
    assert example["evidence"]["link_check"]["status"] == "fail"
    if evidence_id == "p1":
        assert audit["quote"]["status"] == "fail"
        assert "quote_source_url_invalid" in audit["quote"]["reasons"]


@pytest.mark.parametrize("location,expected_reason", [
    ("query", "url_credential_query_key"),
    ("fragment", "url_credential_fragment_key"),
])
def test_jwt_url_is_scrubbed_from_report_stdout_and_receipt(tmp_path, monkeypatch, capsys,
        location, expected_reason):
    sentinel = f"W7_FAKE_JWT_{location.upper()}_SENTINEL"
    unsafe_url = (f"https://example.test/post?jwt={sentinel}" if location == "query"
        else f"https://example.test/post#jwt={sentinel}")
    item = card()
    item["title"] = unsafe_url
    item["evidence"][0]["url"] = unsafe_url
    item["evidence"][0]["text"] = f"Source text echoes {unsafe_url}"
    item["evidence"][0]["quote_text"] = f"Quoted source echoes {unsafe_url}"
    item["evidence"][0]["flags"] = {"nested": {unsafe_url: {"echo": unsafe_url}}}
    item["specificity"]["quote"]["text"] = unsafe_url
    item["explanation"] = f"Why now includes {unsafe_url}"
    item["specificity"]["why_now"] = item["explanation"]
    held = held_item("za-held", "ZA")
    held["reason_text"] = unsafe_url
    payloads = {
        "ZA": {"cards": [item], "more": [], "held_back": {"items": [held]}},
        "NG": {"cards": [], "more": [], "held_back": {"items": []}},
        "KE": {"status": "data_issue", "cards": [], "more": [], "held_back": {"items": []},
            "banners": [{"kind": "data_issue", "text": unsafe_url, "flags": {unsafe_url: unsafe_url}}],
            "coverage": {"issues": [{"source": unsafe_url, "flags": {unsafe_url: {"echo": unsafe_url}}}]}},
    }
    report, marker = tmp_path / "report.md", tmp_path / "marker.json"
    prefix = "# Existing report prefix\n"
    report.write_text(prefix, encoding="utf-8")
    prefix_bytes = report.read_bytes()
    result = gate.run_gate(now=dt.datetime(2026, 10, 1, 3, tzinfo=gate.SAST),
        report_path=report, marker_path=marker,
        client_factory=lambda: (Client(brief_rows_by_market(payloads),
            scopes=[scope(0, 4, 3), scope(1, 4, 3)]), BQ))
    receipt = run_receipt(marker, "brief-20261001-a").read_text(encoding="utf-8")
    monkeypatch.setattr(gate, "run_gate", lambda **kwargs: result)
    expected_exit = 0 if result["status"] in {"completed", "already_used"} else 2
    acceptance_report, receipt_base = gate._paths(dt.date(2026, 10, 1))
    assert gate.main(["--date", "2026-10-01", "--report", str(acceptance_report),
        "--receipts", str(receipt_base)], now=dt.datetime(2026, 10, 1, 3, tzinfo=gate.SAST)) == expected_exit
    stdout = capsys.readouterr().out
    audit = report_data(report)["results"][0]["publication_evidence"]
    serialized = report.read_text(encoding="utf-8")

    assert report.read_bytes().startswith(prefix_bytes)
    assert sentinel not in serialized
    assert sentinel not in json.dumps(result)
    assert sentinel not in stdout
    assert sentinel not in receipt
    assert audit["quote"]["status"] == "fail"
    assert audit["quote"]["url"] is None
    assert audit["quote"]["link_check"]["status"] == "fail"
    assert expected_reason in audit["quote"]["link_check"]["reasons"]
    assert "quote_text_contains_rejected_url" in audit["quote"]["reasons"]
    assert sentinel not in audit["why_now"]
    data = report_data(report)
    assert data["results"][0]["title"] == "[rejected URL omitted]"
    assert data["held_results"][0]["stored_hold"]["reason_text"] == "[rejected URL omitted]"
    assert data["snapshots"][2]["data_issue_banners"][0]["text"] == "[rejected URL omitted]"
    assert "[rejected URL omitted]" in data["snapshots"][2]["coverage_issues"][0]["flags"]


def test_published_card_audit_marks_missing_local_ids_explicitly():
    item = card()
    item["specificity"]["local_evidence_ids"] = [None, "missing"]

    audit = gate.published_evidence_audit(item, "ZA")

    assert audit["checks"]["local_examples"] == "fail"
    assert audit["local_examples"][0] == {
        "evidence_id": None, "status": "fail", "reason": "evidence_id_missing"}


def test_snapshot_keeps_brief_partial_and_data_issue_status(tmp_path):
    card_za = card()
    card_za["item_id"] = "za-card"
    payloads = {
        "ZA": {"cards": [card_za], "more": [], "held_back": {"items": []}},
        "NG": {"status": "partial", "cards": [], "more": [], "held_back": {"items": []},
            "coverage": {"issues": ["source unavailable"]}},
        "KE": {"status": "data_issue", "cards": [], "more": [], "held_back": {"items": []},
            "banners": [{"kind": "data_issue", "text": "Stored producer banner"}]},
    }
    result = gate.run_gate(now=dt.datetime(2026, 10, 1, 3, tzinfo=gate.SAST),
        report_path=tmp_path / "report.md", marker_path=tmp_path / "marker.json",
        client_factory=lambda: (Client(brief_rows_by_market(payloads),
            scopes=[scope(0, 4, 3)]), BQ))
    snapshots = {item["market"]: item for item in report_data(tmp_path / "report.md")["snapshots"]}

    assert result["status"] == "completed"
    assert snapshots["NG"]["status"] == snapshots["NG"]["payload_status"] == "partial"
    assert snapshots["NG"]["payload_partial_status"] == "partial"
    assert snapshots["NG"]["coverage_issues"] == ["source unavailable"]
    assert snapshots["KE"]["status"] == snapshots["KE"]["payload_status"] == "data_issue"
    assert snapshots["KE"]["data_issue_status"] == "present"
    assert snapshots["KE"]["data_issue_banners"] == payloads["KE"]["banners"]
    for market in ("NG", "KE"):
        counts = report_data(tmp_path / "report.md")["market_counts"][market]
        assert counts["producer_placement"]["status"] == "unavailable_empty_partial_payload"
        assert counts["producer_placement"]["published_items"] is None
        assert counts["public"]["result_counts"] is None
        assert counts["stored_held"]["raw_items"] is None


def test_valid_partial_payload_keeps_observed_placement_with_status_caveat(tmp_path):
    item = card()
    item["item_id"] = "ng-card"
    payloads = {
        "ZA": {"cards": [], "more": [], "held_back": {"items": []}},
        "NG": {"status": "partial", "cards": [item], "more": [],
            "held_back": {"items": [held_item("ng-held", "NG")]}},
        "KE": {"cards": [], "more": [], "held_back": {"items": []}},
    }
    client = Client(brief_rows_by_market(payloads), scopes=[scope(0, 4, 3), scope(1, 4, 3)])
    run(client, tmp_path)
    placement = report_data(tmp_path / "report.md")["market_counts"]["NG"]["producer_placement"]

    assert placement["status"] == "observed_partial_payload"
    assert placement["payload_status"] == "partial"
    assert (placement["published_items"], placement["held_items"]) == (1, 1)


def test_clean_published_empty_payload_keeps_numeric_zero_counts(tmp_path):
    result = run(Client(brief_rows_by_market({})), tmp_path)
    data = report_data(tmp_path / "report.md")

    assert result["status"] == "completed"
    for market in ("ZA", "NG", "KE"):
        placement = data["market_counts"][market]["producer_placement"]
        assert placement["status"] == "observed"
        assert (placement["published_items"], placement["held_items"]) == (0, 0)


def test_zero_scope_denominator_is_unmeasured_not_measured_zero(tmp_path):
    first, second = card(), card()
    first["item_id"], second["item_id"] = "zero-total", "measured-zero"
    client = Client(brief_rows([first, second]), scopes=[scope(0, 0, 0), scope(1, 4, 0)])
    run(client, tmp_path)
    data = report_data(tmp_path / "report.md")
    results = data["results"]

    assert [(item["item_id"], item["share"], item["share_status"]) for item in results] == [
        ("zero-total", None, "unmeasured"), ("measured-zero", 0.0, "measured")]
    assert (results[0]["scope"], results[0]["result"]) == ("unmeasured", "Unmeasured source population")
    assert (results[1]["scope"], results[1]["result"]) == ("global", "Global Discover")
    assert data["counts"]["global"] == 1 and data["counts"]["unmeasured"] == 1
    assert data["market_counts"]["ZA"]["public"]["result_counts"]["unmeasured"] == 1


def test_full_publication_status_is_unproven_and_has_no_numeric_zero_counter(tmp_path):
    item = card()
    client = Client(brief_rows([item]), scopes=[scope(0, 4, 3)])
    run(client, tmp_path)
    data = report_data(tmp_path / "report.md")

    assert data["full_publication_status"] == "unproven"
    assert "today" not in data["counts"]
    assert "today_eligible" not in data["market_counts"]["ZA"]["public"]["result_counts"]
    assert all(item["result"] != "Today eligible from stored card" for item in data["results"])
    assert data["results"][0]["would_publish"] == "unproven"


def test_two_of_two_majority_and_specificity_do_not_claim_the_three_post_floor(tmp_path):
    item = card()
    client = Client(brief_rows([item]), scopes=[scope(0, 2, 2)])
    run(client, tmp_path)
    entry = report_data(tmp_path / "report.md")["results"][0]

    assert entry["specificity"] == "pass"
    assert entry["result"] == "Producer evidence floor not met"
    assert entry["producer_floor"] == "not_met_from_current_candidate_count"


def test_candidate_count_at_floor_does_not_prove_showability(tmp_path):
    item = card()
    client = Client(brief_rows([item]), scopes=[scope(0, 3, 3)])
    run(client, tmp_path)
    entry = report_data(tmp_path / "report.md")["results"][0]

    assert entry["specificity"] == "pass"
    assert entry["producer_floor"] == "unproven_showability_set_not_rechecked"
    assert entry["result"] == "Majority and specificity pass, producer floor unproven"
    assert entry["would_publish"] == "unproven"
    assert entry["showability_evidence_needed"]


def test_recovered_report_appends_to_blocker_prefix_and_hashes_combined_file(tmp_path):
    report = tmp_path / "report.md"
    marker = tmp_path / "marker.json"
    prefix = "# First chain remains blocked\n\n## Recovered results, append only\nExisting blocker text.\n"
    report.write_text(prefix, encoding="utf-8")
    prefix_bytes = report.read_bytes()
    result = gate.run_gate(now=dt.datetime(2026, 10, 1, 3, tzinfo=gate.SAST), report_path=report,
        marker_path=marker, client_factory=lambda: (Client(brief_rows([])), BQ))
    text = report.read_text(encoding="utf-8")
    report_bytes = report.read_bytes()
    data = report_data(report)
    saved_marker = json.loads(run_receipt(marker, "brief-20261001-a").read_text(encoding="utf-8"))
    default_report, default_marker = gate._paths()

    assert result["status"] == "completed"
    assert text.startswith(prefix)
    assert report_bytes.startswith(prefix_bytes)
    assert data["gate_baseline_head"] == "8c645ffae9c712db9b05b6a8387cdf194efde5fc"
    assert data["operator_sha256"]
    assert data["operator_sha256"] == hashlib.sha256(Path(gate.__file__).read_bytes()).hexdigest()
    assert saved_marker["report_sha256"] == hashlib.sha256(report_bytes).hexdigest()
    assert saved_marker["operator_sha256"] == data["operator_sha256"]
    assert saved_marker["run_id"] == "brief-20261001-a"
    assert saved_marker["status"] == "completed"
    assert default_report == gate.OCT1_ACCEPTANCE_REPORT_PATH
    assert default_marker == gate.PRIMARY_RECEIPT_BASE


def test_failed_report_append_cannot_erase_blocker_prefix_bytes(tmp_path, monkeypatch):
    report = tmp_path / "report.md"
    marker = tmp_path / "marker.json"
    report.write_text("# Blocker prefix\n\n## Recovered results, append only\n", encoding="utf-8")
    prefix_bytes = report.read_bytes()
    real_open = Path.open
    fail_next_append = [True]

    class FailingWriter:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def write(self, value):
            raise OSError("injected append failure")

    def open_with_failed_append(path, mode="r", *args, **kwargs):
        if path == report and mode == "a" and fail_next_append[0]:
            fail_next_append[0] = False
            return FailingWriter()
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_with_failed_append)
    result = gate.run_gate(now=dt.datetime(2026, 10, 1, 3, tzinfo=gate.SAST), report_path=report,
        marker_path=marker, client_factory=lambda: (Client(brief_rows([])), BQ))

    assert fail_next_append == [False]
    assert result["status"] == "blocked"
    assert report.read_bytes().startswith(prefix_bytes)
    assert not run_receipt(marker, "brief-20261001-a").exists()
