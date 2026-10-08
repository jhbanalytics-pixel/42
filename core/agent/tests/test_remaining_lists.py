import copy
from datetime import date, datetime, timezone

import pytest

from core.agent.context import RunContext, result_hash
from core.agent.tools.warehouse import discover_creators, rising_topics, store_breadth

try:
    from core.agent.remaining_lists import entity_lists
except ModuleNotFoundError as error:
    if error.name != "core.agent.remaining_lists":
        raise

    def entity_lists(*args, **kwargs):
        return [], []


QUESTIONS = {"platforms": "Which platforms are busiest this week?", "topics": "Which topics are rising this week?",
             "creators": "Which creators are posting about amapiano this week?"}


class Warehouse:
    def __init__(self, kind, rows, prior):
        self.kind, self.rows, self.prior = kind, rows, prior

    def dry_run(self, sql, params):
        return {"bytes": 100, "tables": ["intelligence_42_core.posts"]}

    def run(self, sql, params, max_bytes_billed):
        if self.kind == "platforms" and sql.startswith("SELECT p.platform"):
            return copy.deepcopy(self.rows)
        if self.kind == "topics" and sql.startswith("SELECT t.metric_date"):
            return copy.deepcopy(self.prior if params["date"] == date(2026, 9, 28) else self.rows)
        if self.kind == "creators" and sql.startswith("SELECT c.platform"):
            return [{"platform": row["platform"], "creator_id": row["creator_id"], "handle": row["handle"],
                     "display_name": row["handle"], "tier": "macro"} for row in self.rows]
        if self.kind == "creators" and " AS handle" in sql:
            return copy.deepcopy(self.rows)
        return []


def fixture(kind, *, prior=None, split_dates=False):
    ctx = RunContext(run_id="r_entities", tier="T1", market="ZA", as_of=datetime(2026, 10, 6, tzinfo=timezone.utc),
                     window_start=date(2026, 9, 29), window_end=date(2026, 10, 5))
    posts = [{"post_id": "p_" + letter, "platform": platform, "creator_id": "author_" + letter,
              "handle": "maker_" + letter, "url": "https://example.test/" + letter,
              "published_at": datetime(2026, 10, 1, tzinfo=timezone.utc), "post_date": date(2026, 10, 1),
              "geo_market": "ZA", "geo_source": "ext_region", "home_market": "ZA", "text": "An amapiano post",
              "views": 1, "likes": 1, "comments": 0, "shares": 0, "engagement": 2, "source_sightings": []}
             for letter, platform in (("a", "tiktok"), ("b", "instagram"))]
    evidence = [{"id": row["post_id"], "platform": row["platform"], "handle": row["handle"], "url": row["url"],
                 "posted_at": row["published_at"].isoformat(), "market": "ZA", "text": row["text"],
                 "engagement": {}, "flags": []} for row in posts]
    ctx.evidence = {row["id"]: row for row in evidence}
    discovered = None
    if kind == "platforms":
        rows = [{"platform": "tiktok", "creators": 11, "posts": 13},
                {"platform": "instagram", "creators": 5, "posts": 20}]
        ids = store_breadth(ctx, Warehouse(kind, rows, []))
        qid, columns = ids["totals"], ("creators", "posts")
        titles = ["TikTok", "Instagram"]
    elif kind == "topics":
        rows = [{"metric_date": date(2026, 10, 5 if i == 0 or not split_dates else 4), "market": "ZA",
                 "item_id": "topic_" + letter, "kind": "topic", "label": "Topic " + letter.upper(),
                 "creators3": creators, "posts3": count, "run_id": "detect_current"}
                for i, (letter, creators, count) in enumerate((("a", 11, 13), ("b", 5, 5)))]
        before = [] if prior is None else [{**rows[0], "metric_date": date(2026, 9, 28), "posts3": prior}]
        wh = Warehouse(kind, rows, before)
        qid = rising_topics(ctx, wh)["query_id"]
        ids, columns, titles = {"current": qid}, ("creators3", "posts3"), [row["label"] for row in rows]
        if prior is not None:
            ids["prior"] = rising_topics(ctx, wh, date="2026-09-28", window_days=1)["query_id"]
    else:
        discovered = discover_creators(ctx, Warehouse(kind, posts, []), QUESTIONS[kind])
        evidence = list(ctx.evidence.values())
        rows, ids, columns, titles = posts, {"current": discovered["creator_query_id"]}, (), ["@maker_a", "@maker_b"]
        qid = ids["current"]

    def pin(value, unit, query):
        return {"value": value, "unit": unit, "query_id": query, "run_id": ctx.run_id,
                "result_hash": ctx.queries[query]["result_hash"]}

    claims = [{"id": "c" + str(i + 1), "text": titles[i] + " appeared in checked posts.", "kind": "observation",
               "label": "single_source", "evidence_ids": ["p_" + letter], "quotes": [],
               "numbers": [pin(row[column], column, qid) for column in columns]}
              for i, (row, letter) in enumerate(zip(rows, ("a", "b")))]
    if kind == "topics" and prior is not None:
        claims[0]["numbers"].append(pin(prior, "posts3", ids["prior"]))
    answer = {"status": "complete", "as_of": ctx.as_of.isoformat(), "short_answer": "The checked findings follow.", "claims": claims,
              "evidence": evidence, "so_what": [], "watch_next": [], "gaps": [], "context": ""}
    return ctx, answer, discovered, ids


def project(kind, **kwargs):
    ctx, answer, discovered, ids = fixture(kind, **kwargs)
    groups, notices = entity_lists(QUESTIONS[kind], answer, ctx, creator_discovery=discovered)
    return groups, notices, ctx, answer, discovered, ids


@pytest.mark.parametrize("kind", ["platforms", "topics", "creators"])
def test_current_contracts_produce_bound_items_without_mutating_checked_objects(kind):
    ctx, answer, discovered, ids = fixture(kind)
    frozen = copy.deepcopy((answer, ctx.queries, discovered))
    groups, notices = entity_lists(QUESTIONS[kind], answer, ctx, creator_discovery=discovered)
    assert groups and sum(len(group["items"]) for group in groups) == 2
    assert (answer, ctx.queries, discovered) == frozen and notices == []
    if kind == "creators":
        assert groups[0]["order"] is None
        assert groups[0]["unavailable"] == {"creators": "not_recorded", "posts": "not_recorded", "previous_week": "not_recorded"}
        assert all(item["creators_index"] is None and item["posts_index"] is None for item in groups[0]["items"])
    else:
        assert all(group["items"][0]["creators_index"] == 0 and group["items"][0]["posts_index"] == 1 for group in groups)


def test_platforms_rank_posts_first_and_do_not_invent_prior_counts():
    groups, _, *_ = project("platforms")
    assert [item["name"] for item in groups[0]["items"]] == ["Instagram", "TikTok"]
    assert groups[0]["order"] == "posts_desc_creators_desc" and groups[0]["previous_window"] is None


@pytest.mark.parametrize("prior", [None, 0, 3])
def test_topic_windows_and_prior_pins_are_exact_three_day_periods(prior):
    groups, _, *_ = project("topics", prior=prior)
    assert groups[0]["window"] == {"from": "2026-10-03", "to": "2026-10-05"}
    assert groups[0]["measure_columns"] == {"creators": "creators3", "posts": "posts3"}
    assert groups[0]["items"][0]["previous_posts_index"] == (None if prior is None else 2)
    assert groups[0]["previous_window"] == (None if prior is None else {"from": "2026-09-26", "to": "2026-09-28"})


def test_topics_from_different_snapshot_dates_are_separate_groups():
    groups, _, *_ = project("topics", split_dates=True)
    assert len(groups) == 2 and {group["window"]["to"] for group in groups} == {"2026-10-04", "2026-10-05"}


@pytest.mark.parametrize("kind", ["platforms", "topics"])
def test_missing_authority_mismatched_pairs_and_ambiguous_rows_cannot_supply_a_list(kind):
    ctx, answer, discovered, ids = fixture(kind)
    qid = ids["totals"] if kind == "platforms" else ids["current"]
    ctx.queries[qid].pop("tool")
    assert entity_lists(QUESTIONS[kind], answer, ctx)[0] == []


def test_creator_identity_requires_the_actual_discovery_return_and_cited_post():
    ctx, answer, discovered, ids = fixture("creators")
    assert entity_lists(QUESTIONS["creators"], answer, ctx)[0] == []
    answer["claims"][0]["evidence_ids"] = ["p_b"]
    groups, notices = entity_lists(QUESTIONS["creators"], answer, ctx, creator_discovery=discovered)
    assert [item["name"] for item in groups[0]["items"]] == ["@maker_b"] and notices


@pytest.mark.parametrize("field,value", [("market", "NG"), ("kind", "sound"), ("item_id", "other"),
                                        ("metric_date", date(2026, 9, 27))])
def test_an_incompatible_topic_prior_row_stays_unavailable(field, value):
    ctx, answer, discovered, ids = fixture("topics", prior=3)
    ctx.queries[ids["prior"]]["rows"][0][field] = value
    digest = result_hash(ctx.queries[ids["prior"]]["rows"])
    ctx.queries[ids["prior"]]["result_hash"] = digest
    answer["claims"][0]["numbers"][2]["result_hash"] = digest
    groups, _ = entity_lists(QUESTIONS["topics"], answer, ctx)
    assert groups[0]["items"][0]["previous_posts_index"] is None


def test_masking_held_answers_and_other_subjects_do_not_produce_optional_lists():
    ctx, answer, discovered, ids = fixture("platforms")
    assert entity_lists(QUESTIONS["platforms"], answer, ctx, masked=True)[0] == []
    assert entity_lists("Which platform engineers are growing?", answer, ctx)[0] == []
    answer["status"] = "insufficient_evidence"
    assert entity_lists(QUESTIONS["platforms"], answer, ctx)[0] == []


def rehash(ctx, answer, qid):
    ctx.queries[qid]["result_hash"] = result_hash(ctx.queries[qid]["rows"])
    for claim in answer["claims"]:
        for number in claim.get("numbers") or []:
            if number["query_id"] == qid:
                number["result_hash"] = ctx.queries[qid]["result_hash"]


@pytest.mark.parametrize("kind", ["platforms", "topics"])
@pytest.mark.parametrize("fault", ["value", "unit", "hash", "run", "ambiguous", "malformed"])
def test_a_bad_or_ambiguous_checked_pair_leaves_the_other_item_visible(kind, fault):
    ctx, answer, discovered, ids = fixture(kind)
    qid = ids["totals"] if kind == "platforms" else ids["current"]
    pin = answer["claims"][0]["numbers"][0]
    if fault == "value":
        pin["value"] += 1
    elif fault == "unit":
        pin["unit"] = "located_creators"
    elif fault == "hash":
        pin["result_hash"] = "sha256:" + "0" * 64
    elif fault == "run":
        pin["run_id"] = "other"
    elif fault == "malformed":
        answer["claims"][0]["numbers"] = None
    else:
        ctx.queries[qid]["rows"].append(copy.deepcopy(ctx.queries[qid]["rows"][0]))
        rehash(ctx, answer, qid)
    groups, notices = entity_lists(QUESTIONS[kind], answer, ctx)
    assert [item["claim_id"] for group in groups for item in group["items"]] == ["c2"] and notices


@pytest.mark.parametrize("field,value", [("market", "NG"), ("profile_source", "other"),
                                        ("since", date(2026, 9, 28)), ("until", date(2026, 10, 4))])
def test_platform_scope_does_not_mix_markets_profiles_or_windows(field, value):
    ctx, answer, discovered, ids = fixture("platforms")
    ctx.queries[ids["totals"]]["params"][field] = value
    assert entity_lists(QUESTIONS["platforms"], answer, ctx)[0] == []


def test_a_topic_prior_row_without_a_retained_checked_pin_stays_unavailable():
    ctx, answer, discovered, ids = fixture("topics", prior=3)
    answer["claims"][0]["numbers"].pop()
    groups, _ = entity_lists(QUESTIONS["topics"], answer, ctx)
    assert groups[0]["previous_window"] is None
    assert groups[0]["items"][0]["previous_posts_index"] is None


def test_a_later_topic_lookup_date_does_not_change_the_recorded_metric_window():
    groups, _, ctx, _, _, ids = project("topics")
    assert ctx.queries[ids["current"]]["params"]["date"] == date(2026, 10, 6)
    assert groups[0]["window"]["to"] == "2026-10-05"


def test_all_market_topic_queries_keep_the_actual_matching_market_on_a_prior_pin():
    ctx, answer, discovered, ids = fixture("topics", prior=3)
    ctx.market = None
    for qid in (ids["current"], ids["prior"]):
        ctx.queries[qid]["params"].pop("market")
    groups, _ = entity_lists(QUESTIONS["topics"], answer, ctx)
    assert groups[0]["market"] == "ZA" and groups[0]["items"][0]["previous_posts_index"] == 2


def test_same_creator_handle_on_different_platforms_is_not_merged():
    ctx, answer, discovered, ids = fixture("creators")
    identity_qid, posts_qid = discovered["creator_query_id"], discovered["posts_query_id"]
    ctx.queries[identity_qid]["rows"][1]["handle"] = "maker_a"
    discovered["creator_candidates"][1]["handle"] = "maker_a"
    ctx.queries[posts_qid]["rows"][1]["handle"] = "maker_a"
    answer["evidence"][1]["handle"] = "maker_a"
    answer["claims"][1]["text"] = "@maker_a appeared in checked posts."
    for qid in (identity_qid, posts_qid):
        rehash(ctx, answer, qid)
    groups, notices = entity_lists(QUESTIONS["creators"], answer, ctx, creator_discovery=discovered)
    assert [item["entity_id"] for item in groups[0]["items"]] == ["tiktok:author_a", "instagram:author_b"]
    assert [item["name"] for item in groups[0]["items"]] == ["@maker_a", "@maker_a"] and not notices


def test_creator_identity_hashes_cannot_be_reconstructed_from_a_purpose_string():
    ctx, answer, discovered, ids = fixture("creators")
    ctx.queries[discovered["posts_query_id"]]["result_hash"] = "sha256:" + "0" * 64
    assert entity_lists(QUESTIONS["creators"], answer, ctx, creator_discovery=discovered)[0] == []


def test_a_checked_creator_handle_without_an_at_prefix_keeps_its_literal_name():
    ctx, answer, discovered, ids = fixture("creators")
    for claim in answer["claims"]:
        claim["text"] = claim["text"].replace("@", "")
    groups, notices = entity_lists(QUESTIONS["creators"], answer, ctx, creator_discovery=discovered)
    assert [item["name"] for item in groups[0]["items"]] == ["maker_a", "maker_b"] and not notices


def test_one_checked_creator_claim_cannot_expand_the_existing_ten_item_list_cap():
    ctx, answer, discovered, ids = fixture("creators")
    post = ctx.queries[discovered["posts_query_id"]]["rows"][0]
    rows = [{**post, "post_id": f"p_{i}", "creator_id": f"author_{i}", "handle": f"maker_{i:02}"} for i in range(20)]
    discovered = discover_creators(ctx, Warehouse("creators", rows, []), QUESTIONS["creators"])
    answer["evidence"] = list(ctx.evidence.values())
    answer["claims"] = [{**answer["claims"][0], "text": " ".join("@" + row["handle"] for row in rows),
                         "evidence_ids": [row["post_id"] for row in rows]}]
    groups, notices = entity_lists(QUESTIONS["creators"], answer, ctx, creator_discovery=discovered)
    assert len(groups[0]["items"]) == 10 and notices
    assert len(answer["claims"][0]["evidence_ids"]) == 20


@pytest.mark.parametrize("text,expected", [
    ("@maker_a posted.", "@maker_a"), ("maker_a posted.", "maker_a"),
    ("(@maker_a), then another post.", "@maker_a"), ("maker_a. Another sentence.", "maker_a"),
    ("maker_a...", "maker_a"), ("@maker_a.extra posted.", None), ("maker_a.extra posted.", None),
    ("maker_a-extra posted.", None),
    ("person@maker_a.example.test replied.", None), ("maker_a@example.test replied.", None),
    ("contact@maker_a replied.", None),
    ("person@maker_a.example.test; @maker_a posted.", "@maker_a"),
])
def test_creator_names_match_full_handles_and_not_extensions_or_email_occurrences(text, expected):
    ctx, answer, discovered, ids = fixture("creators")
    answer["claims"][0]["text"] = text
    groups, notices = entity_lists(QUESTIONS["creators"], answer, ctx, creator_discovery=discovered)
    first = next((item for item in groups[0]["items"] if item["claim_id"] == "c1"), None)
    assert (first["name"] if first else None) == expected
    assert any(item["claim_id"] == "c2" for item in groups[0]["items"])


@pytest.mark.parametrize("other_platform", [True, False])
def test_a_raw_twitter_and_x_collision_does_not_present_one_rows_partial_canonical_total(other_platform):
    ctx, answer, discovered, ids = fixture("platforms")
    rows = ctx.queries[ids["totals"]]["rows"]
    rows[0]["platform"] = "twitter"
    rows.append({"platform": "x", "creators": 3, "posts": 4})
    answer["claims"][0]["text"] = "X appeared in checked posts."
    answer["evidence"][0]["platform"] = "x"
    if not other_platform:
        rows.pop(1)
        answer["claims"] = answer["claims"][:1]
        answer["evidence"] = answer["evidence"][:1]
    rehash(ctx, answer, ids["totals"])
    frozen = copy.deepcopy((answer, ctx.queries))
    groups, notices = entity_lists(QUESTIONS["platforms"], answer, ctx)
    assert [item["name"] for group in groups for item in group["items"]] == (["Instagram"] if other_platform else [])
    assert notices
    assert frozen == (answer, ctx.queries)


@pytest.mark.parametrize("raw", ["twitter", "x"])
def test_a_single_bound_x_alias_row_can_still_be_presented(raw):
    ctx, answer, discovered, ids = fixture("platforms")
    ctx.queries[ids["totals"]]["rows"][0]["platform"] = raw
    answer["claims"][0]["text"] = "X appeared in checked posts."
    answer["evidence"][0]["platform"] = "x"
    rehash(ctx, answer, ids["totals"])
    groups, _ = entity_lists(QUESTIONS["platforms"], answer, ctx)
    assert {item["name"] for item in groups[0]["items"]} == {"X", "Instagram"}
