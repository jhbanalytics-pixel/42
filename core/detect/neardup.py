"""N21: write post_enrichment.near_dup_size from the stored captions (BUILD.md 2.11, DATA.md post_enrichment).

near_dup_size of a post is 1 plus the number of other posts sighted in the 7 days to d whose caption is a near
duplicate of its own, by the rule core/detect/coaction.py already uses for near-identical text: Jaccard 0.8 on
character 5-grams of the caption in lower case with links, handles and numbers masked and hashtags left out, at least
20 characters of it. MinHash with locality sensitive hashing finds the candidate pairs, as in coaction, and each pair is
confirmed on the exact 5-gram sets. The size counts neighbours, not a chain: a post is counted by what it matches
itself.

Only sizes of 2 or more are written, as a post_enrichment row holding post_id and near_dup_size and nothing else, the
way embed and video rows leave the columns they do not own empty. A post with no row reads as 1, which the views and
the evidence pack (IFNULL(near_dup_size, 1) >= 3) already do. A run appends a row only where its size is larger than
the largest one already stored, so a second run appends nothing and no stored size is lowered. Nothing is deleted.

datasketch is imported inside the function that uses it, so the detect job still imports when an image lacks it; the
job then returns the step as skipped and carries on to state.

account_created_at, the other input N21 names, has no source in the tables: collect does not store a creation date
(core/collect/parse.py CREATOR_COLUMNS) and no job writes one, so none is written here.
"""

from pathlib import Path

from .aggregate import _run, _struct_array
from core.trust.independence import plain_text, shingles, similar

from .coaction import JACCARD, NUM_PERM
from .sqlrun import AGENT, CORE, query

SQL = Path(__file__).parent / "sql" / "neardup.sql"
CHUNK = 5000  # rows per INSERT, so no one request nears BigQuery's 10 MB limit
INSERT_SQL = """
INSERT INTO {core}.post_enrichment (post_id, near_dup_size)
SELECT n.post_id, n.near_dup_size FROM UNNEST(@rows) n
"""
FIELDS = (("post_id", "STRING"), ("near_dup_size", "INT64"))


def read_sql():
    lines = SQL.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.startswith("--"))


def near_dup_sizes(rows):
    """{post_id: size} for each post of rows ({post_id, text}) with size 2 or more.

    Posts are grouped by their exact masked caption first, and MinHash finds neighbours among the distinct captions
    only, so the work follows the number of distinct captions and not the size of the largest cluster of copies. A
    post's size is the number of posts whose caption is its own or a near duplicate of it, itself included."""
    from datasketch import MinHash, MinHashLSH

    members, seen = {}, set()
    for r in sorted(rows, key=lambda r: r["post_id"]):
        plain = plain_text(r.get("text"))
        if plain and r["post_id"] not in seen:
            seen.add(r["post_id"])
            members.setdefault(plain, []).append(r["post_id"])
    sets = {plain: shingles(plain) for plain in members}
    lsh, hashes = MinHashLSH(threshold=JACCARD, num_perm=NUM_PERM), {}
    for plain, grams in sets.items():
        m = MinHash(num_perm=NUM_PERM, seed=1)
        m.update_batch([g.encode("utf-8") for g in grams])
        hashes[plain] = m
        lsh.insert(plain, m)
    sizes = {}
    for plain, grams in sets.items():
        size = len(members[plain])
        size += sum(len(members[q]) for q in lsh.query(hashes[plain]) if q != plain and similar(grams, sets[q]))
        if size > 1:
            for pid in members[plain]:
                sizes[pid] = size
    return sizes


def run_neardup(client, d, core=CORE, agent=AGENT):
    """Append post_enrichment rows for the posts of the 7 days to d that have near duplicates. Returns counts."""
    rows = query(client, read_sql(), {"d": d}, core=core, agent=agent)
    sizes = near_dup_sizes(rows)
    stored = {r["post_id"]: r["near_dup_size"] or 1 for r in rows}
    new = [{"post_id": pid, "near_dup_size": n} for pid, n in sorted(sizes.items()) if n > stored.get(pid, 1)]
    for i in range(0, len(new), CHUNK):
        _run(client, INSERT_SQL, [_struct_array("rows", new[i:i + CHUNK], FIELDS)], core, agent)
    return {"posts": len({r["post_id"] for r in rows}), "near_dup_posts": len(sizes), "written": len(new)}
