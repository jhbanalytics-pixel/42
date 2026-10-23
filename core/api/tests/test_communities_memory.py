"""Communities within the API's 512 MiB (22 Oct 2026: f42-api killed over its memory limit on GET /api/communities).

Creators joined by three shared items come in pairs, and the pairs grow with the square of the creators who share a
few broad items: 1,000 such creators are about 500,000 pairs. The page used to hold every pair, with its item list,
and a cached copy of them. These tests run the route's reads at that shape, with BigQuery's rows handed over one at a
time as its pages arrive, check what the page keeps, and check the one-pass grouping against the old pairwise one.
"""
import json
from pathlib import Path
import random
import subprocess
import sys
import tracemalloc

import pytest

from core.api import fast, people
from core.api import store as store_mod
from core.api.store import BigQueryStore, FixtureStore, community_components

ROOT = Path(__file__).resolve().parents[3]
START, END = "2026-09-03", "2026-09-30"
BROAD = [f"{10 ** 6 + i:064x}" for i in range(4)]  # four items every creator in the block posts
BLOCK = [f"c_block_{i:04d}" for i in range(300)]  # 44,850 pairs
POSTS_EACH = 5


def pair_rows():
    for n, a in enumerate(BLOCK):
        for b in BLOCK[n + 1:]:
            yield {"creator_a": a, "creator_b": b, "items": list(BROAD)}


def post_rows(creator_ids):
    for c in creator_ids:
        for k in range(POSTS_EACH):
            yield {"post_id": f"{c}_p{k}", "platform": "tiktok", "creator_id": c, "url": f"https://example.invalid/{c}",
                   "text": "t" * 600, "published_at": "2026-09-20T10:00:00+00:00", "views": 1, "likes": 1,
                   "comments": 0, "shares": 0, "thumbnail_url": None, "duration_s": 10, "creator_tier_at_post": "micro",
                   "geo_market": "NG", "handle": c, "item_ids": list(BROAD), "langs": ["en"], "formats": None,
                   "first_day": "2026-09-20"}


class Job:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class PagedClient:
    """BigQuery as the page reads it: a generator per statement, so nothing is held unless the store holds it."""

    def query(self, sql, job_config=None):
        if "INFORMATION_SCHEMA" in sql:
            return Job([{"ds": "intelligence_42_core", "n": n, "what": "table"}
                        for n in ("posts", "post_observations", "post_items", "post_enrichment", "creators")])
        params = {p.name: getattr(p, "values", getattr(p, "value", None)) for p in job_config.query_parameters}
        if "a.creator_id < b.creator_id" in sql:
            return Job(pair_rows())
        return Job(post_rows(params.get("creator_ids") or []))


def block_store():
    """The fixture store, its community and post reads answered by BigQueryStore over the block."""
    store_mod._CATALOG.clear()
    bq = BigQueryStore(client=PagedClient())
    store = FixtureStore()
    for name in ("community_edges", "community_groups", "window_posts"):
        if hasattr(bq, name):
            setattr(store, name, getattr(bq, name))
    return store


def measure():
    """[creators of each community] and the peak traced memory of one Communities page over the block."""
    store = block_store()
    tracemalloc.start()
    try:
        body = people.build_communities(fast.prefetch(store, fast.communities_plan, "NG"), "NG")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return [c["creators"]["value"] for c in body["communities"]], peak


def test_the_communities_route_keeps_no_list_of_creator_pairs():
    # Measured in an interpreter of its own: tracemalloc counts every allocation in the process, from any thread, so
    # in the shared test process the peak can take in work that is not the page's (CI run 221 read 16.9 MiB there; the
    # page alone peaks at about 2.5 MiB).
    done = subprocess.run([sys.executable, "-B", "-c", "from core.api.tests.test_communities_memory import measure; "
                           "import json; print(json.dumps(measure()))"], cwd=ROOT, capture_output=True,
                          encoding="utf-8", timeout=300)
    assert done.returncode == 0, done.stderr[-4000:]
    sizes, peak = json.loads(done.stdout.splitlines()[-1])
    assert sizes == [len(BLOCK)]
    # Holding the 44,850 pairs and their cached copy took about 35 MiB here, and about 350 MiB at 1,000 creators; the
    # list alone, with no cached copy, about 12.5 MiB. Streamed, the page keeps the members, their items and their
    # posts, about 2.5 MiB.
    assert peak < 6 * 2 ** 20, f"peak {peak / 2 ** 20:.1f} MiB"


def reference_components(edges):
    """The page's grouping before 22 Oct 2026, kept here as the reference the one-pass grouping must equal."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in edges:
        a, b = find(e["creator_a"]), find(e["creator_b"])
        if a != b:
            parent[max(a, b)] = min(a, b)
    groups, shared = {}, {}
    for x in list(parent):
        groups.setdefault(find(x), set()).add(x)
    for e in edges:
        items = shared.setdefault(find(e["creator_a"]), {})
        for i in e["items"]:
            items.setdefault(i, set()).update((e["creator_a"], e["creator_b"]))
    return [(groups[root], shared.get(root, {})) for root in groups]


def canonical(components):
    return sorted((sorted(m), sorted((i, sorted(c)) for i, c in s.items())) for m, s in components)


@pytest.mark.parametrize("seed", range(25))
def test_one_pass_grouping_equals_the_pairwise_reference_in_any_order(seed):
    rng = random.Random(seed)
    creators = [f"c{n:03d}" for n in range(rng.randint(2, 60))]
    items = [f"i{n:02d}" for n in range(rng.randint(3, 12))]
    edges = []
    for _ in range(rng.randint(0, 120)):
        a, b = sorted(rng.sample(creators, 2))
        edges.append({"creator_a": a, "creator_b": b, "items": sorted(rng.sample(items, rng.randint(3, len(items))))})
    expected = canonical(reference_components(edges))
    rng.shuffle(edges)
    assert canonical(community_components(iter(edges))) == expected


def test_the_fixture_and_bigquery_stores_group_alike():
    fx = FixtureStore()
    groups = fx.community_groups("NG", START, END, [])
    assert canonical(groups) == canonical(reference_components(fx.community_edges("NG", START, END, [])))
    assert any(len(m) >= people.MIN_MEMBERS for m, _ in groups)
