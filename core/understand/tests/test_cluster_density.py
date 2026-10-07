import copy
import sys
import types

import numpy as np
import pytest

from core.understand import cluster
from core.understand.tests.test_cluster import CORE, DAY, core_db, duck_execute


@pytest.fixture
def original_fit(monkeypatch):
    assignments = [0, 0, 0, -1]
    probabilities = [0.9, 0.0, 0.8, 0.0]

    class TopicModel:
        def __init__(self, **kwargs):
            self.updated = False

        def fit_transform(self, docs, embeddings):
            assert len(docs) == len(assignments) == len(embeddings)
            return list(assignments), list(probabilities)

        def reduce_outliers(self, docs, topics, **kwargs):
            return [0 if topic == -1 else topic for topic in topics]

        def update_topics(self, docs, **kwargs):
            self.updated = True

        def get_topic(self, topic):
            assert topic == 0
            return [("kota", 0.7), ("bakery" if self.updated else "dance", 0.3)]

    def constructor(**kwargs):
        return kwargs

    modules = {
        "bertopic": {"BERTopic": TopicModel},
        "bertopic.vectorizers": {"ClassTfidfTransformer": constructor},
        "hdbscan": {"HDBSCAN": constructor},
        "umap": {"UMAP": constructor},
    }
    for name, members in modules.items():
        module = types.ModuleType(name)
        module.__dict__.update(members)
        monkeypatch.setitem(sys.modules, name, module)
    return assignments, probabilities


def density_posts():
    return [
        {"post_id": "today-inlier", "text": "kota dance", "today": True, "embedding": [1.0, 0.0],
         "creator_id": "creator-a", "platform": "tiktok", "hashtags": ["#kota"], "sound_id": "sound-a"},
        {"post_id": "today-zero", "text": "kota dance", "today": True, "embedding": [0.0, 1.0],
         "creator_id": "creator-b", "platform": "tiktok", "hashtags": ["#kota"], "sound_id": "sound-b"},
        {"post_id": "older-inlier", "text": "kota dance", "today": False, "embedding": [8.0, 8.0],
         "creator_id": "creator-old", "platform": "tiktok", "hashtags": ["#older"], "sound_id": "sound-old"},
        {"post_id": "today-outlier", "text": "bakery promotion", "today": True, "embedding": [-1.0, -1.0],
         "creator_id": "creator-noise", "platform": "tiktok", "hashtags": ["#bakery"], "sound_id": "sound-noise"},
    ]


def test_original_density_assignments_probabilities_and_keywords_are_preserved(original_fit):
    posts = density_posts()
    topics, probabilities, keywords = cluster.fit_topics(
        [p["text"] for p in posts], np.asarray([p["embedding"] for p in posts]))
    assert topics == [0, 0, 0, -1]
    assert probabilities == [0.9, 0.0, 0.8, 0.0]
    assert keywords == {0: ["kota", "dance"]}
    [built] = cluster.build_clusters(posts, topics, probabilities, keywords, DAY, "za")
    assert built["members"] == [("today-inlier", 0.9), ("today-zero", 0.0)]
    assert built["centroid"] == [0.5, 0.5]
    assert built["keywords"] == ["kota", "dance"]
    assert built["creators"] == ["creator-a", "creator-b"]
    assert built["hashtags"] == ["kota"]
    assert built["sounds"] == ["sound-a", "sound-b"]


@pytest.mark.parametrize("case, fit_outliers, today_outliers, members", [
    ("mixed", 1, 1, 2),
    ("older_outlier", 2, 1, 2),
    ("all_outliers", 4, 3, 0),
])
def test_run_counts_distinguish_fitted_and_daily_outliers(original_fit, monkeypatch, case,
                                                       fit_outliers, today_outliers, members):
    assignments, probabilities = original_fit
    posts = density_posts()
    if case == "all_outliers":
        assignments[:] = [-1, -1, -1, -1]
        probabilities[:] = [0.0, 0.0, 0.0, 0.0]
    elif case == "older_outlier":
        assignments.append(-1)
        probabilities.append(0.0)
        posts.append({"post_id": "older-outlier", "text": "bakery", "today": False, "embedding": [-2.0, 0.0],
                      "creator_id": "creator-old-noise", "platform": "tiktok"})
    con = core_db()
    con.execute("SET threads = 1")
    duck = duck_execute(con)
    monkeypatch.setattr(cluster, "MIN_POSTS", 4)
    monkeypatch.setattr(cluster, "label_net", lambda execute, clusters, **kwargs:
                        (clusters, {"model_usd": 0.0, "booked_usd": 0.0}))

    def execute(text, params):
        if text == cluster.load("cluster_posts"):
            return copy.deepcopy(posts)
        return duck(text, params)

    counts = cluster.run_cluster(execute, run_date=DAY, market="za")
    assert counts["fit_outliers"] == fit_outliers
    assert counts["today_outliers"] == today_outliers
    assert counts["members"] == members
    assert counts["clusters"] == (0 if case == "all_outliers" else 1)
    assert con.execute(f"SELECT post_id, probability FROM {CORE}.cluster_members ORDER BY post_id").fetchall() == (
        [] if case == "all_outliers" else [("today-inlier", 0.9), ("today-zero", 0.0)])
    if case != "all_outliers":
        assert con.execute(f"SELECT centroid, keywords FROM {CORE}.clusters").fetchall() == [([0.5, 0.5], ["kota", "dance"])]


def test_all_original_outliers_form_no_cluster(original_fit):
    assignments, probabilities = original_fit
    assignments[:] = [-1, -1, -1, -1]
    probabilities[:] = [0.0, 0.0, 0.0, 0.0]
    posts = density_posts()
    topics, probabilities, keywords = cluster.fit_topics(
        [p["text"] for p in posts], np.asarray([p["embedding"] for p in posts]))
    assert topics == [-1, -1, -1, -1]
    assert probabilities == [0.0, 0.0, 0.0, 0.0]
    assert keywords == {}
    assert cluster.build_clusters(posts, topics, probabilities, keywords, DAY, "za") == []


def test_preoptimization_policy_is_separate_from_retained_density_outliers(original_fit):
    from core.understand.tests.test_cluster import _load_preoptimization_fitter

    posts = density_posts()
    documents = [post["text"] for post in posts]
    embeddings = np.asarray([post["embedding"] for post in posts])
    old_topics, old_probabilities, old_keywords = _load_preoptimization_fitter()(documents, embeddings)
    topics, probabilities, keywords = cluster.fit_topics(documents, embeddings)
    assert old_topics == [0, 0, 0, 0]
    assert topics == [0, 0, 0, -1]
    assert old_probabilities == probabilities == [0.9, 0.0, 0.8, 0.0]
    assert old_keywords == {0: ["kota", "bakery"]}
    assert keywords == {0: ["kota", "dance"]}
    [old_cluster] = cluster.build_clusters(posts, old_topics, old_probabilities, old_keywords, DAY, "za")
    [current_cluster] = cluster.build_clusters(posts, topics, probabilities, keywords, DAY, "za")
    assert ("today-outlier", 0.0) in old_cluster["members"]
    assert ("today-outlier", 0.0) not in current_cluster["members"]
    assert ("today-zero", 0.0) in current_cluster["members"]


def test_preoptimization_reference_scope_rejects_original_density_outliers(original_fit):
    from core.understand.tests.test_cluster import _assert_optimization_equivalent_inliers

    assignments, _ = original_fit
    with pytest.raises(AssertionError, match="no original density outliers"):
        _assert_optimization_equivalent_inliers(assignments, density_posts())
