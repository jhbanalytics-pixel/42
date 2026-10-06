#!/usr/bin/env python
"""Regenerate configs/common_english_words.txt from wordfreq.

Run offline (wordfreq is a dev-only dependency, not in the engine image):

    py -3.13 -m pip install wordfreq
    py -3.13 scripts/gen_common_english.py

The list is every English word with zipf frequency >= 2.8 and length >= 4,
minus the SSA discovery config vocabulary. seed_candidates uses it to gate
generic English tokens that carry no genz or slang signal. The 2.8 cutoff sits
above the busiest real SSA terms (kasi 2.47, calabar 2.44) with margin, so a
vernacular term or an entity never lands in the drop set.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import wordfreq
from src.analysis.seed_candidates import load_config_terms

ZIPF_MIN = 2.8
MIN_LEN = 4
OUT = Path(__file__).resolve().parent.parent / "configs" / "common_english_words.txt"


def main() -> None:
    candidates = wordfreq.top_n_list("en", 40000)
    common = {
        w
        for w in candidates
        if len(w) >= MIN_LEN
        and w.isalpha()
        and w.isascii()
        and wordfreq.zipf_frequency(w, "en") >= ZIPF_MIN
    }
    protect: set[str] = set()
    for market in ("za", "ng", "ke"):
        protect |= {t for t in load_config_terms(market) if t.isalpha()}
    words = sorted(common - protect)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Common English words (wordfreq zipf >= 2.8, len >= 4), minus SSA config\n")
        f.write("# vocabulary. Generated offline; used by seed_candidates to gate generic\n")
        f.write("# English tokens that carry no genz or slang signal. Regenerate with\n")
        f.write("# scripts/gen_common_english.py when wordfreq updates.\n")
        for w in words:
            f.write(w + "\n")
    print(f"wrote {len(words)} words to {OUT}")


if __name__ == "__main__":
    main()
