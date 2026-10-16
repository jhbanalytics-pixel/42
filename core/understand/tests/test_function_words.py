"""Function-word keywords ("they", "ni hii") name no topic: fit_topics drops them, and stored ones never give two
clusters a keyword vote. Held topics on 16 October 2026 were titled "ni, walai, hii" and "psychosis, brain, they"."""
from datetime import timedelta

import numpy as np
import pytest

from core.understand import cluster
from core.understand.tests.test_cluster import DAY, a_cluster, an_item, at


class Model:
    def __init__(self, words):
        self.words = words

    def fit_transform(self, docs, embeddings):
        return [0] * len(docs), None

    def get_topic(self, topic):
        return self.words


@pytest.mark.parametrize("word, kept", [
    ("they", False), ("ni hii", False), ("let them", True), ("birthday to", True), ("happy birthday", True),
    ("na", False), ("wahala", True), ("abeg", True), ("die", False), ("braai", True), ("", False), ("  ", False),
])
def test_is_content_word(word, kept):
    assert cluster.is_content_word(word) is kept


def test_function_word_list_is_lower_case_and_free_of_slang():
    words = cluster.function_words()
    assert all(w == w.lower() and " " not in w for w in words)
    assert not {"wahala", "abeg", "omo", "amapiano", "sheng", "walai"} & words


def test_fit_topics_drops_function_word_keywords(monkeypatch):
    import bertopic

    scored = [("ni", 0.5), ("walai", 0.4), ("hii", 0.3), ("ni hii", 0.2), ("curtains", 0.1)]
    monkeypatch.setattr(bertopic, "BERTopic", lambda **kwargs: Model(scored))
    _, _, keywords = cluster.fit_topics(["x"] * 30, np.ones((30, 768), dtype=np.float32))
    assert keywords[0] == ["walai", "curtains"]
    assert cluster.label_candidates(keywords[0], [], []) == ["walai, curtains"]


def test_shared_function_words_are_not_a_keyword_vote():
    c = a_cluster("c1", at(1.0), keywords=["they", "ni", "hii", "psychosis"], creators=["cr_1"])
    item = an_item("unrelated", at(0.3), DAY - timedelta(days=3), keywords=["they", "ni", "hii", "curtains"],
                   creators=["cr_1"])
    [decision] = cluster.assign([c], [item], DAY)
    assert "keywords" not in decision["votes"]
    assert (decision["kind"], decision["item_id"]) == ("new", None)


def test_shared_content_keywords_still_vote_beside_function_words():
    c = a_cluster("c1", at(1.0), keywords=["they", "amapiano", "dance"], creators=["cr_1"])
    item = an_item("same", at(0.3), DAY - timedelta(days=3), keywords=["they", "amapiano", "dance"], creators=["cr_1"])
    [decision] = cluster.assign([c], [item], DAY)
    assert (decision["kind"], decision["item_id"]) == ("match", "same")
