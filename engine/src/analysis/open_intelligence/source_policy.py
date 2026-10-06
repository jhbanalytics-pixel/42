"""Executable source estate reconciliation for the staging source policy.

The executable inventory is derived from the producer's CONNECTORS registry
without importing the producer module. Importing that script loads dotenv,
mutates sys.path, configures root logging and pulls in the BigQuery client,
so the registry is read from the module's syntax tree instead and only the
connector modules it names are imported. A connector's fetch surfaces are its
public fetch entry points, which is the set of callables named ``fetch`` or
``fetch_<name>`` on the connector class. Disabled connectors keep their
surfaces because the policy must state a market status for every executable
route, not only the routes currently switched on.

``reconcile_source_policy`` is the reviewed kernel and is kept verbatim. The
estate builder around it (``build_policy_rows`` and ``reconcile_source_estate``)
turns the executable inventory plus route evidence into one policy row per
source and market, with one route record per executable surface carrying the
vendor, account, platform, feature, lifecycle state, proof reference and owner
action fields. ``policy_digest`` is a content digest of those policy fields.

Nothing in the tree consumes that digest yet, and saying otherwise would be a claim
no code path can make true. The collection receipt takes its ``policy_sha256`` from
the ``COLLECTION_POLICY_SHA256`` environment variable, and no reader compares that
value with the digest computed here, so the two can disagree silently. The handoff
closes when the producer stamps this digest, or when an admission check compares the
stamped value against it; until then this is a reading of the estate and not a
binding on anything downstream.

``feature_blockers`` reads the reconciled rows back as promised features: a feature and
market that no source is proven to serve is reported as a blocker, and a substitute
clears it only by reaching qualified_live with its own capability proof.

``evaluate_route_activation`` is the gate in front of activating one route increment. It
spends nothing and reads nothing: the caller hands it the retained response shape probe,
the normalization taken over that exact retained payload, the ladder controls and the
funded lane gate it already evaluated. A missing gate, a refusing gate and a balance that
was never measured are three separate refusals, so no increment can open on an assumed
funding answer.
"""

from __future__ import annotations

import ast
import importlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.analysis.open_intelligence.brain_contract import canonical_digest

POLICY_MARKETS = ("za", "ng", "ke")
PRODUCER_PATH = Path(__file__).resolve().parents[3] / "scripts" / "run_rss_now.py"
_CONNECTOR_PACKAGE = "src.ingestion.connectors."

STATE_UNAVAILABLE = "unavailable"
STATE_EXCLUDED = "intentionally_excluded"
STATE_APPROVED_UNPROVEN = "approved_unproven"
STATE_KNOWN_EMPTY = "known_empty"
STATE_DEGRADED = "degraded"
STATE_QUALIFIED_LIVE = "qualified_live"
# Weakest first. A source row takes the WEAKEST state among its routes: the
# estate question per source and market is what a reader may rely on, and one
# dead route among live ones is exactly what a row state must not hide. The
# route level detail survives in the row's own routes and in the route table,
# and the row's in scope state carries the weakest state among the routes an
# operator has not deliberately excluded.
STATE_ORDER = (
    STATE_UNAVAILABLE,
    STATE_EXCLUDED,
    STATE_KNOWN_EMPTY,
    STATE_DEGRADED,
    STATE_APPROVED_UNPROVEN,
    STATE_QUALIFIED_LIVE,
)
LIFECYCLE_STATES = frozenset(STATE_ORDER)
ROUTE_FIELDS = (
    "route",
    "state",
    "condition",
    "live",
    "vendor",
    "account",
    "platform",
    "market",
    "feature",
    "required",
    "capability_proof",
    "cadence",
    "freshness",
    "native_identity",
    "date_rule",
    "author_rule",
    "url_rule",
    "geography_method",
    "budget_stage",
    "qualified_contribution",
    "next_owner_action",
    "evidence",
)
# The digest covers the policy fields only. The evidence reading (the switch
# path and the raw value read from the configuration) is excluded so that an
# operational edit such as a new feed URL or a query term does not move the
# digest while no lifecycle state, proof or owner action changed.
DIGEST_FIELDS = tuple(field for field in ROUTE_FIELDS if field != "evidence")
# The only fields an evidence row may override; everything else is derived
# from the connector profile and the configuration reading.
EVIDENCE_FIELDS = frozenset(
    {"state", "condition", "capability_proof", "qualified_contribution", "next_owner_action"}
)
_ROUTE_KEY = ("source", "market", "route")
_DIGEST_TEXT = re.compile(r"[0-9a-f]{64}")


def _is_count(value: object) -> bool:
    """A plain non-negative integer; a bool is never a count."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_credit(value: object) -> bool:
    """A counted, finite credit reading: an integer or an exact finite Decimal."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    return isinstance(value, Decimal) and value.is_finite() and value >= 0


PROOF_VALIDITY_DAYS = 90
_PROOF_SHAPE = "store:YYYY-MM-DD:source:market:feature"


@dataclass(frozen=True, slots=True)
class CapabilityProof:
    """A capability proof reference, read back into the claim it makes.

    The reference names the store the proof was read from, the day it was
    observed, and the source, market and feature it proves, so a proof of one
    market cannot clear another market and a proof of one feature cannot clear
    another feature. ``PROOF_VALIDITY_DAYS`` bounds how long the observation
    stands for: an estate is a reading of a live system, not an archive.
    """

    store: str
    observed_on: date
    source: str
    market: str
    feature: str

    def claims(self, *, source: str, market: str, feature: str) -> bool:
        return (self.source, self.market, self.feature) == (source, market, feature)

    def valid_on(self, as_of: date) -> bool:
        if not isinstance(as_of, date):
            raise ValueError("proof_as_of_invalid")
        elapsed = (as_of - self.observed_on).days
        return 0 <= elapsed <= PROOF_VALIDITY_DAYS


def parse_capability_proof(reference: object) -> CapabilityProof | None:
    """Read a capability proof reference, or None when it is not one.

    A proof is evidence, so it is parsed rather than trusted: a non empty string
    proves nothing by being non empty.
    """
    if not isinstance(reference, str):
        return None
    parts = reference.split(":")
    if len(parts) != 5 or any(not part.strip() for part in parts):
        return None
    store, observed, source, market, feature = (part.strip() for part in parts)
    try:
        observed_on = date.fromisoformat(observed)
    except ValueError:
        return None
    return CapabilityProof(store, observed_on, source, market, feature)


def reconcile_source_policy(executable_routes, rows):
    if not executable_routes:
        raise ValueError("inventory_empty")
    sources = {s for s, m in executable_routes}
    expected = {(s, m) for s in sources for m in ("za", "ng", "ke")}
    if set(executable_routes) != expected:
        raise ValueError("inventory_market_missing")
    actual = [(r["source"], r["market"]) for r in rows]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("estate_incomplete")
    for row in rows:
        routes = [r["route"] for r in row["routes"]]
        expected_routes = executable_routes[(row["source"], row["market"])]
        if len(routes) != len(set(routes)) or set(routes) != expected_routes:
            raise ValueError("route_inventory_mismatch")
        for route in row["routes"]:
            if route.get("live") and route.get("state") != "qualified_live":
                raise ValueError("unproven_live")
    return tuple(sorted(rows, key=lambda r: (r["source"], r["market"])))


def fetch_surfaces(connector_class: type) -> set[str]:
    """Return the public fetch entry points a connector class exposes."""
    if not isinstance(connector_class, type):
        raise ValueError("connector_class_invalid")
    surfaces = {
        name
        for name in dir(connector_class)
        if (name == "fetch" or name.startswith("fetch_"))
        and callable(getattr(connector_class, name, None))
    }
    if not surfaces:
        raise ValueError("connector_surface_missing")
    return surfaces


def executable_route_keys(
    connectors: Iterable[tuple[str, type]],
) -> dict[tuple[str, str], set[str]]:
    """Derive the executable route inventory from a CONNECTORS style registry."""
    items = tuple(connectors)
    keys = [item[0] for item in items]
    if any(not isinstance(key, str) or not key for key in keys):
        raise ValueError("connector_key_invalid")
    if len(keys) != len(set(keys)):
        raise ValueError("connector_key_duplicate")
    inventory: dict[tuple[str, str], set[str]] = {}
    for key, connector_class in items:
        surfaces = fetch_surfaces(connector_class)
        for market in POLICY_MARKETS:
            inventory[(key, market)] = set(surfaces)
    return inventory


def _connector_imports(module: ast.Module) -> dict[str, str]:
    imports: dict[str, str] = {}
    for node in module.body:
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not node.module.startswith(_CONNECTOR_PACKAGE):
            continue
        for alias in node.names:
            imports[alias.asname or alias.name] = node.module
    return imports


def _connector_registry(module: ast.Module) -> list[tuple[str, str]]:
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "CONNECTORS" for target in node.targets
        ):
            continue
        if not isinstance(node.value, ast.List):
            raise ValueError("producer_registry_shape")
        registry: list[tuple[str, str]] = []
        for element in node.value.elts:
            if (
                not isinstance(element, ast.Tuple)
                or len(element.elts) != 2
                or not isinstance(element.elts[0], ast.Constant)
                or not isinstance(element.elts[0].value, str)
                or not isinstance(element.elts[1], ast.Name)
            ):
                raise ValueError("producer_registry_shape")
            registry.append((element.elts[0].value, element.elts[1].id))
        return registry
    raise ValueError("producer_registry_missing")


def load_executable_connectors(producer_path: Path = PRODUCER_PATH) -> tuple[tuple[str, type], ...]:
    """Read CONNECTORS from the producer source and resolve each connector class."""
    module = ast.parse(Path(producer_path).read_text(encoding="utf-8"))
    imports = _connector_imports(module)
    resolved: list[tuple[str, type]] = []
    for key, class_name in _connector_registry(module):
        module_name = imports.get(class_name)
        if module_name is None:
            raise ValueError("producer_connector_import_missing")
        connector_class = getattr(importlib.import_module(module_name), class_name, None)
        if not isinstance(connector_class, type):
            raise ValueError("producer_connector_class_missing")
        resolved.append((key, connector_class))
    return tuple(resolved)


def _check_override(
    key: tuple[str, str, str],
    configured_state: object,
    override: Mapping[str, object],
    state: str,
) -> None:
    """Refuse an evidence row that moves a state without saying why it may.

    An evidence row is a reading of an authorized run, not an authority of its
    own. It may record what a run proved and it may record a route falling back,
    but it may never talk a route the configuration reads as dead into being
    available: approval comes from the configuration, and only a proof moves a
    route above it.
    """
    source, market, route = key
    if "condition" not in override:
        raise ValueError(f"evidence_override_condition_missing:{source}:{market}:{route}")
    action = override.get("next_owner_action")
    if not isinstance(action, str) or not action.strip():
        raise ValueError(f"evidence_override_unjustified:{source}:{market}:{route}")
    if configured_state not in LIFECYCLE_STATES:
        raise ValueError(f"state_unknown:{source}:{market}:{route}:{configured_state}")
    if STATE_ORDER.index(state) <= STATE_ORDER.index(str(configured_state)):
        return
    if state != STATE_QUALIFIED_LIVE:
        raise ValueError(f"evidence_promotion_unproven:{source}:{market}:{route}")
    if configured_state != STATE_APPROVED_UNPROVEN:
        raise ValueError(f"evidence_promotion_unexecutable:{source}:{market}:{route}")


def _check_qualified(key: tuple[str, str, str], record: Mapping[str, object]) -> None:
    """Refuse a qualified_live route whose proof or contribution is not evidence."""
    source, market, route = key
    proof = record["capability_proof"]
    contribution = record["qualified_contribution"]
    if not isinstance(proof, str) or not proof:
        raise ValueError(f"qualified_without_proof:{source}:{market}:{route}")
    # A contribution is a measured count of rows this route put into the estate.
    # Zero, minus five and "none measured" are each a reason to refuse the claim,
    # and None is one of them rather than a separate case.
    if not _is_count(contribution) or contribution < 1:
        raise ValueError(f"qualified_contribution_invalid:{source}:{market}:{route}")
    parsed = parse_capability_proof(proof)
    if parsed is None or not parsed.claims(
        source=source, market=market, feature=str(record["feature"])
    ):
        raise ValueError(f"qualified_proof_unbound:{source}:{market}:{route}:{_PROOF_SHAPE}")


def _route_record(
    key: tuple[str, str, str],
    evidence: Mapping[str, object],
    override: Mapping[str, object] | None,
) -> dict[str, object]:
    source, market, route = key
    record: dict[str, object] = {"route": route, **dict(evidence)}
    configured_state = record.get("state")
    if override:
        for field, value in override.items():
            if field in _ROUTE_KEY:
                continue
            if field not in EVIDENCE_FIELDS:
                raise ValueError(f"evidence_field_unknown:{field}")
            record[field] = value
    missing = [field for field in ROUTE_FIELDS if field not in record and field != "live"]
    if missing:
        raise ValueError(f"route_field_missing:{source}:{market}:{route}:{missing[0]}")
    state = record["state"]
    if state not in LIFECYCLE_STATES:
        raise ValueError(f"state_unknown:{source}:{market}:{route}:{state}")
    condition = record["condition"]
    if not isinstance(condition, str) or not condition.strip():
        raise ValueError(f"route_condition_missing:{source}:{market}:{route}")
    if record["market"] != market or record["route"] != route:
        raise ValueError(f"route_market_mismatch:{source}:{market}:{route}")
    if override and "state" in override and override["state"] != configured_state:
        _check_override(key, configured_state, override, str(state))
    if state == STATE_QUALIFIED_LIVE:
        _check_qualified(key, record)
    record["live"] = state == STATE_QUALIFIED_LIVE
    return {field: record[field] for field in ROUTE_FIELDS}


def build_policy_rows(
    executable_routes: Mapping[tuple[str, str], set[str]],
    route_evidence: Mapping[tuple[str, str, str], Mapping[str, object]],
    evidence_rows: Iterable[Mapping[str, object]] = (),
) -> list[dict[str, object]]:
    """Assemble one policy row per executable source and market from route evidence."""
    overrides: dict[tuple[str, str, str], Mapping[str, object]] = {}
    for item in evidence_rows:
        key = (item["source"], item["market"], item["route"])
        if key[:2] not in executable_routes or key[2] not in executable_routes[key[:2]]:
            raise ValueError(f"evidence_unmatched:{key[0]}:{key[1]}:{key[2]}")
        if key in overrides:
            raise ValueError(f"evidence_duplicate:{key[0]}:{key[1]}:{key[2]}")
        overrides[key] = item
    rows: list[dict[str, object]] = []
    for (source, market), surfaces in sorted(executable_routes.items()):
        routes = []
        for route in sorted(surfaces):
            key = (source, market, route)
            if key not in route_evidence:
                raise ValueError(f"route_evidence_missing:{source}:{market}:{route}")
            routes.append(_route_record(key, route_evidence[key], overrides.get(key)))
        state = min((item["state"] for item in routes), key=STATE_ORDER.index)
        # The row state is the weakest route state, full stop, so a dead route is
        # never hidden. On its own it understates in the other direction: the
        # weakest state of a source with one route an operator switched off and
        # fourteen live ones is intentionally_excluded, which a reader of the row
        # alone takes to mean the operator switched the SOURCE off. The in scope
        # state answers that reader in the same line: the weakest state among the
        # routes nobody deliberately excluded, equal to the row state whenever
        # none or all of the routes are excluded.
        in_scope = [item for item in routes if item["state"] != STATE_EXCLUDED]
        in_scope_state = min(
            (item["state"] for item in (in_scope or routes)), key=STATE_ORDER.index
        )
        rows.append(
            {
                "source": source,
                "market": market,
                "state": state,
                "in_scope_state": in_scope_state,
                "routes": routes,
            }
        )
    return rows


def reconcile_source_estate(
    executable_routes: Mapping[tuple[str, str], set[str]],
    route_evidence: Mapping[tuple[str, str, str], Mapping[str, object]],
    evidence_rows: Iterable[Mapping[str, object]] = (),
) -> tuple[dict[str, object], ...]:
    """Build the policy rows and pass them through the reviewed kernel."""
    rows = build_policy_rows(executable_routes, route_evidence, evidence_rows)
    return reconcile_source_policy(dict(executable_routes), rows)


def policy_digest(rows: Iterable[Mapping[str, object]]) -> str:
    """Content digest of the policy fields of reconciled rows.

    It has no consumer in the tree today: see the module docstring for what the
    handoff still needs before a downstream task can be said to read it.
    """
    ordered = []
    for row in rows:
        if not isinstance(row, Mapping) or not {"source", "market", "state", "routes"} <= set(row):
            raise ValueError("policy_rows_invalid")
        ordered.append(
            {
                "source": row["source"],
                "market": row["market"],
                "state": row["state"],
                "routes": [
                    {field: route[field] for field in DIGEST_FIELDS} for route in row["routes"]
                ],
            }
        )
    if not ordered:
        raise ValueError("policy_rows_invalid")
    return canonical_digest(ordered)


def render_inventory_table(rows: Iterable[Mapping[str, object]]) -> str:
    """Render the reconciled estate as a markdown table, one line per source and market.

    The state column is the weakest route state. On its own it understates a row
    whose weakest route is one route an operator switched off while the rest of
    the row runs, so the document renders ``mixed_state_rows`` beside this table
    and a reader is never left with the row state alone.
    """
    lines = ["| source | market | surfaces | state |", "|---|---|---|---|"]
    for row in rows:
        surfaces = ", ".join(route["route"] for route in row["routes"])
        lines.append(f"| {row['source']} | {row['market']} | {surfaces} | {row['state']} |")
    return "\n".join(lines)


def mixed_state_rows(
    rows: Iterable[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    """Rows whose state is an operator route exclusion while a stronger route still runs.

    An Estate row that prints intentionally_excluded reads as "an operator
    switched this source off", and for a row with one excluded route among live
    ones that is the wrong answer in front of a client. The row keeps the weakest
    route state, because a dead route may never be hidden; these rows are the
    ones where that reading needs the routes behind it, and they carry the
    weakest state among the routes nobody deliberately excluded.
    """
    mixed = []
    for row in rows:
        in_scope = row.get("in_scope_state", row["state"])
        if in_scope == row["state"]:
            continue
        mixed.append(
            {
                "source": row["source"],
                "market": row["market"],
                "state": row["state"],
                "in_scope_state": in_scope,
                "excluded_routes": tuple(
                    str(route["route"])
                    for route in row["routes"]
                    if route["state"] == STATE_EXCLUDED
                ),
            }
        )
    return tuple(mixed)


@dataclass(frozen=True, slots=True)
class FeatureBlocker:
    """One promised feature and market that no source is proven to serve."""

    feature: str
    market: str
    required: bool
    reason: str
    providers: tuple[str, ...]
    next_owner_action: str


BLOCKER_NO_PROVIDER = "no_provider"
BLOCKER_UNPROVEN = "unproven"
BLOCKER_PROOF_EXPIRED = "proof_expired"
_SERVED_NOWHERE = frozenset({STATE_UNAVAILABLE, STATE_EXCLUDED, STATE_KNOWN_EMPTY})
_NO_PROVIDER_ACTION = (
    "Every route of this feature is unavailable, excluded or known empty in this market. "
    "Keep the feature blocked until a substitute source reaches qualified_live with its own "
    "capability proof, or record the feature as withdrawn."
)
_UNPROVEN_ACTION = (
    "A route of this feature can execute in this market but not every executable route of one "
    "provider is qualified_live with a capability proof bound to this market and feature. "
    "Attach the proof from an authorized run before the feature is promised."
)
_EXPIRED_ACTION = (
    f"The capability proof offered for this feature and market is older than "
    f"{PROOF_VALIDITY_DAYS} days. Re-run the authorized probe and attach a current proof, or "
    "record the feature as withdrawn."
)


def feature_blockers(
    rows: Iterable[Mapping[str, object]], *, as_of: date
) -> tuple[FeatureBlocker, ...]:
    """Report every promised feature and market that no source is proven to serve.

    A substitute is expressed by sharing the feature name: apple_music and spotify both
    promise music charts, so a market is served when either of them is qualified_live and
    carries a capability proof. The proof is read rather than counted: it must name this
    source, this market and this feature, and it must have been observed inside
    ``PROOF_VALIDITY_DAYS`` of ``as_of``, so a proof of another market, a proof of another
    feature and a proof from three years ago each leave the blocker standing.

    A provider serves the feature only when every route of its own that can execute in
    that market is proven. One proven route of a fifteen route source is one proven route,
    not a proven feature.
    """
    if not isinstance(as_of, date):
        raise ValueError("blockers_as_of_invalid")
    claims: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        source = row["source"]
        for route in row["routes"]:
            key = (route["feature"], route["market"])
            claim = claims.setdefault(
                key,
                {
                    "providers": set(),
                    "required": False,
                    "executable": {},
                    "proven": {},
                    "expired": set(),
                },
            )
            claim["providers"].add(source)
            claim["required"] = bool(claim["required"] or route["required"])
            if route["state"] in _SERVED_NOWHERE:
                continue
            claim["executable"].setdefault(source, set()).add(route["route"])
            if route["state"] != STATE_QUALIFIED_LIVE:
                continue
            proof = parse_capability_proof(route["capability_proof"])
            if proof is None or not proof.claims(
                source=source, market=route["market"], feature=route["feature"]
            ):
                continue
            if proof.valid_on(as_of):
                claim["proven"].setdefault(source, set()).add(route["route"])
            else:
                claim["expired"].add(source)
    blockers = []
    for (feature, market), claim in sorted(claims.items()):
        executable = claim["executable"]
        proven = claim["proven"]
        if any(
            routes and proven.get(source, set()) >= routes for source, routes in executable.items()
        ):
            continue
        if not executable:
            reason, action = BLOCKER_NO_PROVIDER, _NO_PROVIDER_ACTION
        elif claim["expired"]:
            reason, action = BLOCKER_PROOF_EXPIRED, _EXPIRED_ACTION
        else:
            reason, action = BLOCKER_UNPROVEN, _UNPROVEN_ACTION
        blockers.append(
            FeatureBlocker(
                feature=feature,
                market=market,
                required=bool(claim["required"]),
                reason=reason,
                providers=tuple(sorted(claim["providers"])),
                next_owner_action=action,
            )
        )
    return tuple(blockers)


@dataclass(frozen=True, slots=True)
class ActivationPolicy:
    """The ladder controls an activation must satisfy, read from the rollout policy.

    Every field is required and each one is bounded, because a relaxed control is
    exactly how a route activates without the evidence the package demands.
    """

    min_row_integrity_pct: int
    min_probe_observations: int
    clean_crons_required: int
    burn_overrun_pct: int
    max_increments_open: int
    quality_before_volume: bool

    def __post_init__(self) -> None:
        if not _is_count(self.min_row_integrity_pct) or not 1 <= self.min_row_integrity_pct <= 100:
            raise ValueError("activation_policy_invalid:min_row_integrity_pct")
        if not _is_count(self.min_probe_observations) or self.min_probe_observations < 1:
            raise ValueError("activation_policy_invalid:min_probe_observations")
        if not _is_count(self.clean_crons_required) or self.clean_crons_required < 1:
            raise ValueError("activation_policy_invalid:clean_crons_required")
        if not _is_count(self.burn_overrun_pct):
            raise ValueError("activation_policy_invalid:burn_overrun_pct")
        if not _is_count(self.max_increments_open) or self.max_increments_open < 1:
            raise ValueError("activation_policy_invalid:max_increments_open")
        if type(self.quality_before_volume) is not bool:
            raise ValueError("activation_policy_invalid:quality_before_volume")


@dataclass(frozen=True, slots=True)
class RouteProbe:
    """A caller's DECLARATION about a retained response shape probe and its normalization run.

    Read this as a record for audit, not as a verification. Nothing here fetches the
    retained object, recomputes a digest from it or resolves the reference: the digests
    and the reference are strings the caller hands in, and what is enforced is that they
    agree with each other and are shaped like digests. ``payload_digest`` is the digest
    the caller says the retained response body has, ``normalizer_digest`` the digest the
    caller says the normalization test read, and ``retained_reference`` has to name the
    retained object BY ``payload_digest`` so a reader chasing the record fetches the
    object the digests describe rather than some other one. A caller that declares all
    three consistently and truthfully has recorded a probe; a caller that declares them
    consistently and falsely has recorded a false probe, and only the audit behind the
    reference can tell a reader which it is.
    """

    route: str
    retained_reference: str
    payload_digest: str
    normalizer_digest: str
    observations: int
    integrity_observations: int

    def __post_init__(self) -> None:
        if not isinstance(self.route, str) or not self.route:
            raise ValueError("probe_route_invalid")
        if not isinstance(self.retained_reference, str) or not self.retained_reference:
            raise ValueError("probe_reference_missing")
        for field in ("payload_digest", "normalizer_digest"):
            value = getattr(self, field)
            if not isinstance(value, str) or _DIGEST_TEXT.fullmatch(value) is None:
                raise ValueError(f"probe_digest_invalid:{field}")
        retained = self.retained_reference.rsplit("/", 1)[-1].split(".", 1)[0]
        if retained != self.payload_digest:
            raise ValueError("probe_reference_unbound")
        if not _is_count(self.observations):
            raise ValueError("probe_observations_invalid")
        if not _is_count(self.integrity_observations):
            raise ValueError("probe_integrity_invalid")
        if self.integrity_observations > self.observations:
            raise ValueError("probe_integrity_invalid")


FUNDED_GATE_FIELDS = ("allowed", "reasons", "current_balance", "run_allowance")


def is_funded_gate(gate: object) -> bool:
    """Whether an object is a funded lane gate reading rather than something shaped like one.

    The policy, the probe and the request all refuse an unreadable input in their own
    constructors. The funded gate is the input that decides whether money may be spent,
    so it is held to the same standard rather than read through attribute lookups with
    permissive defaults: a balance of "not measured", an allowed value of "no" and an
    infinite allowance are all refusals, not readings.
    """
    if any(not hasattr(gate, field) for field in FUNDED_GATE_FIELDS):
        return False
    if type(gate.allowed) is not bool:
        return False
    reasons = gate.reasons
    if not isinstance(reasons, tuple) or any(
        not isinstance(item, str) or not item for item in reasons
    ):
        return False
    balance = gate.current_balance
    if balance is not None and not _is_credit(balance):
        return False
    return _is_credit(gate.run_allowance)


@dataclass(frozen=True, slots=True)
class RouteActivationRequest:
    """One proposed route increment and the evidence offered for it."""

    route: str
    stage_id: str
    market: str
    route_state: str
    increment_credits: int
    volume_increment: bool
    pending_quality_stages: tuple[str, ...]
    increments_open: int
    capability_shipped: bool
    clean_run_ids: tuple[str, ...]
    burn_overrun_pct: int
    probe: RouteProbe | None
    funded_gate: object | None
    policy: ActivationPolicy

    def __post_init__(self) -> None:
        for field in ("route", "stage_id", "market"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"activation_request_invalid:{field}")
        if self.route_state not in LIFECYCLE_STATES:
            raise ValueError("activation_request_invalid:route_state")
        for field in ("increment_credits", "increments_open"):
            if not _is_count(getattr(self, field)):
                raise ValueError(f"activation_request_invalid:{field}")
        if not _is_count(self.burn_overrun_pct):
            raise ValueError("activation_request_invalid:burn_overrun_pct")
        for field in ("volume_increment", "capability_shipped"):
            if type(getattr(self, field)) is not bool:
                raise ValueError(f"activation_request_invalid:{field}")
        for field in ("pending_quality_stages", "clean_run_ids"):
            value = getattr(self, field)
            if not isinstance(value, tuple) or any(
                not isinstance(item, str) or not item for item in value
            ):
                raise ValueError(f"activation_request_invalid:{field}")
        if self.probe is not None and not isinstance(self.probe, RouteProbe):
            raise ValueError("activation_request_invalid:probe")
        if not isinstance(self.policy, ActivationPolicy):
            raise ValueError("activation_request_invalid:policy")
        if self.funded_gate is not None and not is_funded_gate(self.funded_gate):
            raise ValueError("activation_request_invalid:funded_gate")


@dataclass(frozen=True, slots=True)
class RouteActivationDecision:
    activate: bool
    reasons: tuple[str, ...]
    route: str
    stage_id: str
    market: str
    funded_reasons: tuple[str, ...]


def evaluate_route_activation(request: RouteActivationRequest) -> RouteActivationDecision:
    """Decide whether one approved route increment may be activated.

    Nothing here reads a balance, calls a vendor or spends: the caller hands in the
    funded lane gate it already evaluated, which is refused outright unless it is a
    readable gate. A missing gate, a gate that refuses, a balance that was never measured
    and a balance that is exhausted are four separate refusals, because an activation that
    assumed any of them would be spending money on an assumption.

    What this function VERIFIES is the shape and the internal consistency of what it is
    handed. The probe digests, the retained reference and the clean run ids are the
    caller's declarations, recorded for audit: no digest is recomputed from a retained
    object and no run id is resolved against ``pipeline_runs`` here, so a caller that
    declares three run ids that never ran is refused by nothing in this module. The
    clean run ids are held to being distinct and to reaching the required count; they
    are not held to being consecutive, and this function cannot see a day on which every
    run failed. Deciding to activate on this reading means deciding to trust the caller
    that assembled it.
    """
    if not isinstance(request, RouteActivationRequest):
        raise ValueError("activation_request_invalid")
    policy = request.policy
    reasons: set[str] = set()
    funded_reasons: tuple[str, ...] = ()

    if request.route_state == STATE_QUALIFIED_LIVE:
        reasons.add("route_already_live")
    elif request.route_state != STATE_APPROVED_UNPROVEN:
        reasons.add("route_not_approved")
    if not request.capability_shipped:
        reasons.add("capability_not_shipped")

    probe = request.probe
    if probe is None:
        reasons.add("probe_missing")
    else:
        if probe.route != request.route:
            reasons.add("probe_route_mismatch")
        if probe.normalizer_digest != probe.payload_digest:
            reasons.add("normalization_not_on_retained_payload")
        if probe.observations == 0:
            reasons.add("probe_observed_nothing")
        elif probe.observations < policy.min_probe_observations:
            reasons.add("probe_observations_short")
        elif probe.integrity_observations * 100 < probe.observations * policy.min_row_integrity_pct:
            reasons.add("row_integrity_below_floor")

    named_runs = set(request.clean_run_ids)
    if len(named_runs) != len(request.clean_run_ids):
        reasons.add("clean_runs_not_distinct")
    if len(named_runs) < policy.clean_crons_required:
        reasons.add("clean_runs_short")
    if request.burn_overrun_pct > policy.burn_overrun_pct:
        reasons.add("burn_over_model")
    if request.increments_open >= policy.max_increments_open:
        reasons.add("increment_already_open")
    if policy.quality_before_volume and request.volume_increment and request.pending_quality_stages:
        reasons.add("quality_increment_pending")
    if request.increment_credits == 0:
        reasons.add("increment_costs_nothing")

    gate = request.funded_gate
    if gate is None:
        reasons.add("funding_unproven")
    else:
        if gate.current_balance is None:
            reasons.add("balance_unmeasured")
        elif gate.current_balance <= 0:
            reasons.add("balance_exhausted")
        if not gate.allowed:
            reasons.add("funding_refused")
            funded_reasons = tuple(gate.reasons)
        if gate.run_allowance < request.increment_credits:
            reasons.add("allowance_below_increment")

    return RouteActivationDecision(
        activate=not reasons,
        reasons=tuple(sorted(reasons)),
        route=request.route,
        stage_id=request.stage_id,
        market=request.market,
        funded_reasons=funded_reasons,
    )
