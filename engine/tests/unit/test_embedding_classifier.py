"""Unit tests for src/enrichment/embedding_classifier.py.

All tests inject a fake embedder so they run with zero network + zero
Vertex spend. The fake maps each text to a controlled vector so the
cosine + threshold + cap logic is exercised deterministically.
"""

from __future__ import annotations

from src.enrichment.embedding_classifier import (
    EmbeddingClassifier,
    _anchor_texts_for_market,
    _cosine,
)


def test_cosine_basic():
    assert _cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    # degenerate inputs never raise
    assert _cosine([], [1.0]) == 0.0
    assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_anchor_texts_curated_returns_phrase_lists():
    # With configs/topic_anchors/za.yaml present, each topic maps to a list
    # of curated example phrases (not a single description string).
    anchors = _anchor_texts_for_market("za")
    assert "music_amapiano" in anchors
    phrases = anchors["music_amapiano"]
    assert isinstance(phrases, list)
    assert len(phrases) >= 3
    assert all(isinstance(p, str) and p for p in phrases)


def test_anchor_texts_fallback_to_description(monkeypatch, tmp_path):
    # When no curated anchors file exists for a market, every topic falls
    # back to a single-element list of "<humanised name>. <description>".
    from src.enrichment import embedding_classifier as ec

    monkeypatch.setattr(ec, "TOPIC_ANCHORS_DIR", tmp_path / "missing")
    anchors = ec._anchor_texts_for_market("za")
    assert len(anchors["music_amapiano"]) == 1
    assert anchors["music_amapiano"][0].lower().startswith("amapiano")


class _FakeEmbedder:
    """Returns a preset vector per text; counts calls + texts embedded."""

    def __init__(self, vec_by_text: dict[str, list[float]], default: list[float]):
        self.vec_by_text = vec_by_text
        self.default = default
        self.calls = 0
        self.texts_embedded = 0

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.texts_embedded += len(texts)
        return [self.vec_by_text.get(t, list(self.default)) for t in texts]


def _classifier_with_known_anchors(tmp_path, threshold=0.65):
    """Build a classifier whose za anchors sit on distinct basis vectors.

    music_amapiano -> e0, politics_crises -> e1, everything else -> e2.
    A row vector equal to e0 matches only music_amapiano, etc.
    """
    anchors = _anchor_texts_for_market("za")
    vec_by_text: dict[str, list[float]] = {}
    for topic, phrases in anchors.items():
        if topic == "music_amapiano":
            vec = [1.0, 0.0, 0.0]
        elif topic == "politics_crises":
            vec = [0.0, 1.0, 0.0]
        else:
            vec = [0.0, 0.0, 1.0]
        for phrase in phrases:
            vec_by_text[phrase] = list(vec)
    fake = _FakeEmbedder(vec_by_text, default=[0.0, 0.0, 1.0])
    clf = EmbeddingClassifier(
        project="test",
        embedder=fake,
        threshold=threshold,
        cache_path=tmp_path / "cache.json",
    )
    return clf, fake, vec_by_text, anchors


def test_classify_batch_matches_above_threshold(tmp_path):
    clf, _fake, vec_by_text, _ = _classifier_with_known_anchors(tmp_path)
    # A row whose vector equals the music anchor's basis vector matches it.
    vec_by_text["new amapiano drop"] = [1.0, 0.0, 0.0]
    vec_by_text["election crisis today"] = [0.0, 1.0, 0.0]
    out = clf.classify_batch(["new amapiano drop", "election crisis today"], "za")
    assert out[0] == ["music_amapiano"]
    assert out[1] == ["politics_crises"]


def test_per_market_threshold_override(tmp_path):
    """A market in market_thresholds uses its threshold; absent markets fall back."""
    clf, _fake, vec_by_text, _ = _classifier_with_known_anchors(tmp_path)
    # Cosine 0.8 to the music anchor: clears the 0.65 instance default but not 0.99.
    vec_by_text["partial amapiano match here"] = [0.8, 0.0, 0.6]
    clf.market_thresholds = {}
    assert clf.classify_batch(["partial amapiano match here"], "za") == [["music_amapiano"]]
    # Raise za to 0.99 via the per-market map: the same row is now rejected.
    clf.market_thresholds = {"za": (0.99, 0.05)}
    assert clf.classify_batch(["partial amapiano match here"], "za") == [[]]


def test_classify_batch_max_pools_over_phrases(tmp_path):
    # A topic's score is the BEST cosine over its phrase vectors. Push every
    # music_amapiano phrase off e0 except one; a row on e0 must still
    # classify. A centroid/average anchor would dilute below threshold, so
    # this distinguishes max-pool from averaging. clf is freshly built so no
    # anchor vectors are cached before this reassignment takes effect.
    clf, _fake, vec_by_text, anchors = _classifier_with_known_anchors(tmp_path)
    amapiano_phrases = anchors["music_amapiano"]
    assert len(amapiano_phrases) >= 2
    for p in amapiano_phrases[:-1]:
        vec_by_text[p] = [0.0, 0.0, 1.0]
    vec_by_text[amapiano_phrases[-1]] = [1.0, 0.0, 0.0]
    vec_by_text["fresh log drum loop"] = [1.0, 0.0, 0.0]
    out = clf.classify_batch(["fresh log drum loop"], "za")
    assert "music_amapiano" in out[0]


def test_classify_batch_empty_when_below_threshold(tmp_path):
    clf, _fake, vec_by_text, _ = _classifier_with_known_anchors(tmp_path)
    # Orthogonal to the e0/e1 anchors -> only matches the e2 'other' anchors,
    # but a 45-degree mix clears nothing at 0.65.
    vec_by_text["totally unrelated"] = [0.7, 0.7, 0.0]  # cos 0.7 vs both e0,e1
    clf.threshold = 0.95  # raise so 0.7 does not clear
    out = clf.classify_batch(["totally unrelated"], "za")
    assert out[0] == []


def test_classify_batch_rejects_ambiguous_rows(tmp_path):
    clf, _fake, vec_by_text, _ = _classifier_with_known_anchors(tmp_path)
    # A row near several topics at once (gap < margin) is ambiguous and left
    # unclassified even above threshold. Text long enough to clear the
    # low-signal filter so the margin path is exercised.
    vec_by_text["generic mixed lifestyle content here"] = [0.6, 0.6, 0.6]
    clf.threshold = 0.5
    out = clf.classify_batch(["generic mixed lifestyle content here"], "za")
    assert out[0] == []


def test_cache_avoids_reembedding(tmp_path):
    clf, fake, vec_by_text, _ = _classifier_with_known_anchors(tmp_path)
    # Text must clear the low-signal filter to reach the embedder.
    vec_by_text["fresh amapiano single dropped today"] = [1.0, 0.0, 0.0]
    clf.classify_batch(["fresh amapiano single dropped today"], "za")
    embedded_after_first = fake.texts_embedded
    # Second call with the same row text must hit the cache, not re-embed.
    clf.classify_batch(["fresh amapiano single dropped today"], "za")
    assert fake.texts_embedded == embedded_after_first


def test_is_low_signal_filters_structural_noise():
    from src.enrichment.embedding_classifier import _is_low_signal

    assert _is_low_signal("[r/Kenya] [ Removed by Reddit ]")
    assert _is_low_signal("Tuko.co.ke (6,050,850 followers) on facebook")
    assert _is_low_signal("Link shared 28 times in Nigeria trends")
    assert _is_low_signal("[r/Nigeria] Beautiful")
    assert _is_low_signal("Good!")
    assert _is_low_signal("#afrobeats #naija #fyp #viral #foryou")
    # Real topical rows survive.
    assert not _is_low_signal("the secret to perfect Nigerian jollof rice")
    assert not _is_low_signal("Jua kali leo grinding the hustle in Nairobi")


def test_decide_margin_rule():
    from src.enrichment.embedding_classifier import _decide

    # Clear winner above threshold and beyond margin.
    assert _decide([("a", 0.80), ("b", 0.60)], threshold=0.65, margin=0.05) == ["a"]
    # Top topic below threshold.
    assert _decide([("a", 0.62), ("b", 0.40)], threshold=0.65, margin=0.05) == []
    # Above threshold but runner-up within margin -> ambiguous.
    assert _decide([("a", 0.70), ("b", 0.68)], threshold=0.65, margin=0.05) == []


def test_classify_batch_excludes_rescue_topics(tmp_path, monkeypatch):
    # A topic in EMBEDDING_RESCUE_EXCLUDE is never an embedding-rescue
    # candidate, even when a row vector sits on its anchor.
    from src.enrichment import embedding_classifier as ec

    monkeypatch.setattr(ec, "EMBEDDING_RESCUE_EXCLUDE", {"za": frozenset({"music_amapiano"})})
    clf, _fake, vec_by_text, _ = _classifier_with_known_anchors(tmp_path)
    vec_by_text["new amapiano single grooving tonight"] = [1.0, 0.0, 0.0]
    out = clf.classify_batch(["new amapiano single grooving tonight"], "za")
    assert "music_amapiano" not in out[0]


def test_is_enabled_default_off(monkeypatch):
    from src.enrichment import embedding_classifier as ec

    monkeypatch.delenv("EMBEDDING_CLASSIFIER_ENABLED", raising=False)
    assert ec.is_enabled() is False
    monkeypatch.setenv("EMBEDDING_CLASSIFIER_ENABLED", "true")
    assert ec.is_enabled() is True


def test_embed_caps_text_length_and_chunks_by_char_budget(tmp_path):
    """A single over-long text is truncated to MAX_CHARS_PER_TEXT before
    embedding, and chunks are bounded by MAX_CHARS_PER_REQUEST so no request
    can breach the ~20k-token aggregate cap that 400s the whole chunk."""
    from src.enrichment.embedding_classifier import (
        MAX_CHARS_PER_REQUEST,
        MAX_CHARS_PER_TEXT,
    )

    seen_chunk_chars: list[int] = []
    seen_text_lens: list[int] = []

    def recording_embedder(texts):
        seen_chunk_chars.append(sum(len(t) for t in texts))
        seen_text_lens.extend(len(t) for t in texts)
        return [[1.0, 0.0] for _ in texts]

    clf = EmbeddingClassifier(
        project="test",
        cache_path=tmp_path / "cache.json",
        embedder=recording_embedder,
    )
    # Mix one giant text with many medium ones so both caps are exercised.
    texts = ["X" * 5000] + [f"distinct row number {i} " * 50 for i in range(200)]
    out = clf._embed(texts)

    assert len(out) == len(texts)
    assert max(seen_text_lens) <= MAX_CHARS_PER_TEXT
    assert max(seen_chunk_chars) <= MAX_CHARS_PER_REQUEST


def test_embed_skips_a_failing_chunk_without_aborting_market(tmp_path):
    """If one chunk's embedder call raises, those rows get [] vectors (left
    unclassified) and the other chunks still embed, instead of the whole
    market's rescue aborting."""
    call_n = {"i": 0}

    def flaky_embedder(texts):
        call_n["i"] += 1
        if call_n["i"] == 1:
            raise RuntimeError("400 INVALID_ARGUMENT: request too large")
        return [[1.0, 0.0] for _ in texts]

    clf = EmbeddingClassifier(
        project="test",
        cache_path=tmp_path / "cache.json",
        embedder=flaky_embedder,
    )
    # Two chars-budget chunks: first ~60k fails, second succeeds.
    big = [("A" * 1000) for _ in range(70)]  # 70k chars -> 2 chunks
    out = clf._embed(big)

    assert len(out) == len(big)
    assert any(v == [] for v in out)  # failed chunk rows skipped
    assert any(v == [1.0, 0.0] for v in out)  # surviving chunk embedded


def test_embed_chunks_requests_under_vertex_cap(tmp_path):
    """_embed never sends more than EMBED_BATCH_LIMIT texts per embedder call.

    Vertex text-multilingual-embedding-002 caps at 250 instances per
    predict request. On 2026-05-30 the unbatched call sent the full
    residual (805-907 rows/market) in one shot, so every market returned
    400 INVALID_ARGUMENT and the embedding rescue contributed zero rows.
    This pins the chunking so a residual above the cap still embeds.
    """
    from src.enrichment.embedding_classifier import EMBED_BATCH_LIMIT

    calls: list[int] = []

    def recording_embedder(texts):
        calls.append(len(texts))
        return [[1.0, 0.0] for _ in texts]

    clf = EmbeddingClassifier(
        project="test",
        cache_path=tmp_path / "cache.json",
        embedder=recording_embedder,
    )
    # One more than two full batches so a naive single call would breach 250.
    texts = [f"distinct residual row number {i}" for i in range(EMBED_BATCH_LIMIT * 2 + 1)]
    out = clf._embed(texts)

    assert len(out) == len(texts)
    assert all(vec == [1.0, 0.0] for vec in out)
    assert calls, "embedder was never called"
    assert max(calls) <= EMBED_BATCH_LIMIT
    assert sum(calls) == len(texts)  # every text embedded exactly once
    assert EMBED_BATCH_LIMIT <= 250


def test_embed_chunk_returning_fewer_vectors_does_not_poison_cache(tmp_path):
    """A chunk that returns fewer vectors than inputs must not silently pair
    each miss index with a neighbour's vector and write the WRONG vector into
    the persistent content-hash cache. That short chunk is treated like a
    failed chunk: its rows skip rescue ([] vectors), the cache stays clean,
    and a later run can retry them with a correct count."""

    def short_embedder(texts):
        # Return one fewer vector than requested, with a recognisable value so
        # any misalignment would surface as a wrong cached vector.
        return [[9.0, 9.0] for _ in texts[:-1]]

    cache_path = tmp_path / "cache.json"
    clf = EmbeddingClassifier(
        project="test",
        cache_path=cache_path,
        embedder=short_embedder,
    )
    texts = [f"distinct row {i}" for i in range(5)]
    out = clf._embed(texts)

    assert len(out) == len(texts)
    # No misaligned vector is returned; the short chunk's rows skip rescue.
    assert all(vec == [] for vec in out)
    # The cache must not be poisoned with any wrong text-hash to vector pair.
    assert clf._vec_cache == {}
