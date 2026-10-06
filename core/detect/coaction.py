"""The co-action authenticity step of detect (TRUST.md section 5, DATA.md coord_signals, BUILD.md 2.11).

For run date d it reads the posts first sighted in each market in the 30 days to d (sql/coaction.sql) and looks
for three signals. Every comparison leaves out the item's own key (for a hashtag item, that hashtag; for a
meme, topic, brand, event or format item, its phrase), so the thing a trend is made of is never itself a link.

coaction: two accounts co-act on an item when their posts on it fall within 10 minutes and share a link, a
    caption template (text with handles, numbers and links masked, at least 4 words that are not hashtags), a
    hashtag sequence (5 or more hashtags once platform-generic tags such as #fyp are left out, after Pacheco et
    al.), near-identical text (MinHash Jaccard 0.8 on character 5-grams of the text without hashtags, at least
    20 characters) or, when post_enrichment holds an embedding for both posts, embedding cosine 0.95. A pair of
    accounts counts only when two of its co-actions repeat: on items with none in common, or on different
    days with different shared content (CooRTweet's co-sharing of distinct objects). The same pair reposting a
    meme's caption on the same item each day does not count. The pair must also co-act more often than
    chance (see below). Components of 5 or more counted accounts in one market are candidate networks. A
    network writes a row for each item it co-acted on with a post in the item's 7-day window.
same_evening_template: a burst is 5 or more accounts posting the same masked template on an item inside one
    30-minute window of a market-local evening (18:00 to 24:00); windows do not overlap and nothing accumulates
    over the evening. Reading that flags less, because a meme is a shared caption posted by many accounts at
    once and reuse alone never holds: two bursts link only on unrelated items with different templates, when
    they share 5 or more accounts that are at least half of the smaller burst, and when that item pair links
    more often than chance. Linked bursts form one pool with one component id, so one pool repeating on an
    item is one signal.
pool_reuse: a pool (a network or an evening pool; pools merged only where they overlap by half of the
    smaller) where 5 or more of its accounts posted on each of 3 or more unrelated items inside one 30-minute
    window per item in the 30 days, at most one row per item and pool. Items are related when one of the
    pool's posts carries both.

Chance: fans of a meme post at the same evening peak, so pairs and bursts line up by chance. Post times are
reshuffled SHUFFLES times (seeded) within each item and market-local day, which keeps each item's daily rhythm
and breaks who posted when. A pair's co-action count, or an item pair's burst-link count, must exceed the 99th
percentile of its shuffled counts and stay significant under Benjamini-Hochberg at FDR_Q across all pairs
tested, with the shuffled mean as a Poisson rate. Where the accounts own an item-day (every post on it is
theirs, inside one 30-minute window, as with a hashtag for hire) there is nothing to shuffle against, so pairs
and burst links there fall back to TRUST.md section 5's literal rules, and an owned burst joins a pool it
shares 5 accounts and half of itself with. The cost: a pool hidden in a large fan community at its peak, or
on a tag with only a few other posts, is missed.

Rows go to coord_signals for (d, item, market): accounts is a count and item_posts_share is the share of the
item's 7-day posts in that market (posts first sighted from d minus 6, as in tvf_item_window) made by the
signal's accounts. No text field names an account; component ids are hashes. creators.coord_score is the number
of candidate networks an account is in; it is written by MERGE on creator_id that only updates coord_score,
and an account no longer in any network goes back to 0. Writes are appends and that MERGE only, in chunks.

datasketch and networkx are imported inside the functions that use them, so the detect job still imports
when an image lacks them; job.py then records the coaction run as skipped and carries on to state.
"""

import hashlib
import math
import random
import re
from collections import Counter, defaultdict
from datetime import timedelta, timezone
from functools import lru_cache
from itertools import combinations
from pathlib import Path

from .aggregate import _run, _struct_array
from .items import canonical_key, is_generic
from .sqlrun import AGENT, CORE, query

SQL = Path(__file__).parent / "sql" / "coaction.sql"

ITEM_DAYS = 7
PAIR_SECONDS = 600
MIN_ACCOUNTS = 5
POOL_ITEMS = 3
JACCARD = 0.8
COSINE = 0.95
NUM_PERM = 128
SHINGLE = 5
MIN_TEMPLATE_WORDS = 4
MIN_TEXT_CHARS = 20
MIN_TAGS = 5
EVENING_HOUR = 18
BURST_SECONDS = 1800
SHUFFLES = 100
NULL_QUANTILE = 0.99
SHUFFLE_SEED = 42
FDR_Q = 0.01
MARKET_UTC_OFFSET = {"ZA": 2, "NG": 1, "KE": 3}   # SAST, WAT, EAT; none keeps daylight saving
PHRASE_KINDS = ("topic", "meme", "brand", "event", "format")

# Rows per coord_signals INSERT, enrichment read or creators MERGE, so no one request nears BigQuery's 10 MB limit.
CHUNK = 5000

URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
HANDLE = re.compile(r"@\w+")
NUMBER = re.compile(r"\d+(?:[.,:]\d+)*")
TOKEN = re.compile(r"#?\w+")
PLACEHOLDERS = {"URL", "HANDLE", "NUM"}

ENRICHMENT_SQL = """
SELECT pe.* FROM {core}.post_enrichment pe WHERE pe.post_id IN (SELECT n.post_id FROM UNNEST(@ids) n)
"""

INSERT_SQL = """
INSERT INTO {core}.coord_signals (metric_date, item_id, market, run_id, signal, component_id, accounts,
  item_posts_share, rule_version)
SELECT n.metric_date, n.item_id, n.market, n.run_id, n.signal, n.component_id, n.accounts, n.item_posts_share,
  n.rule_version
FROM UNNEST(@rows) n
"""

FLAGGED_SQL = "SELECT DISTINCT cr.creator_id FROM {core}.creators cr WHERE cr.coord_score > 0"

MERGE_SQL = """
MERGE {core}.creators t
USING (SELECT n.creator_id, n.coord_score FROM UNNEST(@rows) n) s
ON t.creator_id = s.creator_id
WHEN MATCHED AND IFNULL(t.coord_score, -1) != s.coord_score THEN UPDATE SET coord_score = s.coord_score
"""

ID_FIELDS = (("post_id", "STRING"),)
SIGNAL_FIELDS = (("metric_date", "DATE"), ("item_id", "STRING"), ("market", "STRING"), ("run_id", "STRING"),
                 ("signal", "STRING"), ("component_id", "STRING"), ("accounts", "INT64"),
                 ("item_posts_share", "FLOAT64"), ("rule_version", "STRING"))
SCORE_FIELDS = (("creator_id", "STRING"), ("coord_score", "INT64"))


def posts_sql():
    lines = SQL.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.startswith("--"))


def mask(text):
    """Lower-cased word and hashtag tokens with links as URL, handles as HANDLE and numbers as NUM."""
    t = URL.sub(" URL ", (text or "").lower())
    t = HANDLE.sub(" HANDLE ", t)
    t = NUMBER.sub(" NUM ", t)
    return " ".join(TOKEN.findall(t))


def _strip_key(text, kind, key):
    if kind == "hashtag":
        pattern = r"(?<!\w)#" + re.escape(key) + r"(?!\w)"
    elif kind in PHRASE_KINDS:
        pattern = r"(?<!\w)#?" + re.escape(key) + r"(?!\w)"
    else:
        return text
    return re.sub(pattern, " ", text)


def _tag(raw):
    try:
        return canonical_key("hashtag", raw)
    except ValueError:
        return None


def features(row, embedding=None):
    """What a post shares with others on one item, the item's key left out."""
    kind, key = row["kind"], row["canonical_key"]
    raw = row.get("text") or ""
    masked = mask(_strip_key(raw.casefold(), kind, key))
    plain = " ".join(t for t in masked.split() if not t.startswith("#"))
    words = [t for t in plain.split() if t not in PLACEHOLDERS]
    tags = [t for t in dict.fromkeys(_tag(h) for h in row.get("hashtags") or [])
            if t and not (kind == "hashtag" and t == key) and not is_generic("hashtag", t)]
    return {"urls": frozenset(u.lower().rstrip(".,;:!?)\"'") for u in URL.findall(raw)),
            "template": masked if len(words) >= MIN_TEMPLATE_WORDS else None,
            "tags": tuple(tags) if len(tags) >= MIN_TAGS else None,
            "text": plain if len(plain) >= MIN_TEXT_CHARS else None,
            "embedding": list(embedding) if embedding else None}


@lru_cache(maxsize=100_000)
def _minhash(text):
    from datasketch import MinHash

    m = MinHash(num_perm=NUM_PERM, seed=1)
    m.update_batch([text[i:i + SHINGLE].encode("utf-8") for i in range(len(text) - SHINGLE + 1)])
    return m


def _cosine(a, b):
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def match(fa, fb):
    """The features two posts share, in a fixed order: url, template, hashtags, text, embedding."""
    out = []
    if fa["urls"] & fb["urls"]:
        out.append("url")
    if fa["template"] and fa["template"] == fb["template"]:
        out.append("template")
    if fa["tags"] and fa["tags"] == fb["tags"]:
        out.append("hashtags")
    if fa["text"] and fb["text"] and _minhash(fa["text"]).jaccard(_minhash(fb["text"])) >= JACCARD:
        out.append("text")
    if fa["embedding"] and fb["embedding"] and _cosine(fa["embedding"], fb["embedding"]) >= COSINE:
        out.append("embedding")
    return out


def _local(ts, market):
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone(timedelta(hours=MARKET_UTC_OFFSET.get(market, 0))))


def _by_item(rows):
    out = defaultdict(list)
    for r in rows:
        out[(r["market"], r["item_id"])].append(r)
    return out


def _time(times, market, item, r):
    return r["published_at"] if times is None else times[(market, item, r["post_id"])]


def _close_pairs(by_item, times=None, accounts=None):
    """(market, item, a, b, earlier time) for posts on one item by different accounts within PAIR_SECONDS.
    times replaces published_at (a shuffle); accounts, when given, limits the posts to those accounts."""
    for (market, item), rs in by_item.items():
        timed = sorted(((_time(times, market, item, r), r) for r in rs if r.get("published_at") is not None
                        and (accounts is None or r["creator_id"] in accounts)),
                       key=lambda x: (x[0], x[1]["post_id"]))
        for i, (ta, a) in enumerate(timed):
            for tb, b in timed[i + 1:]:
                if (tb - ta).total_seconds() > PAIR_SECONDS:
                    break
                if a["creator_id"] != b["creator_id"]:
                    yield market, item, a, b, ta


def candidate_posts(rows):
    """Post ids with a close post by another account on the same item; only these need an embedding."""
    return sorted({p["post_id"] for _, _, a, b, _ in _close_pairs(_by_item(rows)) for p in (a, b)})


def _matcher(embeddings):
    """feats(row) and shared(item, a, b) -> (matched feature names, content keys), both cached, since a
    shuffle only moves posts in time and never changes what two posts share."""
    fcache, mcache = {}, {}

    def feats(r):
        k = (r["post_id"], r["item_id"])
        if k not in fcache:
            fcache[k] = features(r, embeddings.get(r["post_id"]))
        return fcache[k]

    def shared(item, a, b):
        posts = tuple(sorted((a["post_id"], b["post_id"])))
        k = (item, posts)
        if k not in mcache:
            fa, fb = feats(a), feats(b)
            names = match(fa, fb)
            mcache[k] = (names, _content(names, fa, fb, posts) if names else set())
        return mcache[k]

    return feats, shared


def _events(by_item, shared, times=None, accounts=None):
    events = {}
    for market, item, a, b, t in _close_pairs(by_item, times, accounts):
        names, content = shared(item, a, b)
        if not names:
            continue
        posts = tuple(sorted((a["post_id"], b["post_id"])))
        ev = events.get((market, posts))
        if ev is None:
            ev = events[(market, posts)] = {
                "market": market, "posts": posts, "accounts": tuple(sorted((a["creator_id"], b["creator_id"]))),
                "day": _local(t, market).date(), "items": set(), "content": set()}
        ev["items"].add(item)
        ev["content"].update(content)
    return list(events.values())


def coaction_events(rows, embeddings=None):
    """One event per market and post pair that co-acts on at least one item: accounts, local day, items,
    content."""
    return _events(_by_item(rows), _matcher(embeddings or {})[1])


def _content(shared, fa, fb, posts):
    """What a co-action shared: each link, the template, the tag sequence, the text (the smaller of the two
    near-identical texts) or, for an embedding match alone, the post pair."""
    out = {("url", u) for u in fa["urls"] & fb["urls"]} if "url" in shared else set()
    if "template" in shared:
        out.add(("template", fa["template"]))
    if "hashtags" in shared:
        out.add(("tags", fa["tags"]))
    if "text" in shared:
        out.add(("text", min(fa["text"], fb["text"])))
    if "embedding" in shared:
        out.add(("embedding", posts))
    return out


def _links(e):
    return {c for c in e["content"] if c[0] == "url"}


def _repeats(e1, e2):
    """Two co-actions of one pair repeat when they share no item; or fall on different days and share no
    content; or fall on the same day, both sharing links, with no link in common. The same pair reposting the
    same content on the same item is what a meme's fans do, and viewers sharing each day's one official link
    under a fixed caption co-act once a day; a pair pushing several new links in one day is neither."""
    if not (e1["items"] & e2["items"]):
        return True
    if e1["day"] != e2["day"]:
        return not (e1["content"] & e2["content"])
    l1, l2 = _links(e1), _links(e2)
    return bool(l1) and bool(l2) and not (l1 & l2)


def qualifying_pairs(events):
    """(market, (account, account)) pairs with two co-actions that repeat across items or days. This is the
    repeat rule only; analyse also requires the pair's co-action count to beat chance."""
    by = defaultdict(list)
    for ev in events:
        by[(ev["market"], ev["accounts"])].append(ev)
    return {k for k, evs in by.items() if any(_repeats(a, b) for a, b in combinations(evs, 2))}


def _shuffles(by_item):
    """SHUFFLES seeded reshuffles of post times within each item and market-local day: each a dict of
    (market, item, post_id) to a time. The item's daily rhythm stays; who posted when is broken."""
    rng = random.Random(SHUFFLE_SEED)
    groups = []
    for (market, item) in sorted(by_item):
        days = defaultdict(list)
        for r in by_item[(market, item)]:
            if r.get("published_at") is not None:
                days[_local(r["published_at"], market).date()].append(r)
        for day in sorted(days):
            groups.append((market, item, sorted(days[day], key=lambda r: r["post_id"])))
    for _ in range(SHUFFLES):
        times = {}
        for market, item, rs in groups:
            ts = [r["published_at"] for r in rs]
            rng.shuffle(ts)
            times.update(((market, item, r["post_id"]), t) for r, t in zip(rs, ts))
        yield times


def _poisson_tail(k, lam):
    """P(X >= k) for X Poisson with mean lam, in log space so a heavy network's count cannot overflow."""
    head = sum(math.exp(-lam + i * math.log(lam) - math.lgamma(i + 1)) for i in range(k))
    return min(1.0, max(0.0, 1.0 - head))


def _beats_chance(observed, nulls):
    """Keys whose observed count exceeds the NULL_QUANTILE of their shuffled counts (nearest rank) and stays
    significant across all keys tested: each key's shuffled mean (floored at 1/SHUFFLES when no shuffle saw
    it) is a Poisson rate, and the tail p-values go through Benjamini-Hochberg at FDR_Q. Thousands of fan
    pairs each beating their own 99th percentile 1% of the time would otherwise chain into networks."""
    rank = math.ceil(NULL_QUANTILE * len(nulls)) - 1
    tested = []
    for k, n in observed.items():
        counts = sorted(c.get(k, 0) for c in nulls)
        if n > counts[rank]:
            tested.append((_poisson_tail(n, max(sum(counts) / len(counts), 1 / SHUFFLES)), k))
    m, kept = len(observed), set()
    for i, (p, k) in enumerate(sorted(tested), start=1):
        if p <= FDR_Q * i / m:
            kept = {kk for _, kk in sorted(tested)[:i]}
    return kept


def _item_days(by_item):
    """(market, item, market-local day) to the (time, account) of every post on it."""
    out = defaultdict(list)
    for (market, item), rs in by_item.items():
        for r in rs:
            if r.get("published_at") is not None:
                out[(market, item, _local(r["published_at"], market).date())].append(
                    (r["published_at"], r["creator_id"]))
    return out


def _owned(posts, accounts):
    """An item-day the accounts own: every post on it is theirs and all fall inside one BURST_SECONDS window,
    so a reshuffle of its times has nothing to shuffle against and the chance check cannot be computed."""
    # accounts is the union of candidate accounts in the whole run, not the pair or burst being judged
    times = [t for t, _ in posts]
    return all(a in accounts for _, a in posts) and (max(times) - min(times)).total_seconds() <= BURST_SECONDS


def _pairs_above_chance(by_item, events, shared, item_days):
    """Qualifying pairs that beat chance. A pair whose co-actions all fall on item-days the candidate accounts
    own (an owned tag, TRUST.md 5.2) is judged on the literal TRUST.md 5.1 rule instead: it already repeats."""
    candidates = qualifying_pairs(events)
    if not candidates:
        return set()
    observed = Counter(k for e in events if (k := (e["market"], e["accounts"])) in candidates)
    accounts = {a for _, pair in candidates for a in pair}
    owned = {k for k, posts in item_days.items() if _owned(posts, accounts)}
    open_pairs = {k for e in events if (k := (e["market"], e["accounts"])) in candidates
                  and any((e["market"], item, e["day"]) not in owned for item in e["items"])}
    fallback = set(observed) - open_pairs
    observed = {k: n for k, n in observed.items() if k in open_pairs}
    if not observed:
        return fallback
    nulls = []
    for times in _shuffles(by_item):
        seen = defaultdict(set)                   # candidate pair to the distinct post pairs that co-act
        for market, item, a, b, _ in _close_pairs(by_item, times, accounts):
            k = (market, tuple(sorted((a["creator_id"], b["creator_id"]))))
            if k in candidates and shared(item, a, b)[0]:
                seen[k].add(tuple(sorted((a["post_id"], b["post_id"]))))
        nulls.append({k: len(v) for k, v in seen.items()})
    return fallback | _beats_chance(observed, nulls)


def _cid(prefix, *parts):
    return f"{prefix}-{hashlib.sha256('|'.join(map(str, parts)).encode('utf-8')).hexdigest()[:12]}"


def _networks(pairs):
    import networkx as nx

    graphs = defaultdict(nx.Graph)
    for market, (a, b) in pairs:
        graphs[market].add_edge(a, b)
    out = []
    for market in sorted(graphs):
        for comp in nx.connected_components(graphs[market]):
            if len(comp) >= MIN_ACCOUNTS:
                members = frozenset(comp)
                out.append({"market": market, "members": members,
                            "component_id": _cid("coaction", market, *sorted(members))})
    return sorted(out, key=lambda n: n["component_id"])


def _bursts(by_item, feats, times=None):
    """Evening bursts: 5 or more accounts posting one template on one item inside one BURST_SECONDS window of a
    market-local evening. Windows do not overlap and nothing accumulates over the evening: a window that
    qualifies is a burst and the next one starts after it. Each is a dict of market, item, template, evening,
    accounts and posts."""
    timed = defaultdict(list)
    for (market, item), rs in by_item.items():
        for r in rs:
            if r.get("published_at") is None:
                continue
            t = _time(times, market, item, r)
            local = _local(t, market)
            template = feats(r)["template"]
            if local.hour >= EVENING_HOUR and template:
                timed[(market, item, local.date(), template)].append((t, r["creator_id"], r["post_id"]))
    out = []
    for (market, item, evening, template), ps in sorted(timed.items()):
        ps.sort()
        i = 0
        while i < len(ps):
            j = i
            while j < len(ps) and (ps[j][0] - ps[i][0]).total_seconds() <= BURST_SECONDS:
                j += 1
            accounts = {a for _, a, _ in ps[i:j]}
            if len(accounts) >= MIN_ACCOUNTS:
                out.append({"market": market, "item": item, "template": template, "evening": evening,
                            "accounts": accounts, "posts": {p for _, _, p in ps[i:j]}})
                i = j
            else:
                i += 1
    return out


def _burst_links(bursts, post_items):
    """(i, j, key) for bursts on unrelated items with different templates sharing 5 or more accounts that are
    at least half of the smaller burst; key is the market and item pair the chance check counts by."""
    items_of = [set().union(*(post_items[(b["market"], p)] for p in b["posts"])) for b in bursts]
    out = []
    for i, a in enumerate(bursts):
        for j in range(i + 1, len(bursts)):
            b = bursts[j]
            common = len(a["accounts"] & b["accounts"])
            if (a["market"] == b["market"] and a["template"] != b["template"]
                    and a["item"] not in items_of[j] and b["item"] not in items_of[i]
                    and common >= MIN_ACCOUNTS and 2 * common >= min(len(a["accounts"]), len(b["accounts"]))):
                out.append((i, j, (a["market"],) + tuple(sorted((a["item"], b["item"])))))
    return out


def _evening_pools(by_item, feats, post_items, item_days):
    """Bursts linked above chance, grouped into pools: a list of (market, pool accounts, bursts). An item pair
    whose links all join bursts on owned item-days is kept on the literal TRUST.md 5.2 rule instead."""
    import networkx as nx

    bursts = _bursts(by_item, feats)
    links = _burst_links(bursts, post_items)
    if not links:
        return []
    owned = [_owned(item_days[(b["market"], b["item"], b["evening"])], b["accounts"]) for b in bursts]
    observed = Counter(k for _, _, k in links)
    fallback = set(observed) - {k for i, j, k in links if not (owned[i] and owned[j])}
    observed = {k: n for k, n in observed.items() if k not in fallback}
    kept = set(fallback)
    if observed:
        nulls = [Counter(k for _, _, k in _burst_links(_bursts(by_item, feats, times), post_items))
                 for times in _shuffles(by_item)]
        kept |= _beats_chance(observed, nulls)
    graph = nx.Graph()
    graph.add_edges_from((i, j) for i, j, k in links if k in kept)
    comps = [sorted(c) for c in nx.connected_components(graph)]
    items_of = [set().union(*(post_items[(b["market"], p)] for p in b["posts"])) for b in bursts]
    for comp in comps:                   # an owned burst joins the pool it shares 5 accounts and half of itself with
        pool = set().union(*(bursts[i]["accounts"] for i in comp))
        for j, b in enumerate(bursts):
            common = len(b["accounts"] & pool)
            if (owned[j] and j not in graph and b["market"] == bursts[comp[0]]["market"]
                    and common >= MIN_ACCOUNTS and 2 * common >= len(b["accounts"])
                    and all(b["template"] != bursts[i]["template"] and b["item"] not in items_of[i]
                            and bursts[i]["item"] not in items_of[j] for i in comp)):
                comp.append(j)
                graph.add_node(j)
    pools = []
    for comp in comps:
        members = [bursts[i] for i in sorted(comp)]
        pools.append((members[0]["market"], frozenset().union(*(b["accounts"] for b in members)), members))
    return pools


def _merge_pools(pools):
    """(market, accounts) pools merged where they overlap by at least half of the smaller one, per market."""
    import networkx as nx

    graph = nx.Graph()
    pools = sorted(set(pools), key=lambda p: (p[0], sorted(p[1])))
    graph.add_nodes_from(range(len(pools)))
    for i, (m, a) in enumerate(pools):
        for j in range(i + 1, len(pools)):
            if pools[j][0] == m and 2 * len(a & pools[j][1]) >= min(len(a), len(pools[j][1])):
                graph.add_edge(i, j)
    return [(pools[min(c)][0], frozenset().union(*(pools[i][1] for i in c)))
            for c in nx.connected_components(graph)]


def _window_accounts(posts):
    """The most distinct accounts among (time, account) posts inside one BURST_SECONDS window."""
    posts = sorted(posts)
    best, start, inside = 0, 0, Counter()
    for t, a in posts:
        inside[a] += 1
        while (t - posts[start][0]).total_seconds() > BURST_SECONDS:
            inside[posts[start][1]] -= 1
            if not inside[posts[start][1]]:
                del inside[posts[start][1]]
            start += 1
        best = max(best, len(inside))
    return best


def analyse(rows, d, embeddings=None):
    """Signals for day d from coaction.sql rows (post_id, market, first_day, creator_id, published_at, text,
    hashtags, item_id, kind, canonical_key). embeddings maps post_id to a vector where one exists.
    Returns signals (coord_signals rows without date, run and rule version), networks (market, component_id
    and an account count) and scores (creator_id to the number of networks it is in)."""
    import networkx as nx

    by_item = _by_item(rows)
    start7 = d - timedelta(days=ITEM_DAYS - 1)
    recent = {k: {r["post_id"]: r["creator_id"] for r in rs if r["first_day"] >= start7}
              for k, rs in by_item.items()}
    feats, shared = _matcher(embeddings or {})
    events = _events(by_item, shared)
    item_days = _item_days(by_item)
    pairs = _pairs_above_chance(by_item, events, shared, item_days)
    nets = _networks(pairs)
    out = {}

    def add(market, item, signal, cid, members):
        posts = {p: c for p, c in recent.get((market, item), {}).items() if c in members}
        if posts:
            out[(item, market, signal, cid)] = {
                "item_id": item, "market": market, "signal": signal, "component_id": cid,
                "accounts": len(set(posts.values())),
                "item_posts_share": len(posts) / len(recent[(market, item)])}

    for net in nets:
        m, members = net["market"], net["members"]
        for ev in events:
            if ev["market"] == m and (m, ev["accounts"]) in pairs and set(ev["accounts"]) <= members:
                for item in ev["items"]:
                    if set(ev["posts"]) & recent.get((m, item), {}).keys():
                        add(m, item, "coaction", net["component_id"], members)

    post_items = defaultdict(set)
    for (m, item), rs in by_item.items():
        for r in rs:
            post_items[(m, r["post_id"])].add(item)
    evening_pools = _evening_pools(by_item, feats, post_items, item_days)
    for m, pool, members in evening_pools:
        cid = _cid("evening", m, *sorted(pool))
        per_item = defaultdict(lambda: (set(), set()))
        for b in members:
            in7 = b["posts"] & recent.get((m, b["item"]), {}).keys()
            if b["evening"] >= start7 and in7:
                per_item[b["item"]][0].update(b["accounts"])
                per_item[b["item"]][1].update(in7)
        for item, (accounts, posts) in per_item.items():
            out[(item, m, "same_evening_template", cid)] = {
                "item_id": item, "market": m, "signal": "same_evening_template", "component_id": cid,
                "accounts": len(accounts), "item_posts_share": len(posts) / len(recent[(m, item)])}

    pools = _merge_pools([(n["market"], n["members"]) for n in nets] + [(m, p) for m, p, _ in evening_pools])
    for m, pool in sorted(pools, key=lambda p: (p[0], sorted(p[1]))):
        timed, pool_items = defaultdict(list), defaultdict(set)
        for (m2, item), rs in by_item.items():
            if m2 == m:
                for r in rs:
                    if r["creator_id"] in pool and r.get("published_at") is not None:
                        timed[item].append((r["published_at"], r["creator_id"]))
                        pool_items[r["post_id"]].add(item)
        counted = {it for it, ps in timed.items() if _window_accounts(ps) >= MIN_ACCOUNTS}
        graph = nx.Graph()
        graph.add_nodes_from(counted)
        for its in pool_items.values():
            its = sorted(its & counted)
            graph.add_edges_from(zip(its, its[1:]))
        if nx.number_connected_components(graph) >= POOL_ITEMS:
            cid = _cid("pool", m, *sorted(pool))
            for item in counted:
                add(m, item, "pool_reuse", cid, pool)

    scores = Counter(a for members in {n["members"] for n in nets} for a in members)
    return {"signals": [s for _, s in sorted(out.items(), key=lambda kv: str(kv[0]))],
            "networks": [{"market": n["market"], "component_id": n["component_id"], "accounts": len(n["members"])}
                         for n in nets],
            "scores": dict(scores)}


def run_coaction(client, d, run_id, rule_version, core=CORE, agent=AGENT):
    """Append coord_signals for d under run_id and merge creators.coord_score. Returns counts."""
    rows = query(client, posts_sql(), {"d": d}, core=core, agent=agent)
    ids = candidate_posts(rows)
    embeddings = {}
    for i in range(0, len(ids), CHUNK):
        chunk = [{"post_id": p} for p in ids[i:i + CHUNK]]
        for r in _run(client, ENRICHMENT_SQL, [_struct_array("ids", chunk, ID_FIELDS)], core, agent):
            if r.get("embedding"):
                embeddings[r["post_id"]] = [float(x) for x in r["embedding"]]
    result = analyse(rows, d, embeddings)

    signal_rows = [{"metric_date": d, "run_id": run_id, "rule_version": rule_version, **s}
                   for s in result["signals"]]
    for i in range(0, len(signal_rows), CHUNK):
        _run(client, INSERT_SQL, [_struct_array("rows", signal_rows[i:i + CHUNK], SIGNAL_FIELDS)], core, agent)

    scores = {r["creator_id"]: 0 for r in query(client, FLAGGED_SQL, core=core, agent=agent)}
    scores.update(result["scores"])
    score_rows = [{"creator_id": c, "coord_score": n} for c, n in sorted(scores.items())]
    for i in range(0, len(score_rows), CHUNK):
        _run(client, MERGE_SQL, [_struct_array("rows", score_rows[i:i + CHUNK], SCORE_FIELDS)], core, agent)

    return {"posts": len({(r["post_id"], r["market"]) for r in rows}), "networks": len(result["networks"]),
            "coord_signals": len(signal_rows), "coord_scores": len(score_rows)}
