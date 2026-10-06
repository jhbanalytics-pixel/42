"""Read-only live verification harness.

Runs connector code against the live vendor APIs and BigQuery public
datasets, classifies the result in-memory, and reports row counts plus
classification rate. Writes NOTHING to production: no raw_content insert,
no enriched_content insert, no pipeline_runs row. Safe to run any time,
including the same day as a cron, because it never touches the INSERT path.

This exists so a code fix can be validated against real data immediately
rather than waiting for the next 06:30 UTC cron. Connector fetch() returns
a DataFrame; this harness consumes it in memory and discards it.

Usage:
    python scripts/verify_live.py gdelt
    python scripts/verify_live.py semrush [market] [keyword]

Loads tokens from .env the same way the connectors do locally
(get_secret falls back to env vars when TRENDS_ENV=dev).
"""

from __future__ import annotations

import logging
import os
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
_TARGET_ARGUMENT_LIMITS = {
    "gdelt": 0,
    "reclass-diff": 1,
    "embed-gate": 3,
    "ensemble-probe": 3,
    "ensemble-parent-probe": 3,
    "semrush": 2,
}


def _load_env() -> None:
    """Load .env into os.environ without overwriting already-set vars."""
    envf = ROOT / ".env"
    if not envf.exists():
        return
    for line in envf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _verify_gdelt() -> None:
    from src.enrichment.topic_classifier import classify_topics
    from src.ingestion.connectors.gdelt import GDELTConnector

    print("=== GDELT live fetch() (no token, public dataset) ===")
    total = 0
    for market in ("za", "ng", "ke"):
        df = GDELTConnector(market=market).fetch()
        n = len(df)
        total += n
        classified = 0
        for _, row in df.iterrows():
            topics = classify_topics(
                str(row.get("title", "")),
                str(row.get("text", "")),
                str(row.get("hashtags", "")),
                market,
                content_type=str(row.get("content_type", "") or "gdelt_gkg"),
            )
            if topics and topics != ["__drop__"]:
                classified += 1
        pct = round(100 * classified / n, 1) if n else 0.0
        print(f"  {market.upper()}: {n} rows, {classified} classify ({pct}%)")
    print(f"  TOTAL: {total} rows")


def _reclass_diff(trend_date: str) -> None:
    """Safety Gate: re-classify a COMPLETED partition's stored enriched_content
    with the current classify_topics and diff against the stored topic_groups.

    Read-only. Reports per-market old vs new classified counts, the net delta,
    and any topic whose share of classified rows moves the most. Pin to a
    completed prior partition so the cron is never mid-writing it.
    """
    from google.cloud import bigquery as bq
    from src.enrichment.topic_classifier import classify_topics
    from src.utils.geo_blocklist import text_matches_geo_blocklist

    client = bq.Client(project="ogilvy-trends-v2")
    sql = """
    SELECT market, title, text, hashtags, query_term, slang_terms, content_type,
           topic_groups AS old_topics
    FROM `ogilvy-trends-v2.trends_v2_dev.enriched_content`
    WHERE DATE(collected_at) = @d
    """
    job = client.query(
        sql,
        job_config=bq.QueryJobConfig(
            query_parameters=[bq.ScalarQueryParameter("d", "DATE", trend_date)]
        ),
    )
    rows = list(job.result())
    print(f"=== Reclass diff for {trend_date} ({len(rows)} stored rows) ===")
    per_market: dict[str, dict[str, int]] = {}
    topic_delta: dict[str, int] = {}
    geo_leaks = 0
    for r in rows:
        m = r.market or "?"
        pm = per_market.setdefault(m, {"n": 0, "old": 0, "new": 0})
        pm["n"] += 1
        old = list(r.old_topics or [])
        slang = r.slang_terms.split(",") if isinstance(r.slang_terms, str) and r.slang_terms else []
        new = classify_topics(
            str(r.title or ""),
            str(r.text or ""),
            str(r.hashtags or ""),
            m,
            query_term=str(r.query_term or ""),
            slang_terms=slang,
            content_type=str(r.content_type or ""),
        )
        old_ok = bool(old) and old != ["__drop__"]
        new_ok = bool(new) and new != ["__drop__"]
        if old_ok:
            pm["old"] += 1
        if new_ok:
            pm["new"] += 1
        for t in set(new) - set(old):
            topic_delta[t] = topic_delta.get(t, 0) + 1
        for t in set(old) - set(new):
            topic_delta[t] = topic_delta.get(t, 0) - 1
        # TRUE geo-leak guard: a geo-blocklist topic newly tagged AND the
        # foreign marker actually present (i.e. the strip FAILED). A clean
        # row legitimately gaining economy_sapa_hustle is not a leak.
        raw = f"{r.title or ''} {r.text or ''} {r.hashtags or ''}"
        for t in ("economy_sapa_hustle", "fashion_ankara_asoebi"):
            if t in new and t not in old and text_matches_geo_blocklist(raw, t):
                geo_leaks += 1
    for m in sorted(per_market):
        pm = per_market[m]
        op = round(100 * pm["old"] / pm["n"], 1) if pm["n"] else 0
        npc = round(100 * pm["new"] / pm["n"], 1) if pm["n"] else 0
        print(f"  {m.upper()}: classified {pm['old']}->{pm['new']} ({op}%->{npc}%) of {pm['n']}")
    movers = sorted(topic_delta.items(), key=lambda kv: -abs(kv[1]))[:8]
    print("  topic deltas (new-old):", {k: v for k, v in movers if v})
    print(f"  geo-blocklist topics newly added (must be 0): {geo_leaks}")


def _embed_gate(market: str, trend_date: str, limit: int) -> None:
    """D2 offline Safety Gate (cloud-run; segfaults locally on Windows).

    Read-only. Pulls the keyword-unclassified, non-GDELT residual for a
    market + completed partition, embeds it once via the real Vertex
    classifier, and sweeps cosine thresholds reporting classified-rate +
    top-topic distribution, plus a few sample assignments for an over-tag
    sanity check. Writes nothing to BigQuery. Used to tune the threshold
    before flipping EMBEDDING_CLASSIFIER_ENABLED on the cron.
    """
    from collections import Counter

    from google.cloud import bigquery as bq
    from src.enrichment.embedding_classifier import (
        DEFAULT_MARGIN,
        EmbeddingClassifier,
        _cosine,
        _decide,
        _is_low_signal,
    )

    client = bq.Client(project="ogilvy-trends-v2")
    sql = """
    SELECT title, text, hashtags
    FROM `ogilvy-trends-v2.trends_v2_dev.enriched_content`
    WHERE DATE(collected_at) = @d AND market = @m
      AND ARRAY_LENGTH(topic_groups) = 0
      AND content_type != 'gdelt_gkg'
    ORDER BY RAND()
    LIMIT @lim
    """
    job = client.query(
        sql,
        job_config=bq.QueryJobConfig(
            query_parameters=[
                bq.ScalarQueryParameter("d", "DATE", trend_date),
                bq.ScalarQueryParameter("m", "STRING", market),
                bq.ScalarQueryParameter("lim", "INT64", int(limit)),
            ]
        ),
    )
    texts = [
        " ".join([r.title or "", r.text or "", r.hashtags or ""]).strip() for r in job.result()
    ]
    texts = [t for t in texts if t]
    print(f"=== Embed gate: {market} {trend_date}, {len(texts)} non-empty residual rows ===")
    if not texts:
        print("  no residual rows; nothing to embed")
        return
    clf = EmbeddingClassifier(threshold=0.0)
    anchors = clf._anchors(market)
    topics = list(anchors)
    n_phrases = sum(len(v) for v in anchors.values())
    kept = [t for t in texts if not _is_low_signal(t)]
    dropped = len(texts) - len(kept)
    print(
        f"  {n_phrases} anchor phrases across {len(topics)} topics; "
        f"dropped {dropped} low-signal rows, embedding {len(kept)} of {len(texts)}"
    )
    if not kept:
        print("  all residual rows low-signal; nothing to embed")
        return
    row_vecs = clf._embed(kept)
    # Max-pool each kept row against every topic, once.
    scored_by_row = [
        [(t, max((_cosine(vec, v) for v in anchors[t]), default=0.0)) for t in topics]
        for vec in row_vecs
    ]
    # Sweep the threshold x margin grid. Rate is over the ORIGINAL residual
    # (low-signal rows are legitimately unclassifiable), so it is comparable
    # to the pre-filter numbers. margin 0.0 reproduces the old no-margin rule.
    for margin in (0.0, DEFAULT_MARGIN, 0.08):
        cells = []
        for thr in (0.60, 0.65, 0.70):
            n = sum(1 for scored in scored_by_row if _decide(scored, thr, margin))
            pct = round(100 * n / len(texts), 1)
            cells.append(f"thr{thr}:{n}/{len(texts)}({pct}%)")
        print(f"  margin {margin}: " + "  ".join(cells))
    # Operating point under review: the recall-rich threshold 0.60 at the
    # default margin (validating whether 0.60 stays precision-clean).
    op_thr = 0.60
    dist: Counter = Counter()
    for scored in scored_by_row:
        hits = _decide(scored, op_thr, DEFAULT_MARGIN)
        if hits:
            dist[hits[0]] += 1
    print(
        f"  @{op_thr} margin {DEFAULT_MARGIN} top topics: "
        + ", ".join(f"{t}:{n}" for t, n in dist.most_common(8))
    )
    print(f"  -- sample assignments @ {op_thr} margin {DEFAULT_MARGIN} --")
    shown = 0
    for txt, scored in zip(kept, scored_by_row, strict=False):
        hits = _decide(scored, op_thr, DEFAULT_MARGIN)
        if hits:
            ordered = sorted(scored, key=lambda p: -p[1])
            gap = round(ordered[0][1] - (ordered[1][1] if len(ordered) > 1 else 0.0), 3)
            print(f"    {hits[0]} ({round(ordered[0][1], 3)}, gap {gap}) <- {txt[:80]!r}")
            shown += 1
        if shown >= 14:
            break


def _ensemble_registry() -> dict[str, dict]:
    """name -> endpoint dict, built from the connector's endpoint constants.

    Only keyword-seeded endpoints (those carrying a ``query_param``) belong
    here; comment / reply surfaces are enrichment-style and go through
    ``_ensemble_parent_probe`` instead.
    """
    from src.ingestion.connectors import ensemble as e

    groups = [
        e.DEFAULT_ENDPOINTS,
        # TRENDING_ENDPOINT removed with the dead /tt/trending endpoint (#50).
        e.USER_ENDPOINTS,
        e.TWITTER_ENDPOINTS,
        e.YOUTUBE_WAVE3_ENDPOINTS,
        e.INSTAGRAM_WAVE3_ENDPOINTS,
        e.TIKTOK_WAVE3_ENDPOINTS,
    ]
    reg: dict[str, dict] = {}
    for grp in groups:
        for ep in grp:
            if isinstance(ep, dict) and ep.get("query_param"):
                reg[ep["name"]] = ep
    return reg


def _classify_summary(rows: list, market: str) -> str:
    """rows + classified% + platform mix for a probed batch, classified in memory."""
    from src.enrichment.topic_classifier import classify_topics

    n = len(rows)
    if not n:
        return "0 rows"
    classified = 0
    platforms: Counter = Counter()
    for r in rows:
        platforms[str(r.get("platform") or "?")] += 1
        topics = classify_topics(
            str(r.get("title") or ""),
            str(r.get("text") or ""),
            str(r.get("hashtags") or ""),
            market,
            query_term=str(r.get("query_term") or ""),
            content_type=str(r.get("content_type") or ""),
        )
        if topics and topics != ["__drop__"]:
            classified += 1
    pct = round(100 * classified / n, 1)
    plat = ", ".join(f"{p}:{c}" for p, c in platforms.most_common(4))
    return f"{n} rows, {classified} classified ({pct}%), platforms [{plat}]"


def _ensemble_probe(endpoint_name: str, term: str, market: str) -> None:
    """Keyword-seeded probe of one EnsembleData endpoint (read-only).

    Resets the shared unit ledger first so a prior probe in the same process
    cannot starve this one into a false 'dead surface' reading. Reports rows,
    units charged, platform mix, and classification rate. No flag gate, no
    BigQuery write.
    """
    from src.ingestion.connectors.ensemble import EnsembleConnector

    token = os.environ.get("ENSEMBLEDATA_API_TOKEN", "").strip()
    if not token:
        print("ENSEMBLEDATA_API_TOKEN not set; cannot probe")
        return
    reg = _ensemble_registry()
    ep = reg.get(endpoint_name)
    if not ep:
        print(f"unknown endpoint {endpoint_name!r}; known: {', '.join(sorted(reg))}")
        return
    EnsembleConnector.reset_global_budget()
    conn = EnsembleConnector(market=market)
    rows = conn._fetch_endpoint(ep, term, token)
    print(f"=== ensemble-probe {endpoint_name} term={term!r} market={market} ===")
    print(f"  path={ep['path']} units={conn._units_spent}")
    print(f"  {_classify_summary(rows, market)}")
    for r in rows[:3]:
        txt = " ".join([str(r.get("title") or ""), str(r.get("text") or "")]).strip()
        print(f"    - {txt[:90]!r}")


def _ensemble_parent_probe(surface: str, term: str, market: str) -> None:
    """Enrichment-off-parent probe for comment / reply surfaces (read-only).

    Fetches a parent feed via a keyword-seeded endpoint, then runs the
    enrichment method off the collected posts (the same path the cron uses
    when the surface flag is on). Resets the shared unit ledger first.
    Wired for threads_post_replies, the cleanest-classifying first-flip
    candidate; extend with the tt / ig / yt comment surfaces as they queue.
    """
    from src.ingestion.connectors.ensemble import DEFAULT_ENDPOINTS, EnsembleConnector

    token = os.environ.get("ENSEMBLEDATA_API_TOKEN", "").strip()
    if not token:
        print("ENSEMBLEDATA_API_TOKEN not set; cannot probe")
        return
    if surface != "threads_post_replies":
        print(f"parent-probe not wired for {surface!r} yet (threads_post_replies only)")
        return
    parent_ep = next((e for e in DEFAULT_ENDPOINTS if e["name"] == "threads_keyword"), None)
    if parent_ep is None:
        print("threads_keyword parent endpoint not found")
        return
    EnsembleConnector.reset_global_budget()
    conn = EnsembleConnector(market=market)
    parents = conn._fetch_endpoint(parent_ep, term, token)
    parent_units = conn._units_spent
    replies = conn._fetch_threads_post_replies_enrichment(
        parents, top_n=3, budget=10_000, token=token
    )
    print(f"=== ensemble-parent-probe {surface} term={term!r} market={market} ===")
    print(
        f"  parents {len(parents)} (units {parent_units}) -> "
        f"replies {len(replies)} (total units {conn._units_spent})"
    )
    print(f"  parent {_classify_summary(parents, market)}")
    print(f"  reply  {_classify_summary(replies, market)}")
    for r in replies[:4]:
        print(f"    - {str(r.get('text') or '')[:90]!r}")


def _verify_semrush(market: str, keyword: str) -> None:
    from src.ingestion.connectors.semrush import SemrushConnector
    from src.utils.secrets import get_secret

    token = get_secret("semrush-api-key") or os.environ.get("SEMRUSH_API_KEY", "")
    if not token:
        print("=== Semrush: SKIPPED (no SEMRUSH_API_KEY) ===")
        return

    cfg_patch = {
        "semrush": {
            "enabled": True,
            "budget_units_per_run": 20,
            "units_per_keyword": 20,
            "markets": {
                market: {
                    "keywords": [{"term": keyword, "query_group": "search_intent"}],
                }
            },
        }
    }
    print(f"=== Semrush live fetch() market={market} keyword={keyword!r} ===")
    with patch("src.ingestion.connectors.semrush.load_sources", return_value=cfg_patch):
        df = SemrushConnector(market=market).fetch(api_key=token)
    print(f"  rows: {len(df)}")
    if df.empty:
        return
    row = df.iloc[0]
    print(f"  volume={row.get('views')} difficulty={row.get('likes')}")
    print(f"  text={str(row.get('text') or '')[:160]!r}")


def _unsupported_target(targets: list[str]) -> str | None:
    index = 0
    while index < len(targets):
        target = targets[index]
        limit = _TARGET_ARGUMENT_LIMITS.get(target)
        if limit is None:
            return target
        index += 1
        consumed = 0
        while index < len(targets) and targets[index] not in _TARGET_ARGUMENT_LIMITS:
            consumed += 1
            if consumed > limit:
                return targets[index]
            index += 1
    return None


def main(argv: list[str]) -> int:
    _load_env()
    logging.disable(logging.CRITICAL)  # silence connector logs for clean output
    sys.path.insert(0, str(ROOT))

    targets = argv[1:] or ["gdelt"]
    unsupported = _unsupported_target(targets)
    if unsupported is not None:
        print(f"unsupported target: {unsupported}", file=sys.stderr)
        return 2
    if "gdelt" in targets:
        _verify_gdelt()
    if "reclass-diff" in targets:
        idx = targets.index("reclass-diff")
        rest = targets[idx + 1 :]
        date = rest[0] if rest else "2026-05-28"
        _reclass_diff(date)
    if "embed-gate" in targets:
        idx = targets.index("embed-gate")
        rest = targets[idx + 1 :]
        market = rest[0] if rest else "ng"
        date = rest[1] if len(rest) > 1 else "2026-05-28"
        limit = int(rest[2]) if len(rest) > 2 else 250
        _embed_gate(market, date, limit)
    if "ensemble-probe" in targets:
        idx = targets.index("ensemble-probe")
        rest = targets[idx + 1 :]
        name = rest[0] if rest else "threads_keyword"
        term = rest[1] if len(rest) > 1 else "amapiano"
        market = rest[2] if len(rest) > 2 else "za"
        _ensemble_probe(name, term, market)
    if "ensemble-parent-probe" in targets:
        idx = targets.index("ensemble-parent-probe")
        rest = targets[idx + 1 :]
        surface = rest[0] if rest else "threads_post_replies"
        term = rest[1] if len(rest) > 1 else "mzansi"
        market = rest[2] if len(rest) > 2 else "za"
        _ensemble_parent_probe(surface, term, market)
    if "semrush" in targets:
        idx = targets.index("semrush")
        rest = targets[idx + 1 :]
        market = rest[0] if rest else "za"
        keyword = rest[1] if len(rest) > 1 else "mpesa"
        _verify_semrush(market, keyword)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
