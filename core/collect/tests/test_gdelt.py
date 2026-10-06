"""GDELT seed generator (task 2.5, collect side). No network: BigQuery and the item functions are faked."""
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.collect import gdelt
from core.collect.socialcrawl_client import quote_for

RUN = date(2026, 9, 29)
SOURCE = Path(gdelt.__file__).read_text(encoding="utf-8")
FIXTURES = Path(__file__).parent / "fixtures"

# Rule 1 words from the reviewer's list, matched as whole words after splitting on underscores. The two
# joined entries cover the two phrases written as two words; joined at run time for the bytecode scan.
RULE_ONE_WORDS = set("""
youth youths child children childhood kid kids teen teens teenage teenager teenagers adolescent adolescence
infant infants baby babies elder elders elderly ageing aging aged age millennial millennials generation
generations pension pensions pensioner pensioners pensionary student students pupil pupils school schools
minor minors juvenile orphan orphans senior seniors girl girls boy boys
young younger newborn newborns generational toddler toddlers retiree retirees retirement
""".split()) | {"".join(["ge", "nz"]), "".join(["old", "age"])}
# Codes whose rule 1 word is fused into a longer token, so a whole-word split alone would miss them.
FUSED = {
    "SOC_AGINGPOPULATION", "SOC_CHILDSOLDIER", "SOC_POINTSOFINTEREST_HIGHSCHOOL",
    "SOC_POINTSOFINTEREST_MIDDLESCHOOL", "SOC_POINTSOFINTEREST_PRESCHOOL", "SOC_POINTSOFINTEREST_PRESCHOOLS",
    "TAX_AIDGROUPS_CHILDFUND", "TAX_DISEASE_AGERELATED_MACULAR_DEGENERATION", "WB_1585_PRESCHOOL_EDUCATION",
    "TAX_DISEASE_INFANTILE_CEREBRAL_PALSY",
}
DROPPED_FAMILIES = ("TAX_ETHNICITY", "TAX_RELIGION", "TAX_FNCACT")
STILL_SEEDS = {
    "ECON_BOYCOTT", "ECON_ELECTRICALGENERATION", "KIDNAP", "TAX_DISEASE_ACUTE_KIDNEY_INJURY",
    "TAX_DISEASE_KIDNEY_DISEASE", "TAX_DISEASE_KIDNEY_FAILURE", "TAX_DISEASE_KIDNEY_STONES",
    "TAX_DISEASE_MACULAR_DEGENERATION", "TAX_WORLDLANGUAGES_BUKIDNON", "TAX_WORLDLANGUAGES_KIDAL",
    "WB_1835_URBAN_REGENERATION", "WB_2889_MINORITIES_AND_DISENFRANCHISED_GROUPS_AND_JOBS",
    "WB_716_MANAGING_PUBLIC_FINANCES",
}


def expected_blocked(code):
    tokens = code.lower().split("_")
    pairs = {a + b for a, b in zip(tokens, tokens[1:])}
    special = "religion" in tokens or any(t.startswith("ethnic") for t in tokens)
    return (bool(RULE_ONE_WORDS & (set(tokens) | pairs)) or code in FUSED or code.startswith(DROPPED_FAMILIES)
            or special)


class FakeIds:
    """Stands in for core.detect.items and records every call."""

    def __init__(self):
        self.calls = []

    def canonical_key(self, kind, raw, platform=None):
        self.calls.append(("canonical_key", kind, raw))
        key = " ".join(str(raw).casefold().split())
        if not key:
            raise ValueError("empty")
        return key

    def item_id(self, kind, key):
        self.calls.append(("item_id", kind, key))
        return f"id[{kind}|{key}]"


class FakeJob:
    def __init__(self, rows=(), total_bytes_processed=None, affected=0):
        self.rows = list(rows)
        self.total_bytes_processed = total_bytes_processed
        self.num_dml_affected_rows = affected

    def result(self):
        return self.rows


class FakeClient:
    def __init__(self, rows=(), dry_bytes=1_000_000_000, merge_dry_bytes=10_000):
        self.rows = rows
        self.dry_bytes = dry_bytes
        self.merge_dry_bytes = merge_dry_bytes
        self.calls = []

    def query(self, sql, job_config=None, location=None):
        self.calls.append(SimpleNamespace(sql=sql, config=job_config, location=location))
        is_merge = sql.lstrip().startswith("MERGE")
        if job_config.dry_run:
            return FakeJob(total_bytes_processed=self.merge_dry_bytes if is_merge else self.dry_bytes)
        if is_merge:
            return FakeJob(affected=len(job_config.query_parameters[0].values))
        return FakeJob(rows=self.rows)


def row(fips, kind, entity, today, baseline):
    return {"fips": fips, "entity_kind": kind, "entity": entity,
            "today_count": today, "baseline_count": baseline}


def rising(fips, n, start=0):
    """n rising people in one market, score falling with the index."""
    return [row(fips, "person", f"person {start + i}", 100 - i, 4) for i in range(n)]


# SQL shape

def test_read_sql_scans_only_the_news_day_and_four_weekly_baseline_partitions():
    sql = gdelt.read_sql(RUN)
    assert "`gdelt-bq.gdeltv2.gkg_partitioned`" in sql
    days = "DATE '2026-09-28', DATE '2026-09-21', DATE '2026-09-14', DATE '2026-09-07', DATE '2026-08-31'"
    assert re.search(r"FROM `gdelt-bq\.gdeltv2\.gkg_partitioned`\s+WHERE _PARTITIONDATE IN \(" + re.escape(days) + r"\)", sql)
    assert sql.count("gdelt-bq.") == 1
    assert "_PARTITIONDATE = DATE '2026-09-28' AS is_today" in sql


def test_read_sql_reads_market_codes_and_the_count_floor():
    sql = gdelt.read_sql(RUN)
    assert "('SF', 'NI', 'KE')" in sql
    assert f"HAVING today_count >= {gdelt.MIN_COUNT}" in sql
    for column in ("Locations", "Persons", "Organizations", "Themes"):
        assert f"d.{column}" in sql
    assert "GCAM" not in sql and "V2" not in sql


def test_read_sql_is_a_select_with_no_dml():
    sql = gdelt.read_sql(RUN).upper()
    for word in ("INSERT", "UPDATE", "DELETE", "MERGE", "CREATE", "DROP", "TRUNCATE", "ALTER", "REPLACE"):
        assert not re.search(rf"\b{word}\b", sql), word


def test_the_only_write_is_an_insert_only_merge_into_seed_queue():
    sql = gdelt.MERGE_SQL
    tables = re.findall(r"`([^`]+)`", sql)
    assert tables == ["ogilvy-trends-v2.intelligence_42_core.seed_queue"]
    assert re.search(r"ON t\.seed_date = s\.seed_date AND t\.market = s\.market AND t\.item_id = s\.item_id"
                     r"\s+AND t\.seed_date = @seed_date\s", sql)
    assert "WHEN NOT MATCHED THEN" in sql and "WHEN MATCHED" not in sql
    for word in ("UPDATE", "DELETE", "TRUNCATE", "DROP", "REPLACE"):
        assert not re.search(rf"\b{word}\b", sql.upper()), word


# Dry run first, refusal above 5 GB

def test_run_dry_runs_first_and_refuses_above_the_cap():
    client = FakeClient(rows=rising("SF", 5), dry_bytes=gdelt.MAX_BYTES + 1)
    with pytest.raises(gdelt.OverCap):
        gdelt.run(client, RUN, ids=FakeIds())
    assert len(client.calls) == 1
    assert client.calls[0].config.dry_run is True


def test_run_refuses_when_the_dry_run_reports_no_bytes():
    client = FakeClient(rows=rising("SF", 5), dry_bytes=None)
    with pytest.raises(gdelt.OverCap):
        gdelt.run(client, RUN, ids=FakeIds())
    assert len(client.calls) == 1


def test_run_caps_every_live_query_and_dry_runs_the_merge():
    client = FakeClient(rows=rising("SF", 5) + rising("KE", 3), dry_bytes=gdelt.MAX_BYTES)
    written = gdelt.run(client, RUN, ids=FakeIds())
    dry_flags = [c.config.dry_run for c in client.calls]
    assert dry_flags == [True, False, True, False]
    assert client.calls[0].sql == client.calls[1].sql == gdelt.read_sql(RUN)
    assert client.calls[2].sql == client.calls[3].sql == gdelt.MERGE_SQL
    for call in (client.calls[1], client.calls[3]):
        assert call.config.maximum_bytes_billed == gdelt.MAX_BYTES
    for call in client.calls[2:]:
        prune = [p for p in call.config.query_parameters if p.name == "seed_date"]
        assert len(prune) == 1 and prune[0].type_ == "DATE" and prune[0].value == RUN
    assert all(c.location == "US" for c in client.calls)
    assert written == 8


def test_run_with_nothing_rising_writes_nothing():
    client = FakeClient(rows=[])
    assert gdelt.run(client, RUN, ids=FakeIds()) == 0
    assert [c.config.dry_run for c in client.calls] == [True, False]


def test_max_bytes_is_five_gigabytes():
    assert gdelt.MAX_BYTES == 5_000_000_000


# Ranking, floors and exploration

def test_ranking_is_by_ratio_against_the_baseline_mean():
    rows = [
        row("SF", "person", "steady", 40, 160),        # mean 40, ratio about 1
        row("SF", "person", "doubling", 20, 40),       # mean 10, ratio 21/11
        row("SF", "person", "brand new", 12, 0),       # mean 0, ratio 13
        row("SF", "organisation", "big club", 30, 20),  # mean 5, ratio 31/6
    ]
    seeds = gdelt.seeds(rows, RUN, ids=FakeIds())
    assert [s["query"] for s in seeds] == ["brand new", "big club", "doubling"]
    assert all(s["lane"] == "expansion" for s in seeds)
    assert seeds[0]["priority"] == pytest.approx(13.0)
    assert seeds[2]["priority"] == pytest.approx(21 / 11, rel=1e-3)


def test_count_floor_drops_small_spikes_even_with_a_huge_ratio():
    rows = [row("SF", "person", "tiny", gdelt.MIN_COUNT - 1, 0), row("SF", "person", "enough", gdelt.MIN_COUNT, 0)]
    assert [s["query"] for s in gdelt.seeds(rows, RUN, ids=FakeIds())] == ["enough"]


def test_top_twenty_per_market_and_exploration_from_below_the_cut():
    rows = rising("SF", 40) + rising("NI", 40, start=100)
    seeds = gdelt.seeds(rows, RUN, ids=FakeIds())
    for market, start in (("ZA", 0), ("NG", 100)):
        mine = [s for s in seeds if s["market"] == market]
        expansion = [s for s in mine if s["lane"] == "expansion"]
        exploration = [s for s in mine if s["lane"] == "exploration"]
        assert [s["query"] for s in expansion] == [f"person {start + i}" for i in range(gdelt.TOP_N)]
        assert len(exploration) == 3
        assert len(exploration) <= 0.15 * len(expansion)
        below = {f"person {start + i}" for i in range(gdelt.TOP_N, 40)}
        assert {s["query"] for s in exploration} <= below


def test_exploration_is_repeatable_for_a_run_date_and_shrinks_with_the_list():
    rows = rising("SF", 40)
    first = gdelt.seeds(rows, RUN, ids=FakeIds())
    again = gdelt.seeds(rows, RUN, ids=FakeIds())
    assert first == again
    few = gdelt.seeds(rising("SF", 10), RUN, ids=FakeIds())
    assert sum(s["lane"] == "exploration" for s in few) <= 0.15 * sum(s["lane"] == "expansion" for s in few)


def test_exploration_takes_what_is_there_when_little_is_below_the_cut():
    seeds = gdelt.seeds(rising("SF", 21), RUN, ids=FakeIds())
    assert sum(s["lane"] == "exploration" for s in seeds) == 1


def test_a_market_never_crosses_into_another():
    seeds = gdelt.seeds(rising("KE", 3), RUN, ids=FakeIds())
    assert {s["market"] for s in seeds} == {"KE"}
    assert gdelt.MARKETS == {"ZA": "SF", "NG": "NI", "KE": "KE"}


# Market attribution: the dominant location or a local outlet, on V1 Locations from the 26 September staging run

def loc(kind, name, fips):
    return f"{kind}#{name}#{fips}#{fips}#0#0#{fips}"


INDIA_CRICKET = ";".join([loc(4, "Thiruvananthapuram, Kerala, India", "IN"), loc(2, "Kerala, India", "IN"),
                          loc(1, "India", "IN"), loc(1, "South Africa", "SF")])
CHICAGO_CLIMATE = ";".join([loc(3, "Chicago, Illinois, United States", "US"), loc(2, "Illinois, United States", "US"),
                            loc(1, "United States", "US"), loc(1, "Nigeria", "NI")])
FRENCH_POLITICS = ";".join([loc(4, "Paris, Ile-De-France, France", "FR"), loc(1, "France", "FR"),
                            loc(1, "Nigeria", "NI")])
INDIA_FOREIGN = ";".join([loc(4, "New Delhi, Delhi, India", "IN"), loc(1, "India", "IN"), loc(1, "Kenya", "KE")])
MOSTLY_KENYA = ";".join([loc(4, "Nairobi, Nairobi Area, Kenya", "KE"), loc(4, "Mombasa, Coast, Kenya", "KE"),
                         loc(1, "Kenya", "KE"), loc(1, "India", "IN")])
LONDON = ";".join([loc(4, "London, London, City of, United Kingdom", "UK"), loc(1, "United Kingdom", "UK")])


@pytest.mark.parametrize("locations, source, markets", [
    (INDIA_CRICKET, "espncricinfo.com", set()),
    (INDIA_CRICKET, "thehindu.com", set()),
    (CHICAGO_CLIMATE, "phys.org", set()),
    (FRENCH_POLITICS, "france24.com", set()),
    (INDIA_FOREIGN, "thehindu.com", set()),
    (MOSTLY_KENYA, "reuters.com", {"KE"}),
    (LONDON, "punchng.com", {"NG"}),
    (LONDON, "guardian.ng", {"NG"}),
    (LONDON, "vanguardngr.com", {"NG"}),
    (INDIA_FOREIGN, "the-star.co.ke", {"KE"}),
    (INDIA_CRICKET, "nation.africa", {"KE"}),
    (INDIA_CRICKET, "citizen.digital", {"KE"}),
    (INDIA_CRICKET, "news24.com", {"ZA"}),
    (INDIA_CRICKET, "iol.co.za", {"ZA"}),
    (INDIA_CRICKET, "www.timeslive.co.za", {"ZA"}),
    (MOSTLY_KENYA, "News24.com", {"ZA", "KE"}),
    (INDIA_CRICKET, "notnews24.com", set()),
    (INDIA_CRICKET, "punchng.com.example.org", set()),
    (INDIA_CRICKET, "zanews.com", set()),
    (";".join([loc(1, "India", "IN"), loc(1, "South Africa", "SF")]), "bbc.co.uk", set()),
    (";".join([loc(4, "Durban, KwaZulu-Natal, South Africa", "SF"), loc(1, "South Africa", "SF"),
               loc(1, "India", "IN")]), "bbc.co.uk", {"ZA"}),
    ("", "bbc.co.uk", set()),
    (None, None, set()),
])
def test_a_document_belongs_to_the_market_it_is_mostly_about_or_whose_outlet_ran_it(locations, source, markets):
    assert gdelt.document_markets(locations, source) == markets


def test_the_dominant_share_is_a_strict_majority_of_country_coded_locations():
    assert gdelt.DOMINANT_SHARE == 0.5
    blank = "1#Somewhere##"
    assert gdelt.document_markets(";".join([loc(1, "Kenya", "KE"), blank, blank]), "") == {"KE"}
    assert gdelt.document_markets(";".join([loc(1, "Kenya", "KE"), loc(1, "Uganda", "UG")]), "") == set()


def test_outlets_cover_the_market_domains_and_the_named_local_outlets():
    assert set(gdelt.OUTLETS) == set(gdelt.MARKETS)
    assert gdelt.OUTLETS["ZA"][0] == "za" and gdelt.OUTLETS["NG"][0] == "ng" and gdelt.OUTLETS["KE"][0] == "ke"
    for market, outlet in (("ZA", "news24.com"), ("NG", "punchng.com"), ("NG", "vanguardngr.com"),
                           ("NG", "premiumtimesng.com"), ("KE", "nation.africa"), ("KE", "citizen.digital")):
        assert outlet in gdelt.OUTLETS[market]


def _sql_patterns(sql):
    return dict(re.findall(r"REGEXP_CONTAINS\(LOWER\(TRIM\(IFNULL\(d\.SourceCommonName, ''\)\)\), r'([^']+)'\) "
                           r"THEN '([A-Z]+)'", sql))


def test_read_sql_attributes_markets_by_dominant_location_or_outlet():
    sql = gdelt.read_sql(RUN)
    assert gdelt.MARKET_SQL in sql
    assert "SourceCommonName" in sql and "DocumentIdentifier" not in sql
    for fips in gdelt.MARKETS.values():
        assert f"WHEN COUNTIF(SPLIT(loc, '#')[SAFE_OFFSET(2)] = '{fips}') > 0.5 * COUNT(*) THEN '{fips}'" in sql
    assert "WHERE SPLIT(loc, '#')[SAFE_OFFSET(2)] != ''" in sql
    assert "SELECT DISTINCT SPLIT(loc, '#')[SAFE_OFFSET(2)]" not in sql


@pytest.mark.parametrize("host", ["news24.com", "iol.co.za", "www.timeslive.co.za", "punchng.com", "guardian.ng",
                                  "the-star.co.ke", "nation.africa", "notnews24.com", "zanews.com", "bbc.co.uk",
                                  "punchng.com.example.org", ""])
def test_the_outlet_patterns_in_the_sql_match_what_the_python_rule_matches(host):
    patterns = _sql_patterns(gdelt.MARKET_SQL)
    fips_market = {fips: market for market, fips in gdelt.MARKETS.items()}
    assert sorted(patterns.values()) == sorted(gdelt.MARKETS.values())
    by_sql = {fips_market[fips] for pattern, fips in patterns.items() if re.search(pattern, host)}
    assert by_sql == gdelt.document_markets("", host)


# Item ids and seed rows

def test_item_ids_come_from_the_injected_item_functions():
    ids = FakeIds()
    rows = [
        row("SF", "person", "Cyril  Ramaphosa", 10, 0),
        row("SF", "organisation", "Kaizer Chiefs", 10, 0),
        row("SF", "theme", "WB_678_ENERGY_SUPPLY_AND_DEMAND", 10, 0),
        row("SF", "place", "Soweto", 10, 0),
    ]
    by_query = {s["query"]: s for s in gdelt.seeds(rows, RUN, ids=ids)}
    assert by_query["cyril ramaphosa"]["item_id"] == "id[topic|cyril ramaphosa]"
    assert by_query["cyril ramaphosa"]["kind"] == "topic"
    assert by_query["kaizer chiefs"]["item_id"] == "id[brand|kaizer chiefs]"
    assert by_query["kaizer chiefs"]["kind"] == "brand"
    assert by_query["energy supply and demand"]["item_id"] == "id[topic|energy supply and demand]"
    assert by_query["soweto"]["item_id"] == "id[topic|soweto]"
    assert ("canonical_key", "brand", "Kaizer Chiefs") in ids.calls
    assert ("item_id", "topic", "soweto") in ids.calls


def test_kinds_used_are_item_kinds():
    assert set(gdelt.ENTITY_KINDS.values()) <= {"topic", "brand", "event"}


def test_the_same_item_from_two_entity_kinds_is_one_seed_with_summed_counts():
    rows = [row("SF", "person", "Durban", 6, 0), row("SF", "place", "Durban", 6, 8)]
    seeds = gdelt.seeds(rows, RUN, ids=FakeIds())
    assert len(seeds) == 1
    assert seeds[0]["priority"] == pytest.approx(13 / 3)


def test_an_entity_the_item_functions_reject_is_skipped():
    rows = [row("SF", "person", "   ", 10, 0), row("SF", "person", "ok name", 10, 0)]
    assert [s["query"] for s in gdelt.seeds(rows, RUN, ids=FakeIds())] == ["ok name"]


def test_seed_rows_carry_the_search_the_collect_job_runs():
    seed = gdelt.seeds([row("NI", "person", "davido", 9, 0)], RUN, ids=FakeIds())[0]
    params = {"query": "davido", "platforms": gdelt.MULTI_PLATFORMS, "since": "2026-09-28"}
    assert seed == {
        "seed_date": RUN, "market": "NG", "item_id": "id[topic|davido]", "query": "davido", "kind": "topic",
        "lane": "expansion", "priority": pytest.approx(10.0), "template": "search/multi", "ttl_days": 3,
        "credits_estimate": float(quote_for("search/multi", "GET", params)),
    }
    assert gdelt.search_params(seed["query"], RUN) == params


def test_theme_labels_are_readable_and_role_themes_are_dropped():
    assert gdelt.theme_label("WB_678_ENERGY_SUPPLY_AND_DEMAND") == "energy supply and demand"
    assert gdelt.theme_label("ELECTION") == "election"
    assert gdelt.theme_label("CRISISLEX_T03_DEAD") == "dead"
    assert gdelt.theme_label("TAX_WORLDLANGUAGES_ZULU") == "zulu"
    assert gdelt.theme_label("TAX_ETHNICITY_ZULU") is None
    assert gdelt.theme_label("TAX_RELIGION_CHRISTIAN") is None
    assert gdelt.theme_label("TAX_FNCACT_PRESIDENT") is None
    assert gdelt.theme_label("") is None
    rows = [row("SF", "theme", "TAX_FNCACT_MINISTER", 50, 0)]
    assert gdelt.seeds(rows, RUN, ids=FakeIds()) == []


@pytest.mark.parametrize("text", ["Kohli", "kohli", "Naman", "naman", "farsightedness", "Farsightedness",
                                  "virat kohli", "TAX_WORLDLANGUAGES_Kohli", "WB_678 ENERGY", "ELECTION;", "TAX_",
                                  "WB_678_", "_ELECTION", "9_LIVES"])
def test_a_theme_that_is_not_a_gkg_code_has_no_label_and_no_seed(text):
    assert gdelt.theme_label(text) is None
    assert gdelt.seeds([row("SF", "theme", text, 50, 0)], RUN, ids=FakeIds()) == []


@pytest.mark.parametrize("code, label", [
    ("TAX_DISEASE_MALARIA", "malaria"), ("WB_678_ENERGY_SUPPLY_AND_DEMAND", "energy supply and demand"),
    ("ECON_INFLATION", "inflation"), ("UNGP_FORESTS_RIVERS_OCEANS", "ungp forests rivers oceans"),
    ("CRISISLEX_CRISISLEXREC", "crisislex crisislexrec"), ("EPU_POLICY", "epu policy"),
    ("SOC_GENERALCRIME", "soc generalcrime"), ("ENV_CLIMATECHANGE", "env climatechange"),
    ("ELECTION", "election"), ("ARMEDCONFLICT", "armedconflict"), (" KIDNAP ", "kidnap"),
])
def test_gkg_codes_keep_their_labels(code, label):
    assert gdelt.theme_label(code) == label


def test_every_live_fixture_code_has_the_gkg_code_shape():
    codes = (FIXTURES / "gdelt_age_themes.txt").read_text(encoding="ascii").split()
    assert [c for c in codes if not gdelt.THEME_CODE.fullmatch(c)] == []


def test_merge_parameter_carries_every_seed_field():
    seeds = gdelt.seeds(rising("SF", 2), RUN, ids=FakeIds())
    param = gdelt.rows_parameter(seeds)
    assert param.name == "rows"
    assert list(param.values[0].struct_types) == ["seed_date", "market", "item_id", "query", "kind", "lane", "priority", "template",
                     "ttl_days", "credits_estimate"]
    assert len(param.values) == 2


# Rule 1 and the POPIA theme families, on the 878 GKG theme codes seen on 27 September 2026

def theme_seeds(code):
    return gdelt.seeds([row("SF", "theme", code, 50, 0)], RUN, ids=FakeIds())


def test_fixture_is_the_full_live_code_list():
    codes = (FIXTURES / "gdelt_age_themes.txt").read_text(encoding="ascii").split()
    assert len(codes) == 878 and len(set(codes)) == 878
    assert STILL_SEEDS | FUSED <= set(codes)


def test_every_blocked_live_code_yields_no_seed_and_every_other_code_still_can():
    codes = (FIXTURES / "gdelt_age_themes.txt").read_text(encoding="ascii").split()
    wrong = [(code, expected_blocked(code)) for code in codes if bool(theme_seeds(code)) == expected_blocked(code)]
    assert wrong == []
    assert sum(map(expected_blocked, codes)) == 865
    assert {code for code in codes if not expected_blocked(code)} == STILL_SEEDS
    assert len(STILL_SEEDS) == 13


def test_religion_and_ethnic_words_drop_a_theme_anywhere_in_the_code():
    for code in ("RELIGION", "DISCRIMINATION_RELIGION_ISLAMOPHOBIA", "DISCRIMINATION_RELIGION_ISLAMOPHOBIC",
                 "DISCRIMINATION_RELIGION_RELIGIOUS_DISCRIMINATION", "WB_933_ETHNIC_RELIGIOUS_AND_OTHER_MINORITIES",
                 "SOC_ETHNICITY_CONFLICT"):
        assert gdelt.theme_label(code) is None, code
        assert theme_seeds(code) == [], code
    assert gdelt.theme_label("RELIGIOUS_FREEDOM") == "religious freedom"
    assert gdelt.theme_label("TECHNICAL_SUPPORT") == "technical support"


def test_non_rule_one_codes_named_by_the_reviewer_still_seed():
    for code in STILL_SEEDS:
        assert len(theme_seeds(code)) == 1, code
    assert theme_seeds("ECON_ELECTRICALGENERATION")[0]["query"] == "electricalgeneration"
    assert gdelt.theme_label("ECON_BOYCOTT") == "boycott"


def test_the_filter_covers_people_organisations_and_places_too():
    you = "".join(["you", "th"])
    for kind, name in (("organisation", f"anc {you} league"), ("person", "baby cham"), ("place", "old age home"),
                       ("organisation", "".join(["ge", "n z"]) + " collective"), ("person", "Kid Fonque"),
                       ("place", "Soweto Senior Centre")):
        assert gdelt.seeds([row("SF", kind, name, 50, 0)], RUN, ids=FakeIds()) == [], name
    for kind, name in (("person", "Kidane Tesfay"), ("organisation", "Minority Front"), ("place", "Oldham")):
        assert len(gdelt.seeds([row("SF", kind, name, 50, 0)], RUN, ids=FakeIds())) == 1, name


def test_young_communist_league_yields_no_seed():
    assert gdelt.seeds([row("SF", "organisation", "Young Communist League", 50, 0)], RUN, ids=FakeIds()) == []


def test_the_added_words_block_as_whole_words():
    for word in ("young", "younger", "newborn", "newborns", "generational", "toddler", "toddlers", "retiree",
                 "retirees", "retirement"):
        assert gdelt.blocked(f"WB_1_{word.upper()}_POLICY"), word
    assert not gdelt.blocked("YOUNGSTOWN")


def test_blocked_matches_whole_words_and_fused_heads_only():
    assert gdelt.blocked("WB_2885_" + "".join(["YOU", "TH"]) + "_EMPLOYMENT")
    assert gdelt.blocked("SOC_AGINGPOPULATION")
    assert gdelt.blocked("SOC_POINTSOFINTEREST_HIGHSCHOOL")
    assert not gdelt.blocked("ECON_ELECTRICALGENERATION")
    assert not gdelt.blocked("TAX_DISEASE_KIDNEY_FAILURE")
    assert not gdelt.blocked("agenda agency")


# Split so no constant ends in "ge" plus "n": the bytecode scan reads the marshal type byte after it.
FUSED_AGE_LENS = ["".join(p) for p in (
    ["ge", "nzhumor"], ["funny", "ge", "nz"], ["ge", "nalphatrend"], ["ge", "nxnostalgia"], ["mil", "lennialmoms"],
    ["mzansimil", "lennials"], ["mil", "lenialvibes"], ["boo", "merhumour"], ["okboo", "mer"], ["boo", "mers"],
    ["zoo", "merlife"], ["tee", "nmom"], ["tee", "nlife"])]
ORDINARY_WORDS = ("canteen", "fifteen", "nineteen", "velveteen", "teeny", "teensy", "genuine", "general", "genre",
                  "genius", "gender", "agenda", "boomerang", "boomerangs", "boombox", "zoomed", "zoomin",
                  "millennium", "".join(["ge", "nzyme"]))


@pytest.mark.parametrize("word", FUSED_AGE_LENS)
def test_fused_age_lens_forms_block_at_either_end(word):
    assert gdelt.blocked(word)
    assert gdelt.blocked("#" + word.upper())
    assert gdelt.blocked(f"cape town {word} braai")


def test_split_age_lens_phrases_block_as_a_pair():
    assert gdelt.blocked("".join(["ge", "n alpha"]) + " trends")
    assert gdelt.blocked("".join(["Ge", "n X"]) + " music")


@pytest.mark.parametrize("word", ORDINARY_WORDS)
def test_ordinary_words_holding_a_short_age_fragment_stay_open(word):
    assert not gdelt.blocked(word)
    assert not gdelt.blocked("#" + word)


# Forms that casefold alone let through: fullwidth, mathematical bold, a format character (zero width joiner,
# zero width space, soft hyphen) inside the word, and a split pair read with the fused rules.
UNICODE_AGE_LENS = [
    "\uff27\uff45\uff4e\uff3a", "\uff47\uff45\uff4e\uff5a\uff48\uff55\uff4d\uff4f\uff52",
    "\U0001d420\U0001d41e\U0001d427\U0001d433", "".join(["ge", "n\u200dzhumor"]), "".join(["ge", "n\u200bzhumor"]),
    "te\u00adens", "kids\u200bparty", "".join(["ge", "n_zhumor"]), "".join(["ge", "n zhumor"]),
]
# Cohort words fused onto the end of a place or other word, and the school and student forms.
COHORT_FORMS = ["nigerianteens", "mzansiteens", "mzansikids", "schoolkids", "studentlife", "preteen", "whizkid",
                "schoollife", "mzansistudents", "studentloans", "africanteens", "lagoskids", "rsateens"]
# Round 2 and 3 forms: a lookalike, accented or dotless letter, small capitals, digits or symbols for letters,
# a stretched, dotted or spaced word, a word spelled in lookalike letters of one other script, and cohort
# words fused onto the start or end of a tag.
ROUND_FORMS = [
    "g\u0435nz", "ge\u0301nz", "te\u0308ens", "k\u0131ds", "\u0262\u1d07\u0274\u1d22", "kidsofinstagram",
    "kidstiktok", "kidsfashion", "youthmonth", "youthday", "youthmonth2026", "mzansiyouth", "tween", "tweens",
    "g3nz", "k1ds", "t33ns", "kidz", "kiddos", "kiddies", "teeeens", "g.e.n.z", "babyshark", "seniorcitizens",
    "newbornphotography", "toddlerlife", "elderlycare", "retireelife", "kiiids", "yoouth", "@kids", "kid$",
    "g e n z", "k i d s", "\u043a\u0456\u0501\u0455", "b4by", "tweenstyle", "kidzworld", "kiddiepool",
    # Round 4: plurals and young forms, decade and bracket cohorts, and cohort words in the market languages,
    # whole and fused at either end.
    "adolescents", "youngster", "youngsters", "youngin", "youngins", "juveniles", "retirements",
    "90sbaby", "90sbabies", "2000sbaby", "1990skids", "y2kbaby", "y2kkids", "over40", "over50fashion", "under18",
    "under18s", "50plus", "twentysomething", "thirtysomething", "agegap", "agegaplove",
    "vijana", "watoto", "mtoto", "abantwana", "intsha", "umntwana", "pikin", "kinders", "tieners", "jeug",
    "vijanaday", "kenyavijana", "watotofashion", "abantwanabami", "mzansiintsha", "naijapikin", "p1k1n",
    "jeugdag", "suidafrikaansetieners", "kindersdag",
    # Round 5: RULE_ONE words fused with each other or another word, year and 2k cohorts at either side,
    # and ranges.
    "girlchild", "boychild", "youngpeople", "youngadults", "middleaged", "oldpeople", "olderpeople", "underage",
    "2kbabies", "2kkids", "ama2000", "ama2k", "2000baby", "05kids", "kids2000", "mzansibabies2k", "zillennial",
    "zillennials", "agemates", "agemate", "18to24", "ages18to24", "age18to24", "boyage", "ageboy", "boyaged",
    "mzansipupils", "welovegirls", "capetownelders", "infantcare",
    # Round 6: a cohort root inside a fused tag, and tags starting with the three letter words for years and
    # for a male child.
    "happychildrensday", "worldchildrensday", "happyyouthday", "nationalyouthday", "sayouthday",
    "africanyouthday", "internationalyouthday", "happyyouthmonth", "nationalgirlchildday", "bringbackourgirlsnow",
    "backtoschool2026", "backtoschoolsale", "highschoolers", "mzansikidsparty", "naijateensday",
    "kenyanyouthsspeak", "thekidsarealright", "canteenkids", "fifteenteens", "kidneykids",
    "ageisjustanumber", "agegroup", "agegrade", "ageism", "agelimit", "boyz", "boyhood", "boymom", "boyband",
    # Round 7: the words for older people, girls and pupils inside a fused tag.
    "blackgirlmagic", "worldelderabuseawarenessday", "worldseniorcitizensday", "worldorphansday",
    "nigerianpensionersprotest", "nationalpupilsday", "naijaadolescenthealth", "nigeriajuvenilecourt",
    "olderpersons", "internationaldayofolderpersons", "older", "olderadults", "cowgirlkids", "kidneykids",
    # Round 8: young, boy, baby and the pension cohort inside a tag, the born free cohort, and new elders.
    "nottooyoungtorun", "nottooyoungtorunact", "sassaoldagegrant", "blackboyjoy", "mzansibabyshower",
    "nigerianbabymama", "healthyageing", "healthyaging", "bornfree", "bornfrees", "sabornfrees", "newelders",
    "newelderly", "generationskids", "cowboykids",
] + [f"{head}{root}{tail}" for root in (
    "youth", "child", "children", "school", "kid", "kids", "teen", "teens", "girls", "boys", "".join(["ge", "nz"]),
    "millennial", "zillennial", "boomer", "zoomer", "toddler", "newborn", "elderly", "retiree", "student", "vijana",
    "kijana", "watoto", "mtoto", "abantwana", "intsha", "umntwana", "pikin", "kinders", "tiener", "jeug", "girl",
    "senior", "orphan", "pupil", "juvenile", "pensioner", "adolescent", "elder", "young", "boy", "baby", "oldage",
    "ageing", "bornfree",
) for head, tail in (("happy", "day"), ("national", "day"), ("", "2026"))]
# Ordinary words, many ending or starting like a cohort form, in English, Afrikaans, Zulu, Xhosa, Swahili,
# Yoruba and Pidgin, plus brands, artists, places, food and SA, NG and KE music and sport tags. None may be
# blocked.
ORDINARY_SWEEP = """
canteen canteens velveteen velveteens sateen lateen mangosteen umpteen posteen karanteen thirteen fourteen fifteen
fifteens sixteen seventeen eighteen nineteen nineteens steen springsteen malmsteen skid skids nonskid kidney kidnap
kidskin agenda agency agent general genuine genre genius gender generous ginger gingerbread engine schooner studio
study stud babylon boycott eldorado village orange cabbage heritage voltage mileage passage vintage savage garage
storage mortgage language image
between inbetween kidneys kidnaps kidnapped kidnapper kidnappers kidnapping kidding kidman seniority babylonian babel
ten tens often kitten listen tender tenant written bitten sweeten fourteenth tweet tweets tweed tweezers elderberry
retire retired juventus scholar zomer boomerangs zoomerang genesis genetics generator stage wage cage page
baksteen hoeksteen grafsteen lekker braai boerewors bakkie jol eish howzit dankie asseblief vriende kuier potjie
vetkoek koeksister biltong mielie stoep rooibos teenoor teenstander teenstanders teenwoordig teenspoed teenaan
teenkanting steenbok tweeling tweespruit steenberg
sawubona ngiyabonga ubuntu amandla yebo hamba shisanyama amapiano gqom mzansi ukhozi isibaya indlovu imali umculo
ukudla inkosi sanibonani ngiyakuthanda umama ubaba isikhathi umhlaba amanzi indaba ukuthula ibhola shosholoza
molo enkosi ndiyabulela unjani umntu iqhawe ukutya uxolo ilizwe umzi babalwa babanango
jambo asante habari karibu rafiki hakuna matata sawa pole chakula muziki safari harambee uhuru nairobi mombasa
sheng gengetone boda matatu mitumba nyama choma mambo poa shwari kesho kazi pesa chai nyumbani mvua furaha upendo
kidogo kidole kidonge kidonda kitabu kijiji kiswahili ugali sukumawiki mandazi pilau kisumu kiambu kakamega
jollof owambe aso ebi oluwa afrobeats naija lagos abuja egusi suya danfo gbese wahala gbege japa sabi abeg oyinbo
ekaro ekaabo alafia oriki amala ewedu gbegiri adire fuji juju apala wetin abi oya dey sef comot palava ogbonge
katakata gist yawa shayo jara puffpuff chinchin zobo peppersoup
kfc nandos checkers shoprite woolworths picknpay vodacom mtn safaricom mpesa glo airtel dstv showmax castle savanna
hunters redbull cocacola amstel heineken nike adidas puma samsung iphone tecno infinix jumia takealot uber bolt
netflix spotify boomplay audiomack kidzania capitec absa nedbank gtbank opay palmpay flutterwave paystack dangote
tusker hansa goldberg maltina indomie milo betway sportybet hollywoodbets 7up 5g 4g b2b mp3 h2o 50cent 2baba 9ice
1xbet bet9ja
afrobeat afropop kwaito maskandi sgubhu 3step alte highlife streethop benga bongoflava arbantone bafana
bafanabafana banyanabanyana springboks proteas superfalcons supereagles harambeestars kaizerchiefs orlandopirates
sundowns gorsahia enyimba blitzboks comrades davido rema asake tems burnaboy blackcoffee focalistic makhadzi
sautisol khaligraph tyla teni olamide tiwasavage kota bunnychow chakalaka nyamachoma
over9000 underarmour 247 top40 afcon2025 1990s 90s y2k over under plus canalplus 365plus kinder kindergarten
kindness vijiji watu mtu pick pikachu jeuk
girlfriend girlfriends boyfriend boyfriends cowboys welder welders suspension suspensions regeneration
degeneration minority minorities infantry infantino brainchild elderflower youngstown images pages stages
messages packages managing engaged damaged imaging packaging sausages villages languages 9to5 2024to2025
amapiano2024 ama amakhosi
canteensale fifteenminutes springsteenlive steenkamp steenhuisen umpteenth nineteenth kidneystone skidmore
bukidnon agenda agency agencies agent agents agege boycott boycotts boycotted boyfriend boyle boysenberry
boyega boyd rothschild
worldkidneyday kidneys kidneyhealth proteasfielder fielder fielders welder folder holder boulder shareholder
stakeholder stakeholders cowgirl cowgirls elderflowers
cowboy tomboy playboy playboys crybaby nikeboycott myboyfriend mboya tommboya rastababylon welders
""".split() + ["\u00f2r\u00ec\u1e63\u00e0", "\u1eb9\u0300k\u1ecd\u0301"]


@pytest.mark.parametrize("word", UNICODE_AGE_LENS + COHORT_FORMS + ROUND_FORMS)
def test_unicode_and_end_position_age_lens_forms_block(word):
    assert gdelt.blocked(word)
    assert gdelt.blocked("#" + word.upper())
    assert gdelt.blocked(f"cape town {word} braai")


ORDINARY_PHRASES = ["Bruce Springsteen", "office canteen", "top fifteen", "skid row", "the umpteen reasons",
                    "Cyril Ramaphosa", "Kaizer Chiefs", "Orlando Pirates", "Tyla", "in between", "Nicole Kidman",
                    "Bafana Bafana", "Super Falcons", "Harambee Stars", "Kidney Stones", "top 10 songs", "7 up",
                    "Black Coffee", "Kidogo kidogo", "Tiwa Savage", "Zoomerang app", "ten tens", "Under Armour",
                    "over 50 killed", "top 40 hits", "AFCON 2025", "the 1990s", "Canal Plus",
                    "Young Famous and African", "Young Famous", "#YoungFamous", "Dallas Cowboys", "my girlfriend",
                    "Gianni Infantino", "urban regeneration", "minority front", "Reeva Steenkamp", "John Steenhuisen",
                    "a general strike", "a gentle giant", "John Boyega", "Susan Boyle", "boycott the canteen",
                    "World Kidney Day", "Proteas fielder", "shareholder meeting", "cowgirl boots",
                    "Tom Mboya Street"]


# Numbered age thresholds, ranges and audience brackets.
NUMERIC_AGE_LENS = [
    "18-24", "18 to 24", "18\u201324", "18\u201424", "18\u221224", "18 - 24", "18\uff0d24",
    "25-34 year olds", "under 25s", "under-25s", "over 50s", "over-50s", "aged 18+", "18+ only",
    "35 plus audience", "lagos 18-24 fashion", "#Under25s", "under 20 squad", "under-20 squad", "under20",
    "over 40s dating", "Bulls lose 18-24", "25 year olds", "16-year-old", "50yearolds", "over\u201150s",
    "50-plus", "50-plus women", "18-plus", "18 +", "30-somethings", "30 somethings", "in their 20s",
    "women in their 30s", "18 and over", "18 and up", "25 and under", "18 or over", "18 & over",
    "18 years and above", "above 18", "below 25", "below 18s", "18yo", "18 yo", "16-yo", "18 y/o",
    "over 30 women", "over 25 audience", "over 18 events", "u25s", "U-25s", "18~24", "18_24",
    "18 through 24", "between 18 and 24", "between 18 & 24", "18 until 24", "18 tot 24", "18 hadi 24",
    "18 zuwa 24",
    "people over 60", "adults over 30", "mums over 40", "moms over 40", "shoppers over 50",
    "readers over 40", "consumers over 40", "in their late 20s", "women in their early 30s", "late 20s women",
    "mid-30s men", "early 20s", "20s crowd", "20s audience", "A18-49", "W25-54", "M18-34", "P18-49",
    "A18+", "W18+", "in their twenties", "early twenties", "mid thirties", "over thirties",
    "below twenty-five", "twenty-somethings", "June 18-24 year olds", "18-24s", "24-18 year olds", "under 20s",
]
NUMBERS_THAT_STAY_OPEN = [
    "2024-25 season", "2025/26", "Top 40", "Top 100 Afrobeats", "Afrobeats 2025", "Amapiano 2024", "Area 51",
    "Big Brother Naija season 10", "4-3 win", "7-0", "Kaizer Chiefs 2-1", "COVID-19", "Hot 97",
    "Channel O 25th anniversary", "1-2-3", "24/7", "50 Cent", "Blink-182", "U2", "9ja", "over 50 killed",
    "U20 AFCON", "Flying Eagles U-17", "2026-09-29", "29-09-2026", "10:30-11:30", "R12.50-13.50",
    "top 10 songs", "90s music", "80s hits", "20-25 September", "Grahamstown 25-30 June",
    "18-24 June", "18-24 March", "18-24 May", "between 18 and 24 June", "between 18 and 24 March",
    "between 18 and 24 May", "between 18 & 24 June", "between 18 & 24 March", "between 18 & 24 May",
    "over 20 years ago", "over 50 people killed",
    "over 30 women were killed",
    "20s and 30s music", "songs of the 90s", "90s party", "90s fans", "T20 World Cup", "U23 Afcon",
    "M1 motorway", "over forty killed",
]
WORD_AGE_GAPS = [
    "forty-year-olds", "sixteen-year-olds", "forty-somethings", "under-eighteen", "under eighteen",
    "eighteen-plus", "eighteen plus", "twentyfive tothirtyfour", "twenty five to thirty four", "25-to-34",
    "inher20s", "in her 20s", "their30s", "their 30s",
]
# Possessive decades, the full run of number words, people before in and a decade, and a month before a bracket.
MORE_AGE_FORMS = [
    "in his 40s", "man in his 30s", "in his forties", "in her forties", "in their forties", "in my 20s",
    "in your 30s", "in our 50s", "his 40s", "her twenties", "seventeen-year-olds", "thirteen year olds",
    "fourteen-year-old", "fifteen year olds", "nineteen-year-olds", "seventy-year-olds", "eighty-somethings",
    "ninety year olds", "seventy-five-year-olds", "under seventeen", "nineteen plus", "over-seventy",
    "thirteen to nineteen", "in his seventies", "late eighties", "people over thirty", "over thirty women",
    "people in 40s", "men in their 50s", "women in 30s", "May 18-24s", "May 18-24 yo", "May 18-24 plus",
    "June 18-24 year olds", "May 18 to 24 year olds", "May 18-24+",
]
OPEN_AFTER_MORE_FORMS = [
    "over 30 shops closed", "over thirty shops closed", "over 30 taxis", "our 90s playlist", "my 80s collection",
    "your 90s favourites", "temperatures in the 30s", "temperatures in 30s", "under thirteen minutes",
    "nineteen eighty-four", "the eighties", "eighties music", "seventy kilometres", "May 18-24",
    "May 18 to 24", "over seventy killed", "number seventeen",
]


# Dates, scores, prices, durations and calendar ranges that read like a bracket but are not one.
CALENDAR_AND_SCORE_NUMBERS = [
    "2026-27 season", "the 2026/27 season", "24-hour", "24 hour shutdown", "Top 50", "R18 000", "R18 000-R24 000",
    "R18-R24", "June 18-24", "18-24 June", "from 18 to 24 June", "June 18 to 24", "Sept 18-24", "18 June-24 June",
    "18/24", "21-17", "Bulls beat Sharks 31-24", "won 24-18", "20-30\u00b0", "20-30 degrees", "10-20 minutes",
    "20-30%", "20-30 percent", "18-24 hours", "18-24 km", "Forbes 30 under 30", "30 Under 30",
    "R25 and under", "over 50 percent", "under 20 minutes", "20 to 30 kg",
]


@pytest.mark.parametrize("text", NUMERIC_AGE_LENS)
def test_numeric_age_ranges_and_thresholds_block(text):
    assert gdelt.blocked(text)
    assert gdelt.blocked("#" + text.upper())
    assert gdelt.blocked(f"cape town {text} braai")


@pytest.mark.parametrize("text", NUMBERS_THAT_STAY_OPEN)
def test_ordinary_numbers_stay_open(text):
    assert not gdelt.blocked(text)
    assert not gdelt.blocked(f"cape town {text} braai")


@pytest.mark.parametrize("text", WORD_AGE_GAPS)
def test_word_age_number_gaps_block(text):
    assert gdelt.blocked(text)
    assert gdelt.blocked(text.upper())
    assert gdelt.blocked(f"cape town {text} braai")


def test_sports_squad_codes_stay_open_and_plural_u_numbers_block():
    for text in ("U20 AFCON", "U-20 World Cup", "U23 Afcon", "u18", "U-21", "#u30"):
        assert not gdelt.blocked(text), text
    for text in ("u25s", "U-25s", "#U25S"):
        assert gdelt.blocked(text), text


@pytest.mark.parametrize("text", MORE_AGE_FORMS)
def test_possessive_decades_number_words_and_month_brackets_block(text):
    assert gdelt.blocked(text)
    assert gdelt.blocked(text.upper())
    assert gdelt.blocked(f"cape town {text} braai")


@pytest.mark.parametrize("text", OPEN_AFTER_MORE_FORMS)
def test_counts_music_decades_and_dates_stay_open(text):
    assert not gdelt.blocked(text)
    assert not gdelt.blocked(f"cape town {text} braai")


@pytest.mark.parametrize("text", CALENDAR_AND_SCORE_NUMBERS)
def test_calendar_ranges_scores_prices_and_units_stay_open(text):
    assert not gdelt.blocked(text)
    assert not gdelt.blocked(f"cape town {text} braai")


def test_age_regex_runtime_is_bounded_on_long_whitespace_inputs():
    script = """
import json
import time
from core.collect.gdelt import blocked

texts = [" " * 5000 + "18/year", "18" + " " * 5000 + "year" + " " * 5000,
         "june" + " " * 5000 + "18" + " " * 5000 + "to" + " " * 5000 + "24" + " " * 5000 + "km",
         "18-24" + " " * 5000 + "-" + " " * 5000 + "minutes"]
started = time.perf_counter()
results = [blocked(text) for text in texts]
print(json.dumps({"elapsed": time.perf_counter() - started, "blocked": results}))
"""
    try:
        result = subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True,
                                encoding="utf-8", timeout=5)
    except subprocess.TimeoutExpired:
        pytest.fail("blocked() exceeded the 5 second child timeout")
    measurement = json.loads(result.stdout)
    assert measurement["blocked"] == [False, False, False, True]
    assert measurement["elapsed"] < 1


def test_every_join_of_two_rule_one_words_is_blocked():
    assert [a + b for a in gdelt.RULE_ONE for b in gdelt.RULE_ONE if not gdelt.blocked(a + b)] == []


def test_the_ordinary_sweep_is_large_and_blocks_nothing():
    assert len(ORDINARY_SWEEP) >= 200
    assert [w for w in ORDINARY_SWEEP if gdelt.blocked(w) or gdelt.blocked("#" + w.upper())] == []
    assert [p for p in ORDINARY_PHRASES if gdelt.blocked(p)] == []


# Artists and a surname that hold a cohort word and are names, not an age lens: excused exactly, joined or spaced.
NAME_TAGS = ["wizkid", "wizkidayo", "burnaboy", "kiddominant", "k1deultimate", "babyface", "kidd", "kidx", "kidum", "youngjonn",
             "youngstunna", "youngstacpt", "youngsta", "youngduu", "moonchildsanelly", "kidtini", "kidi",
             "kidimusic", "childishgambino", "generationsthelegacy", "fireboy", "fireboydml", "joeboy", "rudeboy",
             "boyspyce", "buruklynboyz", "kidjo", "youngjohn", "youngsfield", "babycity"]
NAMES = NAME_TAGS + ["Wizkid", "#WIZKID", "Burna Boy", "burna_boy", "Kiddominant", "K1 De Ultimate",
                     "K1 de ultimate live", "Babyface", "baby face", "Jason Kidd", "Kidd's Beach",
                     "Burna Boy and Wizkid",
                     "Kid X", "kid_x", "KidX live in Joburg", "Kidum", "Kidum in Nairobi", "Young Jonn", "young_jonn",
                     "Young Stunna", "#YOUNGSTUNNA live", "YoungstaCPT", "Youngsta CPT", "Youngsta", "Young Duu",
                     "Moonchild Sanelly", "moonchild_sanelly live", "Kid Tini", "KiDi",
                     "KiDi Music", "kidi_music", "Childish Gambino", "Generations The Legacy", "Generations",
                     "generations_the_legacy", "Fireboy DML", "fireboy_dml", "Fireboy", "Joeboy", "Rudeboy",
                     "Boy Spyce", "boy_spyce", "Buruklyn Boyz", "Angelique Kidjo", "Young John", "young_john",
                     "Youngsfield", "Youngsfield, Cape Town", "BabyCity", "Baby City"]
# Their age word neighbours, and the whole word forms around them, stay blocked.
NAME_NEIGHBOURS = ["kids", "wizkidayokids", "wizkidayo kids", "boys", "babies", "young boy", "boy child", "school boy", "Burna Boy kids",
                   "Wizkid boys", "Babyface babies", "Kidd kids", "young boy Burna Boy", "wizkids", "wizkidz",
                   "kidds", "burnaboykids", "babyfacebabies", "k1dde", "baby faces kids", "Kid X kids", "kidxkids",
                   "kidums", "Kidum babies", "kidumyouth", "tiener", "kijana", "tienerjare", "youngjonnkids",
                   "young stunna teens", "Young Jonn babies", "youngstunnas", "youngstakids", "Youngsta CPT youth",
                   "moonchild sanelly kids", "moonchild", "kidtinikids", "Kid Tini teens",
                   "kidikids", "KiDi teens", "generationskids", "Generations kids", "childish",
                   "Childish Gambino teens", "generationsthelegacykids", "fireboykids", "fireboydmlkids",
                   "joeboyteens", "rudeboy youth", "rudeboys", "kidjokids", "youngjohnbabies", "Young John teens",
                   "boyspycekids", "buruklynboyzkids", "Boy Spyce youth", "youngsfieldkids", "babycitykids",
                   "BabyCity babies", "young people"]


@pytest.mark.parametrize("name", NAMES)
def test_names_holding_a_cohort_word_stay_open(name):
    assert not gdelt.blocked(name)
    assert gdelt.seeds([row("SF", "person", name, 50, 0)], RUN, ids=FakeIds())


@pytest.mark.parametrize("text", NAME_NEIGHBOURS)
def test_age_words_around_the_names_stay_blocked(text):
    assert gdelt.blocked(text)


# Each lookalike letter the skeleton reads as Latin: dotless and IPA letters, small capitals, Cyrillic, Greek.
LOOKALIKE_LETTERS = {
    "\u0131": "i", "\u0237": "j", "\u0261": "g", "\u0251": "a", "\u0269": "i",
    "\u1d00": "a", "\u0299": "b", "\u1d04": "c", "\u1d05": "d", "\u1d07": "e", "\u0262": "g", "\u029c": "h",
    "\u026a": "i", "\u1d0a": "j", "\u1d0b": "k", "\u029f": "l", "\u1d0d": "m", "\u0274": "n", "\u1d0f": "o",
    "\u1d18": "p", "\u0280": "r", "\ua731": "s", "\u1d1b": "t", "\u1d1c": "u", "\u1d20": "v", "\u1d21": "w",
    "\u028f": "y", "\u1d22": "z",
    "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c", "\u0443": "y", "\u0445": "x",
    "\u0456": "i", "\u0458": "j", "\u0455": "s", "\u043a": "k", "\u043c": "m", "\u0442": "t", "\u043d": "h",
    "\u0432": "b", "\u0501": "d", "\u04af": "y", "\u04bb": "h", "\u04cf": "l",
    "\u03b1": "a", "\u03b2": "b", "\u03b5": "e", "\u03b7": "n", "\u03b9": "i", "\u03ba": "k", "\u03bd": "v",
    "\u03bf": "o", "\u03c1": "p", "\u03c4": "t", "\u03c5": "u", "\u03c7": "x",
}


@pytest.mark.parametrize("letter, latin", LOOKALIKE_LETTERS.items())
def test_skeleton_reads_each_lookalike_letter_as_latin(letter, latin):
    assert gdelt.skeleton(letter) == latin
    assert gdelt.skeleton(letter.upper()) == latin
    assert gdelt.skeleton(f"x{letter}y") == f"x{latin}y"


@pytest.mark.parametrize("text, plain", [
    ("\uff27\uff45\uff54", "get"),("\U0001d420\U0001d41e", "ge"), ("te\u0308ens", "teens"), ("K\u0131DS", "kids"),
    ("\u1ecd\u0300m\u1ecd", "omo"), ("\u00e9t\u00e9", "ete"), ("a\u00adb\u200dc\u200bd", "abcd"),
    ("Stra\u00dfe", "strasse"),
    ("amapiano 2026", "amapiano 2026"), ("\u5357\u975e", "\u5357\u975e"),
])
def test_skeleton_folds_width_case_marks_and_format_characters(text, plain):
    assert gdelt.skeleton(text) == plain


def test_skeleton_can_read_format_characters_as_a_word_break():
    assert gdelt.skeleton("kids\u200bparty", " ") == "kids party"


@pytest.mark.parametrize("text", ["\u0430mapiano", "amapi\u03b1no", "braai\u5357\u975e", "Cyril \u0420amaphosa"])
def test_text_mixing_scripts_is_blocked(text):
    assert gdelt.blocked(text)


@pytest.mark.parametrize("text", ["\u043c\u043e\u0441\u043a\u0432\u0430", "\u5357\u975e", "S\u00e3o Paulo",
                                  "C\u00f4te d\u2019Ivoire", "\u01c3X\u00f3\u00f5"])
def test_text_in_one_script_is_not_blocked_for_its_script(text):
    assert not gdelt.blocked(text)


# Rules 1, 2 and 4

def test_no_banned_literals_in_the_module_or_its_sql():
    # Joined at run time: CPython folds "a" + "b" into one constant, which the bytecode scan would catch.
    # Split after "ge", not "gen": marshal writes a short string's type byte "z" right after the one before.
    parts = [
        ["google", "_trends"], ["google", "trends"], ["google", " trends"], ["ge", "n z"], ["ge", "nz"],
        ["ge", "n_z"], ["gdelt", "_slang"], ["sl", "ang"], ["you", "th"], ["tee", "n"], ["millen", "nial"],
        ["gener", "ation"], ["gem", "ini"], ["nano", " banana"],
    ]
    banned = ["".join(p) for p in parts]
    for text in (SOURCE, gdelt.read_sql(RUN), gdelt.MERGE_SQL):
        lower = text.lower()
        for word in banned:
            assert word not in lower, word
        assert not re.search(r"\bages?\b", lower)


# CLI

def test_plan_prints_the_sql_and_a_byte_placeholder_without_touching_the_cloud(monkeypatch, capsys):
    def no_client(*args, **kwargs):
        raise AssertionError("--plan must not build a BigQuery client")

    monkeypatch.setattr(gdelt.bigquery, "Client", no_client)
    assert gdelt.main(["--plan", "--run-date", "2026-09-29"]) == 0
    out = capsys.readouterr().out
    assert gdelt.read_sql(RUN) in out
    assert "dry-run bytes: not estimated under --plan" in out
    assert "5,000,000,000" in out
