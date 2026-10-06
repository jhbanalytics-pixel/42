"""FEATURES 27: tone by language per item (tone_by_language.sql) and the native-speaker quote pool
(native_quotes.sql), run on DuckDB fixture tables transpiled from BigQuery."""
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from core.eval import review
from core.understand import enrich

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
FILES = ("tone_by_language", "native_quotes")
PROJECT_DB = '"ogilvy-trends-v2"'
CORE = f"{PROJECT_DB}.intelligence_42_core"
AS_OF = date(2026, 9, 28)
SINCE = AS_OF - timedelta(days=6)


def sql(name):
    return (SQL_DIR / f"{name}.sql").read_text(encoding="utf-8")


def code_only(text):
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def warehouse(posts, enrichment, post_items=()):
    """posts: (post_id, market, post_date, text); enrichment: (post_id, langs, tone), tone None for an embed row."""
    import duckdb  # test-only

    con = duckdb.connect()
    con.execute(f"ATTACH ':memory:' AS {PROJECT_DB}")
    con.execute(f"CREATE SCHEMA {CORE}")
    con.execute(f"CREATE TABLE {CORE}.posts (post_id VARCHAR, platform VARCHAR, url VARCHAR, text VARCHAR, "
                "post_date DATE, geo_market VARCHAR)")
    con.execute(f"CREATE TABLE {CORE}.post_items (post_id VARCHAR, item_id VARCHAR, via VARCHAR)")
    con.execute(f"CREATE TABLE {CORE}.post_enrichment (post_id VARCHAR, embedding DOUBLE[], langs VARCHAR[], "
                "entities VARCHAR[], tone VARCHAR, stance VARCHAR)")
    for post_id, market, day, text in posts:
        con.execute(f"INSERT INTO {CORE}.posts VALUES (?, 'tiktok', ?, ?, ?, ?)",
                    [post_id, f"https://example.org/{post_id}", text, day, market])
    for post_id, langs, tone in enrichment:
        con.execute(f"INSERT INTO {CORE}.post_enrichment VALUES (?, ?, ?, [], ?, ?)",
                    [post_id, [0.1] if tone is None else None, langs, tone, None if tone is None else "neutral"])
    if post_items:
        con.executemany(f"INSERT INTO {CORE}.post_items VALUES (?, ?, ?)", post_items)
    return con


def run(con, name, params):
    duck = sqlglot.transpile(sql(name), read="bigquery", write="duckdb")[0]
    result = con.execute(duck, {k: v for k, v in params.items() if f"${k}" in duck})
    names = [d[0] for d in result.description]
    return [dict(zip(names, row)) for row in result.fetchall()]


@pytest.mark.parametrize("name", FILES)
def test_sql_parses_as_bigquery_and_is_read_only(name):
    statements = [s for s in sqlglot.parse(sql(name), read="bigquery") if s is not None]
    assert len(statements) == 1 and isinstance(statements[0], (exp.Select, exp.Union))
    text = code_only(sql(name))
    assert not re.search(r"\b(INSERT|DROP|TRUNCATE|DELETE|REPLACE|MERGE|UPDATE|ALTER|CREATE)\b", text, re.I)
    assert not re.search(r"\{\w*\}|%s|%\(", text)
    assert re.search(r"post_date\s+BETWEEN\s+@since\s+AND\s+@as_of", text)


def test_tone_counts_every_label_as_a_column_and_never_a_share():
    names = sqlglot.parse_one(sql("tone_by_language"), read="bigquery").named_selects
    assert names == ["item_id", "market", "lang", "n", *enrich.TONES, "sample", "tone_cap"]
    assert not re.search(r"share|ratio|percent|/", code_only(sql("tone_by_language")), re.I)


def fixture():
    posts = [(f"z{i}", "ZA", AS_OF - timedelta(days=1), "Sho! lekker") for i in range(6)]
    posts += [("n1", "NG", AS_OF, "Wahala dey"), ("n2", "NG", AS_OF, "no wahala"),
              ("old", "ZA", SINCE - timedelta(days=1), "old"), ("loose", "ZA", AS_OF, "no item")]
    enrichment = [("z0", ["zu", "en"], "humorous"), ("z1", ["zu"], "humorous"), ("z2", ["zu"], "sarcastic"),
                  ("z3", ["zu", "zu"], "celebratory"), ("z4", ["zu"], "humorous"), ("z5", ["en"], "angry"),
                  ("z0", None, None),  # the embed step's own row: no tone, never counted
                  ("n1", ["pcm"], "angry"), ("n2", ["pcm", "en"], "humorous"),
                  ("old", ["zu"], "sad"), ("loose", ["zu"], "sad")]
    post_items = [(f"z{i}", "item_a", "cluster") for i in range(6)] + [("z0", "item_a", "hashtag")]
    post_items += [("n1", "item_a", "cluster"), ("n2", "item_a", "cluster"), ("old", "item_a", "cluster")]
    return warehouse(posts, enrichment, post_items)


def test_tone_by_language_counts_posts_per_item_market_and_language():
    rows = run(fixture(), "tone_by_language", {"since": SINCE, "as_of": AS_OF})
    by_key = {(r["item_id"], r["market"], r["lang"]): r for r in rows}
    assert sorted(by_key) == [("item_a", "NG", "en"), ("item_a", "NG", "pcm"), ("item_a", "ZA", "en"),
                              ("item_a", "ZA", "zu")]
    zu = by_key[("item_a", "ZA", "zu")]
    # z0 is linked twice and z3 lists zu twice; each still counts once. The out-of-window post does not count.
    assert zu["n"] == 5
    assert (zu["humorous"], zu["sarcastic"], zu["celebratory"], zu["sad"], zu["angry"]) == (3, 1, 1, 0, 0)
    assert sum(zu[t] for t in enrich.TONES) == zu["n"]
    assert zu["sample"] == "counted" and zu["tone_cap"] is None
    en = by_key[("item_a", "ZA", "en")]
    assert (en["n"], en["humorous"], en["angry"], en["sample"]) == (2, 1, 1, "n < 5 insufficient")
    pcm = by_key[("item_a", "NG", "pcm")]
    assert (pcm["n"], pcm["angry"], pcm["humorous"]) == (2, 1, 1)
    assert pcm["sample"] == "n < 5 insufficient"
    # progress/L3.md, Decisions from Albert: non-English NG and KE tone is capped at Single source.
    assert pcm["tone_cap"] == "single_source"
    # n2 is code-switched (pcm and en), so the NG en row rests on a non-English post and is capped too.
    assert by_key[("item_a", "NG", "en")]["tone_cap"] == "single_source"


def test_the_insufficient_mark_starts_below_five_posts():
    posts = [(f"p{i}", "KE", AS_OF, "niaje") for i in range(5)]
    enrichment = [(f"p{i}", ["sheng"], "humorous") for i in range(5)]
    items = [(f"p{i}", "item_k", "cluster") for i in range(5)]
    rows = run(warehouse(posts, enrichment, items), "tone_by_language", {"since": SINCE, "as_of": AS_OF})
    assert [(r["n"], r["sample"], r["tone_cap"]) for r in rows] == [(5, "counted", "single_source")]


def test_ng_and_ke_tone_is_capped_on_code_switched_english_rows_and_on_und():
    posts = [(p, market, AS_OF, "text") for p, market in
             (("e1", "NG"), ("e2", "NG"), ("k1", "KE"), ("k2", "KE"), ("u1", "KE"), ("m1", "KE"),
              ("z1", "ZA"), ("z2", "ZA"))]
    enrichment = [("e1", ["en"], "neutral"), ("e2", ["en", "pcm"], "humorous"), ("k1", ["en"], "neutral"),
                  ("k2", ["en", "en"], "hopeful"), ("u1", ["und"], "neutral"), ("m1", ["und", "en"], "angry"),
                  ("z1", ["und"], "neutral"), ("z2", ["en", "zu"], "humorous")]
    items = [(p, "item_c", "cluster") for p in ("e1", "e2", "k1", "k2", "u1", "z1", "z2")] + [("m1", "item_d", "cluster")]
    rows = run(warehouse(posts, enrichment, items), "tone_by_language", {"since": SINCE, "as_of": AS_OF})
    caps = {(r["item_id"], r["market"], r["lang"]): r["tone_cap"] for r in rows}
    assert caps == {
        ("item_c", "NG", "en"): "single_source",  # e2 switches into Pidgin
        ("item_c", "NG", "pcm"): "single_source",
        ("item_c", "KE", "en"): None,  # every post is English only
        ("item_c", "KE", "und"): "single_source",
        ("item_c", "ZA", "en"): None,  # the cap is NG and KE only
        ("item_c", "ZA", "und"): None,
        ("item_c", "ZA", "zu"): None,
        ("item_d", "KE", "en"): "single_source",  # und alongside en
        ("item_d", "KE", "und"): "single_source",
    }


def test_native_quotes_keeps_one_row_per_post_and_language_when_posts_holds_a_post_twice():
    posts = [("a", "ZA", AS_OF, "Yebo sharp"), ("a", "ZA", AS_OF, "Yebo")]  # the same post written twice
    enrichment = [("a", ["zu", "zu", "xh"], "celebratory")]
    rows = run(warehouse(posts, enrichment), "native_quotes",
               {"since": SINCE, "as_of": AS_OF, "languages": ["zu", "xh"]})
    assert [(r["id"], r["text"]) for r in rows] == [("a:xh", "Yebo sharp"), ("a:zu", "Yebo sharp")]


def test_native_quotes_keeps_the_fuller_text_of_a_post_held_twice():
    posts = [("a", "ZA", AS_OF, "Molo"), ("a", "ZA", AS_OF, "Molo sisi, unjani namhlanje")]
    rows = run(warehouse(posts, [("a", ["xh"], "neutral")]), "native_quotes",
               {"since": SINCE, "as_of": AS_OF, "languages": ["xh"]})
    assert [(r["id"], r["text"]) for r in rows] == [("a:xh", "Molo sisi, unjani namhlanje")]


def test_native_quotes_lists_each_non_english_language_of_a_tone_labelled_post_in_the_window():
    posts = [("a", "ZA", AS_OF, "Yebo sharp"), ("b", "ZA", AS_OF, "Molo"), ("c", "ZA", AS_OF, "embed only"),
             ("d", "ZA", SINCE - timedelta(days=1), "too old"), ("e", "ZA", AS_OF, "english only"),
             ("f", "ZA", AS_OF, "Dumela")]
    enrichment = [("a", ["zu", "en", "xh"], "celebratory"), ("a", None, None), ("b", ["xh"], "neutral"),
                  ("c", None, None), ("d", ["zu"], "sad"), ("e", ["en"], "angry"), ("f", ["st"], "neutral")]
    rows = run(warehouse(posts, enrichment), "native_quotes",
               {"since": SINCE, "as_of": AS_OF, "languages": ["zu", "xh"]})
    assert [(r["id"], r["post_id"], r["language"], r["tone"]) for r in rows] == [
        ("a:xh", "a", "xh", "celebratory"), ("a:zu", "a", "zu", "celebratory"), ("b:xh", "b", "xh", "neutral")]
    assert all(r["market"] == "ZA" and r["url"] and r["text"] for r in rows)
    assert rows[0]["gloss"] == ""
    sheet = review.native_sheet(review.native_sample(rows, week="2026-W40", seed=1, language=None))
    assert sheet.count("\n") == 4
