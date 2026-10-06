"""GDELT daily aggregate, rising entities and the news to social bridge (task 2.5). No network: BigQuery is faked."""
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.collect import gdelt, gdelt_daily as gd

DAY = date(2026, 9, 25)
LATER = datetime(2026, 9, 29, 7, 0, tzinfo=timezone.utc)
SOURCE = Path(gd.__file__).read_text(encoding="utf-8")
CORE_SQL = Path(gd.__file__).resolve().parents[1] / "schema" / "core.sql"
# Joined at run time so the guard hook and the bytecode scans never read them whole.
WRITES = ["INSERT", "MERGE", "UPDATE", "".join(["DEL", "ETE"]), "CREATE", "".join(["DR", "OP"]),
          "".join(["TRUN", "CATE"]), "ALTER", "".join(["REP", "LACE"])]
AGE_PERSON = "".join(["yo", "uth league"])
AGE_THEME = "".join(["SOC_AG", "INGPOPULATION"])


class FakeJob:
    def __init__(self, rows=(), total_bytes_processed=None, affected=0):
        self.rows = list(rows)
        self.total_bytes_processed = total_bytes_processed
        self.num_dml_affected_rows = affected

    def result(self):
        return self.rows


class FakeClient:
    """Answers each of the module's four queries from fixtures and records every call.

    present holds days written under the current market rule, old_rule days written before the rule
    column existed (market_rule NULL); the presence check only sees days under the rule it is asked for."""

    def __init__(self, present=(), rising_rows=(), bridge_rows=(), dry_bytes=200_000_000, written=1234,
                 old_rule=()):
        self.days = {(d, gd.MARKET_RULE) for d in present} | {(d, None) for d in old_rule}
        self.rising_rows = list(rising_rows)
        self.bridge_rows = list(bridge_rows)
        self.dry_bytes = dry_bytes
        self.written = written
        self.calls = []

    def query(self, sql, job_config=None, location=None):
        params = {p.name: p for p in job_config.query_parameters}
        self.calls.append(SimpleNamespace(sql=sql, config=job_config, location=location, params=params))
        if job_config.dry_run:
            return FakeJob(total_bytes_processed=self.dry_bytes if sql == gd.AGGREGATE_SQL else 10_000_000)
        if sql == gd.PRESENT_SQL:
            start, end, rule = params["start"].value, params["end"].value, params["market_rule"].value
            return FakeJob(rows=[{"day": d} for d in sorted({d for d, r in self.days if r == rule})
                                 if start <= d <= end])
        if sql == gd.AGGREGATE_SQL:
            self.days.add((params["day"].value, params["market_rule"].value))
            return FakeJob(affected=self.written)
        if sql == gd.RISING_SQL:
            return FakeJob(rows=self.rising_rows)
        if sql == gd.BRIDGE_SQL:
            return FakeJob(rows=self.bridge_rows)
        raise AssertionError(f"unexpected query {sql[:60]}")

    def live(self, sql=None):
        return [c for c in self.calls if not c.config.dry_run and (sql is None or c.sql == sql)]


def rrow(market, kind, entity, today, baseline, days=28):
    return {"market": market, "entity_kind": kind, "entity": entity, "today_mentions": today,
            "baseline_mentions": baseline, "baseline_days": days}


def words(sql):
    return set(re.findall(r"[A-Z_]+", sql.upper()))


# The aggregate SQL

def test_aggregate_reads_one_gkg_partition_by_parameter():
    sql = gd.AGGREGATE_SQL
    assert sql.count("`gdelt-bq.gdeltv2.gkg_partitioned`") == 1
    assert re.search(r"FROM `gdelt-bq\.gdeltv2\.gkg_partitioned`\s+WHERE _PARTITIONDATE = @day\s", sql)
    assert not re.search(r"DATE '\d", sql) and "_PARTITIONDATE IN" not in sql
    for column in ("Locations", "Persons", "Organizations", "Themes"):
        assert f"d.{column}" in sql
    assert "GCAM" not in sql and "V2" not in sql


def test_aggregate_maps_markets_with_gdelt_codes():
    sql = gd.AGGREGATE_SQL
    for market, fips in gdelt.MARKETS.items():
        assert f"WHEN '{fips}' THEN '{market}'" in sql
    assert "IN ('SF', 'NI', 'KE')" in sql


def test_aggregate_uses_the_shared_market_rule_and_reads_the_outlet_column():
    sql = gd.AGGREGATE_SQL
    assert gdelt.MARKET_SQL in sql
    assert re.search(r"SELECT Locations, Persons, Organizations, Themes, SourceCommonName\s+FROM `gdelt-bq", sql)
    assert "DocumentIdentifier" not in sql
    assert "SELECT DISTINCT SPLIT(loc, '#')[SAFE_OFFSET(2)]" not in sql


def test_aggregate_is_an_append_only_insert_guarded_against_a_second_run():
    sql = gd.AGGREGATE_SQL
    assert sql.lstrip().startswith(
        "INSERT INTO `ogilvy-trends-v2.intelligence_42_core.gdelt_daily` "
        "(day, market, entity_kind, entity, mentions, computed_at, market_rule)\n"
        "SELECT @day, CASE fips ")
    assert re.search(r"COUNT\(\*\), CURRENT_TIMESTAMP\(\), @market_rule\nFROM \(", sql)
    assert re.search(r"NOT EXISTS \(\s*SELECT 1 FROM `ogilvy-trends-v2\.intelligence_42_core\.gdelt_daily` g\s+"
                     r"WHERE g\.day = @day AND g\.market_rule = @market_rule\)", sql)
    upper = sql.upper()
    for word in WRITES:
        if word != "INSERT":
            assert not re.search(rf"\b{word}\b", upper), word
    assert upper.count("INSERT") == 1


def test_aggregate_applies_the_theme_families_and_rule_one_words():
    sql = gd.AGGREGATE_SQL
    assert "UNNEST(@dropped_themes)" in sql and "UNNEST(@rule_one)" in sql
    assert "'RELIGION'" in sql and "STARTS_WITH(w, 'ETHNIC')" in sql
    params = gd.aggregate_params(DAY)
    assert params["day"] == ("DATE", DAY)
    assert params["market_rule"] == ("STRING", gd.MARKET_RULE)
    assert params["dropped_themes"] == ("ARRAY<STRING>", list(gdelt.DROPPED_THEMES))
    assert params["rule_one"] == ("ARRAY<STRING>", sorted(gdelt.RULE_ONE))


# Aggregate runs: dry run first, the cap, the skip

def test_aggregate_dry_runs_then_inserts_capped():
    client = FakeClient()
    result = gd.aggregate(client, DAY, now=LATER)
    assert result == {"day": DAY, "status": "written", "rows": 1234}
    assert [(c.sql == gd.AGGREGATE_SQL, c.config.dry_run) for c in client.calls] == [
        (False, True), (False, False), (True, True), (True, False)]
    insert = client.live(gd.AGGREGATE_SQL)[0]
    assert insert.config.maximum_bytes_billed == gdelt.MAX_BYTES
    assert insert.params["day"].type_ == "DATE" and insert.params["day"].value == DAY
    assert all(c.location == "US" for c in client.calls)


def test_aggregate_skips_a_day_already_present():
    client = FakeClient(present={DAY})
    assert gd.aggregate(client, DAY, now=LATER) == {"day": DAY, "status": "present", "rows": 0}
    assert not any(c.sql == gd.AGGREGATE_SQL for c in client.calls)


# The market rule marker: rows written under the old any-mention rule carry NULL and are never read

def test_market_rule_is_a_named_constant_for_the_dominant_or_outlet_rule():
    assert gd.MARKET_RULE == "dominant_or_outlet_v2"


def test_every_read_of_gdelt_daily_filters_on_the_market_rule():
    table = "`ogilvy-trends-v2.intelligence_42_core.gdelt_daily`"
    reads = 0
    for name in ("AGGREGATE_SQL", "PRESENT_SQL", "RISING_SQL", "BRIDGE_SQL"):
        sql = getattr(gd, name)
        for m in re.finditer(re.escape(f"FROM {table} g") + r"\s+WHERE ([^\n]*(?:\n\s+AND [^\n]*)*)", sql):
            reads += 1
            assert "g.market_rule = @market_rule" in m.group(1), name
        # any other mention of the table is the INSERT target, never a read
        others = sql.count(table) - len(re.findall(re.escape(f"FROM {table} g"), sql))
        assert others == (1 if name == "AGGREGATE_SQL" else 0), name
    assert reads == 3


def test_every_query_on_gdelt_daily_binds_the_rule():
    assert gd.present_params(DAY, DAY)["market_rule"] == ("STRING", gd.MARKET_RULE)
    assert gd.aggregate_params(DAY)["market_rule"] == ("STRING", gd.MARKET_RULE)
    assert gd.rising_params(DAY)["market_rule"] == ("STRING", gd.MARKET_RULE)
    client = FakeClient(rising_rows=[rrow("ZA", "person", "Cyril Ramaphosa", 40, 56)])
    gd.aggregate(client, DAY, now=LATER)
    gd.rising(client, DAY)
    for sql in (gd.PRESENT_SQL, gd.AGGREGATE_SQL, gd.RISING_SQL):
        calls = client.live(sql)
        assert calls, sql[:40]
        for call in calls:
            assert call.params["market_rule"].type_ == "STRING", sql[:40]
            assert call.params["market_rule"].value == gd.MARKET_RULE, sql[:40]


def test_a_day_present_under_the_old_rule_is_written_again_under_the_new_one():
    client = FakeClient(old_rule={DAY})
    assert gd.aggregate(client, DAY, now=LATER) == {"day": DAY, "status": "written", "rows": 1234}
    (insert,) = client.live(gd.AGGREGATE_SQL)
    assert insert.params["market_rule"].value == gd.MARKET_RULE
    assert client.live(gd.PRESENT_SQL)[0].params["market_rule"].value == gd.MARKET_RULE


def test_backfill_refills_the_old_rule_days_and_skips_only_new_rule_ones():
    first = DAY - timedelta(days=28)
    old = {first + timedelta(days=i) for i in range(29)}
    client = FakeClient(old_rule=old, present={DAY})
    results = gd.backfill(client, DAY, now=LATER)
    assert [r["status"] for r in results] == ["written"] * 28 + ["present"]
    inserted = [c.params["day"].value for c in client.live(gd.AGGREGATE_SQL)]
    assert inserted == sorted(old - {DAY})


def test_rising_reads_only_rows_under_the_rule_for_the_day_and_the_baseline():
    sql = gd.RISING_SQL
    w = sql[:sql.index("),\nb AS")]
    assert re.search(r"WHERE g\.day BETWEEN DATE_SUB\(@day, INTERVAL 28 DAY\) AND @day\s+"
                     r"AND g\.market_rule = @market_rule$", w)
    # the baseline day count and both sums read w only, so NULL-rule rows reach neither
    assert "b AS (SELECT COUNT(DISTINCT w.day) baseline_days FROM w WHERE w.day < @day)" in sql
    assert "FROM w CROSS JOIN b" in sql
    assert sql.count("gdelt_daily`") == 1


def test_aggregate_refuses_above_the_cap_before_anything_is_billed():
    client = FakeClient(dry_bytes=gdelt.MAX_BYTES + 1)
    with pytest.raises(gdelt.OverCap):
        gd.aggregate(client, DAY, now=LATER)
    assert client.live(gd.AGGREGATE_SQL) == []


def test_aggregate_refuses_when_the_dry_run_reports_no_size():
    client = FakeClient(dry_bytes=None)
    with pytest.raises(gdelt.OverCap):
        gd.aggregate(client, DAY, now=LATER)
    assert client.live(gd.AGGREGATE_SQL) == []


def test_aggregate_refuses_a_utc_day_still_filling():
    client = FakeClient()
    with pytest.raises(gd.PartialDay):
        gd.aggregate(client, DAY, now=datetime(2026, 9, 26, 0, 30, tzinfo=timezone.utc))
    assert client.calls == []
    gd.aggregate(client, DAY, now=datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc))


def test_backfill_runs_one_day_at_a_time_oldest_first_and_skips_present_days():
    first = DAY - timedelta(days=28)
    present = {first + timedelta(days=i) for i in (0, 5, 28)}
    client = FakeClient(present=present)
    results = gd.backfill(client, DAY, now=LATER)
    assert [r["day"] for r in results] == [first + timedelta(days=i) for i in range(29)]
    assert [r["status"] for r in results].count("present") == 3
    inserted = [c.params["day"].value for c in client.live(gd.AGGREGATE_SQL)]
    assert inserted == sorted(inserted) and len(inserted) == 26 and not set(inserted) & present
    presence = client.live(gd.PRESENT_SQL)
    assert len(presence) == 1
    assert presence[0].params["start"].value == first and presence[0].params["end"].value == DAY


def test_backfill_stops_at_the_cap():
    client = FakeClient(dry_bytes=gdelt.MAX_BYTES + 1)
    with pytest.raises(gdelt.OverCap):
        gd.backfill(client, DAY, now=LATER)
    assert client.live(gd.AGGREGATE_SQL) == []


# Rising: the 28-day baseline

def test_rising_sql_reads_only_gdelt_daily_over_the_day_and_28_before():
    sql = gd.RISING_SQL
    assert re.findall(r"`([^`]+)`", sql) == ["ogilvy-trends-v2.intelligence_42_core.gdelt_daily"]
    assert "g.day BETWEEN DATE_SUB(@day, INTERVAL 28 DAY) AND @day" in sql
    assert "COUNT(DISTINCT w.day)" in sql and "w.day < @day" in sql
    assert "HAVING today_mentions >= @min_count" in sql
    assert not words(sql) & set(WRITES)


def test_baseline_mean_divides_by_the_days_present_not_by_28():
    rows = [rrow("ZA", "person", "a", 30, 40, days=20)]
    found = gd.rank(rows)
    top = found["ZA"][0]
    assert top["baseline_mean"] == pytest.approx(2.0)
    assert top["score"] == pytest.approx(31 / 3)
    assert top["today"] == 30 and top["baseline_mentions"] == 40 and top["baseline_days"] == 20


def test_rank_orders_by_score_and_applies_the_floors():
    rows = [
        rrow("ZA", "person", "steady", 40, 28 * 40),   # ratio about 1
        rrow("ZA", "person", "doubling", 20, 28 * 10),  # 21/11
        rrow("ZA", "person", "brand new", 12, 0),       # 13
        rrow("ZA", "organisation", "big club", 30, 28 * 5),  # 31/6
        rrow("ZA", "person", "tiny", 4, 0),             # under the count floor
    ]
    found = gd.rank(rows)
    assert [e["label"] for e in found["ZA"]] == ["brand new", "big club", "doubling"]
    assert found["ZA"][2]["score"] == pytest.approx(21 / 11)


def test_rank_ties_break_on_mentions_then_name():
    rows = [rrow("NG", "person", "b", 10, 0), rrow("NG", "person", "a", 10, 0), rrow("NG", "place", "c", 21, 28)]
    assert [e["label"] for e in gd.rank(rows)["NG"]] == ["c", "a", "b"]


def test_rank_keeps_the_top_n_per_market_and_markets_apart():
    rows = [rrow("ZA", "person", f"za {i:02}", 100 - i, 0) for i in range(15)]
    rows += [rrow("KE", "person", f"ke {i}", 50 - i, 0) for i in range(3)]
    found = gd.rank(rows)
    assert gd.TOP_N == 10
    assert [e["label"] for e in found["ZA"]] == [f"za {i:02}" for i in range(10)]
    assert [e["label"] for e in found["KE"]] == ["ke 0", "ke 1", "ke 2"]
    assert all(e["market"] == "KE" for e in found["KE"])


def test_rank_needs_a_baseline_of_14_days():
    assert gd.MIN_BASELINE_DAYS == 14
    assert gd.rank([rrow("ZA", "person", "a", 30, 0, days=13)]) == {}
    assert gd.rank([rrow("ZA", "person", "a", 30, 0, days=14)])["ZA"]


def test_theme_variants_share_one_label_and_sum():
    rows = [rrow("KE", "theme", "WB_2433_CONFLICT_AND_VIOLENCE", 6, 28),
            rrow("KE", "theme", "CONFLICT_AND_VIOLENCE", 6, 28)]
    (entry,) = gd.rank(rows)["KE"]
    assert entry["label"] == "conflict and violence"
    assert entry["today"] == 12 and entry["baseline_mentions"] == 56
    assert entry["entities"] == ["CONFLICT_AND_VIOLENCE", "WB_2433_CONFLICT_AND_VIOLENCE"]


# Rule 1 and the POPIA exclusions

def test_rank_drops_rule_one_entities_and_dropped_theme_families():
    rows = [
        rrow("ZA", "person", AGE_PERSON, 50, 0),
        rrow("ZA", "theme", AGE_THEME, 50, 0),
        rrow("ZA", "theme", "TAX_RELIGION_ANY", 50, 0),
        rrow("ZA", "theme", "TAX_ETHNICITY_ANY", 50, 0),
        rrow("ZA", "theme", "TAX_FNCACT_ANY", 50, 0),
        rrow("ZA", "theme", "SOC_ETHNICVIOLENCE", 50, 0),
        rrow("ZA", "place", "Durban", 50, 0),
    ]
    assert [e["label"] for e in gd.rank(rows)["ZA"]] == ["Durban"]


def test_rank_shows_a_theme_only_when_it_is_a_gkg_code():
    rows = [rrow("ZA", "theme", "Kohli", 50, 0), rrow("ZA", "theme", "naman", 50, 0),
            rrow("ZA", "theme", "farsightedness", 50, 0), rrow("ZA", "theme", "TAX_DISEASE_MALARIA", 50, 0),
            rrow("ZA", "person", "Kohli", 50, 0)]
    assert [(e["entity_kind"], e["label"]) for e in gd.rank(rows)["ZA"]] == [("person", "Kohli"),
                                                                                ("theme", "malaria")]


def test_rank_asks_gdelt_blocked_about_the_raw_entity_and_its_label(monkeypatch):
    asked = []
    monkeypatch.setattr(gdelt, "blocked", lambda text: asked.append(text) or text == "hidden")
    rows = [rrow("ZA", "theme", "ECON_HIDDEN", 9, 0), rrow("NG", "person", "Burna Boy", 9, 0)]
    found = gd.rank(rows)
    assert "ECON_HIDDEN" in asked and "hidden" in asked and "Burna Boy" in asked
    assert "ZA" not in found and found["NG"][0]["label"] == "Burna Boy"


def test_bridge_never_asks_about_a_blocked_entity():
    client = FakeClient(rising_rows=[rrow("ZA", "person", AGE_PERSON, 50, 0)])
    found = gd.rising(client, DAY)
    assert found["markets"] == {}
    assert gd.bridge(client, DAY, found)["markets"] == {}
    assert client.live(gd.BRIDGE_SQL) == []


# Rising result carries its query (rule 5)

def test_rising_returns_the_query_and_params_with_the_numbers():
    client = FakeClient(rising_rows=[rrow("ZA", "person", "Cyril Ramaphosa", 40, 56)])
    found = gd.rising(client, DAY)
    assert found["query"] == gd.RISING_SQL
    assert found["params"] == {"day": ("DATE", DAY), "min_count": ("INT64", gdelt.MIN_COUNT),
                               "market_rule": ("STRING", gd.MARKET_RULE)}
    assert found["market_rule"] == gd.MARKET_RULE
    assert found["day"] == DAY and found["baseline_days"] == 28
    entry = found["markets"]["ZA"][0]
    assert entry["today"] == 40 and entry["score"] == pytest.approx(41 / 3)
    call = client.live(gd.RISING_SQL)[0]
    assert call.params["day"].value == DAY and call.config.maximum_bytes_billed == gdelt.MAX_BYTES


# The bridge

def test_phrase_and_tag_are_word_forms_of_the_label():
    assert gd.phrase("Cyril  Ramaphosa") == "cyril ramaphosa"
    assert gd.phrase("Kaizer Chiefs F.C.") == "kaizer chiefs f c"
    assert gd.tag("Cyril Ramaphosa") == "cyrilramaphosa"
    assert gd.tag("Côte d'Ivoire") == "côtedivoire"


def test_bridge_sql_is_read_only_and_joins_42_posts_in_the_market():
    sql = gd.BRIDGE_SQL
    assert sorted(set(re.findall(r"`([^`]+)`", sql))) == [
        "ogilvy-trends-v2.intelligence_42_core.post_observations",
        "ogilvy-trends-v2.intelligence_42_core.posts"]
    assert sql.lstrip().startswith("WITH")
    assert not words(sql) & set(WRITES)
    assert "m.market = e.market" in sql and "po.lane_class != 'legacy'" in sql
    assert "e.tag IN UNNEST(m.tags)" in sql and "STRPOS(m.words, CONCAT(' ', e.phrase, ' ')) > 0" in sql


def test_bridge_window_is_the_one_to_three_days_after_the_news_day():
    sql = gd.BRIDGE_SQL
    assert "po.observed_date BETWEEN DATE_ADD(@day, INTERVAL 1 DAY) AND DATE_ADD(@day, INTERVAL 3 DAY)" in sql
    assert "ps.post_date BETWEEN @day AND DATE_ADD(@day, INTERVAL 3 DAY)" in sql
    assert gd.BRIDGE_DAYS == (1, 3)


def test_bridge_reports_follow_through_per_entity():
    rising_rows = [rrow("ZA", "person", "Cyril Ramaphosa", 40, 56), rrow("ZA", "place", "Soweto", 12, 0),
                   rrow("KE", "organisation", "Safaricom", 20, 28)]
    bridge_rows = [
        {"market": "ZA", "entity_kind": "person", "phrase": "cyril ramaphosa", "posts": 17, "creators": 9,
         "platforms": 3, "first_day": DAY + timedelta(days=1), "evidence_post_ids": ["p1", "p2"]},
        {"market": "ZA", "entity_kind": "place", "phrase": "soweto", "posts": 0, "creators": 0,
         "platforms": 0, "first_day": None, "evidence_post_ids": []},
        {"market": "KE", "entity_kind": "organisation", "phrase": "safaricom", "posts": 4, "creators": 4,
         "platforms": 1, "first_day": DAY + timedelta(days=3), "evidence_post_ids": ["k1"]},
    ]
    client = FakeClient(rising_rows=rising_rows, bridge_rows=bridge_rows)
    found = gd.rising(client, DAY)
    out = gd.bridge(client, DAY, found)
    assert out["query"] == gd.BRIDGE_SQL and out["params"]["day"] == ("DATE", DAY)
    assert out["market_rule"] == gd.MARKET_RULE
    za = {e["label"]: e for e in out["markets"]["ZA"]}
    assert za["Cyril Ramaphosa"]["follow"] == {"posts": 17, "creators": 9, "platforms": 3, "lag_days": 1,
                                               "evidence_post_ids": ["p1", "p2"]}
    assert za["Soweto"]["follow"]["posts"] == 0 and za["Soweto"]["follow"]["lag_days"] is None
    assert out["markets"]["KE"][0]["follow"]["lag_days"] == 3
    call = client.live(gd.BRIDGE_SQL)[0]
    assert call.params["day"].value == DAY and call.config.maximum_bytes_billed == gdelt.MAX_BYTES
    assert [s.struct_values for s in call.params["entities"].values] == gd.bridge_entities(found)


def test_bridge_entities_parameter_carries_every_rising_entity():
    client = FakeClient(rising_rows=[rrow("ZA", "person", "Cyril Ramaphosa", 40, 56),
                                     rrow("NG", "place", "Lagos", 30, 28)])
    found = gd.rising(client, DAY)
    assert gd.bridge_entities(found) == [
        {"market": "NG", "entity_kind": "place", "phrase": "lagos", "tag": "lagos"},
        {"market": "ZA", "entity_kind": "person", "phrase": "cyril ramaphosa", "tag": "cyrilramaphosa"},
    ]


# The table

def test_gdelt_daily_is_created_once_in_core_sql_partitioned_by_day_with_no_expiry():
    text = CORE_SQL.read_text(encoding="utf-8")
    head = "CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.gdelt_daily` ("
    assert text.count(head) == 1
    start = text.index(head)
    last = text[start:text.index(";", start) + 1]
    body = " ".join(last.split())
    for column in ("day DATE NOT NULL", "market STRING NOT NULL", "entity_kind STRING NOT NULL",
                   "entity STRING NOT NULL", "mentions INT64", "computed_at TIMESTAMP"):
        assert column in body
    assert "computed_at TIMESTAMP, market_rule STRING)" in body
    assert body.endswith("PARTITION BY day CLUSTER BY market, entity_kind;")
    assert "OPTIONS" not in last and "expir" not in last.lower()


def test_gdelt_daily_gains_market_rule_by_an_additive_alter_after_its_create():
    text = CORE_SQL.read_text(encoding="utf-8")
    alter = ("ALTER TABLE `ogilvy-trends-v2.intelligence_42_core.gdelt_daily`\n"
             "ADD COLUMN IF NOT EXISTS market_rule STRING;")
    assert text.count(alter) == 1
    create = "CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.gdelt_daily`"
    assert text.index(create) < text.index(alter)


# Rules 1 and 2 in the source

def test_no_banned_literals_in_the_module_or_its_sql():
    parts = [["google", "_trends"], ["google", "trends"], ["google", " trends"], ["ge", "n z"], ["ge", "nz"],
             ["ge", "n_z"], ["you", "th"], ["tee", "n"], ["millen", "nial"], ["gener", "ation"]]
    banned = ["".join(p) for p in parts]
    for text in (SOURCE, gd.AGGREGATE_SQL, gd.PRESENT_SQL, gd.RISING_SQL, gd.BRIDGE_SQL):
        lower = text.lower()
        for word in banned:
            assert word not in lower, word
        assert not re.search(r"\bages?\b", lower)


# CLI

def test_plan_prints_every_query_without_building_a_client(monkeypatch, capsys):
    def no_client(*args, **kwargs):
        raise AssertionError("--plan must not build a BigQuery client")

    monkeypatch.setattr(gd.bigquery, "Client", no_client)
    assert gd.main(["--plan", "--day", "2026-09-25"]) == 0
    out = capsys.readouterr().out
    for sql in (gd.AGGREGATE_SQL, gd.PRESENT_SQL, gd.RISING_SQL, gd.BRIDGE_SQL):
        assert sql in out
    assert "day: DATE 2026-09-25" in out
    assert out.count(f"market_rule: STRING {gd.MARKET_RULE}") == 3
    assert f"rule_one: ARRAY<STRING> of {len(gdelt.RULE_ONE)} values" in out
    assert "5,000,000,000" in out and "not estimated under --plan" in out


def test_apply_aggregates_then_prints_rising_and_bridge(monkeypatch, capsys):
    client = FakeClient(
        present={DAY - timedelta(days=i) for i in range(1, 29)},
        rising_rows=[rrow("ZA", "person", "Cyril Ramaphosa", 40, 56)],
        bridge_rows=[{"market": "ZA", "entity_kind": "person", "phrase": "cyril ramaphosa", "posts": 17,
                      "creators": 9, "platforms": 3, "first_day": DAY + timedelta(days=2),
                      "evidence_post_ids": ["p1"]}])
    monkeypatch.setattr(gd.bigquery, "Client", lambda project=None: client)
    monkeypatch.setattr(gd, "_utc_now", lambda: LATER)
    assert gd.main(["--apply", "--day", "2026-09-25"]) == 0
    out = capsys.readouterr().out
    order = [c.sql for c in client.live()]
    assert order.index(gd.AGGREGATE_SQL) < order.index(gd.RISING_SQL) < order.index(gd.BRIDGE_SQL)
    assert "Cyril Ramaphosa" in out and "17 posts" in out and "lag 2 days" in out
    assert f"market rule {gd.MARKET_RULE}" in out and f"market_rule={gd.MARKET_RULE}" in out


def test_apply_reports_a_refusal_at_the_cap(monkeypatch, capsys):
    client = FakeClient(dry_bytes=gdelt.MAX_BYTES + 1)
    monkeypatch.setattr(gd.bigquery, "Client", lambda project=None: client)
    monkeypatch.setattr(gd, "_utc_now", lambda: LATER)
    assert gd.main(["--apply", "--day", "2026-09-25"]) == 1
    assert "refused" in capsys.readouterr().out
    assert client.live(gd.AGGREGATE_SQL) == []
