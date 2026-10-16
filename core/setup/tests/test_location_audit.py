"""Unit tests for core/setup/location_audit.py. No cloud access: a fake BigQuery client records every query."""
import csv
import datetime as dt
import re

import pytest

from core.setup import location_audit as la

GB = 1024 ** 3
SINCE, UNTIL = dt.date(2026, 10, 2), dt.date(2026, 10, 15)


class FakeJob:
    def __init__(self, rows, size):
        self.rows, self.total_bytes_processed, self.total_bytes_billed = rows, size, size

    def result(self):
        return self.rows


class FakeClient:
    def __init__(self, rows=None, size=1000, estimates=None):
        self.rows, self.size, self.estimates = rows or {}, size, estimates or {}
        self.calls = []

    def query(self, sql, job_config=None, location=None):
        """rows and estimates are keyed by a substring of the SQL they answer."""
        self.calls.append((sql, job_config.dry_run, job_config.maximum_bytes_billed, location))
        name = next((n for n in self.rows if n in sql), None)
        size = next((v for k, v in self.estimates.items() if k in sql), self.size)
        return FakeJob(self.rows.get(name, []) if not job_config.dry_run else [], size)


def test_every_count_query_is_a_single_read_only_select_on_the_project():
    for q in la.queries(SINCE, UNTIL):
        la.check_statement(q)
        assert "`ogilvy-trends-v2`.intelligence_42_core" in q.sql
        assert "@since" in q.sql and "@until" in q.sql


def test_only_place_mention_queries_read_text():
    readers = [q.name for q in la.queries(SINCE, UNTIL) if re.search(r"\btext\b", q.sql)]
    assert readers == ["place_mention_reach_za", "place_mention_reach_ng", "place_mention_reach_ke"]
    assert all(q.reads_content for q in la.queries(SINCE, UNTIL) if q.name in readers)


def test_guard_refuses_writes_content_and_other_projects():
    bad = [
        la.Query("w", "", "SELECT 1; DELETE FROM x"),
        la.Query("m", "", "WITH a AS (SELECT 1) MERGE x"),
        la.Query("s", "", "SELECT * FROM `ogilvy-trends-v2`.intelligence_42_core.posts"),
        la.Query("u", "", "SELECT url FROM `ogilvy-trends-v2`.intelligence_42_core.posts"),
        la.Query("t", "", "SELECT COUNT(text) FROM `ogilvy-trends-v2`.intelligence_42_core.posts"),
        la.Query("o", "", "SELECT 1 FROM `other-project`.x.y"),
    ]
    for q in bad:
        with pytest.raises(ValueError):
            la.check_statement(q)


def test_sheet_query_may_read_text_and_links_but_stays_read_only():
    q = la.sheet_query(SINCE, UNTIL)
    la.check_statement(q, sheet=True)
    with pytest.raises(ValueError):
        la.check_statement(q)
    assert q.params["za"] == 300 and q.params["ng"] == 150 and q.params["ke"] == 150
    assert q.params["salt"] == la.SALT


def test_place_pattern_matches_whole_words_from_the_gazetteer():
    rx = re.compile(la.place_pattern("ZA"))
    assert rx.search("braai in cape town tonight")
    assert rx.search("#capetown vibes")
    assert rx.search("jozi!")
    assert not rx.search("#jozinights")
    assert not rx.search("lagos traffic")


def test_count_rows_with_free_text_are_withheld_whole():
    client = FakeClient(rows={"AS language_hint": [{"market": "ZA", "platform": "this is a caption somebody wrote!"}]})
    log, budget = [], {"spent": 0}
    q = next(q for q in la.queries(SINCE, UNTIL) if q.name == "located_by_platform")
    assert la.run_count(client, q, log, budget) is None
    assert log[0]["status"].startswith("withheld")


def test_dry_run_first_then_capped_run():
    client = FakeClient()
    q = la.queries(SINCE, UNTIL)[0]
    la.run_count(client, q, [], {"spent": 0})
    assert [bool(c[1]) for c in client.calls] == [True, False]
    assert client.calls[1][2] == la.MAX_BYTES


def test_estimate_over_cap_is_refused_and_never_run():
    client = FakeClient(size=3 * GB)
    with pytest.raises(la.Refused):
        la.run_count(client, la.queries(SINCE, UNTIL)[0], [], {"spent": 0})
    assert [bool(c[1]) for c in client.calls] == [True]


def test_out_writes_counts_and_survives_a_refused_query(tmp_path):
    rows = {"AS language_hint": [{"market": "ZA", "platform": "tiktok", "posts": 100, "in_market": 80,
                                     "elsewhere": 5, "language_hint": 0, "unknown": 15}]}
    client = FakeClient(rows=rows, estimates={"REGEXP_CONTAINS": 3 * GB})
    out = tmp_path / "audit.md"
    assert la.main(["--out", str(out), "--until", "2026-10-15"], client=client) == 0
    text = out.read_text(encoding="utf-8")
    assert "| ZA | tiktok | 100 | 80 | 5 | 0 | 15 |" in text
    assert "refused" in text


def test_label_sheet_is_refused_inside_the_repo():
    with pytest.raises(SystemExit):
        la.main(["--label-sheet", str(la.ROOT / "labels.csv")], client=FakeClient())


def test_label_sheet_and_score_round_trip(tmp_path):
    sample = [
        {"market": "ZA", "platform": "tiktok", "series": "feed_tiktok", "route": "tiktok/trending",
         "geo_market": "ZA", "geo_source": "ext_region", "geo_confidence": 0.9, "place_id": None,
         "post_date": dt.date(2026, 10, 10), "url": "https://example.test/1", "text": "Durban\nJuly"},
        {"market": "ZA", "platform": "instagram", "series": "ig_location", "route": "instagram/location/posts",
         "geo_market": None, "geo_source": "none", "geo_confidence": None, "place_id": "123",
         "post_date": dt.date(2026, 10, 11), "url": "https://example.test/2", "text": "x"},
        {"market": "KE", "platform": "tiktok", "series": "feed_tiktok", "route": "tiktok/trending",
         "geo_market": "TZ", "geo_source": "ext_region", "geo_confidence": 0.9, "place_id": None,
         "post_date": dt.date(2026, 10, 12), "url": "https://example.test/3", "text": "y"},
    ]
    sheet = tmp_path / "labels.csv"
    assert la.main(["--label-sheet", str(sheet), "--until", "2026-10-15"],
                   client=FakeClient(rows={"FARM_FINGERPRINT": sample})) == 0
    with sheet.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["sample_id"] for r in rows] == ["1", "2", "3"]
    assert rows[0]["text"] == "Durban July"
    labels = [("ZA", "KwaZulu-Natal", "Durban", "caption"), ("ZA", "Gauteng", "Soweto", "tag"),
              ("KE", "", "", "cannot_tell")]
    for r, (country, province, city, basis) in zip(rows, labels):
        r.update(label_country=country, label_province=province, label_city=city, label_basis=basis)
    with sheet.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    out = tmp_path / "score.md"
    assert la.main(["--score", str(sheet), "--out", str(out)]) == 0
    score = out.read_text(encoding="utf-8")
    assert "| ZA | 2 | 2 (100.0%) | 2 (100.0%) | 2 (100.0%) | 0 |" in score
    assert "| KE | 1 | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 1 |" in score
    assert "| ZA | ext_region | 1 | 1 | 100.0% |" in score
    assert "https://" not in score and "Durban July" not in score


def test_builder_client_impersonates_f42_builder_from_a_personal_login(monkeypatch):
    import google.auth
    from google.auth import impersonated_credentials
    from google.cloud import bigquery

    made = {}

    class FakeImpersonated:
        def __init__(self, **kw):
            made.update(kw)

    monkeypatch.setattr(google.auth, "default", lambda scopes=None: ("personal-login", "ogilvy-trends-v2"))
    monkeypatch.setattr(impersonated_credentials, "Credentials", FakeImpersonated)
    monkeypatch.setattr(bigquery, "Client", lambda **kw: kw)
    client = la.builder_client()
    assert made["source_credentials"] == "personal-login" and made["target_principal"] == la.BUILDER
    assert isinstance(client["credentials"], FakeImpersonated) and client["project"] == la.PROJECT


def test_builder_client_refuses_an_other_impersonated_account(monkeypatch):
    import google.auth
    from google.auth import impersonated_credentials

    class FakeImpersonated:
        _target_principal = "someone-else@ogilvy-trends-v2.iam.gserviceaccount.com"

    monkeypatch.setattr(impersonated_credentials, "Credentials", FakeImpersonated)
    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (FakeImpersonated(), "ogilvy-trends-v2"))
    with pytest.raises(la.Refused):
        la.builder_client()
