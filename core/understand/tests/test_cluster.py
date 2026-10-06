"""Task 2.2 clustering: BERTopic on stored embeddings, matching to cultural_map, the versioned MERGE and the
discovery review. The SQL runs on DuckDB (transpiled from BigQuery by sqlglot) against fixture tables."""
import csv
import hashlib
import io
import json
import math
import random
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
import sqlglot
from sqlglot import exp

from core.understand import age_scan, cluster, discovery_review

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SQL_FILES = ("cluster_done", "cluster_posts", "cluster_items", "cluster_write", "cluster_review")
DAY = date(2026, 9, 28)
DIM = 768
REAL_NET_MODEL = getattr(cluster, "net_model", None)  # before clean_net stands in for it


def sql(name):
    return (SQL_DIR / f"{name}.sql").read_text(encoding="utf-8")


class NetModel:
    """A stand-in for the fast model as the label net: flags each label and keyword flag(text) is true for, or returns out
    as given, or raises. Each call is logged, and in log when one is given."""

    def __init__(self, flag=lambda text: False, out=None, raises=None, usage=(900, 60), log=None):
        self.flag, self.out, self.raises, self.usage, self.log, self.calls = flag, out, raises, usage, log, []

    def complete_json(self, *, system, user, schema, model, max_tokens):
        self.calls.append({"system": system, "user": user, "schema": schema, "model": model,
                           "max_tokens": max_tokens})
        if self.log is not None:
            self.log.append("net")
        usage = {"input_tokens": self.usage[0], "output_tokens": self.usage[1]}
        if self.raises is not None:
            self.raises.usage = usage
            raise self.raises
        if self.out is not None:
            return self.out, usage
        items = json.loads(re.search(r"<untrusted_content>\n(.*)\n</untrusted_content>", user, re.S).group(1))
        return {"clusters": [{"index": item["index"],
                              "flagged_labels": [i for i, t in enumerate(item["labels"]) if self.flag(t)],
                              "flagged_keywords": [i for i, t in enumerate(item["keywords"]) if self.flag(t)]}
                             for item in items]}, usage


@pytest.fixture(scope="module", autouse=True)
def clean_net():
    """Every run in this module meets a net that flags nothing, a day with room under the cap, and a booking that
    records without a warehouse. The label net tests replace each of these with their own. The net's model is priced
    at USD 1 input and USD 5 output per million tokens, so its spend reads plainly."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("GEMINI_PRICE_INPUT_PER_M", "1")
        patch.setenv("GEMINI_PRICE_OUTPUT_PER_M", "5")
        patch.setattr(cluster, "net_model", NetModel)
        patch.setattr(cluster, "spend_today", lambda execute, now: (0.0, 20.0))
        patch.setattr(cluster, "book_spend", lambda execute, **kw: round(kw["usd"], 6))
        yield


# SQL shape

def test_cluster_sql_files_are_the_expected_set():
    assert {p.stem for p in SQL_DIR.glob("cluster_*.sql")} == set(SQL_FILES)


@pytest.mark.parametrize("name", SQL_FILES)
def test_sql_parses_fully_as_bigquery(name):
    statements = [s for s in sqlglot.parse(sql(name), read="bigquery") if s is not None]
    assert statements
    assert not any(isinstance(s, exp.Command) for s in statements)


@pytest.mark.parametrize("name", SQL_FILES)
def test_every_create_is_if_not_exists(name):
    creates = re.findall(r"\bCREATE\s+(?:TEMP\s+)?(?:TABLE|VIEW)\s+(IF\s+NOT\s+EXISTS)?", sql(name), re.I)
    assert all(creates), f"{name}.sql has a CREATE without IF NOT EXISTS"


@pytest.mark.parametrize("name", SQL_FILES)
def test_no_destructive_statement_or_expiry(name):
    text = sql(name)
    assert not re.search(r"\b(DROP|TRUNCATE|DELETE|REPLACE)\b", text, re.I)
    assert not re.search(r"expir", text, re.I)
    assert not re.search(r"\{\w*\}|%s|%\(", text), f"{name}.sql must not be string-formatted"


def _queries(statement):
    if isinstance(statement, exp.Query):
        yield statement
    elif isinstance(statement, (exp.Create, exp.Insert)) and isinstance(statement.expression, exp.Query):
        yield statement.expression
    elif isinstance(statement, exp.Merge):
        yield statement.args["using"].this


@pytest.mark.parametrize("name", SQL_FILES)
def test_no_correlated_subquery_reads_another_table(name):
    # BigQuery refuses a correlated subquery over a table or CTE unless it can turn it into a join, so every such
    # read is written as a join. A correlated subquery over UNNEST of the outer row's own array is allowed.
    from sqlglot.optimizer.scope import traverse_scope

    found = []
    for statement in sqlglot.parse(sql(name), read="bigquery"):
        for query in _queries(statement) if statement is not None else ():
            for scope in traverse_scope(query):
                tables = sorted({t.name for t in scope.expression.find_all(exp.Table)})
                if scope.is_correlated_subquery and tables:
                    found.append((sorted({c.table for c in scope.external_columns}), tables))
    assert found == [], f"{name}.sql has correlated subqueries (outer aliases, tables read): {found}"


@pytest.mark.parametrize("name", [n for n in SQL_FILES if n != "cluster_write"])
def test_read_queries_write_nothing(name):
    assert not re.search(r"\b(MERGE|UPDATE|INSERT)\b", sql(name), re.I)
    assert isinstance(sqlglot.parse(sql(name), read="bigquery")[-1], exp.Select)


def test_cultural_map_reads_only_current_rows():
    text = sql("cluster_items")
    assert re.search(r"cm\.valid_to\s+IS\s+NULL", text)
    assert re.search(r"cm\.kind\s*=\s*'topic'", text)


def test_write_is_one_versioning_merge_and_two_appends_in_a_transaction():
    statements = [s for s in sqlglot.parse(sql("cluster_write"), read="bigquery") if s is not None]
    merges = [s for s in statements if isinstance(s, exp.Merge)]
    assert len(merges) == 1
    merge = merges[0]
    assert merge.this.name == "cultural_map"
    whens = list(merge.args["whens"].expressions)
    matched = [w for w in whens if w.args.get("matched")]
    unmatched = [w for w in whens if not w.args.get("matched")]
    assert len(matched) == 1 and len(unmatched) == 1
    update = matched[0].args["then"]
    assert isinstance(update, exp.Update)
    # A matched row is only closed: valid_to is the one column the MERGE ever changes on an existing row.
    assert [e.this.name for e in update.expressions] == ["valid_to"]
    insert = unmatched[0].args["then"]
    assert isinstance(insert, exp.Insert)
    assert "valid_from" in [c.name for c in insert.this.expressions]
    assert "valid_to IS NULL" in merge.args["on"].sql("bigquery")
    inserts = [s for s in statements if isinstance(s, exp.Insert)]
    assert sorted(s.this.find(exp.Table).name for s in inserts) == ["cluster_members", "clusters"]
    assert not any(isinstance(s, exp.Update) for s in statements)
    kinds = [type(s).__name__ for s in statements]
    assert kinds.index("Transaction") < kinds.index("Merge") < kinds.index("Commit")
    assert set(re.findall(r"@(\w+)", sql("cluster_write"))) == {"run_date", "market", "map_rows", "cluster_rows",
                                                                   "member_rows"}


# Pure rules

def unit(*values):
    v = np.array(values, dtype=float)
    return (v / np.linalg.norm(v)).tolist()


def at(cos, dim=6, towards=1):
    """A unit vector at the given cosine from the first axis."""
    v = [0.0] * dim
    v[0], v[towards] = cos, math.sqrt(1 - cos * cos)
    return v


def a_cluster(cid, vec, keywords=(), hashtags=(), sounds=(), creators=()):
    return {"cluster_id": cid, "centroid": list(vec), "keywords": list(keywords), "hashtags": list(hashtags),
            "sounds": list(sounds), "creators": list(creators)}


def an_item(iid, vec, last_seen, keywords=(), hashtags=(), sounds=(), creators=(), recurrences=0):
    return {"item_id": iid, "kind": "topic", "canonical_key": f"topic:{iid}", "label": iid, "aliases": [],
            "parent_item_id": None, "centroid": list(vec), "first_seen": date(2026, 6, 1), "first_seen_market": "za",
            "first_seen_platform": "tiktok", "last_seen": last_seen, "recurrences": recurrences, "lifecycle": None,
            "status": "active", "rejected_until": None, "keywords": list(keywords), "hashtags": list(hashtags),
            "sounds": list(sounds), "creators": list(creators)}


def test_ema_is_eight_tenths_old_two_tenths_new():
    assert cluster.ema([1.0, 2.0, -1.0], [3.0, 4.0, 1.0]) == pytest.approx([1.4, 2.4, -0.6])


@pytest.mark.parametrize("days, dormant", [(27, False), (28, True), (90, True), (0, False)])
def test_an_item_unseen_for_28_days_or_more_is_dormant(days, dormant):
    assert cluster.is_dormant(DAY - timedelta(days=days), DAY) is dormant


BASE = [f"w{i}" for i in range(10)]


@pytest.mark.parametrize("case, cos, item_kwargs, cluster_kwargs, last_seen_days, kind", [
    ("cosine and hashtag", 0.9, {"hashtags": ["amapiano"]}, {"hashtags": ["amapiano"]}, 10, "match"),
    ("cosine and sound", 0.9, {"sounds": ["snd_1"]}, {"sounds": ["snd_1"]}, 10, "match"),
    ("cosine only, above the variant band", 0.9, {}, {}, 10, "new"),
    ("keywords and creator without cosine", 0.5, {"keywords": BASE, "creators": ["cr_1"]},
     {"keywords": BASE[:2] + [f"x{i}" for i in range(8)], "creators": ["cr_1"]}, 10, "match"),
    ("one shared keyword of ten is under the Jaccard floor", 0.5, {"keywords": BASE, "creators": ["cr_1"]},
     {"keywords": BASE[:1] + [f"x{i}" for i in range(9)], "creators": ["cr_1"]}, 10, "new"),
    ("variant band, no votes", 0.76, {}, {}, 60, "variant"),
    ("variant band lower edge", 0.70, {}, {}, 60, "variant"),
    ("just under the variant band", 0.69, {}, {}, 60, "new"),
    ("variant band with recent and sound votes", 0.76, {"sounds": ["snd_1"]}, {"sounds": ["snd_1"]}, 2, "match"),
    ("cosine and seen 7 days ago", 0.9, {}, {}, 7, "match"),
    ("cosine and seen 8 days ago", 0.9, {}, {}, 8, "new"),
    ("dormant 28 days, cosine and creator", 0.9, {"creators": ["cr_1"]}, {"creators": ["cr_1"]}, 28, "recurrence"),
    ("27 days, cosine and creator", 0.9, {"creators": ["cr_1"]}, {"creators": ["cr_1"]}, 27, "match"),
    ("far away", 0.3, {}, {}, 1, "new"),
])
def test_match_rule(case, cos, item_kwargs, cluster_kwargs, last_seen_days, kind):
    c = a_cluster("c1", at(1.0), **cluster_kwargs)
    item = an_item("m1", at(cos), DAY - timedelta(days=last_seen_days), **item_kwargs)
    [decision] = cluster.assign([c], [item], DAY)
    assert decision["kind"] == kind, case
    if kind == "new":
        assert decision["item_id"] is None
    else:
        assert decision["item_id"] == "m1"


def test_hashtags_compare_without_case_or_hash():
    c = a_cluster("c1", at(1.0), hashtags=["#Amapiano"])
    item = an_item("m1", at(0.9), DAY - timedelta(days=30), hashtags=["amapiano"])
    assert cluster.assign([c], [item], DAY)[0]["kind"] == "recurrence"


def test_hungarian_assignment_is_one_to_one_and_beats_greedy():
    x, y = unit(1, 0, 0, 0), unit(0, 1, 0, 0)
    recent = DAY - timedelta(days=1)
    items = [an_item("X", x, recent, hashtags=["h"], creators=["cr_x"]), an_item("Y", y, recent, hashtags=["h"])]
    # c1 prefers X (hashtag, creator, recent) over Y (hashtag, recent); c2 can only match X (cosine, recent).
    # Greedy gives c1 to X and leaves c2 unmatched; the optimal one-to-one assignment is c1 to Y and c2 to X.
    c1 = a_cluster("c1", unit(1, 1, 0, 0), hashtags=["h"], creators=["cr_x"])
    c2 = a_cluster("c2", unit(1, 0, 0, 0.1))
    decisions = {d["cluster_id"]: d for d in cluster.assign([c1, c2], items, DAY)}
    assert (decisions["c1"]["kind"], decisions["c1"]["item_id"]) == ("match", "Y")
    assert (decisions["c2"]["kind"], decisions["c2"]["item_id"]) == ("match", "X")


def test_two_clusters_never_match_one_item():
    recent = DAY - timedelta(days=1)
    items = [an_item("X", at(1.0), recent, hashtags=["h"])]
    clusters = [a_cluster(f"c{i}", at(0.95 - i * 0.01), hashtags=["h"]) for i in range(3)]
    decisions = cluster.assign(clusters, items, DAY)
    matched = [d["item_id"] for d in decisions if d["kind"] in ("match", "recurrence")]
    assert matched == ["X"]


def test_only_the_top_three_items_by_centroid_are_candidates():
    recent = DAY - timedelta(days=1)
    # Four items all share the hashtag and were seen recently (two votes each); the one furthest away is
    # fourth by cosine, so it is never a candidate even though its votes would match.
    items = [an_item(f"m{i}", at(0.95 - i * 0.05), recent, hashtags=["h"]) for i in range(3)]
    items.append(an_item("far", at(0.2), recent, hashtags=["h"]))
    clusters = [a_cluster(f"c{i}", at(1.0), hashtags=["h"]) for i in range(4)]
    decisions = cluster.assign(clusters, items, DAY)
    assert "far" not in {d["item_id"] for d in decisions}
    assert sorted(d["kind"] for d in decisions).count("match") == 3


def test_plan_updates_by_ema_and_versions_recurrences_variants_and_new_items():
    old = an_item("dormant", at(0.9), DAY - timedelta(days=40), creators=["cr_1"], recurrences=2)
    parent = an_item("parent", unit(0, 0, 0, 1, 0, 0), DAY - timedelta(days=60))
    c_rec = a_cluster("20260928-za-000", at(1.0), creators=["cr_1"])
    c_var = a_cluster("20260928-za-001", unit(0, 0, 0, 0.76, math.sqrt(1 - 0.76 ** 2), 0))
    c_new = a_cluster("20260928-za-002", unit(0, 0, 0, 0, 0, 1))
    clusters = [c_rec, c_var, c_new]
    for c, n in zip(clusters, (3, 4, 5)):
        c.update(label=f"label {n}", members=[(f"p{n}{i}", 0.5) for i in range(n)], market="za", platform="tiktok",
                 local_terms=[])
    decisions = cluster.assign(clusters, [old, parent], DAY)
    assert [d["kind"] for d in decisions] == ["recurrence", "variant", "new"]
    plan = cluster.plan(clusters, decisions, [old, parent], DAY, "za")
    rows = {r["item_id"]: r for r in plan["map_rows"]}
    rec = rows["dormant"]
    assert rec["change"] == "update" and rec["recurrences"] == 3 and rec["last_seen"] == DAY.isoformat()
    assert rec["centroid"] == pytest.approx(cluster.ema(old["centroid"], c_rec["centroid"]), abs=1e-6)
    assert rec["first_seen"] == old["first_seen"].isoformat() and rec["label"] == "dormant"
    inserts = [r for r in plan["map_rows"] if r["change"] == "insert"]
    variant = next(r for r in inserts if r["parent_item_id"] == "parent")
    new = next(r for r in inserts if r["parent_item_id"] is None)
    for row, c in ((variant, c_var), (new, c_new)):
        assert row["kind"] == "topic" and row["status"] == "active" and row["recurrences"] == 0
        assert row["first_seen"] == row["last_seen"] == DAY.isoformat()
        assert row["canonical_key"] == f"topic:{c['cluster_id']}"
        assert row["item_id"] == cluster.new_item_id("topic", row["canonical_key"])
        assert row["centroid"] == pytest.approx(c["centroid"], abs=1e-6)
    by_cluster = {r["cluster_id"]: r for r in plan["cluster_rows"]}
    assert by_cluster[c_rec["cluster_id"]]["item_id"] == "dormant"
    assert by_cluster[c_rec["cluster_id"]]["match_kind"] == "recurrence"
    assert by_cluster[c_var["cluster_id"]]["item_id"] == variant["item_id"]
    assert by_cluster[c_new["cluster_id"]]["match_kind"] == "new"
    assert len(plan["member_rows"]) == 12


def test_near_duplicates_at_or_above_0_9_go_to_merge_review():
    # Cosine 0.95 is one vote only, so the cluster becomes a new item; the pair goes to the weekly merge review.
    near = an_item("near", at(0.95), DAY - timedelta(days=20))
    far = an_item("far", at(0.85, towards=2), DAY - timedelta(days=20))
    c = a_cluster("20260928-za-000", at(1.0))
    c.update(label="x", members=[("p1", 1.0)], market="za", platform="tiktok", local_terms=[])
    decisions = cluster.assign([c], [near, far], DAY)
    assert decisions[0]["kind"] == "new"
    plan = cluster.plan([c], decisions, [near, far], DAY, "za")
    new_id = plan["map_rows"][0]["item_id"]
    assert plan["merge_review"] == [{"item_a": min(new_id, "near"), "item_b": max(new_id, "near"),
                                     "cosine": pytest.approx(0.95, abs=1e-6)}]


@pytest.mark.parametrize("block", [1024, 1, 3])
def test_merge_review_pairs_recompute_every_current_pair_at_0_9_or_above(monkeypatch, block):
    monkeypatch.setattr(cluster, "REVIEW_BLOCK", block)
    items = [{"item_id": "d", "centroid": at(0.3, towards=3)}, {"item_id": "c", "centroid": at(0.9, towards=2)},
             {"item_id": "b", "centroid": at(0.95)}, {"item_id": "a", "centroid": at(1.0)}]
    # a and b at 0.95, a and c at 0.9; b and c meet at 0.855 and d is far from all three.
    assert cluster.merge_review_pairs(items) == [
        {"item_a": "a", "item_b": "b", "cosine": pytest.approx(0.95, abs=1e-6)},
        {"item_a": "a", "item_b": "c", "cosine": pytest.approx(0.9, abs=1e-6)}]
    assert cluster.merge_review_pairs(items[:1]) == [] and cluster.merge_review_pairs([]) == []


# Labels

@pytest.mark.parametrize("text, clean", [
    ("jollof rice", True), ("amapiano, dance, log drum", True), ("youth jollof", False), ("teens dance", False),
    ("#genzprotest", False), ("young brand launch", True),
    # Gen Z run into a lowercased hashtag word, as the vectorizer hands it over.
    ("genzprotests", False), ("genzke", False), ("genzrevolution", False), ("nairobi genzprotests", False),
    ("genzprotests, nairobi, bill", False), ("gen_z", False), ("generationalpha", False), ("genx", False),
    ("genalpha kota", False), ("genre", True), ("generator", True), ("general strike", True), ("genius", True),
])
def test_age_scan(text, clean):
    assert cluster.passes_age_scan(text) is clean


# Age words run into a longer token, as KE protest hashtags and TikTok age tags reach the scan.
AGE_PROBES = ["kenyangenz", "wearegenz", "nairobigenz", "kotagenz", "rejectfinancebillgenz", "iamgenalpha",
              "zoomerlife", "millennialsbelike", "boomerhumor", "okboomer", "teenlife", "teenagelife",
              "teenagersbelike", "youthvibes", "kidsoftiktok", "bornfree", "bornfrees", "digitalnatives", "90sbabies",
              "nigerianyouths", "nigerianyouth", "kenyanyouth", "kenyanyouths", "sayouth", "africanyouth",
              "mzansiyouth", "ancyouthleague", "naijateens", "kenyanteens", "vijanawakenya", "naijachildren",
              "toddlerlife", "babiesoftiktok", "sayouthparliament", "naijapikin", "pikinsoftiktok", "wazeewetu",
              "youngstersza", "infantsza", "tweenlife", "firsttimevoters", "firsttimevoterske", "oldheadsza",
              "youngpeople", "olderpeople", "sayouthactivists", "sayouthempowerment", "youthaffairs",
              "kenyanyouthagenda",
              # Kid, child and the Swahili, isiZulu, isiXhosa and Afrikaans words for children and young people.
              "kid", "kiddies", "kiddos", "kidz", "kidzone", "kiddiesparty", "naijakid", "kidlife", "child",
              "childcare", "underage", "underagedrinking", "agemates", "myagemates", "watoto", "watotowetu",
              "abantwana", "abantwanabethu", "intsha", "intshayamzansi", "abasha", "abashabamzansi", "jongmense",
              "jongmensevandag",
              # Afrikaans, Hausa, Yoruba and Igbo words for young people or children, the Swahili and isiZulu
              # singulars, and the isiZulu locatives.
              "tieners", "tiener", "tienerlewe", "jeug", "jeugd", "jeugvandag", "matasa", "matasan", "matasanmu",
              "yara", "yaran", "yaranmu", "omode", "omodeyoruba", "awonomode", "umuaka", "umuakaigbo", "kijana",
              "kijanamdogo", "mtoto", "mtotowangu", "umntwana", "umntwanawami", "ebantwaneni", "kubantwana",
              # ama plus a year or decade, the ZA word for people born then; zillennial, juvenile, iGen, and the
              # isiZulu word for old men.
              "ama2000", "ama2000s", "ama-2000", "ama_2000s", "ama2k", "ama2ks", "ama1990s", "ama2005", "ama90s",
              "ama-90s", "ama2000sbelike", "zillennial", "zillennials", "zilennial", "juvenile", "juveniles",
              "juvenilejustice", "igen", "igens", "igeneration", "amakhehla",
              # The isiZulu words for children and child, and the ZA slang for youngsters, old men and grannies.
              "izingane", "ingane", "izinganezami", "zingane", "ezinganeni", "enganeni", "kwengane", "nengane",
              "laaitie", "laaities", "laaitjie", "laaitjies", "madala", "madalas", "amagogo", "toppies", "toppie",
              "toppiesvandag", "whizkids", "whizkid",
              # Tone-marked Yoruba and Igbo, as written with their diacritics.
              "\u1ecdm\u1ecdd\u00e9", "\u00e0w\u1ecdn \u1ecdm\u1ecdd\u00e9", "\u1ee5m\u1ee5aka", "\u1ee5m\u1ee5 aka",
              # Afrikaans kinders and kindertjies (children), jong mense run apart, and the Igbo possessive umu akam.
              "kinders", "die kinders", "ons kinders", "Afrikaanse kinders", "kindertjies", "jong mense",
              "umu akam",
              # Afrikaans kinder compounds and other Afrikaans words for toddlers, youngsters and young girls.
              "kindersorg", "kinderstoel", "kinderhuis", "kindermishandeling", "kinderwelsyn", "kinderoppas",
              "Kinderhuis Mzansi", "jongmensvereniging", "jong mensen", "kleuters", "kleuterskool", "jongspan",
              "jongmeisies", "jong meisies",
              # umu aka and jong mense apart by any separator AMA_YEAR takes, the Unicode hyphens and dashes too.
              "umu.aka", "umu\u2011akam", "umu\u2014aka", "jong.mense", "jong\u2010mense", "jong\u2013mense",
              "jong\u2212mense",
              # Yoruba awon omo (the children), with and without its tone marks, and the Sesotho and Setswana
              # words for young people.
              "awon omo", "awonomo", "\u00e0w\u1ecdn \u1ecdm\u1ecd", "bacha", "baswa",
              # young run into a longer token, the joined form of Ask's young pattern.
              "youngafricans", "youngandfree", "youngkingsza", "youngerfolk"]
# Words that share a stem with an age word and are not one.
CLEAN_NEIGHBOURS = ["genre", "genres", "general", "general strike", "genius", "generator", "generous", "genesis",
                    "gentle", "gents", "genuine", "gender", "geneva", "gengetone", "genge", "progeny", "oxygen",
                    "boomerang", "generational", "young brand", "youth day", "youthday", "youthmonth", "canteen",
                    "fifteen", "seventeen", "canteens", "sixteen", "thirteen", "nineteen", "eighteen", "childrensday",
                    "sidekicks", "skidrow", "hydrogen", "genocide", "generations", "powergeneration",
                    "oldschool", "matric", "alpha", "boomerangs", "thankyouthuli", "weloveyouthuli", "loveyouthabo",
                    "missyouthando", "seeyouthere", "soyouthinkyoucandance", "furbabies", "plantbabies", "sugarbabies",
                    "fourteen", "umpteen", "seniorcounsel", "between", "tweets", "infantry", "goldheads", "boldpeople",
                    "kidney", "kidneys", "kidnap", "kidnapping", "skid", "skids", "childish", "abashane", "kidogo",
                    "kidding", "habasha", "paintshare", "mintshake", "jeugdag", "jeugmaand", "yarab", "kiyara",
                    "sayara", "yaraka", "commode", "photomode", "demomode", "promode", "omodele", "matasalamat",
                    # ama as a prefix or inside another word, a model number, and an era rather than an audience.
                    "amapiano", "ama2", "amalanga", "amazing", "amakhosi", "panama2000", "obama2008", "drama2024",
                    "Samsung Galaxy A20", "a2000", "2000s nostalgia", "90s music",
                    # igen inside a clean word, and Khehla the name.
                    "digen", "sigenyi", "igenga", "indigenous", "antigen", "antigens", "vigenere", "Khehla Sitole",
                    # Wizkid the artist, the isiZulu for friend (umngane, abangane), and the neighbours of toppies.
                    "Wizkid", "wizkid ayo", "Wizkid Starboy", "umngane", "abangane", "mngane", "manganese",
                    "toppiest", "stoppie", "stoppies", "toppier",
                    # young before a thing rather than people, joined as a hashtag joins it.
                    "youngbrands", "youngerbrands", "youngsounds",
                    # isiZulu words holding ngane that are not children: folktale, this side, and lingana (to equal).
                    "inganekwane", "izinganekwane", "nganeno", "ngaphesheya nganeno", "alingane", "kulingane",
                    "zilingane", "ngokulingane", "inganekwane yesiZulu",
                    # kinder before no s, the fold of accented clean words, and umu before a word other than aka.
                    "a kinder world", "Kinder Surprise", "#KinderSurprise", "umu akara", "Umu Akwa",
                    "Kinder Joy", "Kinder Bueno", "kinderspel", "Kindersley", "umuakamu", "bachata", "Bacharach",
                    "awon omoluabi",
                    "je\u00fbne", "\u1eb9\u0300k\u1ecd\u0301", "\u1eccm\u1ecd \u1eccba", "\u1eccm\u1ecd", "omo",
                    "\u1ee5m\u1ee5nna", "Umuahia",
                    "\u1eccm\u1ecdl\u00e1ra", "\u00c0k\u00e0r\u00e0", "Omo!", "Omo washing powder"]


@pytest.mark.parametrize("probe", AGE_PROBES)
def test_an_age_word_inside_a_token_is_refused(probe):
    for text in (probe, probe.upper(), f"#{probe}", f"nairobi {probe}", f"kota, {probe}, kasi"):
        assert cluster.passes_age_scan(text) is False, text
    assert cluster.clean_keywords([probe, f"kasi {probe}", "kota"]) == ["kota"]
    assert cluster.label_for([probe, "kasi", "kota"], [probe], ["s1"]) == "sound s1"
    # The keywords fail, so the label falls back to the hashtag, which fails too.
    assert cluster.label_for(["youth", "kasi"], [f"#{probe}"], ["s1"]) == "sound s1"


@pytest.mark.parametrize("word", CLEAN_NEIGHBOURS)
def test_a_clean_neighbour_of_an_age_word_passes(word):
    for text in (word, word.upper(), f"#{word.replace(' ', '')}", f"kota {word}", f"kota, {word}, kasi"):
        assert cluster.passes_age_scan(text) is True, text
    assert cluster.clean_keywords([word, "kasi", "kota"]) == [word, "kasi", "kota"]
    assert cluster.label_for([word, "kasi", "kota"], [], []) == f"{word}, kasi, kota"


@pytest.mark.parametrize("word", ["ama2000", "Ama-2000", "#ama2000", "AMA2000s", "#Ama2k", "ama 2000",
                                  "ama 2000s", "Zillennials", "Ama\u20112000s", "Ama\u20132000s", "Ama\u20142000s",
                                  "Ama.2000s", "Ama\u20102000", "ama\u2015 2k", "ama\u22122000"])
def test_an_ama_year_word_is_never_stored_or_labelled(word):
    assert cluster.passes_age_scan(word) is False
    assert cluster.clean_keywords([word, "kota", "kasi"]) == ["kota", "kasi"]
    label = cluster.label_for([word, "kota", "kasi"], [word], ["s1"])
    assert label == "sound s1" and "2000" not in label and "2k" not in label.lower()


# Proper names that hold an age word (Wizkid, Young Stunna, Kid Cudi, Youth Day) pass only as the whole value.

NAME_FORMS = ["Wizkid", "#Wizkid", "WIZKID", "Young Stunna", "#YoungStunna", "young_stunna", "Young Jonn",
              "Kid Cudi", "kid-cudi", "Teenage Dream", "Children's Day", "Children’s Day", "#ChildrensDay",
              "Youth Day", "National Youth Day", "#NationalYouthDay", "Khehla Sitole", "Khehla Sitole.",
              "Young John", "Young Stacpt", "Kiddominant", "Kid X", "Kidtini", "Young Duu", "Madala Kunene",
              "Young, Famous & African", "Young Thug", "Young Money", "Kid Ink", "#YoungThug", "Yara International",
              "Bacha Khan", "#BachaKhan"]


def test_the_names_list_is_the_agreed_proper_names_only():
    assert age_scan.NAMES == {"wizkid", "youngstunna", "youngjonn", "kidcudi", "teenagedream", "childrensday",
                              "youthday", "nationalyouthday", "khehlasitole", "youngjohn", "youngstacpt",
                              "kiddominant", "kidx", "kidtini", "youngduu", "madalakunene", "youngfamousafrican",
                              "youngthug", "youngmoney", "kidink", "yarainternational", "bachakhan"}


@pytest.mark.parametrize("name", ["wizkid", "youngstunna", "youngjonn", "kidcudi", "teenagedream", "childrensday",
                                  "youthday", "nationalyouthday", "khehlasitole", "youngjohn", "youngstacpt",
                                  "kiddominant", "kidx", "kidtini", "youngduu", "madalakunene", "youngfamousafrican",
                                  "youngthug", "youngmoney", "kidink", "yarainternational", "bachakhan"] + NAME_FORMS)
def test_a_proper_name_that_holds_an_age_word_passes_whole(name):
    assert cluster.passes_age_scan(name) is True
    assert cluster.clean_keywords([name]) == [name]
    assert cluster.label_for([], [name], ["s1"]) == f"#{name.lstrip('#').lower()}"


@pytest.mark.parametrize("text", ["youngstunnafans", "young stunna fans are teens", "Kid Cudi for kids",
                                  "Teenage Dream for teens", "wizkid kids", "Young Jonn young people",
                                  "sound Kid Cudi", "Khehla Sitole and amakhehla", "#youngstunnaforkids"])
def test_a_value_that_only_contains_a_name_is_scanned_whole(text):
    assert cluster.passes_age_scan(text) is False, text


def test_ama_and_a_year_as_two_keywords_go_as_a_pair():
    # The vectorizer hands "Ama-2000" over as "ama" and "2000"; either alone is clean, together they are the word.
    assert cluster.clean_keywords(["ama", "2000", "kota"]) == ["kota"]
    assert cluster.label_for(["ama", "2000s", "kota"], [], [], short_id="1a2b3c4d") == "Topic 1a2b3c4d"


@pytest.mark.parametrize("text", ["Children's Day", "Children’s Day", "Childrens Day", "children day",
                                  "International Children's Day", "children's day, kota"])
def test_childrens_day_passes_as_a_day_name(text):
    assert cluster.passes_age_scan(text) is True
    assert cluster.clean_keywords([text, "kota"]) == [text, "kota"]


@pytest.mark.parametrize("text", ["children", "children's party", "childrens daycare", "Children's Day kids"])
def test_children_outside_the_day_name_is_refused(text):
    assert cluster.passes_age_scan(text) is False


def test_a_refused_gen_age_pair_takes_its_single_words_with_it():
    assert cluster.clean_keywords(["gen alpha", "kota", "alpha", "dance"]) == ["kota", "dance"]
    assert cluster.clean_keywords(["generation alpha", "alpha", "kasi"]) == ["kasi"]
    # gen beside a word that is not an age word goes alone.
    assert cluster.clean_keywords(["gen dance", "dance", "kota"]) == ["dance", "kota"]
    # gen or generation as a keyword of its own takes the age word beside it in another keyword.
    assert cluster.clean_keywords(["gen", "alpha", "kota"]) == ["kota"]
    assert cluster.clean_keywords(["generation", "z", "amapiano"]) == ["amapiano"]


@pytest.mark.parametrize("tag", ["genzprotests", "genzke", "genzrevolution"])
def test_a_genz_hashtag_word_never_labels_or_is_stored(tag):
    assert cluster.label_for([tag, "nairobi", "bill"], [tag.replace("genz", "GenZ")], ["s1"]) == "sound s1"
    assert cluster.clean_keywords([tag, "nairobi", "bill"]) == ["nairobi", "bill"]


def test_label_is_top_keywords_falling_back_to_hashtag_then_sound():
    assert cluster.label_for(["amapiano", "dance", "log drum", "step"], ["amapiano"], ["snd_1"]) == \
        "amapiano, dance, log drum"
    assert cluster.label_for(["youth", "jollof", "rice"], ["jollofwars"], ["snd_1"]) == "#jollofwars"
    assert cluster.label_for(["youth", "jollof"], ["teens"], ["snd_1"]) == "sound snd_1"
    assert cluster.label_for([], [], []) == "unlabelled topic"


def test_stored_keywords_drop_age_terms():
    assert cluster.clean_keywords(["youth", "jollof", "youth jollof", "rice"]) == ["jollof", "rice"]


@pytest.mark.parametrize("words, kept", [
    (["born", "free", "kasi"], ["kasi"]),
    (["under", "25s", "kota"], ["kota"]),
    (["kota", "digital", "natives"], ["kota"]),
    # youth goes on its own, which leaves born and free side by side, and then they go too.
    (["kota", "born", "youth", "free"], ["kota"]),
    (["jollof", "rice", "party"], ["jollof", "rice", "party"]),
])
def test_stored_keywords_drop_both_words_of_an_age_pair(words, kept):
    assert cluster.clean_keywords(words) == kept


# gen or generation as a whole token, wherever it stands in a keyword or label.
BARE_GEN = re.compile(r"(?<![^\W_])gen(?:eration)?(?![^\W_])", re.I)
# Gen Z or Gen Alpha in any spelling, run together or not; independent of the scan in core.agent.checks.
GEN_RUN = re.compile(r"gen(?:eration)?[\W_]*(?:z|alpha)", re.I)


def test_stored_keywords_drop_a_bare_gen_token():
    assert cluster.clean_keywords(["gen", "gen dance", "generation", "power generation", "alpha", "genre", "kota"]) \
        == ["alpha", "genre", "kota"]


@pytest.mark.parametrize("keywords", [["gen", "alpha", "dance"], ["gen", "vibes", "amapiano"],
                                      ["born", "free", "kasi"], ["digital", "natives", "kasi"]])
def test_a_label_scans_its_keywords_joined_and_in_adjacent_pairs(keywords):
    # Each keyword passes alone in the last two cases; only the pair "born free" or "digital natives" is an age term.
    for label in (cluster.label_for(keywords, [], []), cluster.label_for(keywords, [], [], short_id="1a2b3c4d")):
        assert cluster.passes_age_scan(label) and not BARE_GEN.search(label), label
    assert cluster.label_for(keywords, [], [], short_id="1a2b3c4d") == "Topic 1a2b3c4d"
    assert cluster.label_for(keywords, ["gen"], ["snd_1"], short_id="1a2b3c4d") == "sound snd_1"
    assert cluster.label_for(keywords, ["kota"], [], short_id="1a2b3c4d") == "#kota"
    assert not any(BARE_GEN.search(k) for k in cluster.clean_keywords(keywords))


# The nightly run on fixture tables

PROJECT_DB = '"ogilvy-trends-v2"'
CORE = f"{PROJECT_DB}.intelligence_42_core"
MAP_COLUMNS = ("item_id VARCHAR, kind VARCHAR, canonical_key VARCHAR, label VARCHAR, aliases VARCHAR[], "
               "parent_item_id VARCHAR, centroid DOUBLE[], first_seen DATE, first_seen_market VARCHAR, "
               "first_seen_platform VARCHAR, last_seen DATE, recurrences BIGINT, lifecycle VARCHAR, status VARCHAR, "
               "rejected_until DATE, valid_from TIMESTAMP, valid_to TIMESTAMP")


def duck_execute(con, calls=None):
    def execute(text, params):
        if calls is not None:
            calls.append((text, dict(params)))
        # A cursor per call: its temp tables live only for this call, like a BigQuery script's.
        cur = con.cursor()
        result = None
        for statement in sqlglot.parse(text, read="bigquery"):
            if statement is None:
                continue
            duck = statement.sql("duckdb")
            used = {k: v for k, v in params.items() if re.search(rf"\${k}\b", duck)}
            result = cur.execute(duck, used)
        if result is None or not result.description:
            return []
        names = [d[0] for d in result.description]
        return [dict(zip(names, row)) for row in result.fetchall()]
    return execute


def towards(center, cos, rng):
    """A vector at the given cosine from center, with the same norm."""
    c = center / np.linalg.norm(center)
    r = rng.normal(size=center.size)
    r -= r.dot(c) * c
    r /= np.linalg.norm(r)
    return (np.linalg.norm(center) * (cos * c + math.sqrt(1 - cos * cos) * r)).tolist()


def map_row(item_id, centroid, *, last_seen, first_seen=date(2026, 7, 1), kind="topic", valid_from="2026-07-01",
            valid_to=None, label=None, recurrences=0):
    return (item_id, kind, f"{kind}:{item_id}", label or item_id, [], None, centroid, first_seen, "za", "tiktok",
            last_seen, recurrences, None, "active", None, valid_from, valid_to)


def core_db():
    import duckdb

    con = duckdb.connect()
    con.execute(f"ATTACH ':memory:' AS {PROJECT_DB}")
    con.execute(f"CREATE SCHEMA {CORE}")
    con.execute(f"CREATE TABLE {CORE}.posts (post_id VARCHAR, platform VARCHAR, creator_id VARCHAR, text VARCHAR, "
                "hashtags VARCHAR[], sound_id VARCHAR, post_date DATE)")
    con.execute(f"CREATE TABLE {CORE}.post_observations (post_id VARCHAR, observed_date DATE, market VARCHAR)")
    con.execute(f"CREATE TABLE {CORE}.post_enrichment (post_id VARCHAR, embedding DOUBLE[])")
    con.execute(f"CREATE TABLE {CORE}.cultural_map ({MAP_COLUMNS})")
    con.execute(f"CREATE TABLE {CORE}.clusters (cluster_date DATE, cluster_id VARCHAR, market VARCHAR, "
                "item_id VARCHAR, match_kind VARCHAR, label VARCHAR, keywords VARCHAR[], local_terms VARCHAR[], "
                "centroid DOUBLE[])")
    con.execute(f"CREATE TABLE {CORE}.cluster_members (cluster_id VARCHAR, post_id VARCHAR, probability DOUBLE)")
    return con


def build_world(age_topics=()):
    spec = json.loads((FIXTURES / "cluster_topics.json").read_text(encoding="utf-8"))
    rng = np.random.default_rng(42)
    words = random.Random(42)
    centers = {name: rng.normal(size=DIM) for name in spec["topics"]}
    posts, observations, vectors, truth = [], [], [], {}

    def add(post_id, text, hashtags, sound, creator, vector, day_offset, market="za"):
        seen = DAY - timedelta(days=day_offset)
        posts.append((post_id, "tiktok", creator, text, hashtags, sound, seen))
        observations.append((post_id, seen, market))
        vectors.append((post_id, [float(x) for x in vector]))

    def add_topic(name, topic, center):
        leads = topic.get("leads") or ([topic["lead"]] if "lead" in topic else [])
        for i in range(36):
            text = " ".join(([leads[i % len(leads)]] if leads else []) + words.sample(topic["words"], 4)
                            + topic.get("text_tags", []))
            post_id = f"{name}_{i:02d}"
            # The first hashtag is on every post and the second on every other one, so the first is the top hashtag.
            tags = topic["hashtags"] if i % 2 else topic["hashtags"][:1]
            add(post_id, text, [f"#{h}" for h in tags], topic["sound"], topic["creators"][i % 5],
                center + 0.5 * rng.normal(size=DIM), i % 3)
            truth[post_id] = (name, i % 3 == 0)

    for name, topic in spec["topics"].items():
        add_topic(name, topic, centers[name])
    for post in spec["multilingual"]["posts"]:
        add(post["post_id"], post["text"], [], None, "cr_ml", centers[spec["multilingual"]["topic"]]
            + 0.5 * rng.normal(size=DIM), 0)
        truth[post["post_id"]] = (spec["multilingual"]["topic"], True)
    for i, text in enumerate(spec["noise"]):
        add(f"noise_{i:02d}", text, [], None, f"cr_n{i}", 1.1 * rng.normal(size=DIM), i % 3)
    # Posts sighted only in Nigeria: never read by the za run.
    for i in range(5):
        add(f"ng_{i}", "loadshedding stage eskom", ["#loadshedding"], None, "cr_ng",
            centers["loadshedding"] + 0.5 * rng.normal(size=DIM), 0, market="ng")
    # History: amapiano was clustered three days ago with a member post carrying its hashtag.
    posts.append(("hist_a", "tiktok", "cr_old", "amapiano groove", ["#Amapiano"], None, DAY - timedelta(days=3)))
    item_a = towards(centers["amapiano"], 0.97, rng)
    item_b = towards(centers["loadshedding"], 0.76, rng)
    map_rows = [
        map_row("item_a", item_a, last_seen=DAY - timedelta(days=3), valid_from="2026-09-01"),
        map_row("item_a", [0.0] * DIM, last_seen=DAY - timedelta(days=40), valid_from="2026-07-01",
                valid_to="2026-09-01", label="item_a first version"),
        map_row("item_b", item_b, last_seen=DAY - timedelta(days=60)),
        map_row("item_z", rng.normal(size=DIM).tolist(), last_seen=DAY - timedelta(days=1)),
        map_row("tag_x", None, last_seen=DAY - timedelta(days=1), kind="hashtag"),
    ]
    # Drawn after everything the three-topic world draws, so that world is the same with or without them.
    for name in age_topics:
        add_topic(name, spec["age_topics"][name], rng.normal(size=DIM))

    con = core_db()
    con.executemany(f"INSERT INTO {CORE}.posts VALUES (?, ?, ?, ?, ?, ?, ?)", posts)
    con.executemany(f"INSERT INTO {CORE}.post_observations VALUES (?, ?, ?)", observations)
    con.executemany(f"INSERT INTO {CORE}.post_enrichment VALUES (?, ?)", vectors)
    # An enrichment row without a vector, as enrich.py writes: it must not hide the embedding.
    con.execute(f"INSERT INTO {CORE}.post_enrichment VALUES ('amapiano_00', [])")
    con.executemany(f"INSERT INTO {CORE}.cultural_map VALUES ({', '.join('?' * 17)})", map_rows)
    con.execute(f"INSERT INTO {CORE}.clusters VALUES (?, 'hist-a', 'za', 'item_a', 'new', 'amapiano', "
                "['amapiano', 'dance'], [], NULL)", [DAY - timedelta(days=3)])
    con.execute(f"INSERT INTO {CORE}.cluster_members VALUES ('hist-a', 'hist_a', 1.0)")
    before = con.execute(f"SELECT * FROM {CORE}.cultural_map ORDER BY item_id, valid_from").fetchall()
    calls, raw_keywords = [], {}
    fit = cluster.fit_topics

    def spy(docs, embeddings):
        topics, probs, keywords = fit(docs, embeddings)
        raw_keywords.update(keywords)
        return topics, probs, keywords

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cluster, "fit_topics", spy)
        counts = cluster.run_cluster(duck_execute(con, calls), run_date=DAY, market="za")
    return {"con": con, "counts": counts, "truth": truth, "before": before, "calls": calls, "item_a": item_a,
            "raw_keywords": raw_keywords}


@pytest.fixture(scope="module")
def world():
    return build_world()


@pytest.fixture(scope="module")
def gen_world():
    return build_world(age_topics=("kota",))


def members(world):
    rows = world["con"].execute(f"SELECT m.cluster_id, m.post_id, m.probability FROM {CORE}.cluster_members m "
                                "WHERE m.cluster_id != 'hist-a'").fetchall()
    return {post_id: cid for cid, post_id, _ in rows}, rows


def cluster_of(world, topic):
    by_post, _ = members(world)
    ids = {by_post[p] for p, (name, today) in world["truth"].items() if name == topic and today}
    assert len(ids) == 1, f"{topic}'s posts from today are split across {ids}"
    return ids.pop()


def cluster_row(world, cluster_id):
    names = ["cluster_date", "cluster_id", "market", "item_id", "match_kind", "label", "keywords", "local_terms",
             "centroid"]
    row = world["con"].execute(f"SELECT * FROM {CORE}.clusters WHERE cluster_id = ?", [cluster_id]).fetchone()
    return dict(zip(names, row))


def test_three_topics_form_three_clusters_of_todays_posts(world):
    assert world["counts"]["clusters"] == 3
    ids = {cluster_of(world, t) for t in ("amapiano", "loadshedding", "jollof")}
    assert len(ids) == 3
    by_post, rows = members(world)
    today = {p for p, (_, t) in world["truth"].items() if t} | {f"noise_{i:02d}" for i in (0, 3, 6, 9)}
    assert set(by_post) <= today, "only today's posts are assigned"
    assert {p for p, (_, t) in world["truth"].items() if t} <= set(by_post)
    assert len(rows) == len(by_post), "each post is a member of one cluster"
    assert all(0.0 <= prob <= 1.0 for _, _, prob in rows)
    assert not any(p.startswith("ng_") for p in by_post)
    assert world["counts"]["today_posts"] == 12 * 3 + 2 + 4


def test_multilingual_pair_lands_in_one_cluster(world):
    by_post, _ = members(world)
    assert by_post["ml_zu"] == by_post["ml_pcm"] == cluster_of(world, "amapiano")


def test_labels_pass_the_age_scan_and_fall_back_to_the_top_hashtag(world):
    labels = world["con"].execute(f"SELECT label, keywords FROM {CORE}.clusters WHERE cluster_date = ?",
                                  [DAY]).fetchall()
    assert len(labels) == 3
    for label, keywords in labels:
        assert cluster.passes_age_scan(label)
        assert keywords and all(cluster.passes_age_scan(k) for k in keywords)
    assert cluster_row(world, cluster_of(world, "jollof"))["label"] == "#jollofwars"
    assert "amapiano" in cluster_row(world, cluster_of(world, "amapiano"))["keywords"]


def test_gen_z_and_gen_alpha_posts_leave_no_age_term_or_bare_gen_in_keywords_or_labels(gen_world):
    raw = [w for words in gen_world["raw_keywords"].values() for w in words]
    assert any(BARE_GEN.search(w) for w in raw), f"the fit never surfaced gen, so this test proves nothing: {raw}"
    con = gen_world["con"]
    stored = con.execute(f"SELECT label, keywords FROM {CORE}.clusters WHERE cluster_date = ?", [DAY]).fetchall()
    labels = [label for label, _ in stored] + [r[0] for r in con.execute(f"SELECT label FROM {CORE}.cultural_map")
                                               .fetchall()]
    keywords = [k for _, ks in stored for k in ks]
    for text in labels + keywords:
        assert cluster.passes_age_scan(text) and not BARE_GEN.search(text), text
    assert any(GEN_RUN.search(w) for w in raw), f"the fit never surfaced a genz hashtag word, so this proves nothing: {raw}"
    assert [t for t in labels + keywords if GEN_RUN.search(t)] == []
    for tag in ("kenyangenz", "kidsoftiktok"):
        assert any(tag in w for w in raw), f"the fit never surfaced {tag}, so this proves nothing: {raw}"
        assert [t for t in labels + keywords if tag in t.lower()] == [], tag
    kota = cluster_row(gen_world, cluster_of(gen_world, "kota"))
    assert kota["keywords"], "the topic keeps its clean keywords"


def test_matching_gives_match_variant_and_new(world):
    a = cluster_row(world, cluster_of(world, "amapiano"))
    b = cluster_row(world, cluster_of(world, "loadshedding"))
    c = cluster_row(world, cluster_of(world, "jollof"))
    assert (a["match_kind"], a["item_id"]) == ("match", "item_a")
    assert b["match_kind"] == "variant" and b["item_id"] not in ("item_a", "item_b", "item_z")
    assert c["match_kind"] == "new"
    counts = world["counts"]
    assert (counts["matched"], counts["recurrences"], counts["variants"], counts["new"]) == (1, 0, 1, 1)
    con = world["con"]
    parent, = con.execute(f"SELECT parent_item_id FROM {CORE}.cultural_map WHERE item_id = ?", [b["item_id"]]).fetchone()
    assert parent == "item_b"
    parent, = con.execute(f"SELECT parent_item_id FROM {CORE}.cultural_map WHERE item_id = ?", [c["item_id"]]).fetchone()
    assert parent is None


def test_matched_item_is_versioned_not_overwritten(world):
    con = world["con"]
    a = cluster_row(world, cluster_of(world, "amapiano"))
    rows = con.execute(f"SELECT centroid, last_seen, valid_from, valid_to, label FROM {CORE}.cultural_map "
                       "WHERE item_id = 'item_a' ORDER BY valid_from").fetchall()
    assert len(rows) == 3
    first, closed, current = rows
    assert first[3] is not None and first[4] == "item_a first version"
    assert closed[3] is not None and closed[0] == pytest.approx(world["item_a"]) and closed[1] == DAY - timedelta(3)
    assert current[3] is None and current[1] == DAY and current[4] == "item_a"
    assert closed[3] == current[2], "the old row closes when the new version opens"
    assert current[0] == pytest.approx(cluster.ema(world["item_a"], a["centroid"]), abs=1e-5)


def test_other_rows_are_untouched_and_only_three_rows_are_added(world):
    con = world["con"]
    after = con.execute(f"SELECT * FROM {CORE}.cultural_map ORDER BY item_id, valid_from").fetchall()
    assert len(after) == len(world["before"]) + 3
    untouched = [r for r in world["before"] if r[0] in ("item_b", "item_z", "tag_x")]
    assert all(r in after for r in untouched)
    current = con.execute(f"SELECT item_id, COUNT(*) FROM {CORE}.cultural_map WHERE valid_to IS NULL "
                          "GROUP BY item_id HAVING COUNT(*) > 1").fetchall()
    assert current == []


def test_a_second_run_the_same_day_changes_nothing(world):
    con = world["con"]
    snapshot = [con.execute(f"SELECT * FROM {CORE}.{t} ORDER BY ALL").fetchall()
                for t in ("cultural_map", "clusters", "cluster_members")]
    counts = cluster.run_cluster(duck_execute(con), run_date=DAY, market="za")
    assert counts["skipped"] == "already_clustered"
    # The write script guards itself too: replaying the first run's write adds and closes nothing.
    write_text, write_params = next((t, p) for t, p in world["calls"] if t == cluster.load("cluster_write"))
    duck_execute(con)(write_text, write_params)
    assert snapshot == [con.execute(f"SELECT * FROM {CORE}.{t} ORDER BY ALL").fetchall()
                        for t in ("cultural_map", "clusters", "cluster_members")]


def test_pan_run_reads_every_market_and_a_market_run_only_its_own(world):
    execute = duck_execute(world["con"])
    za = {r["post_id"] for r in execute(cluster.load("cluster_posts"), {"market": "za", "run_date": DAY})}
    pan = {r["post_id"] for r in execute(cluster.load("cluster_posts"), {"market": "pan", "run_date": DAY})}
    assert not any(p.startswith("ng_") for p in za)
    assert {f"ng_{i}" for i in range(5)} <= pan and za <= pan
    assert "hist_a" not in pan, "a post not sighted in the three days is not read"


def test_item_profile_carries_history_for_the_votes(world):
    execute = duck_execute(world["con"])
    # The day before the run, so today's clusters stay out of the profile; today's new items are current rows too.
    items = {r["item_id"]: r for r in execute(cluster.load("cluster_items"), {"run_date": DAY - timedelta(days=1)})}
    assert {"item_a", "item_b", "item_z"} <= set(items) and "tag_x" not in items
    assert items["item_a"]["hashtags"] == ["amapiano"]
    assert items["item_a"]["creators"] == ["cr_old"]
    assert items["item_a"]["keywords"] == ["amapiano", "dance"]
    assert list(items["item_b"]["hashtags"] or []) == []


GOLDEN = FIXTURES / "cluster_world_result.json"
CENTROID_HEAD = 8


def _centroid_summary(values):
    """A centroid as its length, norm, sum and first components, rounded so float32 storage compares equal."""
    v = np.asarray(values, dtype=float)
    return {"n": int(v.size), "norm": round(float(np.linalg.norm(v)), 4), "sum": round(float(v.sum()), 4),
            "head": [round(float(x), 4) for x in v[:CENTROID_HEAD]]}


def world_result(world):
    """What the za run wrote and counted, less the write timestamps: the result the memory changes must keep."""
    con = world["con"]
    clusters = con.execute(f"SELECT cluster_id, item_id, match_kind, label, keywords, centroid FROM {CORE}.clusters "
                           "WHERE cluster_date = ? ORDER BY cluster_id", [DAY]).fetchall()
    map_rows = con.execute(f"SELECT item_id, label, parent_item_id, last_seen, recurrences, status, centroid "
                           f"FROM {CORE}.cultural_map WHERE valid_to IS NULL AND kind = 'topic' ORDER BY item_id"
                           ).fetchall()
    counts = dict(world["counts"])
    review = counts.pop("merge_review")
    return {
        "counts": counts,
        "merge_review": [{**r, "cosine": round(r["cosine"], 4)} for r in review],
        "clusters": [{"cluster_id": c, "item_id": i, "match_kind": k, "label": label, "keywords": list(kw),
                      "centroid": _centroid_summary(cen)} for c, i, k, label, kw, cen in clusters],
        "members": sorted([list(r) for r in members(world)[1]]),
        "map": [{"item_id": i, "label": label, "parent_item_id": parent, "last_seen": last.isoformat(),
                 "recurrences": rec, "status": status, "centroid": _centroid_summary(cen)}
                for i, label, parent, last, rec, status, cen in map_rows],
    }


def test_the_za_run_on_the_fixture_world_writes_the_recorded_result(world):
    # Recorded from the clustering code before embeddings were held as float32 and the run was capped, so this
    # proves those changes leave a run under the cap as it was.
    assert world_result(world) == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_too_few_posts_writes_nothing():
    calls = []

    def execute(text, params):
        calls.append(text)
        return [{"n": 0}] if text == cluster.load("cluster_done") else []

    counts = cluster.run_cluster(execute, run_date=DAY, market="ke")
    assert counts["skipped"] == "too_few_posts"
    assert cluster.load("cluster_write") not in calls


def test_unknown_market_is_refused():
    with pytest.raises(ValueError):
        cluster.run_cluster(lambda *a: [], run_date=DAY, market="gh")


def test_a_post_with_two_vector_rows_reads_the_same_one_whatever_the_insert_order():
    low, high = [0.1, 0.9, 0.3], [0.7, 0.2, 0.3]
    picked = []
    for order in ((high, low), (low, high)):
        con = core_db()
        con.execute(f"INSERT INTO {CORE}.posts VALUES ('p1', 'tiktok', 'cr_1', 'kota', [], NULL, ?)", [DAY])
        con.execute(f"INSERT INTO {CORE}.post_observations VALUES ('p1', ?, 'za')", [DAY])
        for vector in order:
            con.execute(f"INSERT INTO {CORE}.post_enrichment VALUES ('p1', ?)", [vector])
        [row] = duck_execute(con)(cluster.load("cluster_posts"), {"run_date": DAY, "market": "za"})
        picked.append(list(row["embedding"]))
    assert picked == [low, low]


def uppercase_market_db():
    """post_observations as collect writes it, market upper case (core/collect/parse.py), while the job runs the
    clustering markets lower case (cluster.MARKETS)."""
    con = core_db()
    rows = [(f"za_{i:02d}", "ZA") for i in range(cluster.MIN_POSTS)] + [(f"ng_{i}", "NG") for i in range(3)]
    for post_id, market in rows:
        con.execute(f"INSERT INTO {CORE}.posts VALUES (?, 'tiktok', 'cr_1', 'kota', [], NULL, ?)", [post_id, DAY])
        con.execute(f"INSERT INTO {CORE}.post_observations VALUES (?, ?, ?)", [post_id, DAY, market])
        con.execute(f"INSERT INTO {CORE}.post_enrichment VALUES (?, ?)", [post_id, [1.0, 0.0, 0.0]])
    return con


def test_a_market_run_reads_the_upper_case_markets_collect_writes():
    execute = duck_execute(uppercase_market_db())
    za = execute(cluster.load("cluster_posts"), {"market": "za", "run_date": DAY})
    ng = execute(cluster.load("cluster_posts"), {"market": "ng", "run_date": DAY})
    pan = execute(cluster.load("cluster_posts"), {"market": "pan", "run_date": DAY})
    assert {r["post_id"] for r in za} == {f"za_{i:02d}" for i in range(cluster.MIN_POSTS)}
    assert {r["post_id"] for r in ng} == {f"ng_{i}" for i in range(3)}
    assert len(pan) == cluster.MIN_POSTS + 3
    # The market a post carries is the one collect wrote, as the pooled run's first_seen_market takes it.
    assert {r["market"] for r in za} == {"ZA"} and {r["market"] for r in pan} == {"ZA", "NG"}


def test_the_job_markets_reach_the_fit_on_upper_case_observations():
    class Fitted(Exception):
        pass

    def fit(docs, embeddings):
        raise Fitted(len(docs))

    execute = duck_execute(uppercase_market_db())
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cluster, "fit_topics", fit)
        with pytest.raises(Fitted) as fitted:
            cluster.run_cluster(execute, run_date=DAY, market="za")
    assert fitted.value.args == (cluster.MIN_POSTS,)
    assert cluster.run_cluster(execute, run_date=DAY, market="ng") == {
        "market": "ng", "posts": 3, "today_posts": 3, "skipped": "too_few_posts"}


WRITE_PARAMS = ("map_rows", "cluster_rows", "member_rows")


def open_versions(con, item_id):
    return con.execute(f"SELECT COUNT(*) FROM {CORE}.cultural_map WHERE item_id = ? AND valid_to IS NULL",
                       [item_id]).fetchone()[0]


def test_concurrent_market_and_pan_updates_of_one_item_leave_one_open_version():
    item = an_item("x", at(1.0), DAY - timedelta(days=1), hashtags=["h"])

    def write_params(market):
        c = a_cluster(f"{DAY:%Y%m%d}-{market}-000", at(0.99), hashtags=["h"])
        c.update(label="x", members=[(f"{market}_p1", 1.0)], market="za", platform="tiktok", local_terms=[])
        planned = cluster.plan([c], cluster.assign([c], [item], DAY), [item], DAY, market)
        assert [r["change"] for r in planned["map_rows"]] == ["update"]
        return {"run_date": DAY, "market": market, **{k: json.dumps(planned[k]) for k in WRITE_PARAMS}}

    # Both runs matched x from the same snapshot of the map.
    za, pan = write_params("za"), write_params("pan")
    write = cluster.load("cluster_write")
    # Committed one after the other: pan's close lands on the version za opened.
    con = core_db()
    con.execute(f"INSERT INTO {CORE}.cultural_map VALUES ({', '.join('?' * 17)})",
                map_row("x", at(1.0), last_seen=DAY - timedelta(days=1)))
    duck_execute(con)(write, za)
    duck_execute(con)(write, pan)
    assert open_versions(con, "x") == 1
    assert con.execute(f"SELECT COUNT(*) FROM {CORE}.cultural_map WHERE item_id = 'x'").fetchone()[0] == 3
    # Pan's MERGE re-reads the row za already closed and does not see za's new version, as a MERGE racing
    # another commit can: its close finds no open row, and that close row must not open a version of its own.
    con = core_db()
    con.execute(f"INSERT INTO {CORE}.cultural_map VALUES ({', '.join('?' * 17)})",
                map_row("x", at(1.0), last_seen=DAY - timedelta(days=1), valid_to="2026-09-28 00:30:00"))
    duck_execute(con)(write, pan)
    assert open_versions(con, "x") == 1


def synthetic_clusters(n, dim=8, members=2, spread=1.0, seed=0):
    rng = np.random.default_rng(seed)
    base = rng.normal(size=dim)
    out = []
    for t in range(n):
        cid = f"{DAY:%Y%m%d}-za-{t:03d}"
        out.append({"cluster_id": cid, "centroid": (base + spread * rng.normal(size=dim)).tolist(),
                    "keywords": ["kota"], "label": "kota", "hashtags": [], "sounds": [], "creators": [],
                    "members": [(f"{cid}-p{i}", 0.5) for i in range(members)], "market": "za",
                    "platform": "tiktok", "local_terms": []})
    return out


def fake_run(monkeypatch, clusters, written=None, items=(), model=None, log=None, clock=None):
    """run_cluster with the fit replaced by the given clusters and cluster_items answering items. The write answers
    with written when given, else with the counts it was sent, as the write script's last statement does. Each
    statement's name goes in log when one is given."""
    monkeypatch.setattr(cluster, "fit_topics", lambda docs, emb: ([0] * len(docs), [1.0] * len(docs), {}))
    monkeypatch.setattr(cluster, "build_clusters", lambda *args: clusters)
    writes = []

    def execute(text, params):
        if log is not None:
            log.append(next((n for n in SQL_FILES if text == cluster.load(n)), text))
        if text == cluster.load("cluster_done"):
            return [{"n": 0}]
        if text == cluster.load("cluster_posts"):
            return [{"post_id": f"p{i}", "text": "", "today": True, "embedding": [1.0, 0.0]}
                    for i in range(cluster.MIN_POSTS)]
        if text == cluster.load("cluster_items"):
            return [dict(i) for i in items]
        assert text == cluster.load("cluster_write")
        writes.append(params)
        if written is not None:
            return [written]
        rows = {k: json.loads(params[k]) for k in WRITE_PARAMS}
        kinds = Counter(r["match_kind"] for r in rows["cluster_rows"])
        return [{**{k: len(v) for k, v in rows.items()}, "matched": kinds["match"],
                 "recurrences": kinds["recurrence"], "variants": kinds["variant"], "new_items": kinds["new"]}]

    return cluster.run_cluster(execute, run_date=DAY, market="za", model=model, clock=clock), writes


def test_run_counts_are_what_the_write_script_says_it_wrote(monkeypatch):
    # Another run wrote these clusters between the done check and this write, so the write's guard staged nothing.
    nothing = dict.fromkeys(WRITE_PARAMS + ("matched", "recurrences", "variants", "new_items"), 0)
    counts, writes = fake_run(monkeypatch, synthetic_clusters(3), written=nothing)
    assert len(writes) == 1 and counts["clusters"] == 3
    assert [counts[k] for k in ("matched", "recurrences", "variants", "new", "members")] == [0] * 5
    counts, _ = fake_run(monkeypatch, synthetic_clusters(3))
    assert (counts["new"], counts["members"]) == (3, 6)


def test_merge_review_in_counts_is_capped_at_50_with_the_total(monkeypatch):
    counts, _ = fake_run(monkeypatch, synthetic_clusters(12, spread=0.01))
    assert counts["merge_review_total"] == 12 * 11 // 2
    assert len(counts["merge_review"]) == 50
    cosines = [p["cosine"] for p in counts["merge_review"]]
    assert cosines == sorted(cosines, reverse=True) and min(cosines) >= 0.9


def test_a_payload_over_8_mb_is_written_in_batches_of_whole_clusters(monkeypatch):
    clusters = synthetic_clusters(700, dim=768, members=5)
    counts, writes = fake_run(monkeypatch, clusters)
    sizes = [sum(len(p[k].encode("utf-8")) for k in WRITE_PARAMS) for p in writes]
    assert cluster.MAX_PAYLOAD_BYTES == 8 * 1024 * 1024
    assert sum(sizes) > cluster.MAX_PAYLOAD_BYTES, "the synthetic set must pass 8 MB, or this test proves nothing"
    assert len(writes) > 1 and max(sizes) <= cluster.MAX_PAYLOAD_BYTES
    batches = [{k: json.loads(p[k]) for k in WRITE_PARAMS} for p in writes]
    assert [r["cluster_id"] for b in batches for r in b["cluster_rows"]] == [c["cluster_id"] for c in clusters]
    for b in batches:
        ids = {r["cluster_id"] for r in b["cluster_rows"]}
        assert {m["cluster_id"] for m in b["member_rows"]} == ids, "a cluster's members travel with it"
        assert [r["item_id"] for r in b["map_rows"]] == [r["item_id"] for r in b["cluster_rows"]]
    assert (counts["new"], counts["members"]) == (700, 3500)


def test_batches_each_pass_their_own_guard_and_a_replay_writes_nothing():
    clusters = synthetic_clusters(4)
    planned = cluster.plan(clusters, cluster.assign(clusters, [], DAY), [], DAY, "za")
    batches = cluster.write_batches(planned, limit=1)
    assert len(batches) == 4
    con = core_db()
    execute, write = duck_execute(con), cluster.load("cluster_write")
    for batch in batches:
        [row] = execute(write, {"run_date": DAY, "market": "za", **batch})
        assert (row["cluster_rows"], row["member_rows"], row["new_items"]) == (1, 2, 1)
    tables = ("cultural_map", "clusters", "cluster_members")
    snapshot = [con.execute(f"SELECT * FROM {CORE}.{t} ORDER BY ALL").fetchall() for t in tables]
    assert [len(rows) for rows in snapshot] == [4, 4, 8]
    [row] = execute(write, {"run_date": DAY, "market": "za", **batches[1]})
    assert (row["map_rows"], row["cluster_rows"], row["member_rows"]) == (0, 0, 0)
    assert snapshot == [con.execute(f"SELECT * FROM {CORE}.{t} ORDER BY ALL").fetchall() for t in tables]
    assert cluster.write_batches(planned) == [{k: json.dumps(planned[k]) for k in WRITE_PARAMS}]


def test_a_failed_batch_names_the_unwritten_clusters_and_nothing_is_written_twice(monkeypatch):
    clusters = synthetic_clusters(3)
    ids = [c["cluster_id"] for c in clusters]
    batches = cluster.write_batches
    monkeypatch.setattr(cluster, "write_batches", lambda planned: batches(planned, limit=1))
    monkeypatch.setattr(cluster, "fit_topics", lambda docs, emb: ([0] * len(docs), [1.0] * len(docs), {}))
    monkeypatch.setattr(cluster, "build_clusters", lambda *args: clusters)
    con = core_db()
    duck, writes = duck_execute(con), []

    def execute(text, params):
        if text == cluster.load("cluster_posts"):
            return [{"post_id": f"p{i}", "text": "", "today": True, "embedding": [1.0, 0.0]}
                    for i in range(cluster.MIN_POSTS)]
        if text == cluster.load("cluster_write"):
            writes.append(params)
            if len(writes) == 2:
                raise RuntimeError("BigQuery went away")
        return duck(text, params)

    with pytest.raises(RuntimeError) as failed:
        cluster.run_cluster(execute, run_date=DAY, market="za")
    assert failed.value.failed_batch == 1
    assert failed.value.unwritten_cluster_ids == ids[1:]
    tables = ("cultural_map", "clusters", "cluster_members")
    written = [con.execute(f"SELECT * FROM {CORE}.{t} ORDER BY ALL").fetchall() for t in tables]
    assert [len(rows) for rows in written] == [1, 1, 2]
    # A partly written market-day is left as is: a rerun the same day writes nothing.
    assert cluster.run_cluster(execute, run_date=DAY, market="za")["skipped"] == "already_clustered"
    assert len(writes) == 2
    # Replaying every batch by hand writes the logged ids once and the first batch not again.
    for params in writes[:1] + [{"run_date": DAY, "market": "za", **b} for b in
                                cluster.write_batches(cluster.plan(clusters, cluster.assign(clusters, [], DAY), [],
                                                                   DAY, "za"))[1:]]:
        duck(cluster.load("cluster_write"), params)
    counted = con.execute(f"SELECT cluster_id, COUNT(*) FROM {CORE}.clusters GROUP BY ALL ORDER BY 1").fetchall()
    assert counted == [(i, 1) for i in ids]
    assert con.execute(f"SELECT COUNT(*), COUNT(DISTINCT (cluster_id, post_id)) FROM {CORE}.cluster_members"
                       ).fetchone() == (6, 6)


# Discovery review (BUILD.md 2.2 check)

def review_rows():
    rows = [{"cluster_id": f"za-{i:02d}", "cluster_date": DAY, "market": "za", "item_id": f"i{i}",
             "match_kind": "new", "label": f"label {i}", "keywords": ["a", "b"], "posts": ["post one", "post two"]}
            for i in range(40)]
    rows += [{"cluster_id": f"ng-{i}", "cluster_date": DAY, "market": "ng", "item_id": f"n{i}", "match_kind": "match",
              "label": f"ng {i}", "keywords": ["c"], "posts": ["post"]} for i in range(5)]
    return rows


def test_review_sample_draws_up_to_30_per_market_deterministically():
    rows = review_rows()
    sample = cluster.review_sample(rows, seed="2026-W39")
    by_market = {}
    for r in sample:
        by_market.setdefault(r["market"], []).append(r["cluster_id"])
    assert {m: len(v) for m, v in by_market.items()} == {"za": 30, "ng": 5}
    assert sample == cluster.review_sample(list(reversed(rows)), seed="2026-W39")
    assert sample != cluster.review_sample(rows, seed="2026-W40")


def marked_sheet(tmp_path, coherent_za, duplicate_za, blank=0):
    sample = cluster.review_sample(review_rows(), seed=1)
    path = cluster.review_sheet(sample, tmp_path / "review.csv")
    rows = list(csv.DictReader(io.StringIO(Path(path).read_text(encoding="utf-8-sig"))))
    assert set(cluster.REVIEW_COLUMNS) == set(rows[0])
    assert all(r["coherent"] == "" and r["duplicate"] == "" for r in rows)
    za = [r for r in rows if r["market"] == "za"]
    for i, r in enumerate(za):
        if i < blank:
            continue
        r["coherent"] = "yes" if i < coherent_za + blank else "no"
        r["duplicate"] = "yes" if i < duplicate_za + blank else "no"
    for r in rows:
        if r["market"] == "ng":
            r["coherent"], r["duplicate"] = "yes", "no"
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


# The sheet holds za and ng only; these tests promise those two, since the default promise adds ke.
ZA_NG = ("za", "ng")


def test_review_score_meets_the_gate_at_80_percent_coherent_and_under_10_percent_duplicates(tmp_path):
    result = cluster.review_score(marked_sheet(tmp_path, coherent_za=24, duplicate_za=2), markets=ZA_NG)
    za = result["markets"]["za"]
    assert (za["reviewed"], za["coherence"], za["duplication"]) == (30, 0.8, pytest.approx(2 / 30))
    assert za["gate"] == "met" and result["markets"]["ng"]["gate"] == "met"
    assert result["pooled"]["reviewed"] == 35 and result["gate"] == "met"


def test_review_gate_fails_below_80_percent_or_at_10_percent_duplicates(tmp_path):
    assert cluster.review_score(marked_sheet(tmp_path, coherent_za=23, duplicate_za=0), markets=ZA_NG)["gate"] == "unmet"
    result = cluster.review_score(marked_sheet(tmp_path, coherent_za=30, duplicate_za=3), markets=ZA_NG)
    assert result["markets"]["za"]["duplication"] == pytest.approx(0.1)
    assert result["markets"]["za"]["gate"] == "unmet" and result["gate"] == "unmet"


def test_review_with_blank_marks_is_incomplete_and_blanks_are_not_counted(tmp_path):
    result = cluster.review_score(marked_sheet(tmp_path, coherent_za=25, duplicate_za=0, blank=2), markets=ZA_NG)
    za = result["markets"]["za"]
    assert (za["sampled"], za["reviewed"]) == (30, 28)
    assert za["gate"] == "incomplete" and result["gate"] == "incomplete"


def test_review_refuses_a_bad_mark(tmp_path):
    text = marked_sheet(tmp_path, coherent_za=24, duplicate_za=0).replace(",yes,", ",maybe,", 1)
    with pytest.raises(ValueError):
        cluster.review_score(text)


def test_cluster_review_is_the_lifted_discovery_review():
    assert cluster.review_sample is discovery_review.review_sample
    assert cluster.review_sheet is discovery_review.review_sheet
    assert cluster.review_score is discovery_review.review_score
    assert cluster.REVIEW_COLUMNS is discovery_review.REVIEW_COLUMNS


def test_review_sample_is_the_legacy_seeded_hash_order():
    # ops/evaluation/discovery_review.py orders each market's clusters by sha256 of week, market and cluster id
    # and keeps the first thirty, so the sample never depends on the order the rows arrive in.
    rows = review_rows()
    za_ids = [r["cluster_id"] for r in rows if r["market"] == "za"]
    key = lambda cid: hashlib.sha256(f"2026-W39|za|{cid}".encode("utf-8")).hexdigest()  # noqa: E731
    sample = discovery_review.review_sample(rows, seed="2026-W39")
    assert [r["cluster_id"] for r in sample if r["market"] == "za"] == sorted(sorted(za_ids, key=key)[:30])


def test_a_promised_market_with_no_cluster_leaves_the_gate_unmet(tmp_path):
    result = discovery_review.review_score(marked_sheet(tmp_path, coherent_za=30, duplicate_za=0),
                                           markets=("za", "ng", "ke"))
    assert result["markets"]["za"]["gate"] == "met" and result["markets"]["ng"]["gate"] == "met"
    ke = result["markets"]["ke"]
    assert (ke["sampled"], ke["reviewed"], ke["coherence"], ke["gate"]) == (0, 0, None, "unmet")
    assert result["pooled"]["gate"] == "unmet" and result["gate"] == "unmet"


def test_a_cluster_listed_twice_is_refused(tmp_path):
    rows = review_rows()
    with pytest.raises(ValueError):
        discovery_review.review_sample(rows + rows[:1], seed=1)
    lines = marked_sheet(tmp_path, coherent_za=30, duplicate_za=0).splitlines()
    with pytest.raises(ValueError):
        discovery_review.review_score("\n".join(lines + lines[1:2]))


def test_review_promises_za_ng_and_ke_by_default(tmp_path):
    result = discovery_review.review_score(marked_sheet(tmp_path, coherent_za=30, duplicate_za=0))
    assert set(result["markets"]) == {"za", "ng", "ke"}
    assert result["markets"]["ke"]["gate"] == "unmet" and result["gate"] == "unmet"


def test_a_market_outside_the_promise_is_reported_apart_and_leaves_the_verdict_alone(tmp_path):
    text = marked_sheet(tmp_path, coherent_za=30, duplicate_za=0)
    extra = "".join(f"pan-{i},{DAY},pan,p{i},new,pan {i},a,post,no,yes,\r\n" for i in range(3))
    result = discovery_review.review_score(text + extra, markets=ZA_NG)
    assert set(result["markets"]) == {"za", "ng"} and result["gate"] == "met"
    assert result["pooled"]["reviewed"] == 35 and result["pooled"]["gate"] == "met"
    pan = result["other_markets"]["pan"]
    assert (pan["sampled"], pan["reviewed"], pan["coherent"], pan["duplicate"]) == (3, 3, 0, 3)
    assert discovery_review.review_score(text, markets=ZA_NG)["other_markets"] == {}


def test_review_score_reads_a_sheet_that_keeps_its_byte_order_mark(tmp_path):
    text = marked_sheet(tmp_path, coherent_za=30, duplicate_za=0)
    assert discovery_review.review_score("\ufeff" + text, markets=ZA_NG) == \
        discovery_review.review_score(text, markets=ZA_NG)
    # Written as an escape, so the mark is visible in the source.
    assert "\ufeff" not in Path(discovery_review.__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize("markets", [(), ("za", "gh"), ("pan",), ("za", "za"), ("ZA",), "za"])
def test_review_refuses_an_unknown_market_set(tmp_path, markets):
    with pytest.raises(ValueError):
        discovery_review.review_score(marked_sheet(tmp_path, coherent_za=30, duplicate_za=0), markets=markets)


def test_a_write_for_a_past_day_never_moves_last_seen_backwards():
    past = DAY - timedelta(days=5)
    item = an_item("x", at(1.0), DAY, hashtags=["h"])
    c = a_cluster(f"{past:%Y%m%d}-za-000", at(0.99), hashtags=["h"])
    c.update(label="x", members=[("p1", 1.0)], market="za", platform="tiktok", local_terms=[])
    planned = cluster.plan([c], cluster.assign([c], [item], past), [item], past, "za")
    assert [(r["change"], r["last_seen"]) for r in planned["map_rows"]] == [("update", past.isoformat())]
    con = core_db()
    con.execute(f"INSERT INTO {CORE}.cultural_map VALUES ({', '.join('?' * 17)})", map_row("x", at(1.0), last_seen=DAY))
    duck_execute(con)(cluster.load("cluster_write"),
                      {"run_date": past, "market": "za", **{k: json.dumps(planned[k]) for k in WRITE_PARAMS}})
    assert open_versions(con, "x") == 1
    assert con.execute(f"SELECT last_seen FROM {CORE}.cultural_map WHERE item_id = 'x' AND valid_to IS NULL"
                       ).fetchone()[0] == DAY, "last_seen is GREATEST(the old last_seen, the run's)"
    assert "GREATEST(" in sql("cluster_write")


def test_a_write_for_a_later_day_still_moves_last_seen_forward():
    item = an_item("x", at(1.0), DAY - timedelta(days=3), hashtags=["h"])
    c = a_cluster(f"{DAY:%Y%m%d}-za-000", at(0.99), hashtags=["h"])
    c.update(label="x", members=[("p1", 1.0)], market="za", platform="tiktok", local_terms=[])
    planned = cluster.plan([c], cluster.assign([c], [item], DAY), [item], DAY, "za")
    con = core_db()
    con.execute(f"INSERT INTO {CORE}.cultural_map VALUES ({', '.join('?' * 17)})",
                map_row("x", at(1.0), last_seen=DAY - timedelta(days=3)))
    duck_execute(con)(cluster.load("cluster_write"),
                      {"run_date": DAY, "market": "za", **{k: json.dumps(planned[k]) for k in WRITE_PARAMS}})
    assert con.execute(f"SELECT last_seen FROM {CORE}.cultural_map WHERE item_id = 'x' AND valid_to IS NULL"
                       ).fetchone()[0] == DAY


# The model's second net on labels and keywords (RULES.md rules 1 and 4): one fast-model call per market run that
# can only take a label or keyword away, booked on the job's day, and never blocking the write.

NET_ID = f"{DAY:%Y%m%d}-za-000"


def net_cluster(cid=NET_ID, labels=("kota, kasi, dance", "#kotaday", "sound s1"), keywords=("kota", "kasi", "dance")):
    return {"cluster_id": cid, "centroid": [1.0, 0.0, 0.0], "keywords": list(keywords), "label": labels[0],
            "labels": list(labels), "hashtags": ["kotaday"], "sounds": ["s1"], "creators": [],
            "members": [(f"{cid}-p0", 0.5)], "market": "za", "platform": "tiktok", "local_terms": []}


def neutral(cid=NET_ID):
    return f"Topic {hashlib.sha256(f'topic|topic:{cid}'.encode('utf-8')).hexdigest()[:8]}"


def sent_items(call):
    return json.loads(re.search(r"<untrusted_content>\n(.*)\n</untrusted_content>", call["user"], re.S).group(1))


def run_net(clusters, model, execute=lambda text, params: []):
    return cluster.label_net(execute, clusters, model=model, run_id="understand-1", day=DAY)


def test_a_flagged_label_gives_way_to_the_next_candidate_the_net_passes():
    model = NetModel(flag=lambda t: t in {"kota, kasi, dance", "#kotaday"})
    [c], counts = run_net([net_cluster()], model)
    assert c["label"] == "sound s1"
    assert c["keywords"] == ["kota", "kasi", "dance"], "a flagged label takes no keyword with it"
    assert counts["net_failed"] is False and counts["net_relabelled"] == 1 and counts["net_keywords_dropped"] == 0
    [c], _ = run_net([net_cluster()], NetModel(flag=lambda t: True))
    assert c["label"] == neutral() and c["keywords"] == []


def test_a_candidate_that_fails_the_word_scan_is_never_the_replacement():
    model = NetModel(flag=lambda t: t == "kota, kasi, dance")
    [c], _ = run_net([net_cluster(labels=("kota, kasi, dance", "#kidsoftiktok", "sound s1"))], model)
    assert c["label"] == "sound s1"


def test_keywords_left_either_side_of_a_flagged_one_are_scanned_as_a_pair(monkeypatch):
    # rugby between them hid "born free" from clean_keywords; once the net drops it, the pair meets.
    [c], counts = run_net([net_cluster(keywords=("born", "rugby", "free"))], NetModel(flag=lambda t: t == "rugby"))
    assert c["keywords"] == []
    assert counts["net_keywords_dropped"] == 3
    _, writes = fake_run(monkeypatch, [net_cluster(keywords=("born", "rugby", "free"))],
                         model=NetModel(flag=lambda t: t == "rugby"))
    assert json.loads(writes[0]["cluster_rows"])[0]["keywords"] == [], "the stored row holds none of the three"


def test_a_flagged_keyword_is_dropped_and_a_label_built_on_it_gives_way():
    [c], counts = run_net([net_cluster()], NetModel(flag=lambda t: t == "kasi"))
    assert c["keywords"] == ["kota", "dance"]
    assert c["label"] == "#kotaday", "the keyword label holds kasi, so it goes even though the net passed it whole"
    assert counts["net_keywords_dropped"] == 1 and counts["net_relabelled"] == 1


def test_one_model_call_per_market_run_with_every_new_label_and_keyword_fenced():
    model = NetModel()
    clusters = [net_cluster(f"{DAY:%Y%m%d}-za-{t:03d}") for t in range(3)]
    out, counts = run_net(clusters, model)
    assert len(model.calls) == 1
    call = model.calls[0]
    from core.understand import enrich

    assert call["model"] == enrich.MODEL == "gemini-3.8-flash" and call["schema"] == cluster.NET_SCHEMA
    assert call["system"] == cluster.NET_SYSTEM and "age, generation or life stage" in call["system"]
    sent = sent_items(call)
    assert [s["index"] for s in sent] == [0, 1, 2]
    assert sent[0]["labels"] == ["kota, kasi, dance", "#kotaday", "sound s1"]
    assert sent[0]["keywords"] == ["kota", "kasi", "dance"]
    assert [c["label"] for c in out] == ["kota, kasi, dance"] * 3 and counts["net_failed"] is False


def test_a_neutral_label_is_not_sent_and_stays():
    model = NetModel(flag=lambda t: True)
    [c], counts = run_net([net_cluster(labels=(neutral(),), keywords=("kota",))], model)
    assert sent_items(model.calls[0])[0]["labels"] == [] and c["label"] == neutral()
    assert counts["net_relabelled"] == 0


def test_a_cluster_without_candidates_sends_its_label():
    c = net_cluster()
    del c["labels"]
    [c], _ = run_net([c], NetModel(flag=lambda t: t == "kota, kasi, dance"))
    assert c["label"] == neutral()


class Refused(Exception):
    pass


@pytest.mark.parametrize("case", ["raises", "timeout", "not_json_object", "missing_item", "index_out_of_range",
                                  "keyword_out_of_range", "string_index", "duplicate_item", "extra_key", "boolean",
                                  "cap", "spend_unknown", "no_core_agent"])
def test_a_net_that_fails_gives_every_label_a_neutral_name_and_keeps_only_scanned_keywords(monkeypatch, case):
    good = {"index": 0, "flagged_labels": [], "flagged_keywords": []}
    outs = {"not_json_object": ["x"], "missing_item": {"clusters": [good]},
            "index_out_of_range": {"clusters": [good, {**good, "index": 1, "flagged_labels": [3]}]},
            "keyword_out_of_range": {"clusters": [good, {**good, "index": 1, "flagged_keywords": [9]}]},
            "string_index": {"clusters": [good, {**good, "index": "1"}]},
            "duplicate_item": {"clusters": [good, good]},
            "extra_key": {"clusters": [good, {**good, "index": 1, "labels": ["kota"]}]},
            "boolean": {"clusters": [good, {**good, "index": 1, "flagged_labels": [True]}]}}
    model = NetModel(out=outs.get(case), raises={"raises": Refused("model refused"),
                                                  "timeout": TimeoutError("read timed out")}.get(case))
    if case == "cap":
        monkeypatch.setattr(cluster, "spend_today", lambda execute, now: (19.9999, 20.0))
    if case == "spend_unknown":
        def unreadable(execute, now):
            raise RuntimeError("spend query failed")
        monkeypatch.setattr(cluster, "spend_today", unreadable)
    if case == "no_core_agent":
        monkeypatch.setitem(sys.modules, "core.understand.enrich", None)
    other = f"{DAY:%Y%m%d}-za-001"
    clusters = [net_cluster(), net_cluster(other, keywords=("kota", "kidsoftiktok", "kasi"))]
    out, counts = run_net(clusters, model)
    assert [c["label"] for c in out] == [neutral(), neutral(other)]
    assert [c["keywords"] for c in out] == [["kota", "kasi", "dance"], ["kota", "kasi"]]
    assert counts["net_failed"] is True and counts["net_error"]
    if case in ("cap", "spend_unknown", "no_core_agent"):
        assert model.calls == [], "the net is never sent without room under the cap"


def test_the_net_error_names_the_failure_without_its_message():
    _, counts = run_net([net_cluster()], NetModel(raises=Refused("see https://example.com/x?token=y")))
    assert counts["net_error"] == "Refused"
    _, counts = run_net([net_cluster()], NetModel(out={"clusters": []}))
    assert counts["net_error"] == "malformed"


def test_the_net_checks_the_room_on_the_jobs_day_then_books_its_spend_there(monkeypatch):
    from core.understand import embed

    log, reads, booked = [], [], []
    monkeypatch.setattr(cluster, "book_spend", embed.book_spend)
    monkeypatch.setattr(cluster, "spend_today", lambda execute, now: reads.append(now) or log.append("room")
                        or (1.0, 20.0))

    def execute(text, params):
        log.append("book")
        booked.append((text, params))
        return []

    _, counts = cluster.label_net(execute, [net_cluster()], model=NetModel(log=log), run_id="understand-1",
                                  day=date(2026, 9, 29))
    assert log == ["room", "net", "book"]
    assert reads == [datetime(2026, 9, 29, 12, 0, tzinfo=timezone(timedelta(hours=2)))]
    [(text, params)] = booked
    assert text == embed.SPEND_ROW_SQL and params["run_date"] == date(2026, 9, 29)
    assert params["run_id"].startswith("understand-1-spend-")
    # 900 tokens in at USD 1 a million and 60 out at USD 5.
    assert json.loads(params["counts"]) == {"model_usd": 0.0012, "what": "cluster_label_net"}
    assert counts["model_usd"] == counts["booked_usd"] == 0.0012


def test_a_failed_call_that_was_billed_is_booked_too():
    _, counts = run_net([net_cluster()], NetModel(raises=Refused("max_tokens")))
    assert counts["net_failed"] is True and counts["model_usd"] == counts["booked_usd"] == 0.0012


def test_a_booking_that_fails_keeps_the_verdict_and_leaves_the_spend_for_the_understand_row(monkeypatch):
    def unbookable(execute, **kw):
        raise RuntimeError("runs append failed")
    monkeypatch.setattr(cluster, "book_spend", unbookable)
    [c], counts = run_net([net_cluster()], NetModel(flag=lambda t: t == "kasi"))
    assert c["keywords"] == ["kota", "dance"] and counts["net_failed"] is False
    assert counts["model_usd"] == 0.0012 and counts["booked_usd"] == 0.0 and counts["net_book_error"] == "RuntimeError"


def test_the_estimate_that_needs_room_counts_the_prompt_and_the_whole_output_budget(monkeypatch):
    from core.understand import enrich

    model = NetModel()
    run_net([net_cluster()], model)
    call = model.calls[0]
    chars = len((call["system"] + call["user"]).encode("utf-8"))
    estimate = enrich.usd(-(-chars // enrich.CHARS_PER_TOKEN) + enrich.EST_SCHEMA_TOKENS,
                          enrich.reserve_output(enrich.MODEL, call["max_tokens"]), batched=False)
    assert call["max_tokens"] == cluster.NET_BASE_TOKENS + cluster.NET_TOKENS_PER_CLUSTER
    monkeypatch.setattr(cluster, "spend_today", lambda execute, now: (20.0 - estimate, 20.0))
    assert run_net([net_cluster()], NetModel())[1]["net_failed"] is False
    monkeypatch.setattr(cluster, "spend_today", lambda execute, now: (20.0 - estimate + 0.000002, 20.0))
    assert run_net([net_cluster()], NetModel())[1]["net_error"] == "cap"


def test_label_net_stops_when_its_reserved_spend_day_has_ended():
    model = NetModel()
    [result], counts = cluster.label_net(lambda text, params: [], [net_cluster()], model=model,
                                         run_id="understand-1", day=date(2026, 10, 2),
                                         clock=lambda: datetime(2026, 10, 2, 22, 0, tzinfo=timezone.utc))

    assert model.calls == []
    assert result["label"] == neutral()
    assert counts["net_failed"] is True and counts["net_error"] == "day_changed"
    assert counts["model_usd"] == counts["booked_usd"] == 0.0


def test_run_cluster_does_not_write_when_the_label_net_day_has_ended(monkeypatch):
    start = datetime(2026, 10, 2, 21, 50, tzinfo=timezone.utc)
    after_midnight = datetime(2026, 10, 2, 22, 10, tzinfo=timezone.utc)
    current = [start]

    def spend_today(execute, now):
        current[0] = after_midnight
        return 0.0, 20.0

    monkeypatch.setattr(cluster, "spend_today", spend_today)
    model = NetModel()
    counts, writes = fake_run(monkeypatch, [net_cluster()], model=model, clock=lambda: current[0])

    assert writes == []
    assert model.calls == []
    assert counts["net_failed"] is True and counts["net_error"] == "day_changed"
    assert counts["model_usd"] == counts["booked_usd"] == 0.0


def test_nothing_is_written_before_the_net_and_the_write_carries_its_verdict(monkeypatch):
    log = []
    model = NetModel(flag=lambda t: t in {"kota, kasi, dance", "kasi"}, log=log)
    counts, writes = fake_run(monkeypatch, [net_cluster()], model=model, log=log)
    assert log.index("net") < log.index("cluster_write")
    assert "cluster_items" not in log[:log.index("net")], "the keywords the votes compare are the net's"
    rows = {k: json.loads(writes[0][k]) for k in WRITE_PARAMS}
    assert rows["cluster_rows"][0]["label"] == rows["map_rows"][0]["label"] == "#kotaday"
    assert rows["cluster_rows"][0]["keywords"] == ["kota", "dance"]
    assert counts["net_failed"] is False and counts["net_relabelled"] == 1 and counts["net_keywords_dropped"] == 1
    assert counts["model_usd"] == counts["booked_usd"] == 0.0012


def test_a_failed_net_never_blocks_the_write(monkeypatch):
    counts, writes = fake_run(monkeypatch, [net_cluster()], model=NetModel(raises=TimeoutError("timed out")))
    rows = {k: json.loads(writes[0][k]) for k in WRITE_PARAMS}
    assert rows["cluster_rows"][0]["label"] == rows["map_rows"][0]["label"] == neutral()
    assert counts["new"] == 1 and counts["net_failed"] is True


def test_a_matched_item_keeps_its_label_and_is_never_sent(monkeypatch):
    item = {"item_id": "item_old", "kind": "topic", "canonical_key": "topic:item_old", "label": "the old name",
            "centroid": [1.0, 0.0, 0.0], "last_seen": DAY - timedelta(days=1), "keywords": ["kota"]}
    model = NetModel(flag=lambda t: t == "kota, kasi, dance")
    counts, writes = fake_run(monkeypatch, [net_cluster()], items=[item], model=model)
    rows = {k: json.loads(writes[0][k]) for k in WRITE_PARAMS}
    assert rows["map_rows"][0]["change"] == "update" and rows["map_rows"][0]["label"] == "the old name"
    assert rows["cluster_rows"][0]["label"] == "#kotaday"
    assert "the old name" not in model.calls[0]["user"]


def test_a_spend_the_write_never_reached_rides_on_the_error(monkeypatch):
    def execute_raising(text, params):
        if text == cluster.load("cluster_write"):
            raise RuntimeError("BigQuery went away")
        if text == cluster.load("cluster_done"):
            return [{"n": 0}]
        if text == cluster.load("cluster_posts"):
            return [{"post_id": f"p{i}", "text": "", "today": True, "embedding": [1.0, 0.0]}
                    for i in range(cluster.MIN_POSTS)]
        return []

    monkeypatch.setattr(cluster, "fit_topics", lambda docs, emb: ([0] * len(docs), [1.0] * len(docs), {}))
    monkeypatch.setattr(cluster, "build_clusters", lambda *args: [net_cluster()])
    with pytest.raises(RuntimeError) as failed:
        cluster.run_cluster(execute_raising, run_date=DAY, market="za", model=NetModel())
    assert (failed.value.model_usd, failed.value.booked_usd) == (0.0012, 0.0012)


def test_a_new_cluster_carries_its_label_candidates_in_order():
    assert cluster.label_candidates(["kota", "kasi", "dance"], ["kotaday"], ["s1"]) == \
        ["kota, kasi, dance", "#kotaday", "sound s1"]
    assert cluster.label_candidates(["youth", "kasi"], ["kidsoftiktok", "x"], ["s1"]) == ["sound s1"]
    posts = [{"post_id": f"p{i}", "today": True, "hashtags": ["#kotaday"], "sound_id": "s1", "creator_id": "c",
              "embedding": [1.0, 0.0], "market": "za", "platform": "tiktok"} for i in range(3)]
    [c] = cluster.build_clusters(posts, [0, 0, 0], [1.0] * 3, {0: ["kota", "kasi", "dance"]}, DAY, "za")
    assert c["labels"] == ["kota, kasi, dance", "#kotaday", "sound s1"] and c["label"] == c["labels"][0]


def test_the_default_net_model_gives_up_after_its_timeout():
    from core.llm.gemini import GeminiModel

    model = REAL_NET_MODEL()
    assert isinstance(model, GeminiModel)
    assert model.timeout_s == cluster.NET_TIMEOUT_S and model.retries == cluster.NET_RETRIES


def test_the_net_schema_holds_only_flags():
    entry = cluster.NET_SCHEMA["properties"]["clusters"]["items"]
    assert set(entry["properties"]) == {"index", "flagged_labels", "flagged_keywords"}
    assert entry["additionalProperties"] is False and cluster.NET_SCHEMA["additionalProperties"] is False


# Peak memory: float32 embeddings and a cap on the posts one run fits on

def capped_posts(today, older):
    posts = [{"post_id": f"t{i:03d}", "today": True} for i in range(today)]
    posts += [{"post_id": f"o{i:03d}", "today": False} for i in range(older)]
    return sorted(posts, key=lambda p: p["post_id"][::-1])  # mixed, as the SQL orders by post id


def test_max_cluster_posts_is_set():
    assert cluster.MAX_CLUSTER_POSTS == 20000


def test_a_run_under_the_cap_keeps_every_post_in_order():
    posts = capped_posts(10, 20)
    assert cluster.cap_posts(posts, DAY, "za", limit=30) == posts


def test_over_the_cap_every_post_from_today_stays_and_older_ones_fill_the_rest_in_input_order():
    posts = capped_posts(10, 40)
    kept = cluster.cap_posts(posts, DAY, "za", limit=25)
    assert len(kept) == 25
    assert {p["post_id"] for p in posts if p["today"]} <= {p["post_id"] for p in kept}
    assert kept == [p for p in posts if p in kept], "the posts keep their input order"


def test_the_older_sample_is_seeded_and_the_same_whatever_the_input_order():
    posts = capped_posts(10, 40)
    kept = {p["post_id"] for p in cluster.cap_posts(posts, DAY, "za", limit=25)}
    again = {p["post_id"] for p in cluster.cap_posts(list(reversed(posts)), DAY, "za", limit=25)}
    assert kept == again
    assert kept != {p["post_id"] for p in cluster.cap_posts(posts, DAY, "ng", limit=25)}, "the seed takes the market"


def test_more_posts_from_today_than_the_cap_keeps_all_of_them_and_no_older_one():
    posts = capped_posts(30, 10)
    assert cluster.cap_posts(posts, DAY, "za", limit=20) == [p for p in posts if p["today"]]


def test_the_embedding_matrix_is_float32_and_takes_each_posts_list_away():
    posts = [{"post_id": "a", "embedding": [0.5, 0.25]}, {"post_id": "b", "embedding": [1.0, -2.0]}]
    m = cluster.embedding_matrix(posts)
    assert m.dtype == np.float32 and m.tolist() == [[0.5, 0.25], [1.0, -2.0]]
    assert all("embedding" not in p for p in posts)


def test_build_clusters_takes_its_centroids_from_the_matrix():
    posts = [{"post_id": f"p{i}", "today": True, "hashtags": [], "sound_id": None, "creator_id": "c",
              "market": "za", "platform": "tiktok"} for i in range(3)]
    matrix = np.array([[1.0, 0.0], [0.0, 1.0], [5.0, 5.0]], dtype=np.float32)
    [c] = cluster.build_clusters(posts, [0, 0, -1], [1.0] * 3, {0: ["kota"]}, DAY, "za", embeddings=matrix)
    assert c["centroid"] == [0.5, 0.5] and isinstance(c["centroid"][0], float)


def test_run_cluster_fits_on_float32_and_caps_the_posts(monkeypatch):
    seen = {}

    def fit(docs, embeddings):
        seen.update(n=len(docs), dtype=embeddings.dtype)
        return [0] * len(docs), [1.0] * len(docs), {}

    def execute(text, params):
        if text == cluster.load("cluster_done"):
            return [{"n": 0}]
        if text == cluster.load("cluster_posts"):
            return [{"post_id": f"p{i:03d}", "text": "", "today": i % 4 == 0, "embedding": [1.0, float(i)]}
                    for i in range(80)]
        return []

    monkeypatch.setattr(cluster, "MAX_CLUSTER_POSTS", 50)
    monkeypatch.setattr(cluster, "fit_topics", fit)
    monkeypatch.setattr(cluster, "label_net", lambda execute, clusters, **kw: (clusters, {"model_usd": 0.0,
                                                                                         "booked_usd": 0.0}))
    counts = cluster.run_cluster(execute, run_date=DAY, market="za")
    assert seen == {"n": 50, "dtype": np.float32}
    assert (counts["posts"], counts["today_posts"], counts["capped_posts"]) == (80, 20, 50)
    assert counts["members"] == 0 and counts["clusters"] == 1


def test_a_run_under_the_cap_counts_no_cap(world):
    assert "capped_posts" not in world["counts"]
