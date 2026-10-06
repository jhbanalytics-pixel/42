from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from core.collect.seeds import MULTI, cluster
from core.collect.socialcrawl_client import PRICED, params_hash, quote_for


MARKETS = ("ZA", "NG", "KE")
SC_ROUTE = "google_trends/trending"
SC_CALLS_PER_RUN = 3
SC_CREDITS_PER_CALL = 5
MAX_SC_CREDITS = SC_CALLS_PER_RUN * SC_CREDITS_PER_CALL
SIGNAL_LABEL = "Google search interest"

# The free public Google Trends tables. KE had no rows in either table's newest partition on 2 Oct 2026, so
# only ZA and NG are read. The two table reads may bill up to 512 MiB each (Albert, 2 Oct 2026, these reads
# only); the partition read keeps the 64 MiB default. Every query is dry-run first and refused over its cap.
BQ_MARKETS = ("ZA", "NG")
BQ_DATASET = "bigquery-public-data.google_trends"
BQ_TABLES = {"top": "international_top_terms", "rising": "international_top_rising_terms"}
BQ_LOCATION = "US"
BQ_MAX_BYTES = 512 * 1024 * 1024
BQ_META_MAX_BYTES = 64 * 1024 * 1024

SignalSource = Literal["google_trending", "google_bq", "google_rss"]
SignalStatus = Literal["ok", "empty", "capped", "unavailable", "unknown", "not_made"]

# Google search terms as row 14 queries (query triage, RULES.md rule 2): at most this many a market a day take
# existing row 14 slots, never added calls. Sources take turns in SEED_SOURCES order, the daily trends feed
# (google_rss) then SocialCrawl trending (google_trending), each newest fetch day first.
GOOGLE_SEEDS_PER_MARKET = 10
# BigQuery Trends terms (google_bq, top and rising) stay search interest only and never become seeds
# (test_bq_search_interest_never_becomes_a_seed).
SEED_SOURCES = ("google_rss", "google_trending")
SEED_TEMPLATE = "tiktok/search/top"
TERMS_SQL = (
    "SELECT market, term, source, kind, rank, fetched_at\n"
    "FROM `{table}`\n"
    "WHERE DATE(fetched_at) = DATE_SUB(@d, INTERVAL 1 DAY) AND market IN UNNEST(@markets)\n"
    "  AND source IN ('google_rss', 'google_trending')"
)


@dataclass(frozen=True)
class SearchSignal:
    market: str
    term: str
    source: SignalSource
    kind: str
    rank: int | None
    fetched_at: datetime
    refreshed_at: str
    refresh_date: date | None
    raw_payload: dict[str, Any]

    def as_table_row(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "term": self.term,
            "source": self.source,
            "kind": self.kind,
            "fetched_at": self.fetched_at,
            "refreshed_at": self.refreshed_at,
            "rank": self.rank,
            "refresh_date": self.refresh_date,
            "raw_payload": self.raw_payload,
        }

    def as_consumer_row(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "market": self.market,
            "source": self.source,
            "rank": self.rank,
            "refreshed_at": self.refreshed_at,
            "label": SIGNAL_LABEL,
        }


@dataclass(frozen=True)
class SourceState:
    status: SignalStatus
    reason: str | None = None


@dataclass(frozen=True)
class SearchSignalBatch:
    run_id: str
    run_date: date
    signals: tuple[SearchSignal, ...]
    states: dict[tuple[str, str], SourceState]

    def table_rows(self) -> list[dict[str, Any]]:
        return [signal.as_table_row() for signal in self.signals]

    def load_rows(self) -> list[dict[str, Any]]:
        """Rows for google_search_signals, ready for a JSON load."""
        return [_json_value(row) for row in self.table_rows()]


@dataclass(frozen=True)
class MarketSeed:
    market: str
    term: str
    source: SignalSource
    kind: str
    rank: int | None
    refreshed_at: str
    seed_date: date
    signal_label: str = SIGNAL_LABEL

    def as_queue_seed(self) -> dict[str, Any]:
        return {
            "seed_date": self.seed_date,
            "market": self.market,
            "item_id": None,
            "query": self.term,
            "kind": self.source,
            "lane": "expansion",
            "priority": 0.0,
            "template": MULTI,
            "ttl_days": 1,
            "credits_estimate": float(
                quote_for(
                    MULTI,
                    PRICED[MULTI].method,
                    {
                        "query": self.term,
                        "platforms": "instagram,youtube,reddit,twitter,threads,facebook",
                        "since": (self.seed_date - timedelta(days=1)).isoformat(),
                    },
                )
            ),
            "yield_posts": None,
            "yield_new_creators": None,
        }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("fetched_at must be timezone-aware")
    return value.astimezone(timezone.utc)


def _as_date(value: date | datetime | str | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        instant = value
    elif isinstance(value, str):
        try:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if instant.tzinfo is None or instant.utcoffset() is None:
        return None
    return instant.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _utc_text(_utc(value))
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _body(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    return value if isinstance(value, Mapping) else None


def _search_signals(
    body: Any, market: str, fetched_at: datetime
) -> tuple[list[SearchSignal], SourceState]:
    response = _body(body)
    data = response.get("data") if response else None
    items = data.get("items") if isinstance(data, Mapping) else None
    if (
        response is None
        or response.get("success") is not True
        or not isinstance(items, list)
    ):
        return [], SourceState("unavailable", "invalid_response_shape")
    if not items:
        return [], SourceState("empty", "no_trends_in_24_hour_window")

    signals = []
    for item in items:
        if not isinstance(item, Mapping):
            return [], SourceState("unavailable", "invalid_response_item")
        term, rank = item.get("title"), item.get("rank")
        if not isinstance(term, str) or not term.strip():
            return [], SourceState("unavailable", "invalid_response_item")
        if rank is not None and (
            isinstance(rank, bool) or not isinstance(rank, int) or rank < 1
        ):
            return [], SourceState("unavailable", "invalid_response_item")
        signals.append(
            SearchSignal(
                market=market,
                term=term.strip(),
                source="google_trending",
                kind="trending",
                rank=rank,
                fetched_at=fetched_at,
                refreshed_at=_utc_text(fetched_at),
                refresh_date=None,
                raw_payload=dict(_json_value(item)),
            )
        )
    return signals, SourceState("ok")


def _sort_instant(row: Mapping[str, Any], field: str) -> datetime:
    return _instant(row.get(field)) or datetime.min.replace(tzinfo=timezone.utc)


def _recorded_result(
    *,
    raw: Mapping[str, Any] | None,
    ledger: Mapping[str, Any] | None,
    market: str,
) -> tuple[list[SearchSignal], SourceState] | None:
    if raw is None and ledger is None:
        return None
    if raw is None:
        return [], SourceState("unknown", "ledger_receipt_without_raw_response")
    if ledger is None:
        return [], SourceState("unknown", "raw_response_without_ledger_receipt")
    http_status = raw.get("http_status")
    fetched_at = _instant(raw.get("fetched_at"))
    if fetched_at is None:
        return [], SourceState("unknown", "recorded_attempt_without_fetched_at")
    if http_status == 404:
        return [], SourceState("empty", "recorded_http_404")
    if http_status is None:
        return [], SourceState("unknown", "recorded_attempt_without_http_status")
    if (
        isinstance(http_status, bool)
        or not isinstance(http_status, int)
        or not 200 <= http_status < 300
    ):
        return [], SourceState("unavailable", f"recorded_http_status_{http_status}")
    return _search_signals(raw.get("body"), market, fetched_at)


def _attempts(
    client: Any, run_id: str, run_date: date
) -> dict[str, tuple[tuple[SearchSignal, ...], SourceState]]:
    memo = getattr(client, "_google_trends_attempts", None)
    if memo is None:
        memo = {}
        setattr(client, "_google_trends_attempts", memo)
    if not isinstance(memo, dict):
        raise ValueError("client Google Trends attempt state is malformed")
    return memo.setdefault((run_id, run_date), {})


def _recorded_receipt(
    raw_receipts: Iterable[Mapping[str, Any]],
    ledger_receipts: Iterable[Mapping[str, Any]],
    *,
    run_date: date,
    market: str,
    digest: str,
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None] | None:
    raws = [
        row
        for row in raw_receipts
        if row.get("job") == "collect"
        and row.get("market") == market
        and row.get("route") == SC_ROUTE
        and row.get("params_hash") == digest
        and _instant(row.get("fetched_at")) is not None
        and _instant(row.get("fetched_at")).date() == run_date
    ]
    ledgers = [
        row
        for row in ledger_receipts
        if row.get("job") == "collect"
        and _as_date(row.get("trend_date")) == run_date
        and row.get("market") == market
        and row.get("route") == SC_ROUTE
        and row.get("params_hash") == digest
    ]
    if not raws and not ledgers:
        return None

    raw = max(raws, key=lambda row: _sort_instant(row, "fetched_at")) if raws else None
    ledger = (
        max(ledgers, key=lambda row: _sort_instant(row, "logged_at"))
        if ledgers
        else None
    )
    if raw is None or ledger is None:
        return raw, ledger
    if ledger.get("cache_hit") or raw.get("run_id") == ledger.get("run_id"):
        return raw, ledger
    return None, ledger


def _client_result(
    result: Any, market: str, fetched_at: datetime
) -> tuple[list[SearchSignal], SourceState]:
    status = str(getattr(result, "status", "unavailable")).lower()
    reason = getattr(result, "reason", None) or None
    if getattr(result, "cache_hit", False):
        return [], SourceState("unknown", "cached_body_missing_raw_receipt")
    if status == "cap_reached":
        return [], SourceState("capped", reason or "collect_share_cap_reached")
    if status in {"empty", "not_found"} or getattr(result, "http_status", None) == 404:
        return [], SourceState("empty", reason or "no_trends_returned")
    if status in {"error", "refunded"} and getattr(result, "http_status", None) is None:
        return [], SourceState("unknown", reason or "call_outcome_unknown")
    if status not in {"ok", "cached"}:
        return [], SourceState("unavailable", reason or status)
    return _search_signals(getattr(result, "body", None), market, fetched_at)


def collect_search_signals(
    client: Any,
    *,
    run_id: str,
    run_date: date,
    fetched_at: datetime,
    raw_receipts: Iterable[Mapping[str, Any]],
    ledger_receipts: Iterable[Mapping[str, Any]],
) -> SearchSignalBatch:
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id is required for once-per-market collection")
    if not isinstance(run_date, date) or isinstance(run_date, datetime):
        raise ValueError("run_date must be a date")
    if getattr(client, "share", None) != "collect":
        raise ValueError(
            "google_trending requires the collect share inside ENGINE_DAILY"
        )
    if getattr(client, "run_id", None) != run_id:
        raise ValueError("run_id must match the SocialCrawl client run")

    fetched_at = _utc(fetched_at)
    raw_receipts, ledger_receipts = list(raw_receipts), list(ledger_receipts)
    attempts = _attempts(client, run_id, run_date)
    signals = []
    states = {}
    for market in MARKETS:
        params = {"location": market, "hours": "24"}
        digest = params_hash("GET", params)
        receipt = _recorded_receipt(
            raw_receipts,
            ledger_receipts,
            run_date=run_date,
            market=market,
            digest=digest,
        )
        if receipt is not None:
            raw, ledger = receipt
            market_signals, state = _recorded_result(
                raw=raw, ledger=ledger, market=market
            )
            attempts[market] = (tuple(market_signals), state)
        elif market in attempts:
            market_signals, state = attempts[market]
        else:
            attempts[market] = ((), SourceState("unknown", "attempt_started"))
            try:
                result = client.call(
                    SC_ROUTE,
                    params,
                    market=market,
                    lane="google_trending",
                )
            except Exception as exc:
                market_signals, state = (
                    [],
                    SourceState("unknown", f"client_exception:{type(exc).__name__}"),
                )
            else:
                market_signals, state = _client_result(result, market, fetched_at)
            attempts[market] = (tuple(market_signals), state)
        signals.extend(market_signals)
        states[(market, "google_trending")] = state
    return SearchSignalBatch(run_id, run_date, tuple(signals), states)


def select_market_seeds(
    batch: SearchSignalBatch,
    *,
    existing_terms_by_market: Mapping[str, Iterable[str]],
    slots_by_market: Mapping[str, int],
) -> dict[str, list[MarketSeed]]:
    selected: dict[str, list[MarketSeed]] = {market: [] for market in MARKETS}
    for market in MARKETS:
        slots = slots_by_market.get(market, 0)
        if isinstance(slots, bool) or not isinstance(slots, int) or slots < 0:
            raise ValueError(f"slot count for {market} must be a non-negative integer")
        if slots == 0:
            continue
        seen = {
            cluster(term)
            for term in existing_terms_by_market.get(market, ())
            if isinstance(term, str) and cluster(term)
        }
        candidates = [
            signal
            for signal in batch.signals
            if signal.market == market
            and signal.source == "google_trending"
            and batch.states.get(
                (market, "google_trending"), SourceState("unavailable")
            ).status
            == "ok"
        ]
        candidates.sort(
            key=lambda signal: (signal.rank or 2**31 - 1, signal.term.casefold())
        )
        for signal in candidates:
            key = cluster(signal.term)
            if not key or key in seen:
                continue
            seen.add(key)
            selected[market].append(
                MarketSeed(
                    market=market,
                    term=signal.term,
                    source=signal.source,
                    kind=signal.kind,
                    rank=signal.rank,
                    refreshed_at=signal.refreshed_at,
                    seed_date=batch.run_date,
                )
            )
            if len(selected[market]) == slots:
                break
    return selected


# Fixtures ("croatia vs england"): a search for a match between two national teams, neither of them the market's,
# finds foreign posts, so such a term never becomes a seed. A club fixture or one naming the market stays.
# Mirrors core/api/searching.py (Searching now) so core/collect stays self-contained.
_VS = {"vs", "v", "versus"}
_FILLER = {"national", "football", "soccer", "team", "teams", "standings", "standing", "lineups", "lineup", "live",
           "score", "scores", "result", "results", "highlights", "prediction", "predictions", "h2h", "today", "match",
           "fc", "women", "womens", "men", "mens", "u17", "u20", "u23", "stats", "tickets", "timeline", "squad"}
_OWN = {"ZA": ("south africa", "sa", "rsa", "bafana bafana", "bafana", "banyana banyana", "banyana", "mzansi"),
        "NG": ("nigeria", "naija", "super eagles", "super falcons"),
        "KE": ("kenya", "harambee stars", "harambee starlets")}
_COUNTRY_ALIASES = {"czech republic": "czechia", "usa": "united states",
                    "united states of america": "united states", "cote d ivoire": "ivory coast", "drc": "dr congo",
                    "congo dr": "dr congo", "democratic republic of the congo": "dr congo", "korea republic":
                    "south korea", "republic of ireland": "ireland", "uae": "united arab emirates", "turkiye": "turkey",
                    "cabo verde": "cape verde", "eswatini": "eswatini", "swaziland": "eswatini"}
_COUNTRIES = frozenset(_COUNTRY_ALIASES) | frozenset(_COUNTRY_ALIASES.values()) | frozenset((
    "afghanistan, albania, algeria, andorra, angola, argentina, armenia, australia, austria, azerbaijan, bahamas, "
    "bahrain, bangladesh, barbados, belarus, belgium, belize, benin, bhutan, bolivia, bosnia and herzegovina, "
    "bosnia, botswana, brazil, brunei, bulgaria, burkina faso, burundi, cambodia, cameroon, canada, cape verde, "
    "central african republic, chad, chile, china, colombia, comoros, congo, costa rica, croatia, cuba, cyprus, "
    "czechia, denmark, djibouti, dominican republic, ecuador, egypt, el salvador, england, equatorial guinea, "
    "eritrea, estonia, eswatini, ethiopia, fiji, finland, france, gabon, gambia, georgia, germany, ghana, greece, "
    "guatemala, guinea, guinea bissau, haiti, honduras, hong kong, hungary, iceland, india, indonesia, iran, "
    "iraq, ireland, northern ireland, israel, italy, ivory coast, jamaica, japan, jordan, kazakhstan, kenya, "
    "kosovo, kuwait, kyrgyzstan, latvia, lebanon, lesotho, liberia, libya, liechtenstein, lithuania, luxembourg, "
    "madagascar, malawi, malaysia, mali, malta, mauritania, mauritius, mexico, moldova, montenegro, morocco, "
    "mozambique, namibia, netherlands, holland, new zealand, nicaragua, niger, nigeria, north korea, "
    "north macedonia, norway, oman, pakistan, palestine, panama, paraguay, peru, philippines, poland, portugal, "
    "qatar, romania, russia, rwanda, san marino, saudi arabia, scotland, senegal, serbia, seychelles, "
    "sierra leone, singapore, slovakia, slovenia, somalia, south africa, south korea, south sudan, spain, sudan, "
    "sweden, switzerland, syria, tanzania, thailand, togo, trinidad and tobago, tunisia, turkey, uganda, ukraine, "
    "united arab emirates, united states, uruguay, uzbekistan, venezuela, vietnam, wales, yemen, zambia, "
    "zimbabwe, dr congo"
).split(", "))



def _sides(term):
    """(left words, right words) of an "X vs Y" term with filler words ("national football team", "standings")
    dropped, or None when the term is not one fixture."""
    words = _tokens(term)
    at = [i for i, w in enumerate(words) if w in _VS]
    if len(at) != 1:
        return None
    left, right = ([w for w in part if w not in _FILLER] for part in (words[:at[0]], words[at[0] + 1:]))
    return (left, right) if left and right else None


def _country(words, at_end):
    """The country a side names (its last words on the left, its first words on the right), or None."""
    for n in range(min(len(words), 5), 0, -1):
        name = " ".join(words[-n:] if at_end else words[:n])
        if name in _COUNTRIES:
            return _COUNTRY_ALIASES.get(name, name)
    return None


def _names(words, phrase):
    want = phrase.split()
    return any(words[i:i + len(want)] == want for i in range(len(words) - len(want) + 1))


def _fixture(term, market):
    """None for a term that is not a fixture; else (shown, key): shown False for a match between two national
    teams neither of which is the market's, key the same for every way of writing one fixture."""
    sides = _sides(term)
    if sides is None:
        return None
    left, right = sides
    own = any(_names(side, phrase) for side in sides for phrase in _OWN.get(market, ()))
    names = (_country(left, True), _country(right, False))
    shown = own or not all(names)
    key = frozenset(n or " ".join(side) for n, side in zip(names, sides))
    return shown, key


def _tokens(text):
    """Lower-case words with accents folded, split on spaces, punctuation and underscores."""
    folded = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return [t for t in re.split(r"[\W_]+", folded.casefold()) if t]


def _seedable(row: Mapping[str, Any]) -> bool:
    source = row.get("source")
    return source in SEED_SOURCES


def _seed_hold(term: str, market: str) -> float:
    params = {"query": term, "country": market, "publish_time": "this-week", "seen": "x"}
    return float(quote_for(SEED_TEMPLATE, PRICED[SEED_TEMPLATE].method, params))


def queue_seeds(
    rows: Iterable[Mapping[str, Any]],
    *,
    run_date: date,
    existing_terms_by_market: Mapping[str, Iterable[str]],
    cap: int = GOOGLE_SEEDS_PER_MARKET,
) -> dict[str, list[dict[str, Any]]]:
    """Up to cap seed rows a market for row 14 (tiktok/search/top, lane expansion, one day) from Google search
    interest rows (google_search_signals rows, or this run's feed rows in the same shape). A term is dropped when
    rule 1 blocks it (gdelt.blocked), when its cluster is on the platform-generic stoplist, or when its cluster
    is already queued or picked. The sources take turns in SEED_SOURCES order, each newest fetch day first and
    then by rank. kind and source carry the Google source, so a seed stays labelled as Google search data; the
    search it steers finds posts that pass the same locality checks as any other row 14 post."""
    from core.collect.gdelt import blocked
    from core.detect.items import is_generic

    pools: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        term = row.get("term")
        if row.get("market") in MARKETS and isinstance(term, str) and term.strip() and _seedable(row):
            pools.setdefault((row["market"], row["source"]), []).append(row)

    def order(row: Mapping[str, Any]) -> tuple:
        fetched = _instant(row.get("fetched_at"))
        day = fetched.date().toordinal() if fetched else 0
        rank = row.get("rank")
        rank = rank if isinstance(rank, int) and not isinstance(rank, bool) else 2**31 - 1
        return (-day, rank, row["term"].strip().casefold())

    out: dict[str, list[dict[str, Any]]] = {market: [] for market in MARKETS}
    for market in MARKETS:
        seen = {cluster(t) for t in existing_terms_by_market.get(market, ()) if isinstance(t, str)}
        queues = [sorted(pools.get((market, source), []), key=order) for source in SEED_SOURCES]
        while len(out[market]) < cap and any(queues):
            for queue in queues:
                if len(out[market]) >= cap:
                    break
                while queue:
                    row = queue.pop(0)
                    term = row["term"].strip()
                    key = cluster(term)
                    fixture = _fixture(term, market)
                    if fixture is not None and not fixture[0]:
                        continue
                    if not key or key in seen or blocked(term) or is_generic("hashtag", key):
                        continue
                    seen.add(key)
                    out[market].append({
                        "seed_date": run_date, "market": market, "item_id": None, "query": term,
                        "kind": row["source"], "lane": "expansion", "priority": 0.0, "template": SEED_TEMPLATE,
                        "ttl_days": 1, "credits_estimate": _seed_hold(term, market), "yield_posts": None,
                        "yield_new_creators": None, "source": row["source"]})
                    break
    return out


def placeholder_seeds(market: str, run_date: date) -> list[dict[str, Any]]:
    """Stand-ins for --plan, which reads no BigQuery and no feed: a full day of Google terms for one market."""
    return [{"seed_date": run_date, "market": market, "item_id": None,
             "query": f"<{market} Google search term {n}>", "kind": "google_search", "lane": "expansion",
             "priority": 0.0, "template": SEED_TEMPLATE, "ttl_days": 1, "credits_estimate": 1.0,
             "yield_posts": None, "yield_new_creators": None, "source": "google_search"}
            for n in range(1, GOOGLE_SEEDS_PER_MARKET + 1)]


def read_terms(bq: Any, run_date: date, markets: Iterable[str] = MARKETS) -> list[dict[str, Any]]:
    """The previous run date's google_search_signals rows that may seed row 14: every feed (google_rss) and
    trending (google_trending) row; the public tables' google_bq rows are never read here. One parameterised
    SELECT over one partition."""
    from google.cloud import bigquery

    from core.collect.writers import _query, table

    sql = TERMS_SQL.format(table=table("google_search_signals"))
    rows = _query(bq, sql, [bigquery.ScalarQueryParameter("d", "DATE", run_date),
                            bigquery.ArrayQueryParameter("markets", "STRING", list(markets))])
    return [dict(r.items()) if hasattr(r, "items") else dict(r) for r in rows]


class BqOverCap(Exception):
    """The dry run puts the read over its cap, or cannot size it."""


@dataclass(frozen=True)
class BqSignalBatch:
    signals: tuple[SearchSignal, ...]
    states: dict[tuple[str, str], SourceState]
    refresh_dates: dict[str, date]
    bytes_billed: int

    def load_rows(self) -> list[dict[str, Any]]:
        """Rows for google_search_signals, ready for a JSON load."""
        return [_json_value(signal.as_table_row()) for signal in self.signals]


def bq_partition_sql() -> str:
    tables = ", ".join(f"'{name}'" for name in BQ_TABLES.values())
    return (
        "SELECT table_name, MAX(partition_id) AS newest\n"
        f"FROM `{BQ_DATASET}.INFORMATION_SCHEMA.PARTITIONS`\n"
        f"WHERE table_name IN ({tables})\n"
        "  AND partition_id NOT IN ('__NULL__', '__UNPARTITIONED__')\n"
        "  AND total_rows > 0\n"
        "GROUP BY table_name"
    )


def bq_read_sql(kind: str, refresh_date: date) -> str:
    """The newest partition's latest week for ZA and NG. Only the columns stored are read, since the bytes
    billed follow the columns; region_name and score are left out to stay under the cap."""
    if not isinstance(refresh_date, date) or isinstance(refresh_date, datetime):
        raise ValueError("refresh_date must be a date")
    extra = ", percent_gain" if kind == "rising" else ""
    markets = ", ".join(f"'{market}'" for market in BQ_MARKETS)
    return (
        f"SELECT country_code, region_code, term, rank, week, refresh_date{extra}\n"
        f"FROM `{BQ_DATASET}.{BQ_TABLES[kind]}`\n"
        f"WHERE refresh_date = DATE '{refresh_date.isoformat()}'\n"
        f"  AND country_code IN ({markets})\n"
        "QUALIFY week = MAX(week) OVER (PARTITION BY country_code)"
    )


def _bq_checked(client: Any, sql: str, cap: int) -> tuple[list[Any], int]:
    """Dry-run sql, refuse it over cap, then run it with maximum_bytes_billed at cap."""
    from google.cloud import bigquery

    dry = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
        location=BQ_LOCATION,
    )
    size = dry.total_bytes_processed
    if size is None or size > cap:
        raise BqOverCap(f"dry run reports {size} bytes; the cap is {cap:,}")
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(dry_run=False, maximum_bytes_billed=cap),
        location=BQ_LOCATION,
    )
    rows = list(job.result())
    return rows, int(getattr(job, "total_bytes_billed", None) or 0)


def _field(row: Any, name: str) -> Any:
    getter = getattr(row, "get", None)
    return getter(name) if callable(getter) else getattr(row, name, None)


def _bq_signals(
    rows: Iterable[Any], kind: str, refresh_date: date, fetched_at: datetime
) -> list[SearchSignal] | None:
    """None when any row is outside the read or malformed, so the table is dropped whole."""
    table = f"{BQ_DATASET}.{BQ_TABLES[kind]}"
    signals = []
    for row in rows:
        market, term, rank = _field(row, "country_code"), _field(row, "term"), _field(row, "rank")
        if market not in BQ_MARKETS or _as_date(_field(row, "refresh_date")) != refresh_date:
            return None
        if not isinstance(term, str) or not term.strip():
            return None
        if rank is not None and (isinstance(rank, bool) or not isinstance(rank, int) or rank < 1):
            return None
        payload = {
            "table": table,
            "country_code": market,
            "region_code": _field(row, "region_code"),
            "week": _field(row, "week"),
            "rank": rank,
            "refresh_date": refresh_date,
        }
        if kind == "rising":
            payload["percent_gain"] = _field(row, "percent_gain")
        signals.append(
            SearchSignal(
                market=market,
                term=term.strip(),
                source="google_bq",
                kind=kind,
                rank=rank,
                fetched_at=fetched_at,
                refreshed_at=refresh_date.isoformat(),
                refresh_date=refresh_date,
                raw_payload=dict(_json_value(payload)),
            )
        )
    return signals


def _all_states(state: SourceState, kinds: Iterable[str] = BQ_TABLES) -> dict[tuple[str, str], SourceState]:
    return {(market, kind): state for kind in kinds for market in BQ_MARKETS}


def read_bq_signals(client: Any, *, run_date: date, fetched_at: datetime) -> BqSignalBatch:
    """Read the newest partition of each public Google Trends table for ZA and NG. These rows are search
    interest only: never post evidence, never a Today card on their own and never a seed (google_bq is not in
    SEED_SOURCES, so queue_seeds skips both the top and the rising terms). Each table is read
    on its own, so one failure keeps the other; nothing here raises for a failed or refused read."""
    if not isinstance(run_date, date) or isinstance(run_date, datetime):
        raise ValueError("run_date must be a date")
    fetched_at = _utc(fetched_at)
    billed = 0
    try:
        meta, billed = _bq_checked(client, bq_partition_sql(), BQ_META_MAX_BYTES)
    except BqOverCap as exc:
        return BqSignalBatch((), _all_states(SourceState("capped", str(exc))), {}, 0)
    except Exception as exc:
        state = SourceState("unknown", f"partition_read_failed:{type(exc).__name__}")
        return BqSignalBatch((), _all_states(state), {}, 0)
    newest = {}
    for row in meta:
        name, partition = _field(row, "table_name"), _field(row, "newest")
        try:
            day = datetime.strptime(str(partition), "%Y%m%d").date()
        except ValueError:
            continue
        if day <= run_date:
            newest[name] = day

    signals: list[SearchSignal] = []
    states: dict[tuple[str, str], SourceState] = {}
    refresh_dates: dict[str, date] = {}
    for kind, name in BQ_TABLES.items():
        if name not in newest:
            states.update(_all_states(SourceState("empty", "no_partition"), (kind,)))
            continue
        refresh_dates[kind] = newest[name]
        try:
            rows, cost = _bq_checked(client, bq_read_sql(kind, newest[name]), BQ_MAX_BYTES)
        except BqOverCap as exc:
            states.update(_all_states(SourceState("capped", str(exc)), (kind,)))
            continue
        except Exception as exc:
            states.update(_all_states(SourceState("unknown", f"query_failed:{type(exc).__name__}"), (kind,)))
            continue
        billed += cost
        found = _bq_signals(rows, kind, newest[name], fetched_at)
        if found is None:
            states.update(_all_states(SourceState("unavailable", "invalid_row"), (kind,)))
            continue
        signals.extend(found)
        for market in BQ_MARKETS:
            present = any(signal.market == market for signal in found)
            states[(market, kind)] = (
                SourceState("ok") if present else SourceState("empty", "no_rows_in_newest_partition")
            )
    return BqSignalBatch(tuple(signals), states, refresh_dates, billed)
