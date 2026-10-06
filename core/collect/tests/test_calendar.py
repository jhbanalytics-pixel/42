import io
import json
import re
import urllib.error
from datetime import date
from pathlib import Path

import pytest
import yaml

from core.collect import calendar as cal

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
MOMENTS = HERE.parents[1] / "config" / "moments.yaml"
START = date(2026, 9, 28)
KINDS = {"holiday", "payday", "fuel", "election", "culture", "commerce", "season", "results", "sport"}


def nager(market, year):
    return json.loads((FIXTURES / f"nager_{market}_{year}.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def moments():
    return yaml.safe_load(MOMENTS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rows():
    return cal.build_rows(start=START)


def find(rows, market, name):
    return sorted(r["moment_date"] for r in rows if r["market"] == market and r["name"] == name)


# Nager parsing ----------------------------------------------------------------


def test_nager_fixtures_exist_for_every_market_and_year():
    for market in ("ZA", "NG", "KE"):
        for year in (2026, 2027):
            assert nager(market, year), (market, year)


def test_parse_nager_gives_holiday_rows_with_the_route_as_source():
    parsed = cal.parse_nager(nager("ZA", 2026), "ZA", 2026)
    heritage = [r for r in parsed if r["name"] == "Heritage Day"]
    assert heritage == [{
        "moment_date": date(2026, 9, 24), "market": "ZA", "name": "Heritage Day",
        "kind": "holiday", "source": "date.nager.at /api/v3/PublicHolidays/2026/ZA", "item_ids": [],
    }]
    assert len(parsed) == 13


def test_parse_nager_uses_the_english_name():
    parsed = cal.parse_nager(nager("ZA", 2026), "ZA", 2026)
    assert date(2026, 12, 26) in [r["moment_date"] for r in parsed if r["name"] == "Day of Goodwill"]


def test_parse_nager_skips_regional_holidays():
    records = [
        {"date": "2026-10-10", "name": "National One", "global": True, "counties": None},
        {"date": "2026-10-11", "name": "Regional One", "global": False, "counties": ["KE-01"]},
    ]
    assert [r["name"] for r in cal.parse_nager(records, "KE", 2026)] == ["National One"]


def test_no_network_without_fetch(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network used without --fetch")
    monkeypatch.setattr(cal.urllib.request, "urlopen", refuse)
    assert cal.build_rows(start=START)


# Window -----------------------------------------------------------------------


def test_window_is_ninety_days_or_up_to_known_until(rows, moments):
    known_until = date.fromisoformat(str(moments["known_until"]))
    assert known_until == date(2027, 1, 31)
    dates = [r["moment_date"] for r in rows]
    assert min(dates) >= START
    assert max(dates) <= known_until


def test_window_drops_past_and_far_dates(rows):
    assert find(rows, "ZA", "Heritage Day") == []
    assert find(rows, "KE", "Madaraka Day") == []
    assert find(rows, "ZA", "Day of Reconciliation") == [date(2026, 12, 16)]
    assert find(rows, "KE", "Jamhuri Day") == [date(2026, 12, 12)]
    assert find(rows, "NG", "New Year's Day") == [date(2027, 1, 1)]


def test_window_runs_ninety_days_when_that_passes_known_until():
    later = cal.build_rows(start=date(2027, 1, 15))
    dates = [r["moment_date"] for r in later]
    assert max(dates) <= date(2027, 4, 15)
    assert find(later, "ZA", "Good Friday") == [date(2027, 3, 26)]
    assert find(later, "ZA", "Freedom Day") == []


def test_window_edges_are_inclusive():
    rows = cal.build_rows(start=date(2026, 12, 16))
    assert find(rows, "ZA", "Day of Reconciliation") == [date(2026, 12, 16)]


# Every row ----------------------------------------------------------------------


def test_every_row_has_a_source_and_a_known_kind(rows):
    assert rows
    for r in rows:
        assert r["source"].strip(), r
        assert r["kind"] in KINDS, r
        assert r["item_ids"] == []
        assert r["market"] in {"ZA", "NG", "KE"}


def test_no_duplicate_key(rows):
    keys = [(r["moment_date"], r["market"], r["name"]) for r in rows]
    assert len(keys) == len(set(keys))


def test_rows_sorted_by_date_then_market(rows):
    assert rows == sorted(rows, key=lambda r: (r["moment_date"], r["market"], r["name"]))


def test_every_market_has_rows(rows):
    assert {r["market"] for r in rows} == {"ZA", "NG", "KE"}


# Hand list dated from nager and docs --------------------------------------------


def test_ng_independence_day_once_and_not_twice(rows):
    ng_oct1 = [r for r in rows if r["market"] == "NG" and r["moment_date"] == date(2026, 10, 1)]
    nager_rows = [r for r in ng_oct1 if r["source"].startswith("date.nager.at")]
    assert [r["name"] for r in nager_rows] == ["Independence Day"]
    assert "BUILD.md" in nager_rows[0]["source"]


def test_ke_mashujaa_day(rows):
    assert find(rows, "KE", "Mashujaa Day") == [date(2026, 10, 20)]


def test_za_local_elections_is_an_election(rows):
    hits = [r for r in rows if r["market"] == "ZA" and r["name"] == "Local elections"]
    assert [r["moment_date"] for r in hits] == [date(2026, 11, 4)]
    assert hits[0]["kind"] == "election"
    assert find(rows, "ZA", "Election Day") == []


# Rule based dates, October to December 2026 --------------------------------------


def test_black_friday_2026_in_every_market(rows):
    for market in ("ZA", "NG", "KE"):
        assert find(rows, market, "Black Friday") == [date(2026, 11, 27)], market


def test_black_friday_rule_other_years():
    rule = {"rule": "yearly_nth_weekday", "month": 11, "weekday": 3, "nth": 4, "offset_days": 1}
    assert cal.rule_dates(rule, date(2027, 1, 1), date(2028, 12, 31)) == [date(2027, 11, 26), date(2028, 11, 24)]


def test_za_fuel_price_day_is_first_wednesday(rows):
    got = [d for d in find(rows, "ZA", "Fuel price adjustment") if d <= date(2026, 12, 31)]
    assert got == [date(2026, 10, 7), date(2026, 11, 4), date(2026, 12, 2)]
    assert find(rows, "ZA", "Fuel price adjustment")[-1] == date(2027, 1, 6)


def test_ke_epra_review_on_the_14th(rows):
    got = [d for d in find(rows, "KE", "EPRA fuel price review") if d <= date(2026, 12, 31)]
    assert got == [date(2026, 10, 14), date(2026, 11, 14), date(2026, 12, 14)]


def test_za_payday_on_the_25th(rows):
    assert find(rows, "ZA", "Payday on the 25th") == [
        date(2026, 10, 25), date(2026, 11, 25), date(2026, 12, 25), date(2027, 1, 25)]


def test_detty_december_start_and_end_rows(rows):
    assert find(rows, "NG", "Detty December starts") == [date(2026, 12, 1)]
    assert find(rows, "NG", "Detty December ends") == [date(2026, 12, 31)]
    season = [r for r in rows if r["name"].startswith("Detty December")]
    assert {r["kind"] for r in season} == {"season"}


def test_rule_rows_carry_the_rule_text_as_source(rows, moments):
    by_name = {r["name"]: r["source"] for r in rows}
    for rule in moments["rules"]:
        assert rule["source"].startswith("rule: "), rule
        assert by_name[rule["name"]] == rule["source"]


def test_monthly_day_skips_months_without_that_day():
    rule = {"rule": "monthly_day", "day": 31}
    assert cal.rule_dates(rule, date(2026, 11, 1), date(2027, 1, 31)) == [date(2026, 12, 31), date(2027, 1, 31)]


def test_unknown_rule_is_refused():
    with pytest.raises(ValueError):
        cal.rule_dates({"rule": "full_moon"}, START, date(2026, 12, 31))


# Moments file -------------------------------------------------------------------


def test_moments_entries_have_market_name_kind_source(moments):
    for entry in moments["rules"] + moments["nager_renames"]:
        assert entry["market"] and entry["name"] and entry["source"], entry
        assert entry["kind"] in KINDS, entry
    for entry in moments["pending"]:
        assert entry["market"] and entry["name"] and entry["missing"], entry
        assert entry["kind"] in KINDS, entry


def test_pending_items_are_never_loaded(rows, moments):
    assert moments["pending"]
    loaded = {r["name"] for r in rows}
    for entry in moments["pending"]:
        assert entry["name"] not in loaded, entry
        assert not entry.get("date") and not entry.get("rule"), entry


def test_pending_covers_the_undated_hand_list(moments):
    names = " ".join(e["name"] for e in moments["pending"]).lower()
    for word in ("sassa", "bbnaija", "bbmzansi", "felabration", "waec", "fixtures"):
        assert word in names, word


def test_research_moment_seeds_keep_provenance_and_load_only_explicit_dates(moments, monkeypatch):
    research_pending = [entry for entry in moments["pending"] if entry.get("source") == "research_confirmed"]
    dated = [entry for entry in moments.get("dated", []) if entry.get("source") == "research_confirmed"]

    assert len(research_pending) == 60
    assert sum(entry["source_file"] == "LOCAL-RESEARCH-2026-09-30.md" for entry in research_pending) == 60
    assert sum(entry["source_file"] == "LOCAL-RESEARCH-2-2026-09-30.md" for entry in research_pending) == 0
    assert {115, 117} <= {entry["source_line"] for entry in research_pending}
    assert all(not re.search(r"\b20\d{2}\b", entry["supplied_date_text"]) for entry in research_pending)
    assert len(dated) == 27
    assert all(entry["source_file"] == "LOCAL-RESEARCH-2-2026-09-30.md" for entry in dated)
    assert len({entry["source_line"] for entry in dated}) == 26
    assert sum(entry["source_line"] == 265 for entry in dated) == 2
    assert all(entry["active"] is True for entry in dated)
    assert all(entry["kind"] in KINDS for entry in research_pending + dated)
    assert all(entry["source_urls"] for entry in research_pending + dated)
    assert all(entry["source_line"] > 0 for entry in research_pending + dated)
    assert all(entry.get("supplied_date_text") for entry in research_pending + dated)
    assert all(not entry.get("date") and not entry.get("rule") for entry in research_pending)
    assert {(entry["name"], entry["date"]) for entry in dated if entry["source_line"] == 265} == {
        ("Lagos Fashion Week 2026 starts", "2026-10-28"),
        ("Lagos Fashion Week 2026 ends", "2026-11-01"),
    }
    assert all(entry["supplied_date_text"] == "2026-10-28 to 2026-11-01"
               for entry in dated if entry["source_line"] == 265)

    def refuse_http(*args, **kwargs):
        raise AssertionError("a fixture calendar build must not make HTTP calls")

    monkeypatch.setattr(cal.urllib.request, "urlopen", refuse_http)
    rows = cal.build_rows(start=START, fetch=False, moments=moments)
    research_rows = [row for row in rows if row["source"].startswith("research_confirmed;")]
    assert len(research_rows) == len(dated)
    by_key = {(row["market"], row["name"]): row for row in research_rows}
    for entry in dated:
        row = by_key[(entry["market"], entry["name"])]
        assert row["moment_date"] == date.fromisoformat(entry["date"])
        assert entry["source_file"] in row["source"]
        assert all(url in row["source"] for url in entry["source_urls"])
    active_keys = {(row["market"], row["name"]) for row in rows}
    assert not active_keys & {(entry["market"], entry["name"]) for entry in research_pending}


@pytest.mark.parametrize(("field", "value"), [
    ("date", "2026-10-28 to 2026-11-01"),
    ("market", "XX"),
    ("kind", "unrecognized"),
    ("source", "another_source"),
    ("source_urls", []),
])
def test_invalid_active_dated_research_seed_is_rejected(moments, field, value):
    seed = {
        "active": True,
        "date": "2026-10-01",
        "kind": "culture",
        "market": "ZA",
        "name": "Seed boundary event",
        "source": "research_confirmed",
        "source_file": "LOCAL-RESEARCH-2-2026-09-30.md",
        "source_line": 1,
        "source_urls": ["https://example.com/source"],
        "supplied_date_text": "2026-10-01",
    }
    seed[field] = value
    with pytest.raises(ValueError):
        cal.build_rows(start=START, fetch=False, moments={**moments, "dated": [seed]})


def test_inactive_dated_research_seed_is_ignored(moments):
    seed = {
        "active": False,
        "date": "invalid date",
        "kind": "invalid kind",
        "market": "XX",
        "name": "Inactive seed boundary event",
        "source": "invalid source",
        "source_file": "LOCAL-RESEARCH-2-2026-09-30.md",
        "source_line": 1,
        "source_urls": [],
        "supplied_date_text": "unknown",
    }
    rows = cal.build_rows(start=START, fetch=False, moments={**moments, "dated": [seed]})
    assert not any(row["name"] == seed["name"] for row in rows)


def test_moments_file_has_no_banned_words_or_dashes():
    text = MOMENTS.read_text(encoding="utf-8").lower()
    banned = ["".join(["gen", "z"]), "".join(["gen", " z"]), "".join(["gen", "-z"]),
              "".join(["google", "_trends"]), "".join(["google", " trends"])]
    for word in banned:
        assert word not in text, word
    assert not re.search(r"\b" + "age" + r"d?\b|\b" + "age" + " group", text)
    for mark in (chr(0x2014), chr(0x2013), "".join(["-", "-"])):
        assert mark not in text


# MERGE ----------------------------------------------------------------------------


class FakeJob:
    def __init__(self, config):
        self.config = config
        self.total_bytes_processed = 0
        self.num_dml_affected_rows = 3

    def result(self):
        return []


class FakeClient:
    def __init__(self, fail_dry=False):
        self.calls = []
        self.fail_dry = fail_dry

    def query(self, sql, job_config=None, location=None):
        self.calls.append((sql, job_config, location))
        if job_config.dry_run and self.fail_dry:
            raise RuntimeError("dry run failed")
        return FakeJob(job_config)


def test_merge_sql_is_add_only_and_leaves_existing_rows_untouched():
    sql = cal.MERGE_SQL
    assert sql.lstrip().upper().startswith("MERGE")
    assert "WHEN NOT MATCHED THEN" in sql
    assert not re.search(r"\bWHEN MATCHED\b", sql, re.I)
    assert not re.search(r"\bUPDATE\b|\bDELETE\b", sql, re.I)
    assert "`ogilvy-trends-v2.intelligence_42_core.calendar`" in sql
    assert "@rows" in sql
    on = re.search(r"\bON\b(.*?)WHEN", sql, re.S).group(1)
    for col in ("moment_date", "market", "name"):
        assert f"t.{col} = s.{col}" in on


def test_apply_dry_runs_before_the_merge(rows):
    client = FakeClient()
    assert cal.apply(client, rows) == 0
    assert len(client.calls) == 2
    (sql1, cfg1, _), (sql2, cfg2, _) = client.calls
    assert sql1 == sql2 == cal.MERGE_SQL
    assert cfg1.dry_run is True
    assert not cfg2.dry_run
    for cfg in (cfg1, cfg2):
        [param] = cfg.query_parameters
        assert param.name == "rows"
        assert len(param.values) == len(rows)


def test_apply_stops_when_the_dry_run_fails(rows):
    client = FakeClient(fail_dry=True)
    assert cal.apply(client, rows) == 1
    assert len(client.calls) == 1


def test_main_dry_run_prints_rows_and_counts_without_a_client(monkeypatch, capsys):
    def refuse(*a, **k):
        raise AssertionError("client built on a dry run")
    monkeypatch.setattr(cal.bigquery, "Client", refuse)
    assert cal.main(["--start", "2026-09-28"]) == 0
    out = capsys.readouterr().out
    assert "2026-11-27  ZA  Black Friday" in out
    assert re.search(r"ZA: \d+ rows", out) and re.search(r"NG: \d+ rows", out) and re.search(r"KE: \d+ rows", out)
    assert "not loaded" in out
    assert "dry run only" in out


# Last year's analogues (task 3.6) -----------------------------------------------------


def one(rows, market, name, moment_date=None):
    [r] = [r for r in rows if r["market"] == market and r["name"] == name
           and (moment_date is None or r["moment_date"] == moment_date)]
    return r


def test_analogue_window_is_seven_days_either_side():
    assert cal.ANALOGUE_DAYS == 7
    assert cal.analogue_window(date(2025, 11, 28)) == (date(2025, 11, 21), date(2025, 12, 5))
    assert cal.analogue_window(date(2026, 1, 1)) == (date(2025, 12, 25), date(2026, 1, 8))


def test_fixed_holiday_takes_the_same_statutory_day_last_year(rows, moments):
    got = cal.match_analogue(one(rows, "KE", "Mashujaa Day"), moments)
    assert got == {"analogue_date": date(2025, 10, 20), "match_kind": "fixed_date", "reason": None}
    assert one(rows, "NG", "Boxing Day")["moment_date"] == date(2026, 12, 28)
    assert cal.match_analogue(one(rows, "NG", "Boxing Day"), moments)["analogue_date"] == date(2025, 12, 26)
    assert cal.match_analogue(one(rows, "NG", "Independence Day"), moments)["analogue_date"] == date(2025, 10, 1)


def test_moving_moment_takes_last_years_dated_source_row(moments):
    later = cal.build_rows(start=date(2027, 1, 15))
    got = cal.match_analogue(one(later, "ZA", "Good Friday"), moments)
    assert got == {"analogue_date": date(2026, 4, 3), "match_kind": "source_row", "reason": None}


def test_a_held_source_row_wins_over_the_fixed_list(rows, moments):
    got = cal.match_analogue(one(rows, "ZA", "New Year's Day"), moments)
    assert got["match_kind"] == "source_row"
    assert got["analogue_date"] == date(2026, 1, 1)


def test_rule_moments_take_the_rule_date_in_the_same_month_last_year(rows, moments):
    assert cal.match_analogue(one(rows, "ZA", "Black Friday"), moments)["analogue_date"] == date(2025, 11, 28)
    assert cal.match_analogue(one(rows, "NG", "Detty December ends"), moments) == {
        "analogue_date": date(2025, 12, 31), "match_kind": "rule", "reason": None}
    fuel = one(rows, "ZA", "Fuel price adjustment", date(2026, 10, 7))
    assert cal.match_analogue(fuel, moments)["analogue_date"] == date(2025, 10, 1)
    pay = one(rows, "ZA", "Payday on the 25th", date(2027, 1, 25))
    assert cal.match_analogue(pay, moments)["analogue_date"] == date(2026, 1, 25)


def test_rule_with_no_date_that_month_last_year_has_no_analogue(moments):
    rule = {"market": "ZA", "name": "Leap", "kind": "payday", "rule": "yearly_date", "month": 2, "day": 29,
            "source": "rule: test"}
    extended = dict(moments, rules=moments["rules"] + [rule])
    got = cal.match_analogue(cal.row(date(2028, 2, 29), "ZA", "Leap", "payday", "rule: test"), extended)
    assert got["analogue_date"] is None and got["match_kind"] == "none"
    assert "February 2027" in got["reason"]


def test_moving_moment_without_last_years_row_has_no_analogue(rows, moments):
    got = cal.match_analogue(one(rows, "ZA", "Local elections"), moments)
    assert got["analogue_date"] is None and got["match_kind"] == "none"
    assert "2025" in got["reason"] and "Local elections" in got["reason"]


def test_held_source_without_the_moment_gives_no_analogue(moments):
    row = cal.row(date(2027, 1, 10), "ZA", "Invented Day", "holiday", "date.nager.at test")
    got = cal.match_analogue(row, moments)
    assert got["analogue_date"] is None and got["match_kind"] == "none"
    assert "no Invented Day row in date.nager.at 2026 ZA" in got["reason"]


def test_fixed_dates_source_does_not_call_ng_observances_statutory(moments):
    fixed = moments["fixed_dates"]
    assert fixed["NG"]["Children's Day"] == "05-27" and fixed["NG"]["National Youth Day"] == "11-01"
    source = fixed["source"]
    assert "Children's Day" in source and "National Youth Day" in source and "observances" in source
    assert not source.startswith("rule: statutory")
    comment = MOMENTS.read_text(encoding="utf-8").split("fixed_dates:")[0].rsplit("\n\n", 1)[1]
    assert "by statute" not in comment


def fetch_fixtures(calls, fail=lambda url: None):
    def urlopen(url, timeout=None):
        calls.append(url)
        error = fail(url)
        if error:
            raise error
        _, year, market = url.rsplit("/", 2)
        path = FIXTURES / f"nager_{market}_{year}.json"
        return io.BytesIO(path.read_bytes() if path.exists() else b"[]")
    return urlopen


def test_fetch_makes_one_call_per_market_and_year(monkeypatch, moments):
    monkeypatch.setattr(cal, "NAGER_CACHE", {})
    calls = []
    monkeypatch.setattr(cal.urllib.request, "urlopen", fetch_fixtures(calls))
    rows = cal.build_rows(start=START, fetch=True, moments=moments)
    plan = cal.analogue_plan(rows, moments, fetch=True)
    assert len(calls) == len(set(calls))
    assert sorted(calls) == sorted(cal.NAGER_URL.format(year=y, market=m)
                                   for m in ("ZA", "NG", "KE") for y in (2025, 2026, 2027))
    assert rows == cal.build_rows(start=START, moments=moments)
    assert len(plan) == len(rows)


def test_a_failed_fetch_puts_the_reason_on_the_rows_it_affects(monkeypatch, moments):
    monkeypatch.setattr(cal, "NAGER_CACHE", {})
    calls = []
    def fail(url):
        return urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None) if "/2025/" in url else None
    monkeypatch.setattr(cal.urllib.request, "urlopen", fetch_fixtures(calls, fail))
    rows = cal.build_rows(start=START, fetch=True, moments=moments)
    plan = cal.analogue_plan(rows, moments, fetch=True)
    assert len(calls) == len(set(calls))
    mashujaa = next(p for p in plan if p["row"]["market"] == "KE" and p["row"]["name"] == "Mashujaa Day")
    assert mashujaa["analogue_date"] == date(2025, 10, 20) and mashujaa["match_kind"] == "fixed_date"
    assert "date.nager.at 2025 KE" in mashujaa["reason"] and "503" in mashujaa["reason"]
    elections = next(p for p in plan if p["row"]["market"] == "ZA" and p["row"]["name"] == "Local elections")
    assert elections["analogue_date"] is None and "date.nager.at 2025 ZA" in elections["reason"]
    friday = next(p for p in plan if p["row"]["market"] == "ZA" and p["row"]["name"] == "Black Friday")
    assert friday["reason"] is None
    rec = one(cal.analogue_records(plan, []), "KE", "Mashujaa Day")
    assert rec["status"] == "no_evidence" and "503" in rec["reason"]


def test_a_failed_fetch_for_the_calendar_year_is_printed_not_raised(monkeypatch, capsys):
    monkeypatch.setattr(cal, "NAGER_CACHE", {})
    calls = []
    def fail(url):
        return urllib.error.URLError("timed out") if url.endswith("/2026/KE") else None

    def refuse(*a, **k):
        raise AssertionError("client built on a dry run")
    monkeypatch.setattr(cal.urllib.request, "urlopen", fetch_fixtures(calls, fail))
    monkeypatch.setattr(cal.bigquery, "Client", refuse)
    assert cal.main(["--start", "2026-09-28", "--fetch"]) == 0
    out = capsys.readouterr().out
    assert "not loaded: date.nager.at 2026 KE" in out and "timed out" in out
    assert "KE  Jamhuri Day" not in out
    assert "ZA  Day of Reconciliation" in out
    assert calls.count(cal.NAGER_URL.format(year=2026, market="KE")) == 1


def test_fixed_dates_names_are_calendar_names(moments):
    names = {(m, n) for m, table in moments["fixed_dates"].items() if m != "source" for n in table}
    nager = {(m, r["name"]) for m in ("ZA", "NG", "KE") for y in (2026, 2027)
             for r in cal.nager_rows(m, y, renames=cal.renames(moments))}
    assert names <= nager, names - nager
    assert moments["fixed_dates"]["source"].startswith("rule: ")
    for m, table in moments["fixed_dates"].items():
        if m != "source":
            for day in table.values():
                assert re.fullmatch(r"\d\d-\d\d", day), day


# The evidence query ----------------------------------------------------------------


@pytest.fixture(scope="module")
def plan(rows, moments):
    return cal.analogue_plan(rows, moments)


def test_plan_has_one_entry_per_calendar_row(rows, plan):
    assert [p["row"] for p in plan] == rows
    for p in plan:
        if p["analogue_date"]:
            assert (p["window_start"], p["window_end"]) == cal.analogue_window(p["analogue_date"])
        else:
            assert p["window_start"] is None and p["window_end"] is None and p["reason"]


def test_evidence_sql_is_one_read_only_select():
    sql = cal.EVIDENCE_SQL
    assert sql.lstrip().upper().startswith("WITH")
    assert ";" not in sql
    for word in ("INSERT", "UPDATE", "DELETE", "MERGE", "DROP", "TRUNCATE", "CREATE", "ALTER", "REPLACE"):
        assert not re.search(rf"\b{word}\b", sql, re.I), word
    assert "".join(["gen", "z"]) not in sql.lower()
    assert f"`{cal.LEGACY_TABLE}`" in sql and f"`{cal.POSTS_TABLE}`" in sql


def test_evidence_sql_is_parameterised_date_and_market_bounded():
    sql = cal.EVIDENCE_SQL
    assert not re.search(r"\d{4}-\d\d-\d\d", sql)
    assert not re.search(r"'(ZA|NG|KE|za|ng|ke)'", sql)
    legacy, posts = sql.split("UNION ALL")
    assert "e.market IN UNNEST(@legacy_markets)" in legacy
    assert "@min_start" in legacy and "@max_end" in legacy
    assert "DATE(e.collected_at) >=" in legacy
    assert "p.geo_market IN UNNEST(@markets)" in posts
    assert "p.post_date BETWEEN @min_start AND @max_end" in posts
    for part in (legacy, posts):
        assert "w.window_start AND w.window_end" in part
    assert "@top_read" in sql and "UNNEST(@windows)" in sql


def legacy_half():
    return cal.EVIDENCE_SQL.split("UNION ALL")[0]


def test_legacy_half_reads_only_the_social_allowlist():
    legacy = legacy_half()
    for column, allowed in (("source", cal.LEGACY_SOCIAL_SOURCES), ("platform", cal.LEGACY_SOCIAL_PLATFORMS)):
        found = re.search(rf"AND LOWER\(e\.{column}\) IN \(([^)]*)\)", legacy)
        assert found, column
        assert re.findall(r"'([^']*)'", found.group(1)) == list(allowed)
    assert "socialcrawl" in cal.LEGACY_SOCIAL_SOURCES and "tiktok" in cal.LEGACY_SOCIAL_PLATFORMS
    assert "e.source" not in cal.EVIDENCE_SQL.split("UNION ALL")[1]


def test_no_search_or_trends_value_can_be_in_the_allowlist():
    legacy_trends = {"_".join(["google", "trends", "rss"]), "bigquery_trends", "google trends", "google_search", "google_news",
                     "semrush", "semrush_search", "gdelt", "news", "web", "wikipedia", "cloudflare_radar"}
    for value in cal.LEGACY_SOCIAL_SOURCES + cal.LEGACY_SOCIAL_PLATFORMS:
        assert value == value.lower().strip(), value
        assert value not in legacy_trends, value
        for word in ("google", "trend", "search", "news", "semrush", "gdelt", "prism"):
            assert word not in value, value
    legacy = legacy_half()
    assert "google" not in legacy.lower()
    literals = [x for x in re.findall(r"'([^']*)'", legacy) if x != cal.LEGACY_SOURCE]
    assert literals and not [x for x in literals if "trend" in x.lower() or "search" in x.lower()]


def legacy_tags(text):
    return [t for t in re.findall(cal.TAG_PATTERN, text.lower()) if not re.search(cal.HEX_COLOUR, t)]


def test_legacy_tags_need_a_letter_and_skip_colours_and_url_fragments():
    text = ("Ranked #1 and #3 today #BlackFriday, (#sale2025) #2025sale #fff #1a2b3c #FFFFFF color:#abc123 "
            "see https://x.co/page#section2 and /#/route &#x27; #_1 #a #_")
    assert legacy_tags(text) == ["blackfriday", "sale2025", "2025sale", "a"]
    legacy = legacy_half()
    assert f"REGEXP_EXTRACT_ALL(LOWER(CONCAT(IFNULL(e.title, ''), ' ', IFNULL(e.text, ''))), r'{cal.TAG_PATTERN}')" in legacy
    assert f"NOT REGEXP_CONTAINS(tag, r'{cal.HEX_COLOUR}')" in legacy
    assert r"#(\w+)" not in cal.EVIDENCE_SQL


def test_both_scans_have_an_upper_date_bound():
    legacy, posts = cal.EVIDENCE_SQL.split("UNION ALL")
    assert "DATE(e.collected_at) >= DATE_SUB(@min_start, INTERVAL 1 DAY)" in legacy
    assert f"DATE(e.collected_at) <= DATE_ADD(@max_end, INTERVAL {cal.LEGACY_COLLECT_DAYS} DAY)" in legacy
    assert cal.LEGACY_COLLECT_DAYS == 22
    assert "e.published_at < TIMESTAMP(DATE_ADD(@max_end, INTERVAL 2 DAY))" in legacy
    assert "p.post_date BETWEEN @min_start AND @max_end" in posts


def test_evidence_params_cover_only_rows_with_an_analogue(plan):
    params = cal.evidence_params(plan)
    dated = [p for p in plan if p["analogue_date"]]
    assert len(params["windows"]) == len(dated)
    assert params["min_start"] == min(p["window_start"] for p in dated).isoformat()
    assert params["max_end"] == max(p["window_end"] for p in dated).isoformat()
    assert params["markets"] == sorted({p["row"]["market"] for p in dated})
    assert params["legacy_markets"] == [m.lower() for m in params["markets"]]
    assert params["top_read"] == cal.TOP_READ
    w = params["windows"][0]
    assert set(w) == {"moment_date", "market", "name", "timezone", "window_start", "window_end"}
    assert w["timezone"] in {"Africa/Johannesburg", "Africa/Lagos", "Africa/Nairobi"}
    typed = {p.name: p for p in cal.query_params(params)}
    assert set(typed) == {"windows", "markets", "legacy_markets", "min_start", "max_end", "top_read"}
    assert typed["min_start"].type_ == "DATE" and typed["min_start"].value == date.fromisoformat(params["min_start"])
    assert len(typed["windows"].values) == len(dated)


# Records ---------------------------------------------------------------------------


BAD = "#" + "".join(["te", "en"]) + "life"


def hit(row, source, label, posts, first_seen="2025-11-22T08:00:00+00:00"):
    return {"moment_date": row["moment_date"], "market": row["market"], "name": row["name"], "source": source,
            "label": label, "posts": posts, "first_seen": first_seen}


def test_records_link_every_row_and_filter_rule_one(rows, plan):
    assert cal.blocked(BAD)
    friday = one(rows, "ZA", "Black Friday")
    found = [hit(friday, "trends_v2_dev.enriched_content", "#blackfriday", 40),
             hit(friday, "trends_v2_dev.enriched_content", BAD, 90),
             hit(friday, "intelligence_42_core.posts", "#takealot", 12)]
    records = cal.analogue_records(plan, found)
    assert [(r["moment_date"], r["market"], r["name"]) for r in records] == [
        (r["moment_date"], r["market"], r["name"]) for r in rows]
    rec = one(records, "ZA", "Black Friday")
    assert rec["status"] == "evidence"
    assert rec["analogue_date"] == date(2025, 11, 28)
    assert (rec["window_start"], rec["window_end"]) == (date(2025, 11, 21), date(2025, 12, 5))
    evidence = json.loads(rec["evidence"])
    assert [e["label"] for e in evidence] == ["#blackfriday", "#takealot"]
    assert evidence[0] == {"source": "trends_v2_dev.enriched_content", "label": "#blackfriday", "posts": 40,
                           "first_seen": "2025-11-22T08:00:00+00:00"}
    assert BAD not in rec["evidence"]
    assert "1 label held back by rule 1" in rec["reason"]
    assert rec["query_text"] == cal.EVIDENCE_SQL
    assert json.loads(rec["query_params"]) == cal.evidence_params(plan)


def test_records_keep_the_top_labels_per_source(rows, plan):
    friday = one(rows, "NG", "Black Friday")
    found = [hit(friday, "intelligence_42_core.posts", f"#tag{i:02d}", 100 - i) for i in range(cal.TOP_KEEP + 5)]
    rec = one(cal.analogue_records(plan, found), "NG", "Black Friday")
    labels = [e["label"] for e in json.loads(rec["evidence"])]
    assert labels == [f"#tag{i:02d}" for i in range(cal.TOP_KEEP)]
    assert rec["reason"] is None


def test_a_window_with_nothing_found_gets_a_no_evidence_record(rows, plan):
    rec = one(cal.analogue_records(plan, []), "KE", "Mashujaa Day")
    assert rec["status"] == "no_evidence"
    assert rec["evidence"] == "[]"
    assert "KE" in rec["reason"] and "2025-10-13" in rec["reason"] and "2025-10-27" in rec["reason"]
    assert rec["query_text"] == cal.EVIDENCE_SQL


def test_only_rule_one_labels_found_is_no_evidence_with_the_reason(rows, plan):
    friday = one(rows, "KE", "Black Friday")
    rec = one(cal.analogue_records(plan, [hit(friday, "intelligence_42_core.posts", BAD, 7)]), "KE", "Black Friday")
    assert rec["status"] == "no_evidence"
    assert "held back by rule 1" in rec["reason"]
    assert BAD not in rec["evidence"]


def test_no_analogue_record_carries_its_reason_and_no_query(plan):
    rec = one(cal.analogue_records(plan, []), "ZA", "Local elections")
    assert rec["status"] == "no_analogue"
    assert rec["analogue_date"] is None and rec["evidence"] is None
    assert rec["query_text"] is None and rec["query_params"] is None
    assert "Local elections" in rec["reason"]


def test_every_empty_record_has_a_status_and_a_reason(plan):
    for rec in cal.analogue_records(plan, []):
        assert rec["status"] in {"evidence", "no_evidence", "no_analogue"}
        assert rec["reason"], rec


# Running it -------------------------------------------------------------------------


class AnalogueClient:
    def __init__(self, dry_bytes=1_000, found=()):
        self.calls = []
        self.dry_bytes = dry_bytes
        self.found = list(found)

    def query(self, sql, job_config=None, location=None):
        self.calls.append((sql, job_config, location))
        job = FakeJob(job_config)
        job.total_bytes_processed = self.dry_bytes
        reading = sql == cal.EVIDENCE_SQL and not job_config.dry_run
        job.result = (lambda: self.found) if reading else (lambda: [])
        return job


def test_run_analogues_dry_runs_caps_reads_then_appends(rows, plan):
    friday = one(rows, "ZA", "Black Friday")
    client = AnalogueClient(found=[hit(friday, "intelligence_42_core.posts", "#blackfriday", 5)])
    assert cal.run_analogues(client, plan) == 0
    sqls = [c[0] for c in client.calls]
    assert sqls == [cal.EVIDENCE_SQL, cal.EVIDENCE_SQL, cal.INSERT_ANALOGUES_SQL, cal.INSERT_ANALOGUES_SQL]
    configs = [c[1] for c in client.calls]
    assert [c.dry_run for c in configs] == [True, False, True, False]
    assert configs[1].maximum_bytes_billed == cal.MAX_BYTES == 5_000_000_000
    assert configs[3].maximum_bytes_billed == cal.MAX_BYTES
    [param] = configs[3].query_parameters
    assert param.name == "rows" and len(param.values) == len(rows)


def test_run_analogues_refuses_a_read_over_the_cap(plan):
    client = AnalogueClient(dry_bytes=cal.MAX_BYTES + 1)
    assert cal.run_analogues(client, plan) == 1
    assert len(client.calls) == 1 and client.calls[0][1].dry_run is True


def test_run_analogues_refuses_when_the_dry_run_reports_no_size(plan):
    client = AnalogueClient(dry_bytes=None)
    assert cal.run_analogues(client, plan) == 1
    assert len(client.calls) == 1


def test_insert_only_appends_to_the_analogue_table():
    sql = cal.INSERT_ANALOGUES_SQL
    assert sql.lstrip().upper().startswith(f"INSERT INTO `{cal.ANALOGUE_TABLE}`".upper())
    assert cal.ANALOGUE_TABLE == "ogilvy-trends-v2.intelligence_42_core.calendar_analogues"
    for word in ("DELETE", "MERGE", "UPDATE", "DROP", "TRUNCATE", "REPLACE"):
        assert not re.search(rf"\b{word}\b", sql, re.I), word
    assert "UNNEST(@rows)" in sql


def test_main_analogues_plan_prints_the_queries_without_network(monkeypatch, capsys):
    def refuse(*a, **k):
        raise AssertionError("network used on a plan")
    monkeypatch.setattr(cal.bigquery, "Client", refuse)
    monkeypatch.setattr(cal.urllib.request, "urlopen", refuse)
    assert cal.main(["--start", "2026-09-28", "--analogues"]) == 0
    out = capsys.readouterr().out
    assert cal.EVIDENCE_SQL.strip() in out
    assert "2026-11-27  ZA  Black Friday  analogue 2025-11-28 (rule), window 2025-11-21 to 2025-12-05" in out
    assert "2026-11-04  ZA  Local elections  no analogue:" in out
    assert '"min_start"' in out and "5,000,000,000" in out
    assert "plan only; no network" in out


def test_core_sql_holds_the_append_only_analogue_table():
    _check_analogue_ddl((HERE.parents[1] / "schema" / "core.sql").read_text(encoding="utf-8"))


def _check_analogue_ddl(sql):
    assert sql.count("calendar_analogues") == 1
    start = sql.index("CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.calendar_analogues`")
    tail = sql[start:sql.index(";", start) + 1]
    for col in ("moment_date DATE NOT NULL", "market STRING NOT NULL", "name STRING NOT NULL", "analogue_date DATE",
                "match_kind STRING", "window_start DATE", "window_end DATE", "status STRING NOT NULL",
                "reason STRING", "evidence JSON", "query_text STRING", "query_params JSON",
                "computed_at TIMESTAMP NOT NULL"):
        assert col in tail, col
    assert "PARTITION BY moment_date" in tail
    assert tail.count("CREATE") == 1
    for word in ("replace", "expiration", "options", "drop", "truncate", "delete"):
        assert word not in tail.lower(), word


# The weekly analogues step (run inside the f42-drift job) ------------------------------


def test_weekly_analogues_builds_from_the_fixtures_dry_runs_and_appends(rows, monkeypatch):
    def refuse_http(*a, **k):
        raise AssertionError("the weekly step reads the fixtures, never date.nager.at")
    monkeypatch.setattr(cal.urllib.request, "urlopen", refuse_http)
    client = AnalogueClient()
    assert cal.weekly_analogues(client, start=START) == 0
    assert [c[0] for c in client.calls] == [cal.EVIDENCE_SQL, cal.EVIDENCE_SQL,
                                            cal.INSERT_ANALOGUES_SQL, cal.INSERT_ANALOGUES_SQL]
    configs = [c[1] for c in client.calls]
    assert [c.dry_run for c in configs] == [True, False, True, False]
    assert configs[1].maximum_bytes_billed == configs[3].maximum_bytes_billed == cal.MAX_BYTES
    [param] = configs[3].query_parameters
    assert len(param.values) == len(rows)


def test_weekly_analogues_refuses_over_the_cap_without_appending():
    client = AnalogueClient(dry_bytes=cal.MAX_BYTES + 1)
    assert cal.weekly_analogues(client, start=START) == 1
    assert [c[1].dry_run for c in client.calls] == [True]
