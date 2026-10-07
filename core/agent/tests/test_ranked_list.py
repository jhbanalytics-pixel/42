import copy
from datetime import date, datetime, timezone

import pytest

from core.agent import ask
from core.agent.context import RunContext, result_hash
from core.agent.tools.warehouse import store_breadth

QUESTION = "Which sounds are rising on TikTok in South Africa this week?"
CURRENT = [
    {"platform": "tiktok", "sound_id": "sound_a", "sound_title": "Named sound", "earliest_creator": "maker_a",
     "creators": 11, "posts": 13, "sample_post_ids": ["p_a"]},
    {"platform": "tiktok", "sound_id": "sound_b", "sound_title": None, "earliest_creator": "maker_b",
     "creators": 5, "posts": 5, "sample_post_ids": ["p_b"]},
]
TAGS = [{"platform": "tiktok", "hashtag": tag, "creators": creators, "posts": posts,
         "sample_post_ids": ["p_" + letter]}
        for tag, creators, posts, letter in [("amapiano", 11, 13, "a"), ("e\u0301lan", 5, 5, "b")]]


class Warehouse:
    def __init__(self, rows, prior, tags=None):
        self.rows, self.prior, self.tags = rows, prior, tags or []

    def dry_run(self, sql, params):
        return {"bytes": 100, "tables": ["intelligence_42_core.posts"]}

    def run(self, sql, params, max_bytes_billed):
        if sql.startswith("WITH s AS"):
            return copy.deepcopy(self.rows)
        if sql.startswith("WITH b AS"):
            return copy.deepcopy(self.prior)
        if sql.startswith("WITH t AS"):
            return copy.deepcopy(self.tags)
        return []


def fixture(*, prior=None, tie=False, market="ZA", kind="sounds"):
    ctx = RunContext(run_id="r_ranked", tier="T1", market=market, as_of=datetime(2026, 10, 6, tzinfo=timezone.utc),
                     window_start=date(2026, 9, 29), window_end=date(2026, 10, 5))
    rows = copy.deepcopy(CURRENT if kind == "sounds" else TAGS)
    if tie:
        rows[1].update(creators=11, posts=13)
    before = [] if prior is None else [{"kind": "sound" if kind == "sounds" else "hashtag", "platform": "tiktok",
                                       "sound_id": "sound_a" if kind == "sounds" else None,
                                       "hashtag": None if kind == "sounds" else rows[0]["hashtag"],
                                       "creators": 0, "posts": prior}]
    ids = store_breadth(ctx, Warehouse(rows if kind == "sounds" else [], before,
                                     rows if kind == "hashtags" else []), ["tiktok"])
    evidence = [{"id": "p_" + letter, "platform": "tiktok", "handle": "maker_" + letter,
                 "url": "https://example.test/" + letter, "posted_at": "2026-10-01T12:00:00+02:00",
                 "market": "ZA", "text": "A post using this sound", "engagement": {}, "flags": []}
                for letter in ("a", "b")]
    ctx.evidence = {row["id"]: row for row in evidence}

    def pin(value, unit, qid):
        return {"value": value, "unit": unit, "query_id": qid, "run_id": ctx.run_id,
                "result_hash": ctx.queries[qid]["result_hash"]}

    claims = [{"id": "c" + str(i + 1), "text": "Named sound appeared in the posts." if i == 0
               else "An untitled sound was used by @maker_b.", "kind": "observation", "label": "single_source",
               "evidence_ids": [evidence[i]["id"]], "quotes": [],
               "numbers": [pin(row["creators"], "creators", ids[kind]),
                           pin(row["posts"], "posts", ids[kind])]}
              for i, row in enumerate(rows)]
    if kind == "hashtags":
        for claim, row in zip(claims, rows):
            claim["text"] = "#" + row["hashtag"] + " appeared in the posts."
    if prior is not None:
        claims[0]["numbers"].append(pin(prior, "posts in previous week", ids["before"]))
    answer = {"status": "complete", "as_of": ctx.as_of.isoformat(), "short_answer": "The checked findings follow.",
              "claims": claims, "evidence": evidence, "so_what": [], "watch_next": [], "gaps": [], "context": ""}
    return ctx, answer, ids


def project(ctx, answer, **kw):
    return getattr(ask, "ranked_list", lambda *a, **k: (None, []))(QUESTION, answer, ctx, **kw)


def rehash(ctx, answer, qid):
    ctx.queries[qid]["result_hash"] = result_hash(ctx.queries[qid]["rows"])
    for claim in answer["claims"]:
        for number in claim["numbers"]:
            if number["query_id"] == qid:
                number["result_hash"] = ctx.queries[qid]["result_hash"]


def test_comparable_checked_items_lead_with_names_counts_and_unchanged_pins():
    ctx, answer, ids = fixture()
    frozen = copy.deepcopy((answer, ctx.queries))
    result, notices = project(ctx, answer)
    assert result is not None
    assert result["market"] == "ZA" and result["platform"] == "tiktok"
    assert result["measure_columns"] == {"creators": "creators", "posts": "posts"}
    assert result["scope"] == "retained_whole_store_claims"
    assert result["window"] == {"from": "2026-09-29", "to": "2026-10-05"}
    assert result["previous_window"] is None
    assert result["items"] == [
        {"claim_id": "c1", "title": "Named sound", "usage_handle": "maker_a", "creators_index": 0,
         "posts_index": 1, "previous_posts_index": None, "tied_with_previous": False},
        {"claim_id": "c2", "title": None, "usage_handle": "maker_b", "creators_index": 0,
         "posts_index": 1, "previous_posts_index": None, "tied_with_previous": False},
    ]
    assert notices == [] and (answer, ctx.queries) == frozen


def test_an_authoritative_all_market_query_keeps_an_explicit_null_market_basis():
    ctx, answer, ids = fixture(market=None)
    result, notices = project(ctx, answer)
    assert result is not None and result["market"] is None
    assert "market" not in ctx.queries[ids["sounds"]]["params"] and notices == []


@pytest.mark.parametrize("value", [0, 3])
def test_checked_prior_pin_is_preserved_without_deriving_a_change(value):
    ctx, answer, ids = fixture(prior=value)
    result, notices = project(ctx, answer)
    assert result is not None
    assert result["previous_window"] == {"from": "2026-09-22", "to": "2026-09-28"}
    assert result["items"][0]["previous_posts_index"] == 2
    assert result["items"][1]["previous_posts_index"] is None
    assert answer["claims"][0]["numbers"][2]["value"] == value
    assert "percent" not in str(result) and "change" not in str(result)


def test_a_prior_row_without_a_checked_pin_stays_unavailable():
    ctx, answer, ids = fixture(prior=3)
    answer["claims"][0]["numbers"].pop()
    result, _ = project(ctx, answer)
    assert result is not None and result["previous_window"] is None
    assert all(item["previous_posts_index"] is None for item in result["items"])


def test_equal_counts_are_tied_and_not_merged():
    ctx, answer, ids = fixture(tie=True)
    result, _ = project(ctx, answer)
    assert result is not None
    assert [item["claim_id"] for item in result["items"]] == ["c1", "c2"]
    assert [item["tied_with_previous"] for item in result["items"]] == [False, True]


@pytest.mark.parametrize("key, value", [("tool", None), ("market", "NG"), ("profile_source", "different"),
                                       ("platform_0", "instagram"), ("since", date(2026, 9, 28))])
def test_forged_authority_or_mismatched_current_scope_is_unrankable(key, value):
    ctx, answer, ids = fixture()
    query = ctx.queries[ids["sounds"]]
    if key == "tool":
        query.pop("tool")
    else:
        query["params"][key] = value
    result, notices = project(ctx, answer)
    assert result is None and notices


@pytest.mark.parametrize("unit", ["peak_creators", "located_creators"])
def test_a_different_measure_basis_is_excluded_even_when_values_coincide(unit):
    ctx, answer, ids = fixture()
    answer["claims"][0]["numbers"][0]["unit"] = unit
    result, notices = project(ctx, answer)
    assert result is not None and [item["claim_id"] for item in result["items"]] == ["c2"]
    assert notices


def test_coincident_watchlist_query_values_do_not_supply_whole_store_pins():
    ctx, answer, ids = fixture()
    other, digest = ctx.record_query("SELECT 11 AS peak_creators, 13 AS total_posts", {},
                                     [{"peak_creators": 11, "total_posts": 13}], "Whole-store sounds")
    for number in answer["claims"][0]["numbers"]:
        number.update(query_id=other, result_hash=digest)
    result, notices = project(ctx, answer)
    assert result is not None and [item["claim_id"] for item in result["items"]] == ["c2"]
    assert notices


def test_ambiguous_sample_identity_is_excluded():
    ctx, answer, ids = fixture()
    duplicate = {**ctx.queries[ids["sounds"]]["rows"][0], "sound_id": "other_sound"}
    ctx.queries[ids["sounds"]]["rows"].append(duplicate)
    rehash(ctx, answer, ids["sounds"])
    result, notices = project(ctx, answer)
    assert result is not None and [item["claim_id"] for item in result["items"]] == ["c2"]
    assert notices


def test_a_usage_handle_must_belong_to_that_items_cited_sample():
    ctx, answer, ids = fixture()
    ctx.queries[ids["sounds"]]["rows"][1]["earliest_creator"] = "maker_a"
    rehash(ctx, answer, ids["sounds"])
    result, _ = project(ctx, answer)
    assert result is not None and result["items"][1]["usage_handle"] is None


def test_a_generic_original_sound_label_does_not_become_a_song_title_or_artist_credit():
    ctx, answer, ids = fixture()
    ctx.queries[ids["sounds"]]["rows"][0]["sound_title"] = "original sound - maker_a"
    answer["claims"][0]["text"] = "The original sound - maker_a appeared in clips."
    rehash(ctx, answer, ids["sounds"])
    result, _ = project(ctx, answer)
    assert result is not None
    assert result["items"][0]["title"] is None and result["items"][0]["usage_handle"] == "maker_a"


def test_a_checked_song_title_starting_with_untitled_remains_a_known_name():
    ctx, answer, ids = fixture()
    ctx.queries[ids["sounds"]]["rows"][0]["sound_title"] = "Untitled by Maker A"
    answer["claims"][0]["text"] = "Untitled by Maker A appeared in clips."
    rehash(ctx, answer, ids["sounds"])
    result, _ = project(ctx, answer)
    assert result is not None and result["items"][0]["title"] == "Untitled by Maker A"


@pytest.mark.parametrize("key, value", [("market", "NG"), ("profile_source", "other"),
                                       ("platform_0", "instagram"), ("until", date(2026, 9, 27))])
def test_a_prior_pin_with_a_different_scope_is_not_a_week_comparison(key, value):
    ctx, answer, ids = fixture(prior=3)
    ctx.queries[ids["before"]]["params"][key] = value
    result, _ = project(ctx, answer)
    assert result is not None and result["previous_window"] is None
    assert result["items"][0]["previous_posts_index"] is None


def test_masked_or_unfinished_answers_have_no_projection():
    ctx, answer, ids = fixture()
    assert project(ctx, answer, masked=True)[0] is None
    answer["status"] = "insufficient_evidence"
    assert project(ctx, answer)[0] is None


@pytest.mark.parametrize("question", ["Which creators use these sounds?",
                                      "Which sound engineers are popular?", "Which audio platforms are growing?",
                                      "What audio equipment is trending?"])
def test_another_question_subject_does_not_receive_a_sound_ranking(question):
    ctx, answer, ids = fixture()
    result, notices = ask.ranked_list(question, answer, ctx)
    assert result is None and notices == []


def tags_project(ctx, answer, **kw):
    return ask.ranked_list("Which hashtags are rising on TikTok this week?", answer, ctx, **kw)


@pytest.mark.parametrize("prior", [None, 0, 3])
def test_hashtags_use_checked_pairs_and_exact_prior_identity_without_changing_pins(prior):
    ctx, answer, ids = fixture(kind="hashtags", prior=prior)
    frozen = copy.deepcopy((answer, ctx.queries))
    result, notices = tags_project(ctx, answer)
    assert result is not None and result["kind"] == "hashtags" and result["query_id"] == ids["hashtags"]
    assert [item["title"] for item in result["items"]] == ["#amapiano", "#e\u0301lan"]
    assert all(item["usage_handle"] is None for item in result["items"])
    assert result["items"][0]["previous_posts_index"] == (None if prior is None else 2)
    assert result["items"][1]["previous_posts_index"] is None
    assert (result["previous_window"] is None) == (prior is None)
    assert notices == [] and (answer, ctx.queries) == frozen


def test_hashtag_token_disambiguates_equal_counts_on_the_same_sample_post():
    ctx, answer, ids = fixture(kind="hashtags", tie=True)
    ctx.queries[ids["hashtags"]]["rows"][1]["sample_post_ids"] = ["p_a"]
    answer["claims"][1]["evidence_ids"] = ["p_a"]
    rehash(ctx, answer, ids["hashtags"])
    result, notices = tags_project(ctx, answer)
    assert result is not None and notices == []
    assert [item["claim_id"] for item in result["items"]] == ["c1", "c2"]
    assert [item["tied_with_previous"] for item in result["items"]] == [False, True]
    answer["claims"][0]["text"] = "#amapiano and #e\u0301lan appeared together."
    result, notices = tags_project(ctx, answer)
    assert [item["claim_id"] for item in result["items"]] == ["c2"] and notices


@pytest.mark.parametrize("text", ["#amapianomore", "#amapiano\u0301", "x#amapiano", "中#amapiano", "#amapiano中",
                                  "##amapiano", "amapiano"])
def test_hashtag_substrings_and_combining_mark_suffixes_do_not_identify_a_checked_tag(text):
    ctx, answer, ids = fixture(kind="hashtags")
    answer["claims"][0]["text"] = text + " appeared in the posts."
    result, notices = tags_project(ctx, answer)
    assert result is not None and [item["claim_id"] for item in result["items"]] == ["c2"] and notices


@pytest.mark.parametrize("field,value", [("kind", "sound"), ("hashtag", "#amapiano"), ("hashtag", "AMAPIANO")])
def test_prior_hashtag_kind_and_raw_key_are_not_substituted(field, value):
    ctx, answer, ids = fixture(kind="hashtags", prior=3)
    ctx.queries[ids["before"]]["rows"][0][field] = value
    rehash(ctx, answer, ids["before"])
    result, _ = tags_project(ctx, answer)
    assert result is not None and result["items"][0]["previous_posts_index"] is None
    assert result["previous_window"] is None


def test_a_raw_hashtag_key_with_its_hash_keeps_exact_prior_identity_and_one_display_hash():
    ctx, answer, ids = fixture(kind="hashtags", prior=3)
    for qid in (ids["hashtags"], ids["before"]):
        ctx.queries[qid]["rows"][0]["hashtag"] = "#amapiano"
        rehash(ctx, answer, qid)
    result, _ = tags_project(ctx, answer)
    assert result is not None and result["items"][0]["title"] == "#amapiano"
    assert result["items"][0]["previous_posts_index"] == 2


def test_hashtag_authority_measure_and_masking_use_existing_boundaries():
    ctx, answer, ids = fixture(kind="hashtags")
    assert tags_project(ctx, answer, masked=True)[0] is None
    answer["claims"][0]["numbers"][0]["unit"] = "located_creators"
    result, notices = tags_project(ctx, answer)
    assert [item["claim_id"] for item in result["items"]] == ["c2"] and notices
    ctx.queries[ids["hashtags"]].pop("tool")
    assert tags_project(ctx, answer)[0] is None


def test_sound_rows_cannot_supply_a_hashtag_projection():
    ctx, answer, ids = fixture()
    assert tags_project(ctx, answer)[0] is None


def test_a_hashtag_strategy_question_does_not_receive_an_item_projection():
    ctx, answer, ids = fixture(kind="hashtags")
    assert ask.ranked_list("Which hashtag strategies work?", answer, ctx) == (None, [])
