"""Frozen fitter from d8119bef, before float32 storage and the post cap."""

TOP_KEYWORDS = 10


def fit_topics(docs, embeddings):
    """BERTopic on stored embeddings. Returns (topic per doc, HDBSCAN membership probability per doc, top keywords
    per topic). Outliers are reassigned by embedding similarity and keep probability 0; topic -1 remains only when
    HDBSCAN found no topic at all."""
    from bertopic import BERTopic
    from bertopic.vectorizers import ClassTfidfTransformer
    from hdbscan import HDBSCAN
    from sklearn.feature_extraction.text import CountVectorizer
    from umap import UMAP

    n = len(docs)
    vectorizer = CountVectorizer(ngram_range=(1, 2))
    ctfidf = ClassTfidfTransformer(reduce_frequent_words=True)
    model = BERTopic(
        embedding_model=None,
        umap_model=UMAP(n_neighbors=15, n_components=5, metric="cosine", random_state=42),
        hdbscan_model=HDBSCAN(min_cluster_size=max(10, n // 500), min_samples=5, prediction_data=True),
        vectorizer_model=vectorizer,
        ctfidf_model=ctfidf,
    )
    topics, probs = model.fit_transform(docs, embeddings)
    topics = [int(t) for t in topics]
    probs = [float(p) for p in (probs if probs is not None else [0.0] * n)]
    if all(t == -1 for t in topics):
        return topics, probs, {}
    if -1 in topics:
        topics = [int(t) for t in model.reduce_outliers(docs, topics, strategy="embeddings", embeddings=embeddings)]
        model.update_topics(docs, topics=topics, vectorizer_model=vectorizer, ctfidf_model=ctfidf)
    keywords = {t: [w for w, _ in model.get_topic(t) or []][:TOP_KEYWORDS] for t in set(topics) if t != -1}
    return topics, probs, keywords
