import copy
import json

import pytest

from core.api import lexicon, seedpath
from core.api.store import FixtureStore
from core.api.tests.test_lexicon import client as client


class WordsStore(FixtureStore):
    def __init__(self, mode="hidden"):
        super().__init__()
        self.mode, self.hidden_reads, self.post_reads = mode, 0, 0
        self.word_rows = [
            {"item_id": "public", "label": "amapiano", "canonical_key": "amapiano", "kind": "meme",
             "status": "active", "posts_window": 9, "posts_week": 6, "posts_prior_week": 3},
            {"item_id": "hidden_mention", "label": "@hiddenfixture", "canonical_key": "hiddenfixture",
             "kind": "meme", "status": "active", "posts_window": 4, "posts_week": 4, "posts_prior_week": 0},
            {"item_id": "hidden_tag", "label": "#hiddenfixture", "canonical_key": "hiddenfixture",
             "kind": "hashtag", "status": "active", "posts_window": 4, "posts_week": 4, "posts_prior_week": 0},
            {"item_id": "public_longer", "label": "hiddenfixtureother", "canonical_key": "hiddenfixtureother",
             "kind": "meme", "status": "active", "posts_window": 2, "posts_week": 2, "posts_prior_week": 0},
            {"item_id": "hidden_url", "label": "https://x.com/hiddenfixture", "canonical_key": "hiddenfixture",
             "kind": "meme", "status": "active", "posts_window": 2, "posts_week": 2, "posts_prior_week": 0},
        ]

    def hidden_people_rows(self):
        self.hidden_reads += 1
        if self.mode == "failed":
            raise RuntimeError("synthetic suppression read failure")
        if self.mode == "missing":
            return None
        return {"ids": set(), "people": [] if self.mode == "clear" else [("tiktok", "hiddenfixture")]}

    def first_ok_collect_date(self):
        return "2026-08-01"

    def lexicon_terms(self, *args):
        return copy.deepcopy(self.word_rows)

    def seed_path_posts(self, *args):
        self.post_reads += 1
        return [{"grain": "first", "platform": "tiktok", "day": "2026-09-20", "posts": 2}] + [
            {"grain": "post", "platform": "tiktok", "day": "2026-09-20", "post_id": f"synthetic_{n}",
             "text": "amapiano hiddenfixture dance hiddenfixtureother", "hashtags": ["hiddenfixture"],
             "item_ids": []} for n in range(2)]


def test_lexicon_filters_hidden_labels_and_seed_terms_with_one_current_read():
    store = WordsStore()
    body = lexicon.build_lexicon(store, "ZA")
    assert [r["item_id"] for r in body["terms"]] == ["public", "public_longer"]
    assert body["terms"][0]["seed_term"] == "amapiano"
    assert body["terms"][0]["posts"]["value"] == 9
    assert body["terms"][0]["change"]["percent"] == 100
    assert store.hidden_reads == 1
    assert store.word_rows[1]["label"] == "@hiddenfixture"


@pytest.mark.parametrize("mode", ["failed", "missing"])
def test_lexicon_unavailable_suppression_masks_mentions_and_profile_links_but_keeps_public_words(mode):
    store = WordsStore(mode)
    body = lexicon.build_lexicon(store, "ZA")
    assert {r["item_id"] for r in body["terms"]}.isdisjoint({"hidden_mention", "hidden_url"})
    assert next(r for r in body["terms"] if r["item_id"] == "public")["posts"]["value"] == 9
    assert store.hidden_reads == 1


def test_lexicon_nobody_hidden_preserves_existing_terms_and_counts():
    body = lexicon.build_lexicon(WordsStore("clear"), "ZA")
    assert len(body["terms"]) == 5
    assert next(r for r in body["terms"] if r["item_id"] == "hidden_mention")["seed_term"] == "@hiddenfixture"


def test_seed_path_filters_hidden_side_words_after_real_two_post_support():
    store = WordsStore()
    body = seedpath.build_seed_path(store, "amapiano", "ZA")
    assert body["matched_posts"]["value"] == 2
    assert [r["word"] for r in body["side_words"]] == ["dance", "hiddenfixtureother"]
    assert all(r["posts"]["value"] == 2 for r in body["side_words"])
    assert store.hidden_reads == 1


@pytest.mark.parametrize("mode", ["failed", "missing"])
def test_seed_path_unavailable_suppression_keeps_counts_without_unchecked_side_words(mode):
    store = WordsStore(mode)
    body = seedpath.build_seed_path(store, "amapiano", "ZA")
    assert body["keyword"] == "amapiano" and body["matched_posts"]["value"] == 2
    assert body["side_words"] == []
    assert any("hidden" in note for note in body["notes"])


@pytest.mark.parametrize("keyword", ["hiddenfixture", "@hiddenfixture", "#hiddenfixture"])
def test_seed_path_hidden_keyword_is_not_echoed_or_queried(keyword):
    store = WordsStore()
    body = seedpath.build_seed_path(store, keyword, "ZA")
    assert "hiddenfixture" not in json.dumps(body)
    assert body["matched_posts"] is None and body["side_words"] == []
    assert store.post_reads == 0


def test_lexicon_rechecks_suppression_between_requests():
    store = WordsStore("clear")
    assert len(lexicon.build_lexicon(store, "ZA")["terms"]) == 5
    store.mode = "hidden"
    assert len(lexicon.build_lexicon(store, "ZA")["terms"]) == 2
    assert store.hidden_reads == 2


def test_lexicon_all_hidden_terms_do_not_claim_nothing_was_recorded():
    store = WordsStore()
    store.word_rows = [r for r in store.word_rows if r["item_id"].startswith("hidden_")]
    body = lexicon.build_lexicon(store, "ZA")
    assert body["terms"] == []
    assert "privacy" in body["message"]
    assert "has not recorded" not in body["message"]


def test_lexical_routes_return_privacy_projected_output(client, monkeypatch):
    from core.api import store as store_mod
    from core.api.tests.test_lexicon import PASS

    monkeypatch.setattr(store_mod, "get_store", lambda: WordsStore())
    response = client.get("/api/lexicon?market=ZA", headers={"X-Passcode": PASS})
    assert response.status_code == 200
    assert [r["item_id"] for r in response.json()["terms"]] == ["public", "public_longer"]
    response = client.get("/api/seed-path", params={"market": "ZA", "keyword": "@hiddenfixture"},
                          headers={"X-Passcode": PASS})
    assert response.status_code == 200
    assert "hiddenfixture" not in response.text


@pytest.mark.parametrize("keyword", ["hiddenfixture", "@hiddenfixture"])
def test_seed_path_route_does_not_prefetch_hidden_keyword(client, monkeypatch, keyword):
    from core.api import store as store_mod
    from core.api.tests.test_lexicon import PASS

    store = WordsStore()
    monkeypatch.setattr(store_mod, "get_store", lambda: store)
    response = client.get("/api/seed-path", params={"market": "ZA", "keyword": keyword},
                          headers={"X-Passcode": PASS})
    assert response.status_code == 200
    assert "hiddenfixture" not in response.text
    assert store.post_reads == 0


@pytest.mark.parametrize("mode", ["hidden", "failed", "missing"])
def test_seed_path_route_keeps_public_counts_and_reports_privacy_unavailability(client, monkeypatch, mode):
    from core.api import store as store_mod
    from core.api.tests.test_lexicon import PASS

    store = WordsStore(mode)
    monkeypatch.setattr(store_mod, "get_store", lambda: store)
    response = client.get("/api/seed-path", params={"market": "ZA", "keyword": "amapiano"},
                          headers={"X-Passcode": PASS})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok" and body["matched_posts"]["value"] == 2
    assert store.post_reads == 1
    if mode != "hidden":
        assert body["side_words"] == []
        assert any("unavailable" in note for note in body["notes"])
        assert not any("No post" in note for note in body["notes"])
