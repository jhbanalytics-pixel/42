"""Executable source inventory: registry keys, dispatcher surfaces and configuration evidence.

The executable keys come from the producer's ``CONNECTORS`` definition, read
from the script's syntax tree by ``source_policy.load_executable_connectors``
rather than by importing the producer, because importing it loads dotenv,
mutates ``sys.path``, configures root logging and builds a BigQuery client.
Only the connector modules the registry names are imported, and importing
them performs no network call and reads no secret.

Surfaces are the routes a connector can actually execute. A connector with one
fetch method has that method as its surface. A connector whose ``fetch`` is a
route dispatcher is expanded into the routes the dispatcher can select, read
from the connector module itself and never from a vendor catalog: SocialCrawl
runs the phases in ``PHASE_ORDER`` plus the funded Wave 1 routes in
``WAVE1_ROUTE_SPECS``; EnsembleData runs the endpoint tables its module
declares (the ``surfaces`` keyword of its ``fetch`` selects them by name).
Flag gated branches inside one call, such as the YouTube trending, comment
thread and playlist reads, are not separately selectable and stay inside the
``fetch`` surface. Disabled connectors keep their surfaces because the policy
must state a market status for every executable route.

Configuration evidence is read from ``configs/sources.yaml`` (the shipped file
by default, or a mapping passed in) through one ``Switch`` per gate, each read
the way its connector reads it (the gdelt and apple_music ``active`` gates are
off only when exactly False, the ensemble gate consults ``ensemble.enabled``
before ``ensemble_budget.enabled`` and defaults to on, the semrush gate is on
only when exactly True, an absent socialcrawl market block runs with defaults),
and reconciled into a lifecycle state per source, market and route. A source whose
configuration blocks are all absent from the file is unavailable and unconfigured
rather than running on defaults, because tidying a cancelled vendor out of the file
must never read as that vendor being available. A switch value that is text is read
only when it spells a boolean without ambiguity and refused otherwise, so a quoting
mistake cannot flip a gate. A switch that reads off is unavailable when the
reviewed profile records a vendor or credential reason and intentionally
excluded when it records an operator decision; a switch that reads on is
approved but unproven until an evidence row carries a capability proof. The
off reasons rest on the notes inside ``sources.yaml`` and on the registry
comments, which can be stale, so they are listed as unproven in the report.

A source that is switched on while a part of its promised feature is switched off inside
the same call is degraded rather than off: it executes and delivers less than the feature
promises. A part is read the way the connector reads it, so a flag the connector defaults
to off is off when the configuration is silent, and a part switched on with nothing for
the connector to read, an empty handle list or a per run count of zero, is the same
degradation. ``connector_cap_reads`` answers what the connector actually honours by reading
its syntax tree, so ``rollout_capability_reconciliation`` can say whether a capability the
rollout ladder declares is shipped in the code or only in the declaration, and
``ladder_increments`` and ``activation_policy`` turn the ladder and its controls into the
inputs of the activation gate. ``source_estate_document`` renders the whole reconciliation
as the operations record in ``docs/operations/source-estate.md``.
"""

from __future__ import annotations

import ast
import importlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from src.analysis.open_intelligence.source_policy import (
    LIFECYCLE_STATES,
    POLICY_MARKETS,
    PROOF_VALIDITY_DAYS,
    STATE_APPROVED_UNPROVEN,
    STATE_DEGRADED,
    STATE_EXCLUDED,
    STATE_KNOWN_EMPTY,
    STATE_QUALIFIED_LIVE,
    STATE_UNAVAILABLE,
    ActivationPolicy,
    RouteActivationRequest,
    feature_blockers,
    fetch_surfaces,
    load_executable_connectors,
    mixed_state_rows,
    policy_digest,
    reconcile_source_estate,
    render_inventory_table,
)
from src.utils.config_loader import load_yaml

SOURCES_CONFIG_PATH = Path(__file__).resolve().parents[3] / "configs" / "sources.yaml"
ROLLOUT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "configs" / "socialcrawl_rollout.yaml"
SOCIALCRAWL_MODULE = (
    Path(__file__).resolve().parents[2] / "ingestion" / "connectors" / "socialcrawl.py"
)
SOURCE_ESTATE_DOC = Path(__file__).resolve().parents[4] / "docs" / "operations" / "source-estate.md"
CONNECTOR_PACKAGE_PATH = Path(__file__).resolve().parents[2] / "ingestion" / "connectors"
CONFIGS_PATH = Path(__file__).resolve().parents[3] / "configs"
# The connector reads its own configuration through these mappings, so a key one
# of them fetches is a key the code actually honours.
_CONFIGURATION_READERS = frozenset({"caps", "market_cfg", "config"})
# Dispatcher tables are read only from modules inside the connectors package,
# so a class defined elsewhere (a test double) keeps its public fetch surfaces.
_CONNECTOR_PACKAGE = "src.ingestion.connectors."
WAVE1_STAGE = "stage_1_wave_1"
PROOF_ACTION = (
    "Attach the capability proof reference from the staging ledger for this market "
    "before promotion to qualified_live."
)


_SWITCH_MODES = frozenset({"truthy", "unless_false", "true_only", "count"})


@dataclass(frozen=True, slots=True)
class Switch:
    """One configuration gate, read the way the connector reads it.

    ``paths`` are tried in order and the first PRESENT reading decides, where
    present means the file carries the key, null included: a present null is a
    value the connector reads and never its default. ``absent_on`` is the reading
    when no path is present at all. ``mode`` judges a present reading: ``truthy``
    (a bool, or a non empty list or mapping), ``unless_false`` (off only when the
    value is exactly False), ``true_only`` (on only when the value is exactly
    True) or ``count`` (a count the connector slices by, on at one and above, so
    zero and a negative bound read off).
    """

    paths: tuple[str, ...]
    absent_on: bool = False
    mode: str = "truthy"

    def __post_init__(self) -> None:
        if self.mode not in _SWITCH_MODES:
            raise ValueError(f"switch_mode_unknown:{self.mode}")
        if not self.paths:
            raise ValueError("switch_paths_missing")

    def read(self, settings: Mapping[str, object], market: str) -> tuple[str, object, bool]:
        for path in self.paths:
            named = path.format(market=market)
            reading = read_config_reading(settings, path, market=market)
            if reading is ABSENT:
                continue
            if self.mode == "count":
                count = _count_value(named, reading)
                return named, count, count >= 1
            value = _switch_value(named, reading)
            if self.mode == "unless_false":
                on = value is not False
            elif self.mode == "true_only":
                on = value is True
            else:
                on = _reads_on(value)
            return named, value, on
        return self.paths[0].format(market=market), None, self.absent_on

    def root(self) -> frozenset[str]:
        """The configuration blocks this gate reads, by their top level key."""
        return frozenset(path.split(".", 1)[0] for path in self.paths)


def _enabled(block: str) -> Switch:
    """The ``<block>.enabled`` gate with a default of off, the common connector idiom."""
    return Switch((f"{block}.enabled",))


CONDITION_UNCONFIGURED = "unconfigured"
CONDITION_OPERATOR_EXCLUDED = "operator_excluded"
CONDITION_MARKET_EMPTY = "market_expected_empty"
CONDITION_PART_OFF = "part_switched_off"
CONDITION_SWITCH_ON = "switch_on_unproven"
CONDITION_ROUTE_OFF = "route_switched_off"


@dataclass(frozen=True, slots=True)
class PartialFeature:
    """One part of a promised feature, read the way the connector reads it.

    ``switch`` carries the connector's own default in ``absent_on``: the YouTube
    trending, comment thread and playlist reads are each read with a default of
    False, so an absent flag is the promised part switched off, which is exactly
    what the degraded state exists to name. Leaving an absent path to "the
    connector default" reads a fully switched off market as fully approved.

    ``inputs`` are the readings the connector needs once the flag is on. A
    playlist read with no creator handles and a count the connector slices by
    that is zero or negative both leave the connector honouring the flag and
    fetching nothing, which is the same degradation by a different route. An
    input is modelled by the RULE the connector applies to it, a list it iterates
    or a count it slices by, rather than by the one value that was reported: a
    per run count of zero and a per creator count of zero are the same defect.
    """

    switch: Switch
    inputs: tuple[Switch, ...] = ()

    def reading(self, settings: Mapping[str, object], market: str) -> tuple[str, object] | None:
        """The first reading that leaves this part delivering nothing, if any."""
        path, value, on = self.switch.read(settings, market)
        if not on:
            return path, value
        for gate in self.inputs:
            path, value, on = gate.read(settings, market)
            if not on:
                return path, value
        return None

    def root(self) -> frozenset[str]:
        return self.switch.root().union(*(gate.root() for gate in self.inputs))


@dataclass(frozen=True, slots=True)
class ConnectorProfile:
    """Reviewed descriptor of one registry connector (baseline c17ce95)."""

    vendor: str
    account: str
    platform: str
    feature: str
    required: bool
    cadence: str
    freshness: str
    native_identity: str
    date_rule: str
    author_rule: str
    url_rule: str
    geography_method: str
    budget_stage: str
    # Every switch must read on for the source to count as configured;
    # {market} is substituted in each path.
    switches: tuple[Switch, ...]
    off_state: str = STATE_EXCLUDED
    off_reason: str = "Switched off in sources.yaml by operator decision."
    off_action: str = "Leave off until a feature needs it and a budget is approved."
    on_action: str = PROOF_ACTION
    # The condition behind the off state, kept beside it because four different
    # conditions otherwise print as one word: a retired vendor, a dormant
    # account, credentials that have not landed and a source with no
    # configuration at all are not the same fact about the estate.
    off_condition: str = CONDITION_OPERATOR_EXCLUDED
    # Optional path whose False reading marks the market as known empty.
    expected_path: str | None = None
    # Sub feature switches inside one call. The route still executes, so the
    # source is degraded rather than off: it delivers part of what its feature
    # promises.
    partials: tuple[PartialFeature, ...] = ()

    def __post_init__(self) -> None:
        if not self.switches:
            raise ValueError("profile_switches_missing")
        if not self.off_condition:
            raise ValueError("profile_off_condition_missing")

    def roots(self) -> frozenset[str]:
        """Every configuration block this source is read from."""
        return frozenset().union(
            *(gate.root() for gate in self.switches),
            *(part.root() for part in self.partials),
        )


def _profile(**fields) -> ConnectorProfile:
    return ConnectorProfile(**fields)


_RETIRED_REASON = (
    "Vendor account cancelled per the sources.yaml note; connector comments can be stale."
)
_RETIRED_ACTION = (
    "Keep retired. The estate owner removes the registry entry or records the "
    "substitute route with equivalent proof."
)
_RETIRED_CONDITION = "vendor_account_cancelled"

CONNECTOR_PROFILES: Mapping[str, ConnectorProfile] = {
    "rss": _profile(
        vendor="publisher_rss",
        account="none_public_feed",
        platform="web_news",
        feature="news_discovery",
        required=True,
        cadence="daily",
        freshness="feed_pubdate_within_run_window",
        native_identity="feed_entry_id_or_link",
        date_rule="feed_published",
        author_rule="feed_author_or_publisher",
        url_rule="feed_link",
        geography_method="market_feed_list",
        budget_stage="free",
        switches=(Switch(("rss_feeds.{market}",)),),
        off_state=STATE_KNOWN_EMPTY,
        off_reason="No feeds configured for the market.",
        off_action="Add market feeds to rss_feeds or record the market as out of scope.",
        off_condition="no_feeds_configured",
    ),
    "apple_music": _profile(
        vendor="apple",
        account="none_public_rss",
        platform="apple_music",
        feature="music_charts",
        required=True,
        cadence="daily",
        freshness="chart_snapshot_at_run",
        native_identity="track_id",
        date_rule="chart_observed_at",
        author_rule="artist_name",
        url_rule="track_url",
        geography_method="storefront_country",
        budget_stage="free",
        switches=(
            Switch(("apple_music.active",), absent_on=True, mode="unless_false"),
            Switch(("apple_music.markets.{market}.active",), absent_on=True, mode="unless_false"),
        ),
    ),
    "spotify": _profile(
        vendor="spotify",
        account="client_credentials_dormant",
        platform="spotify",
        feature="music_charts",
        required=False,
        cadence="daily",
        freshness="chart_snapshot_at_run",
        native_identity="track_id",
        date_rule="chart_observed_at",
        author_rule="artist_name",
        url_rule="track_url",
        geography_method="market_playlist",
        budget_stage="free_api_dormant",
        switches=(
            _enabled("spotify"),
            # spotify.py reads `not config.get("active", True)`, which is the
            # truthy idiom with a default of on, NOT the `is False` idiom the
            # other three "active" flags use. Declaring unless_false here read a
            # present null and a zero as on where the connector reads them off.
            Switch(("spotify.active",), absent_on=True),
        ),
        off_state=STATE_UNAVAILABLE,
        off_reason=(
            "Access dormant: the vendor deprecated client credentials playlist reads "
            "(registry comment; can be stale)."
        ),
        off_action=(
            "apple_music is the approved substitute for music charts. The estate owner "
            "confirms the substitute proof or removes the entry."
        ),
        off_condition="vendor_access_dormant",
    ),
    "bigquery_trends": _profile(
        vendor="google",
        account="gcp_project_bigquery",
        platform="google_trends_public_dataset",
        feature="search_interest",
        required=True,
        cadence="daily",
        freshness="dataset_refresh_lag_days",
        native_identity="term_plus_date",
        date_rule="dataset_week_or_day",
        author_rule="none",
        url_rule="none",
        geography_method="country_partition",
        budget_stage="bigquery_bytes_scanned",
        switches=(Switch(("bigquery_trends.active",), absent_on=True, mode="unless_false"),),
        expected_path="bigquery_trends.expected_per_market.{market}",
    ),
    "youtube": _profile(
        vendor="google",
        account="youtube_data_api_key",
        platform="youtube",
        feature="video_discovery_and_comments",
        required=True,
        cadence="daily",
        freshness="published_within_query_window",
        native_identity="video_id",
        date_rule="published_at",
        author_rule="channel_title",
        url_rule="watch_url",
        geography_method="region_code_plus_query_terms",
        budget_stage="daily_api_quota_units",
        switches=(Switch(("youtube_queries.{market}",)),),
        partials=(
            # youtube.py reads each flag with a default of False, and then reads
            # the lists it iterates and the counts it slices by once the flag is
            # on. Every one of these declarations is held against youtube.py's own
            # syntax tree by test_source_inventory.py, so a default that moves in
            # the connector and not here is a failing test rather than a comment.
            PartialFeature(Switch(("youtube_queries.{market}.trending_enabled",))),
            PartialFeature(
                Switch(("youtube_queries.{market}.comment_threads_enabled",)),
                inputs=(
                    Switch(
                        ("youtube_queries.{market}.comment_videos_per_run",),
                        absent_on=True,
                        mode="count",
                    ),
                ),
            ),
            PartialFeature(
                Switch(("youtube_queries.{market}.playlist_items_enabled",)),
                inputs=(
                    Switch(("youtube_queries.{market}.playlist_creator_handles",)),
                    Switch(
                        ("youtube_queries.{market}.playlist_items_per_creator",),
                        absent_on=True,
                        mode="count",
                    ),
                ),
            ),
            # The search read has no flag of its own: the terms ARE the switch,
            # and the call budget is the count the connector slices them by.
            PartialFeature(
                Switch(("youtube_queries.{market}.queries",)),
                inputs=(
                    Switch(
                        ("youtube_queries.{market}.max_calls_per_run",),
                        absent_on=True,
                        mode="count",
                    ),
                ),
            ),
        ),
        on_action=(
            "API key lives in Secret Manager; attach the quota and row proof for this market."
        ),
    ),
    "youtube_scrape": _profile(
        vendor="youtube_public",
        account="none_scrape",
        platform="youtube",
        feature="video_discovery_breadth",
        required=False,
        cadence="daily",
        freshness="published_within_query_window",
        native_identity="video_id",
        date_rule="published_at",
        author_rule="channel_title",
        url_rule="watch_url",
        geography_method="query_terms_per_market",
        budget_stage="free",
        switches=(Switch(("youtube_queries.{market}.scrape_enabled",)),),
    ),
    "gdelt": _profile(
        vendor="gdelt_project",
        account="gcp_project_bigquery",
        platform="global_news",
        feature="news_events_and_tone",
        required=True,
        cadence="daily",
        freshness="gkg_date_within_run_window",
        native_identity="gkg_record_id",
        date_rule="gkg_date",
        author_rule="source_common_name",
        url_rule="document_identifier",
        geography_method="source_country_and_location_filters",
        budget_stage="bigquery_bytes_scanned",
        switches=(Switch(("gdelt.active",), absent_on=True, mode="unless_false"),),
    ),
    "ensemble": _profile(
        vendor="ensembledata",
        account="bronze_cancelled",
        platform="tiktok_instagram_threads_twitter_youtube",
        feature="social_posts_comments_creators",
        required=False,
        cadence="daily",
        freshness="vendor_period_days",
        native_identity="vendor_item_id",
        date_rule="create_time",
        author_rule="handle",
        url_rule="item_url",
        geography_method="market_terms_and_creator_watchlist",
        budget_stage="vendor_units_cancelled",
        switches=(Switch(("ensemble.enabled", "ensemble_budget.enabled"), absent_on=True),),
        off_state=STATE_UNAVAILABLE,
        off_reason=_RETIRED_REASON,
        off_action=_RETIRED_ACTION,
        off_condition=_RETIRED_CONDITION,
    ),
    "reddit": _profile(
        vendor="ensembledata",
        account="bronze_cancelled",
        platform="reddit",
        feature="community_discussion",
        required=False,
        cadence="daily",
        freshness="listing_sort_window",
        native_identity="post_id",
        date_rule="created_utc",
        author_rule="author",
        url_rule="permalink",
        geography_method="market_subreddit_list",
        budget_stage="vendor_units_cancelled",
        switches=(_enabled("reddit"),),
        off_state=STATE_UNAVAILABLE,
        off_reason=_RETIRED_REASON,
        off_action=_RETIRED_ACTION,
        off_condition=_RETIRED_CONDITION,
    ),
    "socialcrawl": _profile(
        vendor="socialcrawl",
        account="ogilvy_funded",
        platform="multi_platform",
        feature="social_posts_comments_creators",
        required=True,
        cadence="daily",
        freshness="max_age_days",
        native_identity="vendor_item_id",
        date_rule="published_at_normalized",
        author_rule="handle",
        url_rule="item_url",
        geography_method="market_terms_creator_watchlist_geo_verify",
        budget_stage="funded_stage_caps_100_250_750",
        switches=(
            _enabled("socialcrawl"),
            Switch(("socialcrawl.markets.{market}",), absent_on=True, mode="unless_false"),
        ),
    ),
    "wikipedia": _profile(
        vendor="wikimedia",
        account="none_public_api",
        platform="wikipedia",
        feature="encyclopedia_attention",
        required=False,
        cadence="daily",
        freshness="pageview_day",
        native_identity="article_title",
        date_rule="pageview_day",
        author_rule="none",
        url_rule="article_url",
        geography_method="top_per_country",
        budget_stage="free",
        switches=(_enabled("wikipedia"),),
    ),
    "bluesky": _profile(
        vendor="bluesky",
        account="none_public_api_unconfigured",
        platform="bluesky",
        feature="social_posts",
        required=False,
        cadence="daily",
        freshness="created_within_run_window",
        native_identity="at_uri",
        date_rule="created_at",
        author_rule="handle",
        url_rule="post_url",
        geography_method="per_market_terms",
        budget_stage="free",
        switches=(_enabled("bluesky"),),
        off_state=STATE_UNAVAILABLE,
        off_reason=(
            "No bluesky block in sources.yaml, so bluesky.enabled reads off and the "
            "connector produces zero rows."
        ),
        off_action="Add a bluesky block and probe searchPosts per market before any flip.",
        off_condition=CONDITION_UNCONFIGURED,
    ),
    "semrush": _profile(
        vendor="semrush",
        account="api_units",
        platform="search_keywords",
        feature="keyword_metrics",
        required=False,
        cadence="daily",
        freshness="monthly_keyword_metrics",
        native_identity="keyword_plus_database",
        date_rule="observed_at",
        author_rule="none",
        url_rule="none",
        geography_method="country_database",
        budget_stage="api_units_20_per_keyword",
        switches=(Switch(("semrush.enabled",), mode="true_only"),),
    ),
    "google_trends_rss": _profile(
        vendor="google",
        account="none_public_rss",
        platform="google_trends",
        feature="daily_trending_searches",
        required=True,
        cadence="daily",
        freshness="feed_pubdate",
        native_identity="trend_title_plus_date",
        date_rule="feed_pubdate",
        author_rule="none",
        url_rule="explore_link",
        geography_method="geo_parameter",
        budget_stage="free",
        switches=(_enabled("google_trends_rss"),),
    ),
    "app_charts": _profile(
        vendor="apple",
        account="none_public_rss",
        platform="app_store",
        feature="app_charts",
        required=False,
        cadence="daily",
        freshness="chart_snapshot_at_run",
        native_identity="app_id",
        date_rule="chart_observed_at",
        author_rule="developer_name",
        url_rule="app_url",
        geography_method="storefront_country",
        budget_stage="free",
        switches=(_enabled("app_charts"),),
    ),
    "audiomack": _profile(
        vendor="audiomack",
        account="oauth1_pending",
        platform="audiomack",
        feature="music_trending",
        required=False,
        cadence="daily",
        freshness="trending_snapshot_at_run",
        native_identity="song_id",
        date_rule="trending_observed_at",
        author_rule="artist_name",
        url_rule="song_url",
        geography_method="genre_list_per_market",
        budget_stage="free_pending_credentials",
        switches=(_enabled("audiomack"),),
        off_state=STATE_UNAVAILABLE,
        off_reason=(
            "OAuth1 consumer keys pending; the connector returns empty without them "
            "(registry comment; can be stale)."
        ),
        off_action=("Live probe the response shape after credentials land, then attach the proof."),
        off_condition="credentials_pending",
    ),
    "cloudflare_radar": _profile(
        vendor="cloudflare",
        account="api_token",
        platform="cloudflare_radar",
        feature="domain_ranking",
        required=False,
        cadence="daily",
        freshness="ranking_snapshot_at_run",
        native_identity="domain",
        date_rule="ranking_observed_at",
        author_rule="none",
        url_rule="domain_url",
        geography_method="location_parameter",
        budget_stage="free_token",
        switches=(_enabled("cloudflare_radar"),),
        on_action=(
            "Switch reads on but the token lives in Secret Manager; confirm the token is "
            "wired for staging and attach the proof."
        ),
    ),
    "pulsar": _profile(
        vendor="pulsar",
        account="trial_token",
        platform="social_listening",
        feature="social_listening_eval",
        required=False,
        cadence="daily",
        freshness="search_window",
        native_identity="post_id",
        date_rule="published_at",
        author_rule="author",
        url_rule="post_url",
        geography_method="configured_search_hashes",
        budget_stage="trial",
        switches=(_enabled("pulsar"),),
    ),
}


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    source: str
    market: str
    switch: str
    switch_value: object
    configured: bool
    state: str
    condition: str
    reason: str
    next_owner_action: str


def _endpoint_names(module: object) -> set[str]:
    """Endpoint names from the module level endpoint tables a connector declares."""
    names: set[str] = set()
    for attribute, value in vars(module).items():
        if not attribute.isupper():
            continue
        candidates = value if isinstance(value, list) else [value]
        if not candidates or not all(
            isinstance(item, dict) and isinstance(item.get("name"), str) and "path" in item
            for item in candidates
        ):
            continue
        names.update(item["name"] for item in candidates)
    return names


def dispatcher_surfaces(source: str, connector_class: type) -> set[str]:
    """Return the routes a connector can execute, expanding a route dispatcher."""
    surfaces = fetch_surfaces(connector_class)
    expansion: set[str] = set()
    if not connector_class.__module__.startswith(_CONNECTOR_PACKAGE):
        return surfaces
    module = importlib.import_module(connector_class.__module__)
    phase_order = getattr(module, "PHASE_ORDER", None)
    if isinstance(phase_order, tuple):
        expansion.update(phase_order)
    wave1_routes = getattr(module, "WAVE1_ROUTE_SPECS", None)
    if isinstance(wave1_routes, Mapping):
        expansion.update(wave1_routes)
    expansion.update(_endpoint_names(module))
    if expansion:
        surfaces.discard("fetch")
        surfaces.update(expansion)
    if not surfaces:
        raise ValueError(f"connector_surface_missing:{source}")
    return surfaces


def executable_inventory(
    connectors: Iterable[tuple[str, type]] | None = None,
) -> dict[tuple[str, str], set[str]]:
    """Derive the executable route inventory, one key per source and market."""
    items = tuple(load_executable_connectors() if connectors is None else connectors)
    keys = [key for key, _ in items]
    if len(keys) != len(set(keys)):
        raise ValueError("connector_key_duplicate")
    inventory: dict[tuple[str, str], set[str]] = {}
    for key, connector_class in items:
        surfaces = dispatcher_surfaces(key, connector_class)
        for market in POLICY_MARKETS:
            inventory[(key, market)] = set(surfaces)
    return inventory


class _AbsentReading:
    """The reading of a configuration path that the file does not carry at all."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "ABSENT"

    def __bool__(self) -> bool:
        raise TypeError("absent_reading_has_no_truth_value")


ABSENT = _AbsentReading()


def read_config_reading(config: Mapping[str, object], path: str, *, market: str) -> object:
    """Read a dotted configuration path, telling an absent path from a present null.

    A missing segment reads as ``ABSENT``. A key that is present and holds null
    reads as ``None``, because a present null is a value the connector reads: the
    ``.get(key, default)`` idiom never reaches its default for a key that is
    there, so a present null is ``bool(None)`` or ``int(None)``. Reading the two
    the same way let one character in sources.yaml carry a cancelled vendor into
    the estate as approved.
    """
    node: object = config
    for segment in path.format(market=market).split("."):
        if not isinstance(node, Mapping) or segment not in node:
            return ABSENT
        node = node[segment]
    return node


def read_config_path(config: Mapping[str, object], path: str, *, market: str) -> object:
    """Read a dotted configuration path; a missing segment reads as None.

    Readers that have to tell an absent path from a present null read through
    ``read_config_reading``; this one flattens both to None for the readers that
    do not care which it was.
    """
    reading = read_config_reading(config, path, market=market)
    return None if reading is ABSENT else reading


_BOOLEAN_TEXT = {"true": True, "false": False}


def _switch_value(path: str, value: object) -> object:
    """A switch reading, with text judged rather than guessed at.

    A switch is a boolean question. ``enabled: "false"`` is a quoting mistake,
    and reading it for its truthiness answers the opposite of what it says, so a
    one character slip would move a cancelled vendor from unavailable to approved
    unproven. Text that unambiguously spells a boolean is read as that boolean;
    any other text is refused rather than interpreted.
    """
    if not isinstance(value, str):
        return value
    reading = _BOOLEAN_TEXT.get(value.strip().lower())
    if reading is None:
        raise ValueError(f"switch_value_unreadable:{path}:{value}")
    return reading


def _reads_on(value: object) -> bool:
    """Whether a reading already judged by ``_switch_value`` reads on.

    Text never reaches here through ``Switch``: ``_switch_value`` has already
    spelled it out or refused it. The refusal is kept so a future caller that
    reaches this helper directly is refused rather than guessed at.
    """
    if value is None or value is False:
        return False
    if isinstance(value, (list, tuple, Mapping)):
        return len(value) > 0
    if isinstance(value, str):
        raise ValueError(f"switch_value_unreadable::{value}")
    return bool(value)


def _count_value(path: str, value: object) -> int:
    """A count reading, read the way the connector reads a count.

    The connector wraps these in ``int(...)`` and then slices by the result, so
    the question is not whether the value is truthy but whether it is a count the
    connector can slice by. Zero and a negative bound leave the connector
    honouring its flag and fetching nothing, and a value ``int`` cannot read is
    the TypeError the connector would raise, named by its key rather than bare.
    """
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        raise ValueError(f"switch_value_unreadable:{path}:{value}") from None


def flag_reads_on(block: object, key: str, *, absent_on: bool = False) -> bool:
    """One boolean configuration flag, read once for every reader of it.

    The estate and ``scripts/engine_pulse.py`` gate on the same flags, and two
    readers of one flag may not disagree: text read for its truthiness answers
    the opposite of what it spells, and a present null is the connector's own
    ``bool(None)`` rather than its default. Both readers call this, so there is
    one reading and not two.
    """
    settings: Mapping[str, object] = block if isinstance(block, Mapping) else {}
    _path, _value, on = Switch((key,), absent_on=absent_on).read(settings, "")
    return on


def _load_config(config: Mapping[str, object] | None) -> Mapping[str, object]:
    return load_yaml(SOURCES_CONFIG_PATH) if config is None else config


def configuration_evidence(
    config: Mapping[str, object] | None = None,
    connectors: Iterable[tuple[str, type]] | None = None,
) -> dict[tuple[str, str], SourceEvidence]:
    """Reconcile every registry source and market into a lifecycle state."""
    settings = _load_config(config)
    items = tuple(load_executable_connectors() if connectors is None else connectors)
    evidence: dict[tuple[str, str], SourceEvidence] = {}
    for key, _connector_class in items:
        profile = CONNECTOR_PROFILES.get(key)
        if profile is None:
            raise ValueError(f"connector_profile_missing:{key}")
        unconfigured = _unconfigured_roots(settings, profile)
        for market in POLICY_MARKETS:
            switch, value, configured = profile.switches[0].read(settings, market)
            for gate in profile.switches[1:]:
                if not configured:
                    break
                switch, value, configured = gate.read(settings, market)
            condition = CONDITION_SWITCH_ON
            if unconfigured:
                # Tidying a cancelled vendor out of sources.yaml is exactly the
                # edit that used to promote it: with every block gone the gates
                # read their defaults and a dead vendor came back as approved.
                # No configuration is not a default; it is no source.
                switch = sorted(unconfigured)[0]
                value, configured = None, False
                state, condition = STATE_UNAVAILABLE, CONDITION_UNCONFIGURED
                blocks = ", ".join(sorted(unconfigured))
                reason = (
                    f"No {blocks} block in sources.yaml, so the source has no configuration "
                    "to run from."
                )
                action = (
                    f"Add the {blocks} configuration and probe the source per market, or "
                    "remove the registry entry."
                )
            elif not configured:
                state, reason, action = profile.off_state, profile.off_reason, profile.off_action
                condition = profile.off_condition
            elif (
                profile.expected_path is not None
                and read_config_path(settings, profile.expected_path, market=market) is False
            ):
                switch = profile.expected_path.format(market=market)
                value = False
                state = STATE_KNOWN_EMPTY
                condition = CONDITION_MARKET_EMPTY
                reason = "Configured, and the market is expected empty by the configuration."
                action = f"Keep known_empty until {switch} carries data for this market."
            else:
                partial = _partial_reading(settings, profile, market)
                if partial is not None:
                    switch, value = partial
                    state = STATE_DEGRADED
                    condition = CONDITION_PART_OFF
                    reason = (
                        "Switch reads on, and a part of the promised feature is switched off "
                        f"at {switch}."
                    )
                    action = (
                        f"Switch {switch} back on, or record the missing part of "
                        f"{profile.feature} as withdrawn for this market."
                    )
                else:
                    state, reason, action = (
                        STATE_APPROVED_UNPROVEN,
                        "Switch reads on.",
                        (profile.on_action),
                    )
            evidence[(key, market)] = SourceEvidence(
                key, market, switch, value, configured, state, condition, reason, action
            )
    return evidence


def _unconfigured_roots(
    settings: Mapping[str, object], profile: ConnectorProfile
) -> frozenset[str]:
    """The source's configuration blocks when the file carries none of them."""
    roots = profile.roots()
    if any(isinstance(settings, Mapping) and root in settings for root in roots):
        return frozenset()
    return roots


def _partial_reading(
    settings: Mapping[str, object], profile: ConnectorProfile, market: str
) -> tuple[str, object] | None:
    """The first promised part that delivers nothing, if any."""
    for part in profile.partials:
        reading = part.reading(settings, market)
        if reading is not None:
            return reading
    return None


# The estate state each Wave 1 route spec state stands for. The spec state is
# the connector's own word for the route, so the estate state is DERIVED from it
# rather than printed as a constant beside it: a spec state this map does not
# carry is a route the estate cannot place, which is a refusal and not a guess.
WAVE1_SPEC_STATES = {"blocked_fixture_unapproved": STATE_APPROVED_UNPROVEN}

# The route level switches, read through the same Switch the source level gates
# use so that one rule for a quoted boolean and for a present null covers both,
# and declared here rather than inline so they are held against the connector's
# own defaults alongside the source level gates.
ROUTE_SWITCHES: dict[tuple[str, str], Switch] = {
    ("gdelt", "fetch_events"): Switch(("gdelt.gdelt_events_enabled",)),
}


def _route_switch(
    settings: Mapping[str, object], source: str, market: str, route: str, module: object
) -> tuple[str, object, str] | None:
    """Route level switch as (path, reading, state) or None when the source switch rules."""
    if source == "socialcrawl":
        wave1 = getattr(module, "WAVE1_ROUTE_SPECS", {})
        if route in wave1:
            declared = str(wave1[route].state)
            state = WAVE1_SPEC_STATES.get(declared)
            if state is None:
                raise ValueError(f"wave1_route_state_unknown:{route}:{declared}")
            return ("WAVE1_ROUTE_SPECS", wave1[route].state, state)
        phases = read_config_path(settings, "socialcrawl.phases", market=market)
        if isinstance(phases, list):
            on = route in phases
            return ("socialcrawl.phases", on, STATE_APPROVED_UNPROVEN if on else STATE_EXCLUDED)
        return None
    gate = ROUTE_SWITCHES.get((source, route))
    if gate is not None:
        path, reading, on = gate.read(settings, market)
        return (path, reading, STATE_APPROVED_UNPROVEN if on else STATE_EXCLUDED)
    return None


def route_evidence(
    config: Mapping[str, object] | None = None,
    connectors: Iterable[tuple[str, type]] | None = None,
) -> dict[tuple[str, str, str], dict[str, object]]:
    """Route records for every executable source, market and surface."""
    settings = _load_config(config)
    items = tuple(load_executable_connectors() if connectors is None else connectors)
    modules = {key: importlib.import_module(cls.__module__) for key, cls in items}
    inventory = executable_inventory(items)
    sources = configuration_evidence(settings, items)
    records: dict[tuple[str, str, str], dict[str, object]] = {}
    for (source, market), surfaces in inventory.items():
        profile = CONNECTOR_PROFILES[source]
        evidence = sources[(source, market)]
        for route in sorted(surfaces):
            state, action = evidence.state, evidence.next_owner_action
            condition = evidence.condition
            platform, budget = profile.platform, profile.budget_stage
            detail: dict[str, object] = {
                "switch": evidence.switch,
                "value": evidence.switch_value,
                "reason": evidence.reason,
            }
            route_switch = _route_switch(settings, source, market, route, modules[source])
            if route_switch is not None:
                path, reading, route_state = route_switch
                if path == "WAVE1_ROUTE_SPECS":
                    spec = modules[source].WAVE1_ROUTE_SPECS[route]
                    platform, budget = spec.channel_family, WAVE1_STAGE
                    detail["route_state"] = reading
                    if evidence.configured:
                        state = route_state
                        condition = str(reading)
                        action = (
                            "Retained response shape probe and normalization test before "
                            "activation; native route activation is out of scope."
                        )
                else:
                    detail["switch"], detail["value"] = path, reading
                    if evidence.configured:
                        state = route_state
                        if route_state == STATE_EXCLUDED:
                            condition = CONDITION_ROUTE_OFF
                            action = f"Switched off at {path}; leave off until a feature needs it."
            records[(source, market, route)] = {
                "vendor": profile.vendor,
                "account": profile.account,
                "platform": platform,
                "market": market,
                "feature": profile.feature,
                "required": profile.required,
                "state": state,
                "condition": condition,
                "capability_proof": None,
                "cadence": profile.cadence,
                "freshness": profile.freshness,
                "native_identity": profile.native_identity,
                "date_rule": profile.date_rule,
                "author_rule": profile.author_rule,
                "url_rule": profile.url_rule,
                "geography_method": profile.geography_method,
                "budget_stage": budget,
                "qualified_contribution": None,
                "next_owner_action": action,
                "evidence": detail,
            }
    return records


@dataclass(frozen=True, slots=True)
class CapabilityFinding:
    """One declared rollout capability against what the connector code reads."""

    capability: str
    declared: bool
    shipped_in_code: bool
    stages: tuple[str, ...]
    config_keys: tuple[str, ...]
    missing_config_keys: tuple[str, ...]

    @property
    def agrees(self) -> bool:
        return self.declared == self.shipped_in_code


@dataclass(frozen=True, slots=True)
class LadderIncrement:
    """One stage of the rollout ladder as an activation increment.

    ``activation_route`` is the vendor route the stage opens, declared by the
    ladder itself. A stage that names no route cannot be probed, so it cannot be
    activated: the activation request is refused rather than guessed at.
    """

    stage_id: str
    description: str
    requires_capability: str | None
    config_keys: tuple[str, ...]
    credits_per_day: int
    expected_rows_per_day: int
    volume_increment: bool
    capability_shipped: bool
    activation_route: str | None = None


def connector_cap_reads(module_path: Path = SOCIALCRAWL_MODULE) -> set[str]:
    """Configuration keys the connector actually reads, from its own syntax tree.

    A vendor catalog, a rollout stage and a comment can all name a key the code
    never reads. Only a literal ``caps.get("name")`` style read inside the connector
    counts as a shipped capability.
    """
    module = ast.parse(Path(module_path).read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(module):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in _CONFIGURATION_READERS
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)
    return keys


def _literal(node: ast.expr, constants: Mapping[str, object]) -> tuple[object, ...]:
    """The literal a default expression stands for, or () when it is not one."""
    if isinstance(node, ast.Constant):
        return (node.value,)
    if isinstance(node, ast.List) and not node.elts:
        return ([],)
    if isinstance(node, ast.Dict) and not node.keys:
        return ({},)
    if isinstance(node, ast.Name) and node.id in constants:
        return (constants[node.id],)
    return ()


def connector_read_defaults(module_path: Path) -> dict[str, tuple[object, ...]]:
    """Every default the connector reads a named configuration key with, from its own tree.

    "The connector defaults this off" is a claim about the connector's code, so
    it is read from the connector's syntax tree rather than copied into a comment
    beside the declaration. Only a ``<name>.get("key", <default>)`` whose default
    is a literal, or a module level constant bound to one, is reported: a
    computed default is not a default a declaration can be held to.

    The value is the tuple of DISTINCT defaults the module reads that key with,
    so a key two call sites disagree about is visible as the disagreement it is
    rather than resolved silently in favour of whichever came first.
    """
    module = ast.parse(Path(module_path).read_text(encoding="utf-8"))
    constants: dict[str, object] = {}
    for node in module.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
                constants[target.id] = node.value.value
    defaults: dict[str, list[object]] = {}
    for node in ast.walk(module):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and len(node.args) == 2
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            continue
        literal = _literal(node.args[1], constants)
        if not literal:
            continue
        seen = defaults.setdefault(node.args[0].value, [])
        if not any(value is literal[0] or value == literal[0] for value in seen):
            seen.append(literal[0])
    return {key: tuple(values) for key, values in sorted(defaults.items())}


_IDIOM_CALLS = {"bool": "truthy", "list": "truthy", "tuple": "truthy", "int": "count"}


def _idiom_of(node: ast.expr, parents: Mapping[int, ast.AST]) -> str | None:
    """The Switch mode the connector's own use of a ``.get`` reading amounts to."""
    parent = parents.get(id(node))
    if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name):
        return _IDIOM_CALLS.get(parent.func.id)
    if isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not):
        return "truthy"
    if isinstance(parent, ast.Compare) and len(parent.ops) == 1 and parent.left is node:
        other = parent.comparators[0]
        if not isinstance(other, ast.Constant) or not isinstance(other.value, bool):
            return None
        if isinstance(parent.ops[0], ast.Is):
            return "unless_false" if other.value is False else "true_only"
        if isinstance(parent.ops[0], ast.IsNot):
            return "true_only" if other.value is True else None
    return None


def connector_read_idioms(module_path: Path) -> dict[str, tuple[str, ...]]:
    """The Switch mode each configuration read amounts to, from the connector's own tree.

    A declared default is half the claim; the other half is the RULE the connector
    applies to what it read. ``not cfg.get("active", True)`` and
    ``cfg.get("active", True) is False`` share a default and disagree about a
    present null, a zero and an empty list, so a declaration that names the wrong
    one reads a switched off source as running. The idiom is taken from how the
    connector uses the reading: wrapped in ``bool``, ``int``, ``list`` or ``not``,
    or compared against True or False with ``is``. A use this cannot read reports
    nothing rather than a guess.
    """
    module = ast.parse(Path(module_path).read_text(encoding="utf-8"))
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(module):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    idioms: dict[str, set[str]] = {}
    for node in ast.walk(module):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and len(node.args) == 2
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            continue
        idiom = _idiom_of(node, parents)
        if idiom is not None:
            idioms.setdefault(node.args[0].value, set()).add(idiom)
    return {key: tuple(sorted(values)) for key, values in sorted(idioms.items())}


def _load_rollout(rollout: Mapping[str, object] | None) -> Mapping[str, object]:
    return load_yaml(ROLLOUT_CONFIG_PATH) if rollout is None else rollout


def _config_leaf(key: str) -> str:
    return key.rsplit(".", 1)[-1]


def _stage_keys(stage: Mapping[str, object]) -> tuple[str, ...]:
    delta = stage.get("config_delta") or {}
    if not isinstance(delta, Mapping):
        raise ValueError(f"rollout_stage_delta_invalid:{stage.get('id')}")
    return tuple(sorted(str(key) for key in delta))


def rollout_capability_reconciliation(
    rollout: Mapping[str, object] | None = None,
    module_path: Path = SOCIALCRAWL_MODULE,
) -> dict[str, CapabilityFinding]:
    """Reconcile each declared rollout capability against the connector's own reads."""
    config = _load_rollout(rollout)
    declared = config.get("capabilities") or {}
    stages = config.get("stages") or []
    reads = connector_cap_reads(module_path)
    findings: dict[str, CapabilityFinding] = {}
    for capability, claim in sorted(declared.items()):
        named = [stage for stage in stages if stage.get("requires_capability") == capability]
        keys: tuple[str, ...] = tuple(
            sorted({key for stage in named for key in _stage_keys(stage)})
        )
        missing = tuple(key for key in keys if _config_leaf(key) not in reads)
        findings[capability] = CapabilityFinding(
            capability=capability,
            declared=claim is True,
            shipped_in_code=bool(named) and not missing,
            stages=tuple(str(stage["id"]) for stage in named),
            config_keys=keys,
            missing_config_keys=missing,
        )
    return findings


def ladder_increments(
    rollout: Mapping[str, object] | None = None,
    module_path: Path = SOCIALCRAWL_MODULE,
) -> tuple[LadderIncrement, ...]:
    """Read the rollout ladder as increments, in the order the file declares them."""
    config = _load_rollout(rollout)
    findings = rollout_capability_reconciliation(config, module_path)
    reads = connector_cap_reads(module_path)
    increments = []
    for stage in config.get("stages") or []:
        capability = stage.get("requires_capability")
        keys = _stage_keys(stage)
        if capability is None:
            shipped = bool(keys) and all(_config_leaf(key) in reads for key in keys)
        else:
            finding = findings.get(capability)
            shipped = finding is not None and finding.shipped_in_code
        rows = int(stage.get("expect_rows_per_day", 0))
        increments.append(
            LadderIncrement(
                stage_id=str(stage["id"]),
                description=str(stage.get("description", "")),
                requires_capability=capability,
                config_keys=keys,
                credits_per_day=int(stage.get("credits_per_day", 0)),
                expected_rows_per_day=rows,
                volume_increment=rows > 0,
                capability_shipped=shipped,
                activation_route=(
                    str(stage["activation_route"]) if stage.get("activation_route") else None
                ),
            )
        )
    return tuple(increments)


def pending_quality_stages(
    increments: Iterable[LadderIncrement], *, activated: Iterable[str]
) -> tuple[str, ...]:
    """Quality increments, the ones that buy accuracy rather than rows, still unactivated."""
    done = set(activated)
    return tuple(
        item.stage_id
        for item in increments
        if not item.volume_increment and item.stage_id not in done
    )


# Each control reads its own key. ``max_stages_per_day`` used to stand in for
# ``max_increments_open``, and they are different quantities: one change a day is
# a rate, one increment open at a time is a count of increments in flight. A
# control that borrows a neighbour's key moves when that neighbour moves.
_ACTIVATION_CONTROLS = (
    ("min_row_integrity_pct", "min_row_integrity_pct"),
    ("min_probe_observations", "min_probe_observations"),
    ("clean_crons_required", "clean_crons_required"),
    ("burn_overrun_pct", "burn_overrun_pct"),
    ("max_increments_open", "max_increments_open"),
    ("quality_before_volume", "quality_before_volume"),
)


def activation_policy(rollout: Mapping[str, object] | None = None) -> ActivationPolicy:
    """Build the activation controls from the rollout policy, refusing a missing control."""
    policy = _load_rollout(rollout).get("policy") or {}
    values: dict[str, object] = {}
    for field, key in _ACTIVATION_CONTROLS:
        if key not in policy:
            raise ValueError(f"activation_policy_missing:{key}")
        values[field] = policy[key]
    return ActivationPolicy(**values)


def route_activation_request(
    increment: LadderIncrement,
    *,
    market: str,
    route_state: str,
    clean_run_ids: Iterable[str],
    burn_overrun_pct: int,
    increments_open: int,
    probe: object,
    funded_gate: object,
    activated: Iterable[str] = (),
    increments: Iterable[LadderIncrement] | None = None,
    policy: ActivationPolicy | None = None,
) -> RouteActivationRequest | None:
    """Build the activation request for one ladder increment, deriving what can be derived.

    The credits, the kind of increment, whether the capability is shipped in the
    connector and which quality increments are still pending are read from the ladder
    and from the connector's own syntax tree, so the caller cannot assert them. The
    caller supplies only what it observed: the market, the route state, the named clean
    runs, the measured burn, the retained probe and the funded lane gate.

    None means the increment names no vendor route, which is a refusal to build a
    request rather than a request that happens to pass.
    """
    if increment.activation_route is None:
        return None
    ladder = tuple(ladder_increments() if increments is None else increments)
    return RouteActivationRequest(
        route=increment.activation_route,
        stage_id=increment.stage_id,
        market=market,
        route_state=route_state,
        increment_credits=increment.credits_per_day,
        volume_increment=increment.volume_increment,
        pending_quality_stages=pending_quality_stages(ladder, activated=activated),
        increments_open=increments_open,
        capability_shipped=increment.capability_shipped,
        clean_run_ids=tuple(clean_run_ids),
        burn_overrun_pct=burn_overrun_pct,
        probe=probe,
        funded_gate=funded_gate,
        policy=activation_policy() if policy is None else policy,
    )


def unregistered_connector_modules(
    package_path: Path = CONNECTOR_PACKAGE_PATH,
    connectors: Iterable[tuple[str, type]] | None = None,
) -> tuple[str, ...]:
    """Connector modules that exist in the tree and that the producer registry does not run.

    Read from the package directory and the registry rather than from a list, so a
    connector that is retired, or added and never registered, is named by the estate
    instead of being left for a reader to notice. A class that only subclasses the
    abstract base is a connector; the abstract base itself is not.
    """
    registered = {
        key for key, _ in (load_executable_connectors() if connectors is None else connectors)
    }
    found: set[str] = set()
    for module_path in sorted(Path(package_path).glob("*.py")):
        if module_path.name == "__init__.py":
            continue
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {base.id for base in node.bases if isinstance(base, ast.Name)}
            if not any(name.endswith("Connector") for name in bases):
                continue
            if any(
                isinstance(item, ast.FunctionDef)
                and (item.name == "fetch" or item.name.startswith("fetch_"))
                for item in node.body
            ):
                found.add(module_path.stem)
    return tuple(sorted(found - registered))


def configuration_files_naming(name: str, configs_path: Path = CONFIGS_PATH) -> tuple[str, ...]:
    """Configuration files that still carry ``name`` as a key, read from the files."""
    naming: set[str] = set()
    for path in sorted(Path(configs_path).glob("*.yaml")):
        document = load_yaml(path)
        if isinstance(document, Mapping) and _names_key(document, name):
            naming.add(path.name)
    return tuple(sorted(naming))


def _names_key(node: object, name: str) -> bool:
    if isinstance(node, Mapping):
        return name in node or any(_names_key(value, name) for value in node.values())
    if isinstance(node, list):
        return any(_names_key(item, name) for item in node)
    return False


def staging_source_estate(
    evidence_rows: Iterable[Mapping[str, object]] = (),
    config: Mapping[str, object] | None = None,
) -> tuple[tuple[dict[str, object], ...], str]:
    """Reconcile the live registry against the configuration and return rows plus digest."""
    connectors = load_executable_connectors()
    rows = reconcile_source_estate(
        executable_inventory(connectors), route_evidence(config, connectors), evidence_rows
    )
    return rows, policy_digest(rows)


DOC_HEADING = "# 42 staging source estate"
_STATE_LEGEND = (
    (
        STATE_UNAVAILABLE,
        "The route cannot execute: the vendor, the access or the credential is gone.",
    ),
    (
        STATE_EXCLUDED,
        "A ROUTE switched off by an operator decision that the configuration records.",
    ),
    (STATE_KNOWN_EMPTY, "The route executes and this market is expected to carry nothing."),
    (STATE_DEGRADED, "The route executes and part of its promised feature is switched off."),
    (STATE_APPROVED_UNPROVEN, "The route executes and no authorized run has proved it yet."),
    (
        STATE_QUALIFIED_LIVE,
        "Proven by an authorized run: a capability proof and a measured contribution.",
    ),
)


def _state_counts(rows: Iterable[Mapping[str, object]]) -> list[tuple[str, int]]:
    counts = {state: 0 for state, _text in _STATE_LEGEND}
    for row in rows:
        for route in row["routes"]:
            counts[route["state"]] += 1
    return [(state, counts[state]) for state, _text in _STATE_LEGEND]


def render_route_table(rows: Iterable[Mapping[str, object]]) -> str:
    """Every executable route with the state it is in and the condition behind it.

    Four different conditions print as one word in a state column: a retired vendor, a
    dormant account, credentials that have not landed and a source with no configuration
    at all all read as unavailable. The condition is the column that tells them apart,
    and this table is what the state counts count.
    """
    lines = ["| source | market | route | condition | state |", "|---|---|---|---|---|"]
    for row in rows:
        for route in row["routes"]:
            lines.append(
                f"| {row['source']} | {row['market']} | {route['route']} | "
                f"{route['condition']} | {route['state']} |"
            )
    return "\n".join(lines)


def source_estate_document(
    config: Mapping[str, object] | None = None, *, as_of: date | None = None
) -> str:
    """Render the operations record of the estate from the reconciliation itself.

    The document is generated, never typed: every line below is read from the producer
    registry, the connector modules, the shipped configuration and the rollout ladder, so
    a source, a route or a switch that moves and is not reconciled shows up as a drift.
    """
    rows, digest = staging_source_estate(config=config)
    blockers = feature_blockers(rows, as_of=date.today() if as_of is None else as_of)
    findings = rollout_capability_reconciliation()
    increments = ladder_increments()
    policy = activation_policy()
    lines = [
        DOC_HEADING,
        "",
        "Generated by `src.analysis.open_intelligence.source_inventory.source_estate_document`.",
        "Do not edit by hand: `tests/unit/test_source_inventory.py` fails when this file and the",
        "reconciliation disagree.",
        "",
        f"Policy digest: `{digest}`",
        "",
        "## States",
        "",
        "| state | meaning | routes |",
        "|---|---|---|",
    ]
    counts = dict(_state_counts(rows))
    lines.extend(f"| {state} | {text} | {counts[state]} |" for state, text in _STATE_LEGEND)
    lines.extend(
        [
            "",
            "The route count is a count of the lines in Routes below. The Estate table carries",
            "one line per source and market and prints the WEAKEST state among that row's routes,",
            "so a source with one dead route never reads as live and the two tables reconcile",
            "route by route.",
            "",
            "The state column understates a row whose weakest route is one route an operator",
            "switched off while the rest of the row runs: such a row prints intentionally_excluded",
            "without the SOURCE having been switched off. Every row in that position is named",
            "under Rows to read with their routes below, with the weakest state among the routes",
            "nobody excluded. Neither table answers for a single route; the Routes table does.",
            "",
            "## Estate",
            "",
            render_inventory_table(rows),
            "",
            "### Rows to read with their routes",
            "",
        ]
    )
    mixed = mixed_state_rows(rows)
    if mixed:
        lines.extend(
            [
                "| source | market | row state | weakest state in scope | routes excluded |",
                "|---|---|---|---|---|",
            ]
        )
        lines.extend(
            "| {source} | {market} | {state} | {in_scope} | {routes} |".format(
                source=item["source"],
                market=item["market"],
                state=item["state"],
                in_scope=item["in_scope_state"],
                routes=", ".join(item["excluded_routes"]),
            )
            for item in mixed
        )
    else:
        lines.append(
            "No row prints an operator route exclusion while a stronger route of the same row "
            "still runs, so every row state above is the state of the whole row."
        )
    lines.extend(
        [
            "",
            "## Routes",
            "",
            render_route_table(rows),
            "",
            "## Promised features",
            "",
        ]
    )
    if blockers:
        lines.extend(
            [
                "| feature | market | required | reason | providers |",
                "|---|---|---|---|---|",
            ]
        )
        lines.extend(
            "| {feature} | {market} | {required} | {reason} | {providers} |".format(
                feature=item.feature,
                market=item.market,
                required="yes" if item.required else "no",
                reason=item.reason,
                providers=", ".join(item.providers),
            )
            for item in blockers
        )
    else:
        lines.append("Every promised feature has a qualified live provider in every market.")
    lines.extend(
        [
            "",
            "A substitute clears a blocker only by reaching qualified_live with its own",
            "capability proof, so a substitute named in prose alone leaves the blocker standing.",
            "The proof names the source, the market and the feature it claims and is read back",
            f"from that reference, and it stands for {PROOF_VALIDITY_DAYS} days from the day it",
            "was observed. Every route of a provider that can execute in the market has to carry",
            "one: a single proven route of a many route source is one proven route.",
            "",
            "## Sources the registry does not run",
            "",
            "Connector modules that exist in the tree and that the producer registry does not",
            "name, with the configuration files that still carry them. Read from the package and",
            "the registry, so a retired connector is named here rather than left for a reader to",
            "wonder whether it was considered or forgotten.",
            "",
            "| module | in the producer registry | configuration files that still name it |",
            "|---|---|---|",
        ]
    )
    lines.extend(
        "| {module} | no | {files} |".format(
            module=module, files=", ".join(configuration_files_naming(module)) or "none"
        )
        for module in unregistered_connector_modules()
    )
    lines.extend(
        [
            "",
            "## Connector capabilities",
            "",
            "| capability | declared | shipped in code | stages | keys the code does not read |",
            "|---|---|---|---|---|",
        ]
    )
    lines.extend(
        "| {name} | {declared} | {shipped} | {stages} | {missing} |".format(
            name=finding.capability,
            declared="yes" if finding.declared else "no",
            shipped="yes" if finding.shipped_in_code else "no",
            stages=", ".join(finding.stages) or "none",
            missing=", ".join(finding.missing_config_keys) or "none",
        )
        for finding in findings.values()
    )
    lines.extend(
        [
            "",
            "## Activation ladder",
            "",
            f"Row integrity floor {policy.min_row_integrity_pct} percent over at least "
            f"{policy.min_probe_observations} probe observations, "
            f"{policy.clean_crons_required} distinct clean runs named by their run id, burn "
            f"within {policy.burn_overrun_pct} percent of the model, at most "
            f"{policy.max_increments_open} increment open at a time, "
            f"quality before volume {'on' if policy.quality_before_volume else 'off'}. The gate "
            "counts the named runs and holds them to being distinct; it does not check that "
            "they are consecutive, and the proposer that names them reads a window of days "
            "in which a day whose every run failed is a day that is not clean.",
            "",
            "| stage | kind | route it opens | credits per day | capability shipped |",
            "|---|---|---|---|---|",
        ]
    )
    lines.extend(
        "| {stage} | {kind} | {route} | {credits} | {shipped} |".format(
            stage=item.stage_id,
            kind="volume" if item.volume_increment else "quality",
            route=item.activation_route or "not declared",
            credits=item.credits_per_day,
            shipped="yes" if item.capability_shipped else "no",
        )
        for item in increments
    )
    lines.extend(
        [
            "",
            "WHAT THIS GATE IS, AND WHAT IT IS NOT. It refuses a PROPOSAL, not a charge.",
            "`scripts/socialcrawl_rollout.py` is a read only script that prints one proposed",
            "stage and the line `Apply by hand in configs/sources.yaml`; no cron, Makefile or",
            "workflow runs it, and nothing in this repository applies its proposal. A stage the",
            "gate refuses is a stage that is never printed. Spending is still prevented by the",
            "connector's own budget breaker and by the credits the vendor holds, not by this.",
            "",
            "The proposer proposes an increment only when",
            "`src.analysis.open_intelligence.source_policy.evaluate_route_activation` allows it:",
            "it builds the request through",
            "`src.analysis.open_intelligence.source_inventory.route_activation_request`, which",
            "derives the credits, the kind of increment, the shipped capability and the pending",
            "quality increments from this ladder and from the connector itself, and the gate",
            "refuses without a retained response shape probe whose reference names its own",
            "payload digest, a normalization digest the caller declares over that exact retained",
            "payload, the row integrity floor over enough observations, the named clean runs, an",
            "unopened increment and a funded lane gate reading that carries a measured balance.",
            "A missing balance refuses rather than assumes, so no proposal can rest on an",
            "assumed funding answer.",
            "",
            "The probe digests, the retained reference and the clean run ids are DECLARATIONS",
            "the caller records for audit. The gate checks that they are well shaped and that",
            "they agree with one another; it does not fetch the retained object, recompute a",
            "digest from it or resolve a run id against `pipeline_runs`. A reader who needs to",
            "know that a probe happened follows the retained reference; the gate cannot tell",
            "them. The proposer also reaches the gate only after its own budget gates have all",
            "passed, so on that path the four funding refusals and the clean run refusals",
            "cannot fire: they stand for callers other than this one.",
            "",
            "No probe has been retained for any route yet, so every increment is refused today",
            "with `probe_missing`.",
            "",
        ]
    )
    lines.extend(_unresolved_rows_section(rows))
    lines.extend(_SAFE_RECOVERY)
    return "\n".join(lines)


RECOVERY_RUNBOOK = "engine/docs/runbooks/discovery-release-recovery.md"
_SAFE_RECOVERY = (
    "## Safe recovery",
    "",
    "Rollback of a released discovery run is a selection read. It never edits evidence so",
    "that an older image accepts it, and it never deletes or changes a newer run, result or",
    "prediction row. The exact steps, every refusal code and the rows still open on the",
    f"release side are in `{RECOVERY_RUNBOOK}`; in short:",
    "",
    "1. Name the image the service is rolling back to, the run contract versions and the",
    "   source policy digests that image accepts. Never widen those sets to make a run fit.",
    "2. Call `read_rollback_selection` in",
    "   `engine/scripts/staging/release_open_intelligence_run.py` against staging. It reads",
    "   the release records, the run receipts and the approved profile registry and issues",
    "   nothing but SELECT statements; any other statement refuses with",
    "   `rollback_write_refused`.",
    "3. Accept only a selection. A refusal (`rollback_no_compatible_release`,",
    "   `rollback_current_release_unknown`, `rollback_record_invalid`,",
    "   `rollback_compatibility_invalid`) stops the recovery; it is never retried with a",
    "   wider set.",
    "4. The selection is not served. The served run is the newest enabled run receipt and",
    "   the release write refuses a run that is already released, so moving display to the",
    "   selected run is a native, reviewed step that the recovery runbook names as open.",
    "",
)


def _unresolved_rows_section(rows: Iterable[Mapping[str, object]]) -> list[str]:
    """Every source whose rows are not qualified live, read from the reconciliation."""
    by_source: dict[str, dict[str, set[str]]] = {}
    for row in rows:
        if row["state"] == STATE_QUALIFIED_LIVE:
            continue
        entry = by_source.setdefault(
            str(row["source"]), {"markets": set(), "states": set(), "conditions": set()}
        )
        entry["markets"].add(str(row["market"]))
        entry["states"].add(str(row["state"]))
        entry["conditions"].update(str(route["condition"]) for route in row["routes"])
    lines = [
        "## Unresolved rows",
        "",
        "Every source and market row that is not qualified_live, grouped by source, with the",
        "row states and the route conditions behind them. Read from the same reconciliation",
        "as the tables above, so a row that qualifies leaves this list without an edit.",
        "",
    ]
    if not by_source:
        lines.extend(["Every source and market row is qualified_live.", ""])
        return lines
    lines.extend(
        [
            "| source | markets | row states | route conditions |",
            "|---|---|---|---|",
        ]
    )
    lines.extend(
        "| {source} | {markets} | {states} | {conditions} |".format(
            source=source,
            markets=", ".join(sorted(entry["markets"])),
            states=", ".join(sorted(entry["states"])),
            conditions=", ".join(sorted(entry["conditions"])),
        )
        for source, entry in sorted(by_source.items())
    )
    lines.append("")
    return lines


__all__ = [
    "ABSENT",
    "CONFIGS_PATH",
    "CONNECTOR_PACKAGE_PATH",
    "CONNECTOR_PROFILES",
    "LIFECYCLE_STATES",
    "ROLLOUT_CONFIG_PATH",
    "ROUTE_SWITCHES",
    "SOCIALCRAWL_MODULE",
    "SOURCES_CONFIG_PATH",
    "SOURCE_ESTATE_DOC",
    "STATE_APPROVED_UNPROVEN",
    "STATE_DEGRADED",
    "STATE_EXCLUDED",
    "STATE_KNOWN_EMPTY",
    "STATE_QUALIFIED_LIVE",
    "STATE_UNAVAILABLE",
    "WAVE1_SPEC_STATES",
    "CapabilityFinding",
    "LadderIncrement",
    "PartialFeature",
    "SourceEvidence",
    "Switch",
    "activation_policy",
    "configuration_evidence",
    "configuration_files_naming",
    "connector_cap_reads",
    "connector_read_defaults",
    "connector_read_idioms",
    "dispatcher_surfaces",
    "executable_inventory",
    "flag_reads_on",
    "ladder_increments",
    "pending_quality_stages",
    "read_config_path",
    "read_config_reading",
    "render_route_table",
    "rollout_capability_reconciliation",
    "route_activation_request",
    "route_evidence",
    "source_estate_document",
    "staging_source_estate",
    "unregistered_connector_modules",
]
