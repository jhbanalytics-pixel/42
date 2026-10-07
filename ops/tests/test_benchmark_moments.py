"""Offline benchmark regressions using retained brief shapes and observation fixtures."""
import csv
import json
from datetime import date
from pathlib import Path

import duckdb
import pytest
import sqlglot
from google.cloud import bigquery

from ops.runners import benchmark_moments as bm


MOMENT = {"id": "bbnaija", "market": "NG", "d": date(2026, 10, 5), "any_re": r"\bbbnaija\b", "and_re": ""}


def held_item(**changes):
    return {"item_id": "bbnaija-item", "title": "temi, teminators, nkem", "rule": "G10",
            "reason": "explanation_failed", "evidence": [{"id": "post-fixture", "platform": "twitter",
                "text": "Breaking: Temi Nkem wins BBNaija season 11"}], **changes}


def test_observation_cohort_excludes_poison_lanes_not_lane_classes():
    observation_sql = bm.POSTS_SQL.split(", o AS (", 1)[1].split(")\n, j AS (", 1)[0]
    observation_sql = observation_sql.replace(f"`{bm.CORE}.post_observations`", "post_observations")
    observation_sql = observation_sql.replace("@start", "DATE '2026-10-01'").replace("@obs_end", "DATE '2026-10-07'")
    with duckdb.connect() as con:
        con.execute("CREATE TABLE post_observations(post_id VARCHAR, market VARCHAR, lane VARCHAR, "
                    "lane_class VARCHAR, source_market VARCHAR, observed_date DATE)")
        con.executemany("INSERT INTO post_observations VALUES (?, 'NG', ?, ?, 'NG', '2026-10-05')", [
            ("measured", "sweep", "unbiased_rank"), ("null-lane", None, "panel"),
            ("placebo-only", "placebo", "search_presence"), ("agent-only", "agent_live", "search_presence"),
            ("legacy-only", "legacy", "legacy")])
        rows = con.execute(sqlglot.transpile(observation_sql, read="bigquery", write="duckdb")[0]).fetchall()
    assert {row[0] for row in rows} == {"measured", "null-lane"}


def test_reviewed_bbnaija_hold_matches_retained_identity_not_headline():
    moment = {**MOMENT, "retained_items": {("2026-10-05", "held", "bbnaija-item")}}
    cards, held = bm.brief_hits(moment, {("2026-10-05", "NG"): {"held_back": {"items": [held_item()]}}})
    assert cards == []
    assert len(held) == 1
    assert "retained item match" in held[0] and "headline not matched" in held[0]
    assert "post-fixture" in held[0]
    assert bm.verdict({"cards": cards, "held": held}) == "HELD"


@pytest.mark.parametrize("evidence, extra", [
    ([{"id": "search-fixture", "platform": "google_search", "text": "BBNaija"}], {}),
    ([{"id": "post-fixture", "platform": "twitter", "text": "An unrelated painting exhibition"}],
     {"reason_text": "The critic mentioned BBNaija"}),
    ([{"platform": "twitter", "text": "BBNaija"}], {}),
])
def test_search_generated_and_unidentified_text_are_not_brief_evidence(evidence, extra):
    item = held_item(evidence=evidence, **extra)
    assert bm.brief_hits(MOMENT, {("2026-10-05", "NG"): {"held_back": {"items": [item]}}}) == ([], [])


def test_incidental_any_match_does_not_satisfy_the_second_pattern():
    moment = {**MOMENT, "and_re": r"\bfinale\b"}
    item = held_item(title="BBNaija unrelated reference", evidence=[
        {"id": "post-one", "platform": "twitter", "text": "BBNaija casting advert"},
        {"id": "post-two", "platform": "twitter", "text": "A different programme finale"}])
    assert bm.brief_hits(moment, {("2026-10-05", "NG"): {"held_back": {"items": [item]}}}) == ([], [])


def test_brief_items_are_deduplicated_by_identity_across_more_and_days():
    item = held_item(title="BBNaija finale")
    briefs = {("2026-10-05", "NG"): {"cards": [item], "more": [item]},
              ("2026-10-06", "NG"): {"cards": [item]}}
    cards, held = bm.brief_hits(MOMENT, briefs)
    assert len(cards) == 1 and held == []
    assert "headline match" in cards[0]


def generic_card():
    return {"item_id": "generic-fyp-7oct", "title": "fyp, viral, nigeria", "evidence": [
        {"id": "incidental-bb-post", "platform": "twitter", "text": "I watched BBNaija after work"},
        *[{"id": f"fyp-{n}", "platform": "tiktok", "text": f"generic FYP clip {n}"} for n in range(20)]]}


@pytest.mark.parametrize("with_hold, expected", [(False, "POSTS ONLY"), (True, "HELD")])
def test_incidental_card_evidence_cannot_upgrade_headline_coverage(with_hold, expected):
    moment = {**MOMENT, "retained_items": {("2026-10-05", "held", "bbnaija-item")}}
    briefs = {("2026-10-07", "NG"): {"cards": [generic_card()]}}
    if with_hold:
        briefs[("2026-10-05", "NG")] = {"held_back": {"items": [held_item()]}}
    cards, held = bm.brief_hits(moment, briefs)
    assert cards == []
    assert bm.verdict({"cards": cards, "held": held, "map_n": 0, "clusters_n": 0,
                       "posts_market": 1, "posts_all": 1, "search_n": 0, "gdelt_mentions": 0}) == expected
    if with_hold:
        assert len(held) == 1 and "G10 explanation_failed" in held[0]


@pytest.mark.parametrize("with_hold, expected", [(False, "POSTS ONLY"), (True, "HELD")])
def test_saved_csv_and_console_keep_incidental_matches_out_of_coverage(tmp_path, monkeypatch, capsys,
                                                                      with_hold, expected):
    path = tmp_path / "moments.csv"
    path.write_text("id,date,market,moment,any_re,and_re\n"
                    "BB,2026-10-05,NG,BBNaija finale,bbnaija,\n", encoding="utf-8")
    mapping = {("BB", "2026-10-05", "NG", "BBNaija finale", "bbnaija", ""):
               {("2026-10-05", "held", "bbnaija-item")}}
    monkeypatch.setattr(bm, "RETAINED_BRIEF_ITEMS", mapping, raising=False)
    payloads = [("2026-10-07", {"cards": [generic_card()]})]
    if with_hold:
        payloads.append(("2026-10-05", {"held_back": {"items": [held_item()]}}))
    brief_rows = [{"brief_date": day, "market": "NG", "run_id": f"fixture-{day}",
                   "published_at": f"{day}T08:00:00Z", "payload": json.dumps(payload)} for day, payload in payloads]
    answers = [[{"id": "BB", "posts_market": 1, "posts_all": 1}], [], [], [], [], brief_rows]

    class Client:
        def query(self, sql, job_config=None):
            assert sql.lstrip().split(None, 1)[0] in ("WITH", "SELECT")
            assert job_config.maximum_bytes_billed == bm.MAX_BYTES
            rows = answers.pop(0)
            return type("Result", (), {"result": lambda self: rows})()

    monkeypatch.setattr(bigquery, "Client", lambda **kwargs: Client())
    folder = tmp_path / "output"
    monkeypatch.setattr(bm.os.path, "expanduser", lambda path: str(folder))
    assert bm.main(["benchmark_moments.py", str(path)]) == 0
    output = capsys.readouterr().out
    assert "0 matched a Today record" in output
    assert "0 of 1" in output if not with_hold else "1 of 1" in output
    assert f"2026-10-05 {expected}: BBNaija finale" in output
    assert "evidence-only" in output
    [saved] = folder.glob("*.csv")
    with saved.open(encoding="utf-8", newline="") as file:
        [row] = csv.DictReader(file)
    assert row["verdict"] == expected and row["cards"] == ""
    assert "fyp, viral, nigeria" in row["brief_evidence_matches"]
    assert "not coverage" in row["brief_evidence_matches"]
    assert answers == []


@pytest.mark.parametrize("changes, mapped", [
    ({}, True),
    ({"moment": "Fuel price protest"}, False),
    ({"any_re": "fuel"}, False),
    ({"and_re": "protest"}, False),
    ({"moment": "Fuel price protest", "any_re": "fuel", "and_re": "protest"}, False),
])
def test_reviewed_mapping_binds_the_complete_catalog_definition(tmp_path, changes, mapped):
    catalog = Path(bm.__file__).parent / "benchmark" / "moments-2026-09-28.csv"
    with catalog.open(encoding="utf-8-sig", newline="") as file:
        original = next(row for row in csv.DictReader(file) if row["id"] == "NG1")
    path = tmp_path / "reused-moment-key.csv"
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(original))
        writer.writeheader()
        writer.writerow({**original, **changes})
    [moment] = bm.load_moments(path)
    item = held_item(item_id="1b6715ab16faf660a2c440ed13d03d35d6152838b6e83843b2934a06f21684db")
    cards, held = bm.brief_hits(moment, {("2026-10-05", "NG"): {"held_back": {"items": [item]}}})
    assert cards == []
    if mapped:
        assert len(held) == 1 and "retained item match" in held[0]
    else:
        assert moment["retained_items"] == set()
        assert held == []
