import json

from core.brief import holds_report


def _post(pid, market=None, source=None, assumed=False):
    return {"id": pid, "market": market, "source_market": source, "flags": ["market_assumed"] if assumed else []}


def test_post_counts_match_the_gate():
    evidence = [_post("a", "ZA"), _post("b", None, "ZA", assumed=True), _post("c", "NG"),
                _post("d", None, None, assumed=True)]
    # local: a (located) and b (ZA feeds); showable: a, b and d (no market); c is located elsewhere
    assert holds_report.post_counts(evidence, "ZA") == (4, 2, 3)


def test_tally_reads_held_items_and_their_failed_checks():
    payload = {"cards": [], "more": [], "banners": [{"kind": "data_issue", "text": "Data issue: 1 of 2"}],
               "held_back": {"count": 2, "items": [
                   {"item_id": "i1" * 6, "title": "Amapiano", "rule": "G10", "reason": "explanation_failed",
                    "reason_text": "Explanation failed its checks", "evidence": [_post("a", "KE"), _post("b", "KE")]},
                   {"item_id": "i2" * 6, "title": "Derby", "rule": "G1", "reason": "data_issue",
                    "reason_text": "Data issue: 1 of the last 3 market-days invalid on the main platform",
                    "evidence": []}]}}
    briefs = [{"market": "KE", "run_id": "r1", "status": "data_issue", "payload": json.dumps(payload)}]
    checks = [{"answer_or_brief_id": f"r1:KE:{'i1' * 6}", "claim_id": "c1", "rule": "K4", "verdict": "cut",
               "reason": "Support check: a claim was not supported by its posts"},
              {"answer_or_brief_id": f"r1:KE:{'i1' * 6}", "claim_id": "c2", "rule": "K4", "verdict": "cut",
               "reason": "Support check: a claim was not supported by its posts"},
              {"answer_or_brief_id": f"r1:KE:{'i1' * 6}", "claim_id": "c3", "rule": "K1", "verdict": "pass",
               "reason": "Quote check: passed"},
              {"answer_or_brief_id": f"r1:ZA:{'i1' * 6}", "claim_id": "c4", "rule": "K1", "verdict": "cut",
               "reason": "elsewhere"}]
    markets, held = holds_report.tally(briefs, checks)
    assert markets == [{"market": "KE", "status": "data_issue", "cards": 0, "held": 2,
                        "banners": ["data_issue: Data issue: 1 of 2"]}]
    assert [(h["title"], h["rule"], h["posts"], h["local"], h["showable"]) for h in held] == [
        ("Amapiano", "G10", 2, 2, 2), ("Derby", "G1", 0, 0, 0)]
    assert held[0]["failed"] == ["2x Support check: a claim was not supported by its posts"]
    assert held[1]["failed"] == []
    assert holds_report.causes(held)[0][1] == ["KE Amapiano"]


def test_every_query_is_read_only():
    for sql in (holds_report.RUN, holds_report.BRIEFS, holds_report.CHECKS, holds_report.HEALTH):
        assert sql.lstrip().upper().startswith("SELECT")
        assert not any(w in sql.upper() for w in ("INSERT", "UPDATE", "DELETE", "MERGE", "CREATE", "DROP"))


def test_a_title_a_cp1252_console_cannot_show_does_not_stop_the_report(monkeypatch):
    """Albert's PC, piped: stdout is cp1252, and a held title such as 昕昕 raised UnicodeEncodeError mid report."""
    import io
    import sys

    from google.cloud import bigquery

    payload = {"cards": [], "more": [], "banners": [], "held_back": {"count": 1, "items": [
        {"item_id": "i1" * 6, "title": "昕昕", "rule": "G10", "reason": "explanation_failed",
         "reason_text": "Explanation failed its checks", "evidence": [_post("a", "KE")]}]}}
    answers = {holds_report.BRIEFS: [{"market": "KE", "run_id": "r1", "status": "published",
                                      "payload": json.dumps(payload)}]}
    monkeypatch.setattr(bigquery, "Client", lambda **kw: object())
    monkeypatch.setattr(holds_report, "_query", lambda client, sql, params: (answers.get(sql, []), 0))
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
    assert holds_report.main(["holds_report", "2026-10-03"]) == 0
    sys.stdout.flush()
    text = raw.getvalue().decode("cp1252")
    assert "KE | ?? | " in text and "CAUSES" in text and sys.stdout.encoding == "cp1252"


def test_held_items_print_the_full_item_id_and_their_title(monkeypatch):
    """BR-4 (4 Oct): the report printed item_id[:12], so Albert could not tell which item was held."""
    import io
    import sys

    from google.cloud import bigquery

    item_id = "f" * 64
    payload = {"cards": [], "more": [], "banners": [], "held_back": {"count": 1, "items": [
        {"item_id": item_id, "title": "Lasizwe", "rule": None, "reason": "not_confirmed",
         "reason_text": "Fewer than 2 supported local posts", "evidence": [_post("a", "ZA")]}]}}
    answers = {holds_report.BRIEFS: [{"market": "ZA", "run_id": "r1", "status": "published",
                                      "payload": json.dumps(payload)}]}
    monkeypatch.setattr(bigquery, "Client", lambda **kw: object())
    monkeypatch.setattr(holds_report, "_query", lambda client, sql, params: (answers.get(sql, []), 0))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert holds_report.main(["holds_report", "2026-10-05"]) == 0
    assert f"ZA | Lasizwe | {item_id} | rule=None reason=not_confirmed" in out.getvalue()


def test_a_held_creator_stored_under_its_raw_key_is_named_from_the_creators_table(monkeypatch):
    """Briefs stored before BR-3 carry "youtube:uc…" titles; the report names them from the creators table, read
    once, and keeps the stored title beside the name."""
    import io
    import sys

    from google.cloud import bigquery

    raw = "youtube:ucabcdefghijklmnopqrstuvwx"
    payload = {"cards": [], "more": [], "banners": [], "held_back": {"count": 2, "items": [
        {"item_id": "a" * 64, "title": raw, "rule": "G1", "reason": "data_issue", "reason_text": "Data issue",
         "evidence": []},
        {"item_id": "b" * 64, "title": "youtube:uczzzzzzzzzzzzzzzzzzzzzzzz", "rule": "G1", "reason": "data_issue",
         "reason_text": "Data issue", "evidence": []}]}}
    seen = []

    def fake_query(client, sql, params):
        seen.append((sql, params))
        if sql == holds_report.BRIEFS:
            return [{"market": "ZA", "run_id": "r1", "status": "published", "payload": json.dumps(payload)}], 0
        if sql == holds_report.CREATORS:
            return [{"key": raw, "handle": "lasizwe", "display_name": "Lasizwe"}], 0
        return [], 0

    monkeypatch.setattr(bigquery, "Client", lambda **kw: object())
    monkeypatch.setattr(holds_report, "_query", fake_query)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert holds_report.main(["holds_report", "2026-10-03"]) == 0
    text = out.getvalue()
    assert f"ZA | Lasizwe ({raw}) | {'a' * 64} |" in text
    assert f"ZA | youtube:uczzzzzzzzzzzzzzzzzzzzzzzz | {'b' * 64} |" in text
    [(sql, params)] = [q for q in seen if q[0] == holds_report.CREATORS]
    assert sorted(params["keys"]) == [raw, "youtube:uczzzzzzzzzzzzzzzzzzzzzzzz"]


def test_the_creators_read_is_read_only_and_skips_suppressed_creators():
    sql = holds_report.CREATORS
    assert sql.lstrip().upper().startswith("SELECT")
    assert not any(w in sql.upper() for w in ("INSERT", "UPDATE", "DELETE", "MERGE", "CREATE", "DROP"))
    assert "v_suppressed_creators" in sql


def test_a_refused_creators_read_leaves_the_stored_titles_and_the_report_goes_on(monkeypatch):
    import io
    import sys

    from google.cloud import bigquery

    raw = "youtube:ucabcdefghijklmnopqrstuvwx"
    payload = {"cards": [], "more": [], "banners": [], "held_back": {"count": 1, "items": [
        {"item_id": "a" * 64, "title": raw, "rule": "G1", "reason": "data_issue", "reason_text": "Data issue",
         "evidence": []}]}}

    def fake_query(client, sql, params):
        if sql == holds_report.CREATORS:
            raise SystemExit("refused: query would read 999999999 bytes, over 67108864")
        if sql == holds_report.BRIEFS:
            return [{"market": "ZA", "run_id": "r1", "status": "published", "payload": json.dumps(payload)}], 0
        return [], 0

    monkeypatch.setattr(bigquery, "Client", lambda **kw: object())
    monkeypatch.setattr(holds_report, "_query", fake_query)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert holds_report.main(["holds_report", "2026-10-03"]) == 0
    text = out.getvalue()
    assert "creator names not read: refused" in text
    assert f"ZA | {raw} | {'a' * 64} |" in text and "CAUSES" in text
