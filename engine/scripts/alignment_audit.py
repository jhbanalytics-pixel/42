"""Full alignment audit across the v2 changes. One-off verification."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

problems: list[str] = []
warnings: list[str] = []


# 1. YAML files parse
print("=" * 60)
print("[1/10] YAML parse check")
print("=" * 60)
yaml_files = [
    "configs/sources.yaml",
    "configs/scoring.yaml",
    "configs/alerts.yaml",
    "configs/trend_cycles.yaml",
    "configs/topic_groups/za.yaml",
    "configs/topic_groups/ng.yaml",
    "configs/topic_groups/ke.yaml",
    "configs/creators/za.yaml",
    "configs/creators/ng.yaml",
    "configs/creators/ke.yaml",
]
for f in yaml_files:
    p = ROOT / f
    if not p.exists():
        problems.append(f"MISSING FILE: {f}")
        continue
    try:
        yaml.safe_load(p.read_text(encoding="utf-8"))
        print(f"  OK  {f}")
    except Exception as e:
        problems.append(f"YAML parse error in {f}: {e}")


# 2. Python AST
print()
print("=" * 60)
print("[2/10] Python AST parse check")
print("=" * 60)
py_files = [
    "src/enrichment/topic_classifier.py",
    "src/ingestion/enrichment.py",
    "src/analysis/generate_briefs.py",
    "src/alerts/email_digest.py",
    "scripts/run_rss_now.py",
]
for f in py_files:
    p = ROOT / f
    try:
        ast.parse(p.read_text(encoding="utf-8"))
        print(f"  OK  {f}")
    except SyntaxError as e:
        problems.append(f"Python syntax error in {f}: {e}")


# 5. EnsembleData pool size
print()
print("=" * 60)
print("[5/10] EnsembleData pool size per market")
print("=" * 60)
src = yaml.safe_load((ROOT / "configs/sources.yaml").read_text(encoding="utf-8"))
for mkt in ("za", "ng", "ke"):
    ens = src["ensembledata"][mkt]
    tt_kws = {k.lower() for k in ens.get("tiktok_keywords", [])}
    tt_hash = {k.lower() for k in ens.get("tiktok_hashtags", [])}
    th_kws = {k.lower() for k in ens.get("threads_keywords", [])}
    ig_hash = {k.lower() for k in ens.get("instagram_hashtags", [])}
    total_pool = tt_kws | tt_hash | th_kws | ig_hash
    print(
        f"  {mkt.upper()}: {len(total_pool)} pool entries "
        f"(tt_kw={len(tt_kws)} tt_hash={len(tt_hash)} th_kw={len(th_kws)} ig_hash={len(ig_hash)})"
    )


# 6. Classifier smoke test
print()
print("=" * 60)
print("[6/10] Topic classifier 3-layer smoke test")
print("=" * 60)
from src.enrichment.topic_classifier import (
    _BRAND24_LABEL_TO_TOPIC,
    _SLANG_TO_TOPIC,
    DROP_SENTINEL,
    classify_topics,
)

for mkt in ("za", "ng", "ke"):
    lbls = _BRAND24_LABEL_TO_TOPIC.get(mkt, {})
    drops = sum(1 for v in lbls.values() if v == DROP_SENTINEL)
    maps = len(lbls) - drops
    print(
        f"  {mkt.upper()} retired Brand24 label dict: {len(lbls)} entries "
        f"({maps} mapped, {drops} drop)"
    )
for mkt in ("za", "ng", "ke"):
    print(f"  {mkt.upper()} slang map: {len(_SLANG_TO_TOPIC.get(mkt, {}))} entries")

test_cases = [
    ("za", "Music and Entertainment", "", "", ["music_amapiano"]),
    ("za", "visionsoul.co.za", "", "", [DROP_SENTINEL]),
    ("ng", "", "Davido new afrobeats track", "", ["music_afrobeats"]),
    ("ke", "Mobile Payment Services", "", "", ["fintech_mpesa"]),
    ("ke", "raai laxmi", "", "", [DROP_SENTINEL]),
    ("ng", "", "random unmatched content", "", []),
]
for mkt, qt, title, text, expected_result in test_cases:
    actual = classify_topics(title=title, text=text, hashtags="", market=mkt, query_term=qt)
    ok = actual == expected_result
    status = "OK" if ok else f"FAIL got {actual}, expected {expected_result}"
    if not ok:
        problems.append(f"classifier {mkt}/{qt!r}: {status}")
    label = qt or title
    print(f"  {mkt}/{label[:40]!r}: {actual}  [{status}]")


# 7. Scoring weights sum
print()
print("=" * 60)
print("[7/10] Scoring weights sum to 1.0")
print("=" * 60)
sc = yaml.safe_load((ROOT / "configs/scoring.yaml").read_text(encoding="utf-8"))
w = sc["weights"]
total_w = sum(w.values())
print(f"  weights: {w}")
print(f"  sum: {total_w}")
if abs(total_w - 1.0) > 0.001:
    problems.append(f"scoring weights sum {total_w} != 1.0")
else:
    print("  OK")


# 8. Creator watchlist structure
print()
print("=" * 60)
print("[8/10] Creator watchlist structure check")
print("=" * 60)
for mkt in ("za", "ng", "ke"):
    c = yaml.safe_load((ROOT / f"configs/creators/{mkt}.yaml").read_text(encoding="utf-8"))
    if "watchlists" not in c:
        problems.append(f"creators/{mkt}.yaml missing watchlists key")
        continue
    if "tier_weights" not in c:
        problems.append(f"creators/{mkt}.yaml missing tier_weights key")
        continue
    tw = c["tier_weights"]
    if not (tw.get("tier_1") and tw.get("tier_2") and tw.get("tier_3")):
        problems.append(f"creators/{mkt}.yaml missing tier weights")
    counts = sum(
        len(c["watchlists"].get(p, {}).get(t, []))
        for p in ("tiktok", "instagram", "threads")
        for t in ("tier_1", "tier_2", "tier_3")
    )
    print(f"  {mkt.upper()}: {counts} total creator handles, tier weights OK")


# 9. run_rss_now drop sentinel handling
print()
print("=" * 60)
print("[9/10] run_rss_now.py __drop__ sentinel handling")
print("=" * 60)
rs = (ROOT / "scripts/run_rss_now.py").read_text(encoding="utf-8")
if '"__drop__"' in rs:
    print("  OK  __drop__ sentinel reference found")
else:
    problems.append("scripts/run_rss_now.py missing __drop__ sentinel handling")
if 'topics == ["__drop__"]' in rs:
    print("  OK  drop-sentinel guard clause present")
else:
    warnings.append("drop-sentinel guard pattern not in expected form")


# 10. enrichment.py passes new args
print()
print("=" * 60)
print("[10/10] enrichment.py passes query_term + slang_terms")
print("=" * 60)
en = (ROOT / "src/ingestion/enrichment.py").read_text(encoding="utf-8")
has_qt = "query_term=" in en
has_st = "slang_terms=" in en
if has_qt and has_st:
    print("  OK  enrichment.py passes new args to classify_topics")
else:
    problems.append(f"enrichment.py missing args (query_term={has_qt}, slang_terms={has_st})")


# Summary
print()
print("=" * 60)
print("ALIGNMENT AUDIT SUMMARY")
print("=" * 60)
print(f"Problems: {len(problems)}")
for p in problems:
    print(f"  [PROBLEM] {p}")
print(f"\nWarnings: {len(warnings)}")
for w in warnings:
    print(f"  [WARN] {w}")
if not problems and not warnings:
    print("FULL ALIGNMENT. No problems detected.")
