"""Client skins (core/api/contract.md section 15.1): words, accounts, the narrowed Today, the report plan, masking."""
import copy
import json
import sys
import types
from pathlib import Path

import pytest

from core.api import skins

NOT_ACCEPTED = "Not accepted (rule 1, rule 2 or mixed script)"
NOT_APPROVED = "Only approved organisation accounts"
ACCOUNTS = {
    "bsa": [{"platform": "x", "handle": "@BrandSA", "org": "Brand South Africa", "role": "client"},
            {"platform": "instagram", "handle": "brandsouthafrica", "org": "Brand South Africa", "role": "client"},
            {"platform": "x", "handle": "@RivalOrg", "org": "Rival Org", "role": "competitor"}],
    "other": [{"platform": "x", "handle": "@OtherOrg", "org": "Other Org", "role": "client"}],
}


def body(**change):
    out = {"skin_key": "bsa", "name": "Brand South Africa", "markets": ["ZA", "NG"],
           "terms": ["taxi fares", "Heritage Day"], "hashtags": ["#fixture_za_step", "owambe"],
           "accounts": [{"platform": "x", "handle": "@brandsa"}], "watch_ids": ["w_0123456789ab"],
           "template": "weekly_report"}
    out.update(change)
    return out


@pytest.fixture
def word_check(monkeypatch):
    """L1's core.collect.gdelt stand-in: blocks one stand-in word and records what it was asked."""
    seen = []

    def blocked(text):
        seen.append(text)
        return "blockedword" in str(text).casefold()

    monkeypatch.setitem(sys.modules, "core.collect.gdelt", types.SimpleNamespace(blocked=blocked))
    return seen


# The approved accounts file.


def test_the_shipped_accounts_file_is_an_empty_mapping_with_a_comment():
    path = Path(skins.__file__).with_name("skin_accounts.yaml")
    text = path.read_text(encoding="utf-8")
    assert text.lstrip().startswith("#") and "Albert" in text
    assert skins.load_accounts(path) == {}
    assert skins.load_accounts() == {}


def test_load_accounts_reads_each_skin_key(tmp_path):
    path = tmp_path / "accounts.yaml"
    path.write_text("# from Albert\nbsa:\n  - {platform: x, handle: '@BrandSA', org: Brand South Africa, "
                    "role: client}\n", encoding="utf-8")
    assert skins.load_accounts(path) == {"bsa": [{"platform": "x", "handle": "@BrandSA",
                                                  "org": "Brand South Africa", "role": "client"}]}


# Rule 1, rule 2 and mixed script.


def test_clean_words_pass_and_every_value_goes_through_the_rule_1_check(word_check):
    assert skins.check_words(["Brand South Africa", "taxi fares", "#owambe"]) is None
    assert word_check == ["Brand South Africa", "taxi fares", "#owambe"]


def test_a_rule_1_word_is_refused_without_naming_it(word_check):
    assert skins.check_words(["fine", "some BlockedWord here"]) == NOT_ACCEPTED


@pytest.mark.parametrize("value", [
    "Google Trends", "googletrends", "trends google", "TRENDS-GOOGLE", "g.o.o.g.l.e t.r.e.n.d.s",
    "see google - trends", "trends.google.com explore", "google_trends", "TRENDS__GOOGLE",
    "\uff27\uff4f\uff4f\uff47\uff4c\uff45 \uff34\uff52\uff45\uff4e\uff44\uff53", "google\u2010trends",
    "google/trends", "google+trends", "google\u2013trends", "google\u2014trends", "google\u200btrends",
    "google'trends", "google,trends", "google:trends",
])
def test_rule_2_refuses_the_phrase_with_spaces_hyphens_and_dots_removed(word_check, value):
    assert skins.check_words(["fine", value]) == NOT_ACCEPTED


@pytest.mark.parametrize("value", ["google", "trends", "google search", "trending now"])
def test_rule_2_leaves_other_words_alone(word_check, value):
    assert skins.check_words([value]) is None


def test_the_word_check_fails_closed_when_l1_check_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "core.collect.gdelt", None)
    with pytest.raises(skins.Unavailable) as caught:
        skins.check_words(["Brand South Africa"])
    assert str(caught.value) == "The word check is not available yet"


# Field limits and the approved list.


def test_a_good_body_is_cleaned_and_accounts_come_from_the_list():
    clean, why = skins.validate(body(name="  Brand South Africa  ", terms=["taxi fares", " taxi fares "]), ACCOUNTS)
    assert why is None
    assert clean == {"skin_key": "bsa", "name": "Brand South Africa", "markets": ["ZA", "NG"],
                     "terms": ["taxi fares"], "hashtags": ["#fixture_za_step", "owambe"],
                     "accounts": [{"platform": "x", "handle": "@BrandSA", "org": "Brand South Africa",
                                   "role": "client"}],
                     "watch_ids": ["w_0123456789ab"], "template": "weekly_report"}


def test_optional_lists_default_to_empty():
    clean, why = skins.validate({"skin_key": "bsa", "name": "Brand South Africa", "markets": ["KE"],
                                 "template": "weekly_report"}, ACCOUNTS)
    assert why is None
    assert (clean["terms"], clean["hashtags"], clean["accounts"], clean["watch_ids"]) == ([], [], [], [])


@pytest.mark.parametrize("change", [
    {"skin_key": ""}, {"skin_key": None}, {"skin_key": "BSA key!"}, {"skin_key": "x" * 41},
    {"name": "ab"}, {"name": "x" * 81}, {"name": None}, {"name": 5},
    {"markets": []}, {"markets": ["GH"]}, {"markets": "ZA"}, {"markets": ["ZA", "all"]}, {"markets": None},
    {"terms": ["x"]}, {"terms": ["x" * 61]}, {"terms": [f"term {n}" for n in range(31)]},
    {"terms": "taxi"}, {"terms": [5]},
    {"hashtags": ["#"]}, {"hashtags": ["#" + "x" * 60]}, {"hashtags": [f"#tag{n}" for n in range(31)]},
    {"hashtags": "#x"},
    {"accounts": "x"}, {"accounts": [{"platform": "x"}]}, {"accounts": ["@BrandSA"]},
    {"watch_ids": "w_1"}, {"watch_ids": [5]}, {"watch_ids": ["bad id!"]}, {"watch_ids": ["w_1"] * 51},
    {"template": "daily_digest"}, {"template": None},
])
def test_bad_fields_give_a_reason(change):
    clean, why = skins.validate(body(**change), ACCOUNTS)
    assert clean is None and isinstance(why, str) and why


@pytest.mark.parametrize("account", [
    {"platform": "x", "handle": "@someone_private"},
    {"platform": "tiktok", "handle": "@BrandSA"},      # on the list for X only
    {"platform": "x", "handle": "@OtherOrg"},          # on the list for another skin
    {"platform": "x", "handle": ""},
])
def test_an_account_not_on_the_list_for_this_skin_is_refused(account):
    assert skins.validate(body(accounts=[account]), ACCOUNTS) == (None, NOT_APPROVED)


def test_no_account_is_approved_for_a_skin_key_the_list_does_not_hold():
    assert skins.validate(body(skin_key="newclient"), ACCOUNTS) == (None, NOT_APPROVED)
    clean, why = skins.validate(body(skin_key="newclient", accounts=[]), ACCOUNTS)
    assert why is None and clean["accounts"] == []


def test_accounts_match_without_the_at_sign_or_case_and_x_as_twitter():
    clean, _ = skins.validate(body(accounts=[{"platform": "twitter", "handle": "RIVALORG"},
                                             {"platform": "instagram", "handle": "@BrandSouthAfrica"},
                                             {"platform": "x", "handle": "@rivalorg"}]), ACCOUNTS)
    assert [a["handle"] for a in clean["accounts"]] == ["@RivalOrg", "brandsouthafrica"]
    assert [a["role"] for a in clean["accounts"]] == ["competitor", "client"]


# Today narrowed to the skin.


def card(item_id, title, market, **extra):
    return {"item_id": item_id, "title": title, "market": market, "state": "rising", "count_line": "31 creators",
            "evidence": [{"id": f"ev_{item_id}"}], **extra}


def market(code, cards=(), more=(), held=(), dropped=()):
    return {"market": code, "label": code, "status": "published", "banners": [], "cards": list(cards),
            "more": list(more),
            "dropped": {"first_morning": False, "text": "", "items": list(dropped)},
            "held_back": {"count": len(held), "text": f"{len(held)} held back", "items": list(held)},
            "moments": [], "boards": [], "coverage": {}}


def today_resp():
    return {
        "date": "2026-09-30", "status": "published", "heading": "Taking off, 30 September 2026",
        "headline": {"text": "Big mover", "market": "ZA", "item_id": "za_step", "claim_ids": ["c1"]},
        "markets": [
            market("ZA",
                   cards=[card("za_step", "#Fixture_ZA_Step", "ZA"), card("za_taxi", "fixture za TAXI   fares", "ZA"),
                          card("za_other", "#something_else", "ZA"),
                          card("za_tagged", "A dance", "ZA", hashtags=["#owambe"])],
                   more=[card("za_taxis", "taxi faresharing", "ZA"), card("za_label", "Untitled", "ZA",
                                                                        label="Heritage Day braai")],
                   held=[{"item_id": "za_held", "title": "#fixture_za_step_two", "reason": "too_few_creators",
                          "reason_text": "Too few creators"},
                         {"item_id": "za_held2", "title": "Heritage day", "reason": "paid_led",
                          "reason_text": "Paid-led", "rule": "G5", "evidence": [{"id": "ev_h"}]}],
                   dropped=[{"item_id": "za_gone", "title": "#owambe", "reason": "faded", "reason_text": "Faded"},
                            {"item_id": "za_gone2", "title": "#other", "reason": "faded", "reason_text": "Faded"}]),
            market("NG", cards=[card("ng_owambe", "#owambe", "NG")]),
            market("KE", cards=[card("ke_taxi", "taxi fares", "KE")]),
        ],
    }


SKIN = {"skin_id": "sk_0123456789ab", "skin_key": "bsa", "name": "Brand South Africa", "markets": ["ZA", "NG"],
        "terms": ["taxi fares", "Heritage Day"], "hashtags": ["#fixture_za_step", "owambe"], "accounts": [],
        "watch_ids": ["w_a"], "template": "weekly_report", "status": "active"}


def test_today_keeps_the_skin_markets_and_the_cards_that_match_a_term_or_hashtag():
    resp = today_resp()
    out = skins.narrow_today(resp, SKIN)
    assert [m["market"] for m in out["markets"]] == ["ZA", "NG"]
    za, ng = out["markets"]
    assert [c["item_id"] for c in za["cards"]] == ["za_step", "za_taxi", "za_tagged"]
    assert [c["item_id"] for c in za["more"]] == ["za_label"]
    assert [c["item_id"] for c in ng["cards"]] == ["ng_owambe"]
    # Cards keep their state, count_line and evidence as they were.
    assert za["cards"][0] == today_resp()["markets"][0]["cards"][0]
    assert resp == today_resp()  # the input is not changed


def test_held_back_items_that_match_stay_with_their_reason():
    za = skins.narrow_today(today_resp(), SKIN)["markets"][0]
    assert [h["item_id"] for h in za["held_back"]["items"]] == ["za_held2"]
    assert za["held_back"]["count"] == 1
    assert za["held_back"]["items"][0]["reason_text"] == "Paid-led"
    assert [d["item_id"] for d in za["dropped"]["items"]] == ["za_gone"]


def test_nothing_is_dropped_silently():
    out = skins.narrow_today(today_resp(), SKIN)
    za, ng = out["markets"]
    assert (za["skin_note"]["kept"], za["skin_note"]["left_out"]) == (6, 4)
    assert "6" in za["skin_note"]["text"] and "4" in za["skin_note"]["text"]
    assert (ng["skin_note"]["kept"], ng["skin_note"]["left_out"]) == (1, 0)
    assert out["skin"]["left_out_markets"] == ["KE"]
    assert "Kenya" in out["skin"]["text"]


def test_breaking_lines_are_narrowed_to_the_skin_terms_too():
    resp = today_resp()
    resp["markets"][0]["breaking"] = [{"item_id": "za_b1", "market": "ZA", "title": "#fixture_za_step"},
                                      {"item_id": "za_b2", "market": "ZA", "title": "#unrelated"}]
    za = skins.narrow_today(resp, SKIN)["markets"][0]
    assert [b["item_id"] for b in za["breaking"]] == ["za_b1"]
    assert (za["skin_note"]["kept"], za["skin_note"]["left_out"]) == (6, 4)


def test_the_headline_stays_only_when_its_card_is_kept():
    assert skins.narrow_today(today_resp(), SKIN)["headline"]["item_id"] == "za_step"
    assert skins.narrow_today(today_resp(), {**SKIN, "hashtags": ["owambe"]})["headline"] is None
    assert skins.narrow_today(today_resp(), {**SKIN, "markets": ["NG"]})["headline"] is None


def test_a_skin_with_no_terms_or_hashtags_keeps_no_cards_and_says_so():
    out = skins.narrow_today(today_resp(), {**SKIN, "terms": [], "hashtags": []})
    za = out["markets"][0]
    assert za["cards"] == za["more"] == za["held_back"]["items"] == []
    assert za["skin_note"]["kept"] == 0 and za["skin_note"]["left_out"] == 10


def test_alerts_of_the_skin_watches_are_added():
    alerts = {"date": "2026-09-30", "run_id": "r_1",
              "alerts": [{"watch_id": "w_a", "item_id": "x"}, {"watch_id": "w_other", "item_id": "y"}],
              "waiting": [{"watch_id": "w_a", "waiting": "Waiting"}, {"watch_id": "w_other", "waiting": "W"}]}
    out = skins.narrow_today(today_resp(), SKIN, alerts)
    assert out["alerts"]["alerts"] == [{"watch_id": "w_a", "item_id": "x"}]
    assert out["alerts"]["waiting"] == [{"watch_id": "w_a", "waiting": "Waiting"}]
    assert out["alerts"]["date"] == "2026-09-30"
    assert skins.narrow_today(today_resp(), SKIN)["alerts"] is None


# The weekly report plan.


def test_report_plan_is_an_investigation_body_scoped_to_the_skin():
    from core.api import agent_app

    skin = {**SKIN, "accounts": [{"platform": "x", "handle": "@BrandSA", "org": "Brand South Africa",
                                  "role": "client"}]}
    plan = skins.report_plan(skin)
    assert set(plan) == {"question", "market", "angles", "skin_id"}
    assert plan["skin_id"] == "sk_0123456789ab"
    assert plan["market"] is None
    assert "Brand South Africa" in plan["question"]
    assert "South Africa" in plan["question"] and "Nigeria" in plan["question"]
    joined = " ".join(plan["angles"])
    for word in ("taxi fares", "Heritage Day", "#fixture_za_step", "owambe", "@BrandSA"):
        assert word in joined
    assert agent_app.investigation_body(plan) is None
    assert skins.report_plan({**skin, "markets": ["KE"]})["market"] == "KE"


def test_report_plan_fits_the_angle_limits_and_says_what_did_not_fit():
    from core.api import agent_app
    from core.api.investigations import MAX_SUB_QUESTIONS

    skin = {**SKIN, "terms": [f"term {n:02d} " + "x" * 50 for n in range(30)],
            "hashtags": [f"#tag{n:02d}" + "y" * 50 for n in range(30)]}
    plan = skins.report_plan(skin)
    assert agent_app.investigation_body(plan) is None
    assert len(plan["angles"]) <= MAX_SUB_QUESTIONS
    assert all(len(a) <= agent_app.MAX_VALUE for a in plan["angles"])
    assert "more" in plan["angles"][-1]


# People in a skin report.


# Profile and post URL forms. otherperson is no evidence author, so only the URL patterns can mask it; privperson
# is a sub-tier author of the record, so its known handle is masked wherever it appears, in any host form.
URL_FORMS = ("web.facebook.com/{h}/posts/1", "mbasic.facebook.com/{h}", "fb.com/{h}", "youtube.com/c/{h}",
             "youtube.com/user/{h}", "reddit.com/user/{h}", "u/{h}", "linkedin.com/in/{h}",
             "vxtwitter.com/{h}/status/1", "https://www.threads.net/@{h}", "reddit.com/u/{h}")
OTHER_URLS = ", ".join(form.format(h="otherperson") for form in URL_FORMS)
PRIV_URLS = ", ".join(form.format(h="privperson") for form in URL_FORMS)


def skin_record():
    """A finished answer with an approved organisation, a page-tier author and a sub-tier author on X."""
    evidence = [
        {"id": "x_org", "platform": "x", "handle": "@BrandSA", "url": "https://x.com/BrandSA/status/1",
         "text": "Proud of the launch with @Pal_Of_Micro", "author_name": "Brand South Africa"},
        {"id": "tt_macro", "platform": "tiktok", "handle": "@fixture_ng_macro",
         "url": "https://www.tiktok.com/@fixture_ng_macro/video/2", "text": "Big day, thanks @BrandSA"},
        {"id": "x_micro", "platform": "x", "handle": "@fixture_ng_micro", "author_name": "Micro Person",
         "creator_id": "cr_micro", "url": "https://x.com/fixture_ng_micro/status/3",
         "text": "Loved the launch with @pal_of_micro and @BrandSA, see you soon",
         "transcript_span": {"start_s": 1, "end_s": 2, "text": "shout out @pal_of_micro"}},
        {"id": "x_nobody", "platform": "x", "url": "https://x.com/unknown_person/status/4",
         "text": "no author; see " + OTHER_URLS},
        {"id": "x_priv", "platform": "x", "handle": "@PrivPerson", "author_name": "Priv Person",
         "url": "https://x.com/PrivPerson/status/7", "text": "privperson says the launch was fine, fb.com/privperson"},
    ]
    claims = [{"id": "c1", "text": "People tag @pal_of_micro and @BrandSA together.", "label": "corroborated",
               "kind": "proposal", "evidence_ids": ["x_micro", "x_org", "x_priv"],
               "quotes": [{"evidence_id": "x_micro", "text": "with @pal_of_micro and @BrandSA"}],
               "basis": "Posts by @pal_of_micro", "falsifier": "If @pal_of_micro stops tagging @BrandSA"},
              {"id": "c2", "text": "A creator thanked @BrandSA.", "label": "observed", "kind": "observation",
               "evidence_ids": ["tt_macro"], "quotes": [{"evidence_id": "tt_macro", "text": "thanks @BrandSA"}]}]
    answer = {"status": "complete", "as_of": "2026-09-29", "short_answer": "Mostly @pal_of_micro and friends.",
              "claims": claims, "evidence": evidence,
              "so_what": [{"text": "Reply to @pal_of_micro from @BrandSA"}],
              "watch_next": [{"text": "Watch @my-channel.tv next week", "forecast": True}],
              "context": "See x.com/pal_of_micro, https://www.instagram.com/pal.of.micro/ and x.com/BrandSA",
              "gaps": [{"what": "Private posts by @pal_of_micro",
                        "searched": "https://twitter.com/pal_of_micro/status/9 and tiktok.com/@my-channel.tv",
                        "why": "@pal_of_micro keeps a closed profile; youtube.com/@pal_of_micro and "
                               "facebook.com/pal.of.micro are closed too"},
                       {"what": "privperson says the posts moved", "searched": PRIV_URLS,
                        "why": "Only forum.example.org/members/privperson and PrivPerson's own list remain"},
                       {"what": "Other places", "searched": OTHER_URLS, "why": "not collected"}]}
    return {"ask_id": "a_1", "status": "complete", "steps": [{"seq": 1, "text": "Reading @pal_of_micro posts"}],
            "answer": answer}


# Every leak the skin fixture carries: sub-tier handles, names and ids, and the handles inside bare URLs.
LEAKS = ("fixture_ng_micro", "micro person", "cr_micro", "pal_of_micro", "pal.of.micro", "my-channel", "channel.tv",
         "unknown_person", "privperson", "priv person", "otherperson")


def leaks(text):
    lowered = text.lower()
    return [leak for leak in LEAKS if leak in lowered]


APPROVED = ["x:brandsa", "instagram:brandsouthafrica"]
ALLOWED = ["tiktok:fixture_ng_macro"]


def test_unapproved_sub_tier_authors_lose_handle_name_and_url():
    out = skins.mask_people(skin_record(), APPROVED, ALLOWED)
    ev = {e["id"]: e for e in out["answer"]["evidence"]}
    for eid in ("x_micro", "x_nobody"):
        for key in ("handle", "author_name", "creator_id", "url"):
            assert key not in ev[eid]
    assert ev["x_org"]["handle"] == "@BrandSA" and ev["x_org"]["url"] == "https://x.com/BrandSA/status/1"
    assert ev["tt_macro"]["handle"] == "@fixture_ng_macro"
    assert leaks(json.dumps(out)) == []


def test_unapproved_at_handles_are_masked_and_approved_ones_stay():
    out = skins.mask_people(skin_record(), APPROVED, ALLOWED)
    ev = {e["id"]: e for e in out["answer"]["evidence"]}
    assert ev["x_micro"]["text"] == f"Loved the launch with {skins.MASK} and @BrandSA, see you soon"
    assert ev["x_micro"]["transcript_span"]["text"] == f"shout out {skins.MASK}"
    assert ev["x_org"]["text"] == f"Proud of the launch with {skins.MASK}"
    claim = out["answer"]["claims"][0]
    assert claim["quotes"][0]["text"] == f"with {skins.MASK} and @BrandSA"
    assert claim["text"] == f"People tag {skins.MASK} and @BrandSA together."
    assert out["answer"]["short_answer"] == f"Mostly {skins.MASK} and friends."
    assert out["steps"][0]["text"] == f"Reading {skins.MASK} posts"
    assert skins.MASK.startswith("@")


def test_masking_works_on_a_copy_and_leaves_the_stored_quotes_verbatim():
    record = skin_record()
    kept = copy.deepcopy(record)
    skins.mask_people(record, APPROVED, ALLOWED)
    assert record == kept


def test_masking_reads_a_dossier_body_as_well_as_an_ask_record():
    record = skin_record()
    body = {"summary": record["answer"]["short_answer"], "claims": record["answer"]["claims"],
            "evidence": record["answer"]["evidence"]}
    out = skins.mask_people(body, APPROVED, ALLOWED)
    assert leaks(json.dumps(out)) == []


class FakeStore:
    def __init__(self, creators, suppressed=None):
        self.rows, self.suppressed = creators, suppressed

    def creators_by_handle(self, keys):
        from core.api.store import creator_key
        return [c for c in self.rows if creator_key(c["platform"], c["handle"]) in set(keys)]

    def creators_by_id(self, ids):
        return [c for c in self.rows if c["creator_id"] in ids]

    def suppressed_creators(self):
        return self.suppressed


def test_nameable_authors_are_at_page_tier_now_and_not_suppressed():
    creators = [{"creator_id": "a", "platform": "tiktok", "handle": "@fixture_ng_macro", "tier": "macro"},
                {"creator_id": "b", "platform": "twitter", "handle": "fixture_ng_micro", "tier": "micro"},
                {"creator_id": "c", "platform": "x", "handle": "@hidden_star", "tier": "mega"}]
    evidence = [{"platform": "tiktok", "handle": "@Fixture_NG_Macro"}, {"platform": "x", "handle": "@fixture_ng_micro"},
                {"platform": "x", "handle": "@hidden_star"}, {"platform": "x", "handle": "@not_in_42"},
                {"platform": "x"}]
    assert skins.nameable_authors(FakeStore(creators, {"c"}), evidence) == {"tiktok:fixture_ng_macro"}
    # No suppression list: no one is named (fail closed, L1 29 September).
    assert skins.nameable_authors(FakeStore(creators, None), evidence) == set()
    assert skins.nameable_authors(FakeStore(creators), []) == set()


def test_mixed_script_is_refused_here_even_when_l1_check_passes_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "core.collect.gdelt", types.SimpleNamespace(blocked=lambda text: False))
    assert skins.check_words(["Brand S\u043euth Africa"]) == NOT_ACCEPTED  # a Cyrillic o among Latin letters
    assert skins.check_words(["\u0395\u03bb\u03bb\u03ac\u03b4\u03b1"]) is None  # one script, Greek
    assert skins.check_words(["Caf\u00e9 culture 2026 #braai"]) is None


def test_every_model_written_field_is_masked():
    answer = skins.mask_people(skin_record(), APPROVED, ALLOWED)["answer"]
    mask = skins.MASK
    claim = answer["claims"][0]
    assert claim["basis"] == f"Posts by {mask}"
    assert claim["falsifier"] == f"If {mask} stops tagging @BrandSA"
    assert answer["so_what"][0]["text"] == f"Reply to {mask} from @BrandSA"
    assert answer["watch_next"][0]["text"] == f"Watch {mask} next week"
    assert answer["context"] == "See x.com/***, https://www.instagram.com/***/ and x.com/BrandSA"
    gap = answer["gaps"][0]
    assert gap["what"] == f"Private posts by {mask}"
    assert gap["searched"] == "https://twitter.com/***/status/9 and tiktok.com/@***"
    assert gap["why"] == f"{mask} keeps a closed profile; youtube.com/@*** and facebook.com/*** are closed too"


def test_a_named_author_keeps_handle_and_url_but_other_text_is_masked():
    ev = {e["id"]: e for e in skins.mask_people(skin_record(), APPROVED, ALLOWED)["answer"]["evidence"]}
    assert ev["tt_macro"]["url"] == "https://www.tiktok.com/@fixture_ng_macro/video/2"
    assert ev["tt_macro"]["handle"] == "@fixture_ng_macro"
    assert ev["x_org"]["author_name"] == "Brand South Africa"
    out = skins.mask_people({"summary": "Also @fixture_ng_macro and tiktok.com/@fixture_ng_macro"}, APPROVED, ALLOWED)
    assert out["summary"] == f"Also {skins.MASK} and tiktok.com/@***"


def test_a_whole_handle_with_hyphens_and_dots_is_masked():
    out = skins.mask_people({"summary": "by @my-channel, @a.b. and @x_y-z.w!"}, [], [])
    assert out["summary"] == f"by {skins.MASK}, {skins.MASK}. and {skins.MASK}!"


def test_mask_event_masks_a_live_event_and_keeps_a_named_author():
    record = skin_record()
    micro = next(e for e in record["answer"]["evidence"] if e["id"] == "x_micro")
    macro = next(e for e in record["answer"]["evidence"] if e["id"] == "tt_macro")
    out = skins.mask_event({"seq": 3, "evidence": micro}, APPROVED, ALLOWED)
    assert out["seq"] == 3 and leaks(json.dumps(out)) == [] and "url" not in out["evidence"]
    out = skins.mask_event({"seq": 4, "evidence": macro}, APPROVED, ALLOWED)
    assert out["evidence"]["url"] == macro["url"] and out["evidence"]["handle"] == "@fixture_ng_macro"
    out = skins.mask_event({"seq": 5, "claim": record["answer"]["claims"][0], "check": "verified"}, APPROVED, [])
    assert leaks(json.dumps(out)) == [] and out["check"] == "verified"


def test_still_nameable_drops_an_author_suppressed_or_below_page_tier_now():
    creators = [{"creator_id": "a", "platform": "tiktok", "handle": "@fixture_ng_macro", "tier": "macro"},
                {"creator_id": "b", "platform": "x", "handle": "@was_big", "tier": "micro"}]
    allowed = ["tiktok:fixture_ng_macro", "x:was_big"]
    assert skins.still_nameable(allowed, lambda: FakeStore(creators, set())) == ["tiktok:fixture_ng_macro"]
    assert skins.still_nameable(allowed, lambda: FakeStore(creators)) == []  # no suppression list, no one named
    assert skins.still_nameable(allowed, lambda: FakeStore(creators, {"a"})) == []

    def broken():
        raise RuntimeError("no store")

    assert skins.still_nameable(allowed, broken) == []
    assert skins.still_nameable([], broken) == []


MASKED_FORMS = ("web.facebook.com/***/posts/1, mbasic.facebook.com/***, fb.com/***, youtube.com/c/***, "
                "youtube.com/user/***, reddit.com/user/***, u/***, linkedin.com/in/***, vxtwitter.com/***/status/1, "
                "https://www.threads.net/@***, reddit.com/u/***")


def test_profile_and_post_urls_of_anyone_unapproved_are_masked_in_every_host_form():
    answer = skins.mask_people(skin_record(), APPROVED, ALLOWED)["answer"]
    assert answer["gaps"][2]["searched"] == MASKED_FORMS
    nobody = next(e for e in answer["evidence"] if e["id"] == "x_nobody")
    assert nobody["text"] == "no author; see " + MASKED_FORMS


def test_a_stripped_author_is_masked_by_their_known_handle_in_any_form():
    answer = skins.mask_people(skin_record(), APPROVED, ALLOWED)["answer"]
    gap = answer["gaps"][1]
    assert gap["what"] == "*** says the posts moved"
    assert gap["searched"] == MASKED_FORMS
    assert gap["why"] == "Only forum.example.org/members/*** and ***'s own list remain"
    priv = next(e for e in answer["evidence"] if e["id"] == "x_priv")
    assert priv["text"] == "*** says the launch was fine, fb.com/***"
    assert "handle" not in priv and "author_name" not in priv and "url" not in priv
    assert leaks(json.dumps(answer)) == []


def test_a_known_handle_is_masked_only_as_a_whole_word():
    out = skins.mask_people({"evidence": [{"platform": "x", "handle": "@ann"}],
                             "summary": "Ann, annual, @ann, ann2 and ann.b; banner, x.com/Ann."}, [], [])
    # A dot and a word after the handle is the handle inside a longer name (ann.bsky.social), so it is masked.
    assert out["summary"] == "***, annual, @***, ann2 and ***.b; banner, x.com/***."


def test_a_live_stream_remembers_the_handles_it_has_masked():
    record = skin_record()
    priv = next(e for e in record["answer"]["evidence"] if e["id"] == "x_priv")
    hidden = set()
    skins.mask_event({"seq": 1, "evidence": priv}, APPROVED, [], hidden)
    assert hidden == {"privperson", "priv person"}  # the handle, and the author name as a phrase
    out = skins.mask_event({"seq": 2, "claim": {"text": "As privperson put it"}}, APPROVED, [], hidden)
    assert out["claim"]["text"] == "As *** put it"


# Structure is never masked: ids, labels, kinds, platforms, markets, statuses and times stay as they are, whatever
# handle an author happens to have.


def structural_record(handle, platform="x"):
    return {"ask_id": "a_1", "status": "complete", "market": "ZA", "created_at": "2026-09-29T10:00:00+02:00",
            "answer": {"status": "complete", "as_of": "2026-09-29",
                       "claims": [{"id": "c1", "label": "observed", "kind": "observation", "text": "Seen.",
                                   "evidence_ids": ["e1"], "quotes": [{"evidence_id": "e1", "text": "seen"}],
                                   "numbers": [{"value": 3, "query_id": "q_1", "run_id": "r_1",
                                                "result_hash": "sha256:ab"}]}],
                       "evidence": [{"id": "e1", "platform": platform, "handle": handle, "market": "ZA",
                                     "posted_at": "2026-09-28T10:00:00+02:00", "text": "seen"},
                                    {"id": "e2", "platform": "tiktok", "handle": "@someone_else", "text": "hi"}]},
            "run": {"run_id": "r_1", "tier": "T3", "mode": "live"}, "steps": [{"seq": 1, "at": "x", "kind": "read"}]}


@pytest.mark.parametrize("handle, platform, field", [
    ("@za", "x", "market"), ("@observed", "x", "label"), ("@e1", "x", "evidence_ids"),
    ("@tiktok", "tiktok", "platform"),
])
def test_a_hidden_handle_never_blanks_a_structural_field(handle, platform, field):
    record = structural_record(handle, platform)
    out = skins.mask_people(record, [], [])
    answer, claim = out["answer"], out["answer"]["claims"][0]
    assert (out["market"], out["status"], out["ask_id"], out["created_at"]) == (
        "ZA", "complete", "a_1", "2026-09-29T10:00:00+02:00")
    assert (claim["id"], claim["label"], claim["kind"], claim["evidence_ids"]) == ("c1", "observed", "observation",
                                                                                     ["e1"])
    assert claim["quotes"][0]["evidence_id"] == "e1"
    assert claim["numbers"][0] == record["answer"]["claims"][0]["numbers"][0]
    assert [(e["id"], e["platform"]) for e in answer["evidence"]] == [("e1", platform), ("e2", "tiktok")]
    assert answer["evidence"][0]["market"] == "ZA" and answer["as_of"] == "2026-09-29"
    assert out["run"] == record["run"] and out["steps"] == record["steps"]
    assert "handle" not in answer["evidence"][0]


def test_short_links_are_masked_whole():
    out = skins.mask_people({"summary": "see https://t.co/AbC12, vm.tiktok.com/ZMabc/ and http://instagr.am/p/x). "
                                        "Also vt.tiktok.com/1 but not art.co/x or tiktok.com/music"}, [], [])
    assert out["summary"] == ("see [link], [link] and [link]). Also [link] but not art.co/x or tiktok.com/music")


def test_a_stripped_author_name_is_masked_as_a_whole_phrase():
    out = skins.mask_people({"evidence": [{"platform": "x", "handle": "@pp", "author_name": "Priv Person",
                                           "display_name": "Priv P.", "text": "by Priv Person"}],
                             "summary": "Priv Person and PRIV  PERSON wrote; Priv Personal and Priv P. too"}, [], [])
    assert out["summary"] == "*** and *** wrote; Priv Personal and *** too"
    assert out["evidence"][0]["text"] == "by ***"


def test_a_named_author_name_is_not_masked():
    out = skins.mask_people({"evidence": [{"platform": "x", "handle": "@BrandSA", "author_name": "Brand SA"}],
                             "summary": "Brand SA said"}, ["x:brandsa"], [])
    assert out["summary"] == "Brand SA said" and out["evidence"][0]["author_name"] == "Brand SA"


def test_a_known_handle_catches_a_bluesky_name_but_not_a_longer_handle():
    out = skins.mask_people({"evidence": [{"platform": "bluesky", "handle": "@privperson"}],
                             "summary": "bsky.app/profile/privperson.bsky.social and privperson2 and privperson-x"},
                            [], [])
    assert out["summary"] == "bsky.app/profile/***.bsky.social and privperson2 and privperson-x"


# Shapes are masked under structural keys too; only the bare known-word layer is skipped there.

PLANTED = ("privperson",)


def planted_record():
    return {"market": "ZA", "status": "complete",
            "steps": [{"seq": 1, "kind": "read", "text": "Reading", "label": "@privperson on x:privperson"}],
            "answer": {"status": "complete",
                       "claims": [{"id": "c1", "label": "observed", "kind": "observation", "text": "Seen.",
                                   "evidence_ids": ["e1"], "item_ids": ["tiktok:privperson", "item_1"],
                                   "creator_id": "x_privperson", "author_id": "x:privperson",
                                   "quotes": [{"evidence_id": "e1", "text": "seen"}]}],
                       "so_what": [{"label": "@privperson", "text": "Reply"}],
                       "evidence": [{"id": "e1", "platform": "x", "handle": "@za", "market": "ZA", "text": "seen"},
                                    {"id": "e2", "platform": "x", "handle": "@observed", "text": "hi"},
                                    {"id": "e3", "platform": "x", "handle": "@e1", "text": "hi"},
                                    {"id": "e4", "platform": "x", "handle": "@PrivPerson", "text": "hi"},
                                    {"id": "e5", "platform": "x", "handle": "@BrandSA", "text": "hi",
                                     "item_ids": ["x:privperson", "https://t.co/abc", "bit.ly/xyz"],
                                     "creator_id": "cr_brand"}]}}


def test_shapes_and_platform_handles_are_masked_under_structural_keys():
    out = skins.mask_people(planted_record(), ["x:brandsa"], [])
    answer, claim = out["answer"], out["answer"]["claims"][0]
    assert "privperson" not in json.dumps(out).lower()
    assert claim["item_ids"] == ["tiktok:***", "item_1"]
    assert claim["creator_id"] == "x_***" and claim["author_id"] == "x:***"
    assert answer["so_what"][0]["label"] == skins.MASK
    assert out["steps"][0]["label"] == f"{skins.MASK} on x:***"
    assert answer["evidence"][4]["item_ids"] == ["x:***", "[link]", "[link]"]
    assert answer["evidence"][4]["creator_id"] == "cr_brand"
    # The bare known-word layer stays off structure: hidden "za", "observed" and "e1" leave it intact.
    assert out["market"] == "ZA" and answer["evidence"][0]["market"] == "ZA"
    assert claim["label"] == "observed" and claim["evidence_ids"] == ["e1"]
    assert claim["quotes"][0]["evidence_id"] == "e1" and [e["id"] for e in answer["evidence"]][:3] == ["e1", "e2", "e3"]


def test_a_live_event_masks_shapes_under_structural_keys():
    hidden = set()
    skins.mask_event({"seq": 1, "evidence": {"id": "e4", "platform": "x", "handle": "@privperson"}}, [], [], hidden)
    out = skins.mask_event({"seq": 2, "kind": "read", "text": "x", "label": "@privperson",
                            "claim": {"id": "c1", "item_ids": ["tiktok:privperson"], "author_id": "x:privperson"}},
                           [], [], hidden)
    assert "privperson" not in json.dumps(out).lower()
    assert out["seq"] == 2 and out["claim"]["id"] == "c1"


def test_bit_ly_links_are_masked_whole():
    out = skins.mask_people({"summary": "see bit.ly/3abc and https://bit.ly/x."}, [], [])
    assert out["summary"] == "see [link] and [link]"


def test_a_hidden_host_word_never_damages_an_approved_link():
    out = skins.mask_people({"evidence": [{"platform": "x", "handle": "@tiktok"},
                                          {"platform": "tiktok", "handle": "@x"},
                                          {"platform": "x", "handle": "@youtube"}],
                             "summary": "https://tiktok.com/@brandza, www.tiktok.com/@brandza/video/1, x.com/BrandZA "
                                        "and youtube.com/@brandza; but @tiktok and x said so"},
                            ["tiktok:brandza", "x:brandza", "youtube:brandza"], [])
    assert out["summary"] == ("https://tiktok.com/@brandza, www.tiktok.com/@brandza/video/1, x.com/BrandZA "
                              "and youtube.com/@brandza; but @*** and *** said so")


def test_a_sub_tier_author_own_domain_is_masked():
    out = skins.mask_people({"evidence": [{"platform": "x", "handle": "@thandim"},
                                          {"platform": "x", "handle": "@privperson"},
                                          {"platform": "instagram", "handle": "priv.person"}],
                             "summary": "thandim.co.za, thandim.com, privperson.com, privperson.me, priv.person.co "
                                        "and fb.me/privperson"}, [], [])
    assert out["summary"] == "***.co.za, ***.com, ***.com, ***.me, ***.co and fb.me/***"


def test_fb_me_profile_links_are_masked_for_anyone_unapproved():
    out = skins.mask_people({"summary": "fb.me/otherperson and fb.me/BrandSA"}, ["facebook:brandsa"], [])
    assert out["summary"] == "fb.me/*** and fb.me/BrandSA"


@pytest.mark.parametrize("word", ["tiktok", "x", "com"])
def test_a_hidden_host_word_leaves_approved_links_intact(word):
    out = skins.mask_people({"evidence": [{"platform": "x", "handle": "@" + word}],
                             "summary": "tiktok.com/@brandza and x.com/brandza"}, ["tiktok:brandza", "x:brandza"], [])
    assert out["summary"] == "tiktok.com/@brandza and x.com/brandza"


def test_nameable_authors_name_no_one_when_the_suppression_list_is_missing():
    class Store:
        def creators_by_handle(self, keys):
            return [{"creator_id": "a", "platform": "tiktok", "handle": "@fixture_ng_macro", "tier": "macro"}]

        def creators_by_id(self, ids):
            return []

        def suppressed_creators(self):
            return None

    assert skins.nameable_authors(Store(), [{"platform": "tiktok", "handle": "@fixture_ng_macro"}]) == set()
