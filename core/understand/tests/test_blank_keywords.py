"""BERTopic pads a topic with fewer than ten scored words with ("", 1e-05). Those blanks are not keywords: before
16 October 2026 fit_topics kept them, so caption-less clusters were labelled ", , " and any two of them shared the ""
keyword, which voted unrelated clusters together."""
from datetime import timedelta

import numpy as np

from core.understand import cluster
from core.understand.tests.test_cluster import DAY, a_cluster, an_item, at


class PaddedModel:
    """The part of a fitted BERTopic model fit_topics reads, with BERTopic's own padding of short topics."""

    def __init__(self, words):
        self.words = words

    def fit_transform(self, docs, embeddings):
        return [0] * len(docs), None

    def get_topic(self, topic):
        return self.words


def test_fit_topics_drops_bertopic_padding(monkeypatch):
    import bertopic

    padded = [("lol", 0.4), ("lol lol", 0.2)] + [("", 1e-05)] * 8
    monkeypatch.setattr(bertopic, "BERTopic", lambda **kwargs: PaddedModel(padded))
    _, _, keywords = cluster.fit_topics(["lol"] * 30, np.ones((30, 768), dtype=np.float32))
    assert keywords[0] == ["lol", "lol lol"]


def test_blank_keywords_never_make_a_label():
    assert cluster.label_candidates(["", "", ""], ["amapiano"], []) == ["#amapiano"]
    assert cluster.label_candidates(["lol", "", ""], [], []) == ["lol"]
    assert cluster.clean_keywords(["", " ", "derby"]) == ["derby"]


def test_shared_blank_keywords_are_not_a_keyword_vote():
    c = a_cluster("c1", at(1.0), keywords=[""] * 10, creators=["cr_1"])
    item = an_item("unrelated", at(0.0), DAY - timedelta(days=40), keywords=[""] * 10, creators=["cr_1"])
    [decision] = cluster.assign([c], [item], DAY)
    assert "keywords" not in decision["votes"]
    assert (decision["kind"], decision["item_id"]) == ("new", None)


def test_real_shared_keywords_still_vote():
    words = [f"w{i}" for i in range(10)]
    c = a_cluster("c1", at(1.0), keywords=words + [""], creators=["cr_1"])
    item = an_item("same", at(0.5), DAY - timedelta(days=40), keywords=words + [""], creators=["cr_1"])
    [decision] = cluster.assign([c], [item], DAY)
    assert decision["votes"][:2] == ["keywords", "creator"]
