"""Ask depth, item 2: a hashtag search matches the tag column and the caption, strips the leading #, defaults to 100
posts, adds the tags that co-occur with it up to the eight term cap, and reads a curated local terms file that ships
empty."""

import logging
from datetime import date, datetime

import pytest

from core.agent import toolset
from core.agent.context import Refused, RunContext
from core.agent.tests.test_history import DuckWarehouse
from core.agent.tests.test_warehouse_tools import SplitWarehouse, kw, sem
from core.agent.tools import warehouse as wh_module
from core.agent.tools.sql_query import check_sql
from core.agent.tools.warehouse import LOCAL_TERMS_PATH, MAX_TERMS, load_local_terms, search_posts

NOW = datetime(2026, 10, 10, 6, 0)


@pytest.fixture
def ctx():
    return RunContext(run_id="r_tags", tier="T1", as_of=NOW, market="ZA")


def related(*tags):
    return [{"tag": t, "co_posts": 10, "co_creators": 5, "all_posts": 20} for t in tags]


class TagWarehouse(SplitWarehouse):
    """Related-tag rows for the co-occurrence query, keyword rows and semantic rows for the two search legs."""

    def __init__(self, tags=(), keyword=(), semantic=(), related_error=None):
        super().__init__(list(keyword), list(semantic))
        self.tags = list(tags)
        self.related_error = related_error

    def run(self, sql, params, max_bytes_billed):
        if "co_posts" in sql:
            self.runs.append((sql, params, max_bytes_billed))
            if self.related_error:
                raise self.related_error
            return [dict(r) for r in self.tags]
        return super().run(sql, params, max_bytes_billed)

    def legs(self):
        return {
            "related": [r for r in self.runs if "co_posts" in r[0]],
            "keyword": [r for r in self.runs if "co_posts" not in r[0] and "tvf_search_posts" not in r[0]],
            "semantic": [r for r in self.runs if "tvf_search_posts" in r[0]],
        }


def terms_of(params):
    named = {int(k.split("_")[1]): v for k, v in params.items() if k.startswith("term_")}
    return [named[i] for i in sorted(named)]


# the tag column, the leading #, the default limit


def humour_con():
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE SCHEMA intelligence_42_core")
    con.execute("CREATE TABLE intelligence_42_core.posts (post_id VARCHAR, platform VARCHAR, url VARCHAR, "
                "creator_id VARCHAR, published_at TIMESTAMP, post_date DATE, geo_market VARCHAR, geo_source VARCHAR, "
                "text VARCHAR, views BIGINT, likes BIGINT, comments BIGINT, shares BIGINT, engagement BIGINT, "
                "hashtags VARCHAR[])")
    con.execute("CREATE TABLE intelligence_42_core.creators (platform VARCHAR, creator_id VARCHAR, handle VARCHAR, "
                "home_market VARCHAR)")
    con.execute("CREATE TABLE intelligence_42_core.v_post_source_markets (post_id VARCHAR, "
                "source_markets VARCHAR[], source_sightings STRUCT(source_market VARCHAR, source_region VARCHAR, "
                "route VARCHAR, protocol VARCHAR, observed_at TIMESTAMP, obs_date DATE)[])")
    day = date(2026, 10, 8)
    rows = [
        ("caption_only", "a caption that says #Humor out loud", ["memes"]),
        ("tag_only", "no joke in the words here", ["Humor", "fyp"]),
        ("neither", "a post about the braai", ["braai"]),
    ]
    con.executemany("INSERT INTO intelligence_42_core.posts VALUES (?, 'tiktok', ?, ?, NULL, ?, NULL, NULL, ?, "
                    "10, 1, NULL, NULL, 11, ?)",
                    [(pid, f"https://t.example/{pid}", f"u_{pid}", day, text, tags) for pid, text, tags in rows])
    return con


def test_a_hashtag_query_matches_the_tag_column_and_the_caption_without_the_hash():
    ctx = RunContext(run_id="r_duck", tier="T1", as_of=NOW)
    out = search_posts(ctx, DuckWarehouse(humour_con()), "#humor", since="2026-10-04", until="2026-10-10")
    assert {e["id"] for e in out["evidence"]} == {"caption_only", "tag_only"}
    assert terms_of(ctx.queries[out["query_ids"][0]]["params"]) == ["humor"]


def test_the_tag_clause_is_an_exact_case_blind_match_on_the_column_with_the_value_as_a_parameter(ctx):
    wh = TagWarehouse(keyword=[kw("a")])
    search_posts(ctx, wh, "#Humor")
    sql, params = wh.legs()["keyword"][0][0], wh.legs()["keyword"][0][1]
    check_sql(sql)
    assert "UNNEST(p.hashtags)" in sql and "LOWER(TRIM(h)) = LOWER(@term_0)" in sql
    assert "humor" not in sql.lower()
    assert params["term_0"] == "Humor"


def test_a_hash_alone_is_not_a_term(ctx):
    wh = SplitWarehouse([kw("a")], [sem("b", 0.1)])
    with pytest.raises(Refused, match="empty"):
        search_posts(ctx, wh, "# ##")
    assert wh.runs == []


def test_the_default_is_100_posts_and_the_tool_text_says_so(ctx):
    wh = SplitWarehouse([], [])
    search_posts(ctx, wh, "braai")
    assert wh.runs[0][1]["limit"] == 100 and wh.runs[1][1]["k"] == 200
    assert "100" in toolset.DESCRIPTIONS["search_posts"]
    assert "hashtag" in toolset.DESCRIPTIONS["search_posts"].lower()
    assert "100" in toolset.SCHEMAS["search_posts"]["properties"]["limit"]["description"]


# the tags that co-occur with a hashtag query


def test_a_hashtag_query_adds_the_co_occurring_tags_as_alternatives(ctx):
    wh = TagWarehouse(tags=related("funny", "comedy", "memes"), keyword=[kw("a")])
    out = search_posts(ctx, wh, "#humor")
    legs = wh.legs()
    assert len(legs["related"]) == 1 and len(legs["keyword"]) == 1 and len(legs["semantic"]) == 1
    keyword_sql, keyword_params = legs["keyword"][0][0], legs["keyword"][0][1]
    assert terms_of(keyword_params) == ["humor", "funny", "comedy", "memes"]
    assert keyword_sql.count("UNNEST(p.hashtags)") == 4 and keyword_sql.count("CONTAINS_SUBSTR") == 1
    assert out["also_searched"] == ["funny", "comedy", "memes"]
    assert legs["semantic"][0][1]["q"] == "#humor"  # the semantic leg keeps the words the model wrote
    purposes = [q["purpose"] for q in ctx.queries.values()]
    assert any(p.startswith("search_posts related tags") for p in purposes)


def test_the_co_occurrence_read_is_scoped_to_the_market_and_window_and_holds_no_value_in_its_sql(ctx):
    wh = TagWarehouse(tags=related("funny"), keyword=[kw("a")])
    search_posts(ctx, wh, "#Humor", since="2026-10-04", until="2026-10-10", platforms=["tiktok"])
    sql, params = wh.legs()["related"][0][0], wh.legs()["related"][0][1]
    check_sql(sql)
    assert params["market"] == "ZA" and params["since"] == date(2026, 10, 4) and params["until"] == date(2026, 10, 10)
    assert "tiktok" in params.values() and "|humor|" in params["seed_keys"]
    assert "humor" not in sql.lower() and "tiktok" not in sql
    assert "geo_market = @market" in sql


@pytest.mark.parametrize("query,seeds", [("#a", 1), ("#a OR #b", 2), ("#a OR #b OR #c", 3)])
def test_the_term_cap_of_eight_holds_with_the_added_tags(ctx, query, seeds):
    wh = TagWarehouse(tags=related(*[f"t{i}" for i in range(20)]), keyword=[kw("a")])
    out = search_posts(ctx, wh, query)
    terms = terms_of(wh.legs()["keyword"][0][1])
    assert len(terms) == MAX_TERMS == 8
    assert terms[:seeds] == [w.lstrip("#") for w in query.split() if w != "OR"]
    assert len(out["also_searched"]) == MAX_TERMS - seeds


@pytest.mark.parametrize("query", ["braai", "#humor south africa", "#humor #funny", "humor OR funny"])
def test_only_a_query_of_hashtag_alternatives_is_widened(ctx, query):
    wh = TagWarehouse(tags=related("funny"), keyword=[kw("a")])
    out = search_posts(ctx, wh, query)
    assert wh.legs()["related"] == [] and "also_searched" not in out


def test_related_rows_are_cleaned_before_they_become_terms(ctx):
    junk = related("funny", "Funny", "humor", "", "two words", "semi;colon", "and", "comedy") + [
        {"tag": None, "co_posts": 1, "co_creators": 1, "all_posts": 1},
        {"tag": 7, "co_posts": 1, "co_creators": 1, "all_posts": 1}]
    wh = TagWarehouse(tags=junk, keyword=[kw("a")])
    out = search_posts(ctx, wh, "#humor")
    assert out["also_searched"] == ["funny", "and", "comedy"]
    assert terms_of(wh.legs()["keyword"][0][1]) == ["humor", "funny", "and", "comedy"]


def test_a_failed_co_occurrence_read_leaves_the_plain_search_and_is_logged(ctx, caplog):
    wh = TagWarehouse(related_error=RuntimeError("related boom"), keyword=[kw("a")])
    with caplog.at_level(logging.WARNING, logger="core.agent.tools.warehouse"):
        out = search_posts(ctx, wh, "#humor")
    assert [e["id"] for e in out["evidence"]] == ["a"] and "also_searched" not in out
    assert terms_of(wh.legs()["keyword"][0][1]) == ["humor"]
    assert any("related tags" in r.getMessage() and "related boom" in r.getMessage() for r in caplog.records)


# the curated local terms file


def write_terms(tmp_path, body):
    path = tmp_path / "local_terms.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_shipped_terms_file_is_empty_and_says_it_awaits_review():
    assert load_local_terms() == []
    header = LOCAL_TERMS_PATH.read_text(encoding="utf-8").split("\n\n")[0]
    assert header.lstrip().startswith("#")
    for word in ("review", "Albert", "Thapelo"):
        assert word in header


def test_curated_terms_join_a_matching_query_before_the_co_occurring_tags(ctx, tmp_path, monkeypatch):
    path = write_terms(tmp_path, '[[topics]]\nwhen = ["Humor", "funny"]\nterms = ["terma", "#termb"]\n')
    monkeypatch.setattr(wh_module, "LOCAL_TERMS_PATH", path)
    wh = TagWarehouse(tags=related("funny2"), keyword=[kw("a")])
    out = search_posts(ctx, wh, "#HUMOR")
    assert terms_of(wh.legs()["keyword"][0][1]) == ["HUMOR", "terma", "termb", "funny2"]
    assert out["also_searched"] == ["terma", "termb", "funny2"]


def test_curated_terms_also_widen_a_plain_word_query_but_not_an_unrelated_one(ctx, tmp_path, monkeypatch):
    path = write_terms(tmp_path, '[[topics]]\nwhen = ["humor"]\nterms = ["terma"]\n')
    monkeypatch.setattr(wh_module, "LOCAL_TERMS_PATH", path)
    plain = TagWarehouse(keyword=[kw("a")])
    search_posts(ctx, plain, "humor")
    assert terms_of(plain.legs()["keyword"][0][1]) == ["humor", "terma"] and plain.legs()["related"] == []
    other = TagWarehouse(keyword=[kw("a")])
    search_posts(ctx, other, "braai")
    assert terms_of(other.legs()["keyword"][0][1]) == ["braai"]


def test_curated_terms_respect_the_term_cap(ctx, tmp_path, monkeypatch):
    many = ", ".join(f'"w{i}"' for i in range(20))
    path = write_terms(tmp_path, f'[[topics]]\nwhen = ["humor"]\nterms = [{many}]\n')
    monkeypatch.setattr(wh_module, "LOCAL_TERMS_PATH", path)
    wh = TagWarehouse(keyword=[kw("a")])
    search_posts(ctx, wh, "humor")
    assert len(terms_of(wh.legs()["keyword"][0][1])) == MAX_TERMS


@pytest.mark.parametrize("body", [
    "this is not toml [[[",
    'topics = "nope"',
    '[[topics]]\nwhen = "humor"\nterms = ["x"]\n',
    '[[topics]]\nwhen = ["humor"]\nterms = [1, "two words", "", "ok"]\n',
])
def test_a_bad_terms_file_never_breaks_a_search(ctx, tmp_path, monkeypatch, body):
    monkeypatch.setattr(wh_module, "LOCAL_TERMS_PATH", write_terms(tmp_path, body))
    wh = TagWarehouse(keyword=[kw("a")])
    out = search_posts(ctx, wh, "humor")
    assert [e["id"] for e in out["evidence"]] == ["a"]
    assert set(terms_of(wh.legs()["keyword"][0][1])) <= {"humor", "ok"}


# the co-occurrence read itself, run on DuckDB over fixture tables


def test_related_hashtags_ranks_by_how_much_of_a_tag_sits_with_the_seed():
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE SCHEMA intelligence_42_core")
    con.execute("CREATE TABLE intelligence_42_core.posts (post_id VARCHAR, platform VARCHAR, url VARCHAR, "
                "creator_id VARCHAR, published_at TIMESTAMP, post_date DATE, geo_market VARCHAR, geo_source VARCHAR, "
                "text VARCHAR, views BIGINT, likes BIGINT, comments BIGINT, shares BIGINT, engagement BIGINT, "
                "hashtags VARCHAR[])")
    con.execute("CREATE TABLE intelligence_42_core.creators (platform VARCHAR, creator_id VARCHAR, handle VARCHAR, "
                "home_market VARCHAR)")
    day = date(2026, 10, 8)
    rows = [("h1", "u1", ["Humor", "funny", "fyp"]), ("h2", "u2", ["humor", "funny", "fyp"]),
            ("h3", "u3", ["humor", "comedy"]), ("h4", "u4", ["funny"])]
    rows += [(f"g{i}", f"g{i}", ["fyp"]) for i in range(16)]
    con.executemany("INSERT INTO intelligence_42_core.posts VALUES (?, 'tiktok', 'u', ?, NULL, ?, NULL, NULL, 't', "
                    "1, 1, NULL, NULL, 2, ?)", [(pid, who, day, tags) for pid, who, tags in rows])
    ctx = RunContext(run_id="r_rel", tier="T1", as_of=NOW)
    found = wh_module.related_hashtags(ctx, DuckWarehouse(con), ["humor"], date(2026, 10, 4), date(2026, 10, 10), None)
    assert [r["tag"] for r in found] == ["funny"]  # comedy has one creator; fyp is 2 of 18 posts, under the floor
    assert found[0]["co_posts"] == 2 and found[0]["all_posts"] == 3


# Review, Important 1: the family is tag matches only, at least three characters, and a share floor


PROBE = [("memes", 8, 10), ("comedia", 6, 10), ("fyp", 9, 90), ("viral", 9, 60), ("a", 9, 9), ("foryou", 9, 200),
         ("sa", 9, 9)]


def probe_rows():
    return [{"tag": tag, "co_posts": co, "co_creators": 5, "all_posts": total} for tag, co, total in PROBE]


def test_the_probe_family_keeps_only_tags_that_mostly_sit_with_the_seed(ctx):
    wh = TagWarehouse(tags=probe_rows(), keyword=[kw("a")])
    out = search_posts(ctx, wh, "#humor")
    assert out["also_searched"] == ["memes", "comedia"]
    assert terms_of(wh.legs()["keyword"][0][1]) == ["humor", "memes", "comedia"]


def test_a_row_under_the_share_floor_or_three_characters_is_dropped_even_if_the_warehouse_returns_it(ctx):
    rows = [{"tag": "edge", "co_posts": 2, "co_creators": 3, "all_posts": 10},        # exactly 0.2: kept
            {"tag": "under", "co_posts": 19, "co_creators": 3, "all_posts": 100},     # 0.19: dropped
            {"tag": "abc", "co_posts": 5, "co_creators": 3, "all_posts": 5},          # three characters: kept
            {"tag": "ab", "co_posts": 5, "co_creators": 3, "all_posts": 5},           # two: dropped
            {"tag": "noshare", "co_posts": 5, "co_creators": 3},                      # no total: dropped
            {"tag": "zero", "co_posts": 5, "co_creators": 3, "all_posts": 0}]         # no posts: dropped
    out = search_posts(ctx, TagWarehouse(tags=rows, keyword=[kw("a")]), "#humor")
    assert out["also_searched"] == ["edge", "abc"]


def test_the_family_tags_are_matched_against_the_tag_column_only(ctx):
    wh = TagWarehouse(tags=related("funny", "comedy"), keyword=[kw("a")])
    search_posts(ctx, wh, "#humor")
    sql, params = wh.legs()["keyword"][0][0], wh.legs()["keyword"][0][1]
    check_sql(sql)
    assert sql.count("CONTAINS_SUBSTR") == 1  # the seed only; a family tag is never a caption substring
    assert sql.count("UNNEST(p.hashtags)") == 3
    assert "LOWER(TRIM(h)) = LOWER(@term_1)" in sql and "LOWER(TRIM(h)) = LOWER(@term_2)" in sql
    assert params["term_1"] == "funny" and params["term_2"] == "comedy"


def test_a_curated_local_word_is_still_a_caption_or_tag_match(ctx, tmp_path, monkeypatch):
    path = write_terms(tmp_path, '[[topics]]\nwhen = ["humor"]\nterms = ["terma"]\n')
    monkeypatch.setattr(wh_module, "LOCAL_TERMS_PATH", path)
    wh = TagWarehouse(tags=related("funny"), keyword=[kw("a")])
    search_posts(ctx, wh, "#humor")
    assert wh.legs()["keyword"][0][0].count("CONTAINS_SUBSTR") == 2  # the seed and the curated word


def test_a_one_letter_tag_cannot_match_every_caption(ctx):
    wh = TagWarehouse(tags=[{"tag": "a", "co_posts": 9, "co_creators": 5, "all_posts": 9}], keyword=[kw("a")])
    out = search_posts(ctx, wh, "#humor")
    assert "also_searched" not in out and terms_of(wh.legs()["keyword"][0][1]) == ["humor"]


def test_the_co_occurrence_sql_applies_the_floor_and_the_length_on_duckdb():
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE SCHEMA intelligence_42_core")
    con.execute("CREATE TABLE intelligence_42_core.posts (post_id VARCHAR, platform VARCHAR, url VARCHAR, "
                "creator_id VARCHAR, published_at TIMESTAMP, post_date DATE, geo_market VARCHAR, geo_source VARCHAR, "
                "text VARCHAR, views BIGINT, likes BIGINT, comments BIGINT, shares BIGINT, engagement BIGINT, "
                "hashtags VARCHAR[])")
    con.execute("CREATE TABLE intelligence_42_core.creators (platform VARCHAR, creator_id VARCHAR, handle VARCHAR, "
                "home_market VARCHAR)")
    day, rows = date(2026, 10, 8), []
    for i in range(10):  # ten humour posts, by ten creators
        rows.append((f"h{i}", f"u{i}", ["humor", "memes"] + (["comedia"] if i < 6 else []) + ["fyp", "viral", "a", "sa"]
                     + (["foryou"] if i < 9 else [])))
    # the generic tags are everywhere else too
    rows += [(f"fy{i}", f"x{i}", ["fyp"]) for i in range(80)] + [(f"vi{i}", f"y{i}", ["viral"]) for i in range(50)]
    rows += [(f"fo{i}", f"z{i}", ["foryou"]) for i in range(190)]
    con.executemany("INSERT INTO intelligence_42_core.posts VALUES (?, 'tiktok', 'u', ?, NULL, ?, NULL, NULL, 't', "
                    "1, 1, NULL, NULL, 2, ?)", [(pid, who, day, tags) for pid, who, tags in rows])
    ctx = RunContext(run_id="r_floor", tier="T1", as_of=NOW)
    found = wh_module.related_hashtags(ctx, DuckWarehouse(con), ["humor"], date(2026, 10, 4), date(2026, 10, 10), None)
    assert [r["tag"] for r in found] == ["memes", "comedia"]
