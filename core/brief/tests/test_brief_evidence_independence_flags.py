"""N22: the K5 independence flags the evidence pack writes (core/brief/evidence.py), so core/trust/claims.py K5 and
gatectx can see a paid or brand-owned author. Run on DuckDB fixture tables."""

from core.brief import evidence
from core.brief.tests.test_brief_evidence import D, add_post, build, current, duck, utc, world, day


def flags_of(pack):
    return {e["id"]: e["flags"] for e in pack["evidence"]}


def creators(con, *rows):
    duck.load(con, "core.creators", [{"creator_id": c, "platform": "tiktok", "handle": h, "coord_score": 0}
                                     for c, h in rows])


def test_a_caption_or_hashtag_paid_marker_sets_the_paid_flag_without_an_enrich_row():
    con = world()
    add_post(con, "p_text", "c1", utc(day(1), 9), 900, text="New drop #Sponsored today")
    add_post(con, "p_phrase", "c2", utc(day(1), 9), 800, text="In Paid Partnership with a brand")
    add_post(con, "p_tag", "c3", utc(day(1), 9), 700, hashtags=["AD", "dance"])
    add_post(con, "p_plain", "c4", utc(day(1), 9), 600, text="#adventure time #adidas", hashtags=["adventure"])
    add_post(con, "p_collab", "c5", utc(day(1), 9), 500, text="New track #collab", hashtags=["collab"])
    creators(con, ("c1", "a"), ("c2", "b"), ("c3", "c"), ("c4", "d"), ("c5", "e"))
    pack, _, _, _ = build(con)
    by_id = flags_of(pack)
    assert all("paid" in by_id[p] for p in ("p_text", "p_phrase", "p_tag"))
    assert not any("paid" in by_id[p] for p in ("p_plain", "p_collab"))


def test_a_brand_item_whose_key_is_the_authors_handle_sets_brand_owned():
    con = world()
    add_post(con, "p_brand", "c1", utc(day(1), 9), 900)
    add_post(con, "p_fan", "c2", utc(day(1), 9), 800)
    creators(con, ("c1", "@Acme_"), ("c2", "@acme_fan"))
    row = {**current(con), "kind": "brand", "canonical_key": "acme", "label": "Acme"}
    pack, _, _ = evidence.build_pack(duck.Client(con), row, D, "ZA", core="core", agent="agent")
    by_id = flags_of(pack)
    assert "brand_owned" in by_id["p_brand"]
    assert "brand_owned" not in by_id["p_fan"]


def test_the_same_handle_on_a_hashtag_item_is_not_brand_owned():
    con = world()
    add_post(con, "p1", "c1", utc(day(1), 9), 900)
    creators(con, ("c1", "acme"))
    row = {**current(con), "kind": "hashtag", "canonical_key": "acme", "label": "Acme"}
    pack, _, _ = evidence.build_pack(duck.Client(con), row, D, "ZA", core="core", agent="agent")
    assert "brand_owned" not in flags_of(pack)["p1"]


def test_a_short_brand_key_never_marks_an_author():
    con = world()
    add_post(con, "p1", "c1", utc(day(1), 9), 900)
    creators(con, ("c1", "ab"))
    row = {**current(con), "kind": "brand", "canonical_key": "ab", "label": "AB"}
    pack, _, _ = evidence.build_pack(duck.Client(con), row, D, "ZA", core="core", agent="agent")
    assert "brand_owned" not in flags_of(pack)["p1"]


def test_a_brand_is_also_known_by_its_label_when_the_key_differs():
    con = world()
    add_post(con, "p1", "c1", utc(day(1), 9), 900)
    creators(con, ("c1", "@Acme"))
    row = {**current(con), "kind": "brand", "canonical_key": "acme_brand_sa", "label": "Acme"}
    pack, _, _ = evidence.build_pack(duck.Client(con), row, D, "ZA", core="core", agent="agent")
    assert "brand_owned" in flags_of(pack)["p1"]
