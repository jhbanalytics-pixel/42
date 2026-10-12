"""Today's card order per market: the accounts posting in the counted window first, then posts in that window, then
worth_raw, then item_id. On 12 October 2026 ZA card 01 had 2 accounts and 3 posts and led cards with 6 and 5 accounts,
because the order was worth_raw alone. The fixture is that day's stored order, read from BigQuery."""

import json
from pathlib import Path

from core.brief.payload import build_market_payload

from . import test_brief_job as jobt
from .test_brief_payload import D, cand, decision, num

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "today_order_20261012.json").read_text(encoding="utf-8"))
ACCOUNTS, POSTS = "creators in 3 days", "posts in 3 days"


def day_cands(market):
    rows = FIXTURE["markets"][market]["cards"]
    return [cand(i, worth=r["worth_raw"], item_id=r["item_id"], title=r["title"],
                 creators3=r[ACCOUNTS], posts3=r[POSTS],
                 numbers=[num(r[ACCOUNTS], ACCOUNTS, 1), num(r[POSTS], POSTS, 2), num(2.8, "times usual", 3)])
            for i, r in enumerate(reversed(rows), 1)]


def titles(p):
    return [c["title"] for c in p["cards"] + p["more"]]


def build(market, cands):
    p = build_market_payload(market, D, cands, moments=[], boards=[], banners=[])
    top = p["cards"][0]
    # The brief job's headline is the rank one card's explanation (core/brief/job.py _market_payload).
    headline = {"text": top["explanation"], "item_id": top["item_id"], "claim_ids": top["explanation_claim_ids"]}
    return build_market_payload(market, D, cands, moments=[], boards=[], banners=[], headline=headline)


def test_the_fixture_replays_the_stored_order_of_12_october():
    for market in ("ZA", "NG", "KE"):
        stored = sorted(FIXTURE["markets"][market]["cards"], key=lambda r: r["stored_rank"])
        assert [r["worth_raw"] for r in stored] == sorted((r["worth_raw"] for r in stored), reverse=True)
        assert FIXTURE["markets"][market]["headline_item_id"] == stored[0]["item_id"]
    assert [(r["title"], r[ACCOUNTS], r[POSTS]) for r in FIXTURE["markets"]["ZA"]["cards"]] == [
        ("#MTN8Final", 2, 3), ("ouaddou, abdeslam, abdeslam ouaddou", 6, 15),
        ("bafanabafana, mofokeng, relebohile", 5, 11), ("#MTN8", 5, 9)]


def test_za_12_october_leads_with_the_most_accounts_and_the_headline_follows():
    p = build("ZA", day_cands("ZA"))
    assert titles(p) == ["ouaddou, abdeslam, abdeslam ouaddou", "bafanabafana, mofokeng, relebohile", "#MTN8",
                         "#MTN8Final"]
    assert [c["rank"] for c in p["cards"]] == [1, 2, 3, 4]
    assert p["headline"]["item_id"] == "2e884d7ea20320b7cda04df96a89a79b10d7ebd4e5ecad0e34e8c19dd07d2232"


def test_ng_12_october_orders_by_accounts_then_posts():
    p = build("NG", day_cands("NG"))
    assert titles(p) == ["manchester, league, portugal", "#premierleague", "#liverpool", "messi, soccer, argentina",
                         "#football", "#manchesterunited", "#chelseafc"]
    assert len(p["cards"]) == 5 and len(p["more"]) == 2
    assert p["headline"]["item_id"] == "3791be7f803e3761334d8cf11299f492c53947e2d7a6c709c302356513e33bbc"


def test_ke_12_october_single_card_is_unchanged():
    p = build("KE", day_cands("KE"))
    assert titles(p) == ["kikuyucomedy, tata wa, tata"]


def test_the_set_shown_is_still_the_top_ten_by_worth():
    cands = [cand(i, worth=1 - i / 100, numbers=[num(3, ACCOUNTS)]) for i in range(1, 11)]
    cands.append(cand(11, worth=0.01, numbers=[num(90, ACCOUNTS)]))
    p = build_market_payload("ZA", D, cands, moments=[], boards=[], banners=[])
    shown = [c["item_id"] for c in p["cards"] + p["more"]]
    assert "it_11" not in shown and len(shown) == 10


def test_ties_fall_to_posts_then_worth_then_item_id():
    cands = [
        cand(1, worth=0.9, numbers=[num(5, ACCOUNTS), num(9, POSTS)]),
        cand(2, worth=0.1, numbers=[num(5, ACCOUNTS), num(11, POSTS)]),
        cand(3, worth=0.5, numbers=[num(5, ACCOUNTS), num(9, POSTS)]),
        cand(4, worth=0.5, item_id="it_0", numbers=[num(5, ACCOUNTS), num(9, POSTS)]),
    ]
    p = build_market_payload("ZA", D, cands, moments=[], boards=[], banners=[])
    assert [c["item_id"] for c in p["cards"]] == ["it_2", "it_1", "it_0", "it_3"]


def test_the_accounts_figure_is_the_one_the_card_shows_not_the_row_column():
    # Today says the card's own "creators in 3 days" figure as "accounts posting, last 3 days".
    cands = [cand(1, worth=0.9, creators3=50, numbers=[num(2, ACCOUNTS)]),
             cand(2, worth=0.1, creators3=1, numbers=[num(6, ACCOUNTS)])]
    p = build_market_payload("ZA", D, cands, moments=[], boards=[], banners=[])
    assert [c["item_id"] for c in p["cards"]] == ["it_2", "it_1"]
    assert [n["value"] for c in p["cards"] for n in c["numbers"] if n["unit"] == ACCOUNTS] == [6, 2]


def test_a_card_without_an_accounts_figure_comes_after_those_with_one():
    cands = [cand(1, worth=0.9, numbers=[]), cand(2, worth=0.1, numbers=[num(1, ACCOUNTS)])]
    p = build_market_payload("ZA", D, cands, moments=[], boards=[], banners=[])
    assert [c["item_id"] for c in p["cards"]] == ["it_2", "it_1"]


def test_held_and_seasonal_items_keep_the_worth_order():
    held = decision("held_back", rule="G6", reason="Not in this market")
    cands = [cand(1, worth=0.9, decision=held, numbers=[num(1, ACCOUNTS)]),
             cand(2, worth=0.1, decision=held, numbers=[num(9, ACCOUNTS)]),
             cand(3, worth=0.8, decision=decision("moments"), numbers=[num(1, ACCOUNTS)]),
             cand(4, worth=0.2, decision=decision("moments"), numbers=[num(9, ACCOUNTS)])]
    p = build_market_payload("ZA", D, cands, moments=[], boards=[], banners=[])
    assert [i["item_id"] for i in p["held_back"]["items"]] == ["it_1", "it_2"]
    assert [m["item_ids"] for m in p["moments"]] == [["it_3"], ["it_4"]]


def test_the_brief_job_headlines_the_broadest_card():
    con = jobt.world(n=0, markets=("ZA",))
    jobt.add_item(con, "ZA", "za_thin", 0.53, creators3=2, posts3=3)
    jobt.add_item(con, "ZA", "za_broad", 0.39, creators3=6, posts3=15)
    jobt.add_item(con, "ZA", "za_mid", 0.38, creators3=5, posts3=11)
    p = jobt.payload(jobt.brief(con), "ZA")
    assert [c["item_id"] for c in p["cards"]] == ["za_broad", "za_mid", "za_thin"]
    assert p["headline"]["item_id"] == "za_broad"
