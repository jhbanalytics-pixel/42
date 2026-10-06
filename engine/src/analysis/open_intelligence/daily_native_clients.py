"""Native bindings for the clients the daily stage handlers dispatch over.

``build_native_stage_clients`` fills the client seam ``build_stage_handlers`` validates
with the engine's own durable pieces. The collect stage and the shared infrastructure are
real: the wiring's clock, a read only ``DailyStore`` over the injected object client, the
collection profile the collect stage hands ``collect_staging``, the producer's collection
only terminal bound in process to a create only receipt ledger in the evidence bucket, the
source run relation the completed run validator reads, the frozen trusted generation, a
SQL callable over the injected BigQuery client and the live staging release profile of the
closed observation day. The capture client is real: ``NativeCapture`` runs the snapshot
capture runner's validated body once per business attempt through the runner seam, keeps
the runner's own result as a create only readback object per attempt, and exposes the
capture registry as create only objects under generation preconditions, the writer the
D03 admission kernel consumes. The certifier is ``daily_certification.NativeCertifier``
over the release script's readers and a create only certification record store.

The composer and persist clients bind over ``compose_clients``, and only over a compose
binding a caller hands in: the reader that resolves an admitted capture entry to its
source window, and the principal, instant and rule version of the approval that admits
the daily composition rules. This module creates none of them and carries no default for
them, because no native authorization for a paid composition exists. Left unbound, which
is what every caller does today, they are explicit ``UnboundClient`` instances that carry
the attribute names the validator requires and refuse every call or attribute use with
``client_unbound:<name>``, and the guard refuses the compose stage before the handler
reaches either of them, so nothing paid can run through them.

``guarded_stage_handlers`` wraps the bound handlers so a stage whose clients are unbound
refuses before its handler runs. The handler would otherwise reach the unbound client
through its native call boundary and record the stage as unknown, which the kernel never
retries; the guard records the same refusal as a retry safe failure naming the code. A
bound client can carry its own pre dispatch refusal the same way: the collect stage
refuses a build the runtime never bound and a warehouse target that is not the staging
source dataset. The capture runner seam is bound by default: the capture handler hands
``snapshot`` the execution authority the daily authority issued for the attempt (the
issued authority object and its consumption, under the v2 capture origin with mode
``new_consume``), and the seam reads the attempt's input artifacts from the source
capture objects by the digests that authority's approval names, so the stage refuses
before dispatch only when the receipt carries no such authority.

The collector hands the producer its exact execution authority in process, built per call
from the business attempt id the collect stage keys the ledger with, the runtime's own
build facts (the source commit the image was built from and the job's image digest) and
the two digests the collection profile carries, so a retried attempt under another Cloud
Run execution reconciles to the same receipt and the producer reads no identity from the
environment.

That in process collector writes the staging source dataset as the daily identity, which
holds only read there. ``ingest_collector`` is the public entry point a collect step
calls instead: it runs the ingest job and reads its receipt back; see its docstring.

Nothing here opens a connection or constructs a cloud client. The producer module is
imported only when the collector runs, and the release script only when the warehouse
callable or the release profile is built.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256

from scripts.staging.collect_42_sources import (
    BOOTSTRAP_MARKETS,
    STAGING_ENVIRONMENT,
    STAGING_SOURCE_DATASET,
    VERIFIED_AUTHORITY,
    collection_receipt,
    entry_point_profile,
    producer_collector,
    reconciled_policy_digest,
)

from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.coverage_telemetry_sink import (
    BoundaryTelemetry,
    DisabledDispositionSink,
)
from src.analysis.open_intelligence.daily_certification import NativeCertifier
from src.analysis.open_intelligence.daily_cycle import validate_daily_profile
from src.analysis.open_intelligence.daily_stages import (
    CAPTURE_AUTHORITY_REFUSAL,
    CAPTURE_ORIGIN_MODE,
    CAPTURE_PRE_COVERAGE_READBACK_FIELDS,
    CAPTURE_READBACK_FIELDS,
    CLIENT_FIELDS,
    StageRefusal,
    build_stage_handlers,
)
from src.analysis.open_intelligence.daily_store import PreconditionFailed
from src.analysis.open_intelligence.execution_generations import active_generation
from src.analysis.open_intelligence.geographic_scope import bounded_name, bounded_text
from src.analysis.open_intelligence.ingest_receipts import (
    INGEST_EXECUTION,
    READ_TIMEOUT_SECONDS,
    ReceiptNotYetReadable,
    daily_ledger_copy,
    native_receipt_table,
    read_receipt_row,
    receipt_source_digest,
)
from src.analysis.open_intelligence.readiness import EXPLICIT_ORIGIN_POLICY, ReadinessRules

COLLECTION_PREFIX = "42/daily/collection"
SOURCE_RUN_PREFIX = "42/daily/source_runs"
CAPTURE_PREFIX = "42/daily/captures"
STORE_OWNER = "daily_stage_clients"
COLLECTION_PROFILE_CONTRACT = "collection_profile_daily_v1"
COLLECTION_POLICY_VARIABLE = "COLLECTION_POLICY_SHA256"
COLLECTION_PROFILE_VARIABLE = "COLLECTION_PROFILE_SHA256"
COLLECTION_TARGET_REFUSAL = "collection_target_not_staging"
COLLECTION_BUILD_REFUSAL = "collection_build_unbound"
INGEST_RECEIPT_REFUSAL = "collection_ingest_receipt_mismatch"
INGEST_DISPATCH_REFUSAL = "collection_ingest_dispatch_forbidden"
INGEST_RUN_REFUSAL = "collection_ingest_dispatch_refused"
INGEST_POLICY_REFUSAL = "collection_ingest_policy_unstamped"
INGEST_PROFILE_REFUSAL = "collection_ingest_profile_unstamped"
INGEST_JOB_NAME = (
    "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-ingest-staging"
)
INGEST_OPERATION_PREFIX = "projects/ogilvy-trends-v2/locations/us-central1/operations/"
# The ingest job's task timeout is 2700 seconds. The sleeps alone, (95 - 1) x 30 = 2820
# seconds, outlast it; with the run request (30 s) and every read (95 x 5 s) the whole
# dispatch is bounded by 3325 seconds, inside the daily job's 3600 second task timeout.
INGEST_POLLS = 95
INGEST_POLL_INTERVAL_SECONDS = 30
INGEST_RUN_TIMEOUT_SECONDS = 30
INGEST_TIME_BUDGET_SECONDS = (
    (INGEST_POLLS - 1) * INGEST_POLL_INTERVAL_SECONDS
    + INGEST_POLLS * READ_TIMEOUT_SECONDS
    + INGEST_RUN_TIMEOUT_SECONDS
)
CAPTURE_RUNNER_REFUSAL = "capture_runner_unbound"
CAPTURE_MODE = "initial"
CAPTURE_RESULT_SUFFIX = "#source-snapshot"
# The readiness geo confidence floor of the daily chain, bound into the rules the composer
# hands ``build_producing_batch`` and the certifier re-evaluates under. The daily composer
# resolves every evidence row through the geographic scope kernel: a local row carries the
# deciding method's own confidence, a measured contextual or foreign row carries zero
# because the behaviour is evidenced elsewhere, and an unresolved row never reaches
# readiness at all, being withheld at component level with ``geo_scope_unknown``.
# What the daily chain can actually produce, read off ``daily_composer._resolve_geography``
# and not off the tier table. Two methods decide local here. The retained geography receipt
# at 0.9 decides it for a row that carries a measured geography method and receipt; the
# socialcrawl search's own market is not one, it is a contextual search frame. The vendor
# region at 0.6 decides it for a row whose source the reviewed source inventory records as
# measuring the requested country's own people (a storefront chart, a country partition of
# search interest, trending searches by geo, top pageviews per country), cited by the row
# the vendor answered; see ``VENDOR_MEASURED_SOURCES``. No evidence relation carries a
# vendor market column, so the region is always the row's own market and the vendor region
# decides local only. The blocklist at 0.95 and the script marker at 0.7 decide foreign,
# the content marker at 0.4 and the search frame at 0.3 decide contextual, and each of
# those carries zero. So the geo confidence a daily evidence row can carry is exactly
# {0.0, 0.6, 0.9}.
# The floor sits at 0.6, the weakest tier the kernel can decide a local scope by at all,
# so a vendor measured local row is admitted at exactly the floor by the inclusive
# comparison, and every measured contextual or foreign row is refused. A floor above 0.6
# would refuse every vendor measured row; a floor at 0.4 would sit under a tier that
# decides contextual only. It is also the value the replay chain's approved geo floors
# carry (``APPROVED_GEO_FLOORS``, approved 2026-09-03), though on a different scale.
# The floor is the second statement of the separation and not the first: what keeps a
# measured contextual or foreign row out of readiness is the composer zeroing its in
# market confidence, and the floor would not do it alone, since a foreign row decided by
# the blocklist carries the highest tier in the table.
# The value is written out rather than read off the kernel so a later change to a tier
# cannot move the floor without a test firing. The replay chain keeps its own floor at
# zero; see the note beside ``REPLAY_READINESS_GEO_FLOOR``.
DAILY_READINESS_GEO_FLOOR = 0.6
CAPTURE_ARTIFACT_TIMEOUT = 30
UNBOUND_CLIENTS = {
    "composer": (),
    "persist": (),
}
# What the runtime must hand over before a paid composition can be dispatched, and what
# this engine cannot derive for itself: the reader that resolves an admitted capture entry
# to its source window, and the principal, instant and rule version of the approval that
# admits the daily composition rules. Nothing here creates any of them.
COMPOSE_BINDING_FIELDS = ("source_window", "approved_by", "approved_at", "rule_version")
COMPOSE_BINDING_REFUSAL = "compose_binding_invalid"
COMPOSE_BUILD_REFUSAL = "compose_binding_unbuilt"
# An approval cannot be granted later than the observations it admits a composition of.
# The comparand is the observation cutoff, not the instant the composition runs at. The
# earlier lane compared against the instant, arguing that a cutoff comparison would refuse
# an honest approval granted on the morning of the run and citing this repository's own
# approval of 2026-09-03 over a window that closed on 2026-09-03. That precedent needs no
# deviation: the approval is at or before the close of its window, which the cutoff rule
# admits, and the comparison is inclusive. What the instant comparand admitted instead was
# the whole span from the cutoff to the run, which is a party reading the closed day's
# observations, choosing the rule version those observations suit, having it approved that
# morning and binding a composition of the day it has already read.
# What that costs, said plainly because whoever operates this will meet it: the cutoff is
# the midnight that closed the observation day, and the composition runs after it, so no
# approval granted on the day of the run can bind that run. An approval recorded at any
# point between the midnight cutoff and the composition itself now refuses
# ``compose_approval_not_yet_granted``. An approval for a given day's composition has to
# exist before that day's midnight, which in practice means approving the rule version the
# day before the run at the latest, not on the morning of it. This is a positive behaviour
# withdrawn rather than a weakness closed: the previous revision of this lane pinned an
# approval at the composition instant as binding, and this one refuses the identical input.
# It is withdrawn deliberately, because what the instant comparand bought was exactly the
# span described above.
COMPOSE_APPROVAL_REFUSAL = "compose_approval_not_yet_granted"
# The guard above is one sided without a lower bound, and the Unix epoch binds, which is
# what an unset or defaulted instant looks like once it has been made aware. No runtime
# value here derives one, so it is written out: no approval this repository records is
# earlier than it (the noncanary release approval of 2026-08-29 and the replay geo floor of
# 2026-09-03 are the two it holds), and no rule can admit a composition by an engine that
# did not exist when it was approved.
COMPOSE_APPROVAL_NOT_BEFORE = datetime(2026, 1, 1, tzinfo=UTC)
COMPOSE_APPROVAL_STALE_REFUSAL = "compose_approval_not_credible"
# ``compose_clients`` is exported and takes the cutoff and the instant as independent
# arguments, with nothing in the signature tying them together, so a caller that passed a
# cutoff of its own choosing would move the comparand above with it. The cutoff is derived
# from the instant by ``due_cutoff`` and the builder now says so.
COMPOSE_CUTOFF_REFUSAL = "compose_cutoff_underived"
# The reader that resolves an admitted capture entry to its source window. It carries its
# own refusal rather than the binding's, because ``NativeComposer`` refuses a non callable
# reader on its own account with the identical ``compose_binding_invalid`` string, which
# left this guard unobservable: it was deleted as a mutation with the whole suite passing.
# The earlier lane held a stack location in place of a distinct code because a distinct
# code would have changed a pinned expected result, which was circular, the same commit
# being the one that wrote that expectation.
COMPOSE_SOURCE_WINDOW_REFUSAL = "compose_source_window_uncallable"
CAPTURE_INSTANT_FIELDS = ("capture_available_at", "snapshot_as_of")
CAPTURE_ENTRY_INSTANT_FIELDS = (
    "observation_window_end",
    "collection_started_at",
    "collection_completed_at",
    "snapshot_as_of",
    "capture_available_at",
    "expires_at",
)
# The attempt a stored capture result was produced for, carried beside the readback fields
# so a reader can tell whether the object under a normalised name is the one it asked for.
# The readback field set itself carries no observation and no attempt identity, which is
# what left ``verified`` answering another attempt's result as this attempt's.
CAPTURE_RESULT_ATTEMPT_FIELD = "attempt_id"
CAPTURE_RESULT_ATTEMPT_REFUSAL = "capture_result_attempt_differs"
# The attempt field was added to a durable store that already held objects, and the store
# carries no version field, so the read has to say what an object written before it looks
# like rather than call it unparseable. A stored result whose field set is exactly the
# readback set is that object: it can still not be shown to belong to the attempt being
# asked about, so it is refused, but under a code of its own, which is what lets an
# operator tell a record written under the older rule from a corrupt one. Both attempt
# refusals are stage refusals, so the handler records them by their own code instead of
# the single unresolved string every other ValueError from a native call reaches it as.
CAPTURE_RESULT_LEGACY_REFUSAL = "capture_result_attempt_absent"
# The same shape on the collection receipt. A receipt written before the day index fix
# carries the raw execution id under the normalised name, so the payload this module now
# computes no longer equals the stored bytes and the create only comparison refused it.
# A stored receipt that differs only in that spelling is the same receipt and is
# tolerated; one that differs in anything else is still refused, under this code, so the
# refusal names the older rule rather than arriving as an ordinary conflict.
COLLECTION_RECEIPT_LEGACY_REFUSAL = "collection_receipt_legacy_id"
CAPTURE_VERIFICATION_FIELDS = frozenset(
    {"result_id", "image_digest", "generation", "schema_digest", "source_digest"}
)
# The clients each handler touches, in the order the handler reaches them.
STAGE_CLIENTS = {
    "collect": ("clock", "collection_profile", "collector", "collection_ledger", "source_runs"),
    "capture": ("clock", "store", "source_runs", "generation", "capture"),
    "compose": (
        "clock",
        "store",
        "capture",
        "composer",
        "persist",
        "warehouse",
        "release_profile",
        "products",
    ),
    "certify": ("store", "capture", "certifier", "warehouse", "release_profile", "products"),
    "release": ("clock", "store", "certifier", "warehouse", "release_profile", "products"),
}
# The client whose paid call each stage dispatches; its pre dispatch refusal guards the stage.
PRE_DISPATCH_CLIENTS = {
    "collect": ("collector",),
    "capture": ("capture",),
    "compose": ("products",),
}
# A field this module guards is a principal, an identifier, a digest, a day or a rule
# version, never prose. The bound and the character rules are ``geographic_scope``'s own,
# imported rather than restated, because that module guards fields of exactly the same
# shape and the two disagreeing is what the previous lane was closing when it introduced a
# second rule here. See the note beside ``bounded_text`` for what is refused and why.
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_IMAGE_DIGEST_URI = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}\Z")
_INSTANT_FIELDS = ("collection_started_at", "collection_completed_at")
_DAY_WRITE_ATTEMPTS = 3


class UnboundClient:
    """A client this runtime has not bound; every call and every attribute use refuses."""

    __slots__ = ("attributes", "code", "name")

    def __init__(self, name: str, attributes: tuple[str, ...] = ()) -> None:
        self.name = name
        self.code = f"client_unbound:{name}"
        self.attributes = tuple(attributes)

    def __getattr__(self, attribute: str):
        if attribute.startswith("__") and attribute.endswith("__"):
            raise AttributeError(attribute)
        if attribute in self.attributes:
            return self
        raise StageRefusal(self.code)

    def __call__(self, *args, **kwargs):
        raise StageRefusal(self.code)

    def __repr__(self) -> str:
        return f"UnboundClient({self.name!r})"


def _text(value: object, code: str) -> str:
    """One field of a record, as a bounded single line of honestly printable text.

    ``geographic_scope.bounded_text`` does the judging, so this module and the scope kernel
    cannot disagree about what such a field may contain: normalised to NFKC, stripped
    before it is judged so a field that is nothing but whitespace is empty rather than
    present, bounded in characters and in bytes, and refused outright for the character
    categories that let a value look like something it is not.

    The guard is applied at every call site of this module and not only at the four fields
    of the compose binding, deliberately. It is the same shape of field everywhere, and the
    alternative is a second weaker rule for the identifiers that key the object store,
    which is the disagreement this is closing rather than opening. Where the value goes on
    to build an object name, ``_name`` is used instead, which additionally refuses a
    relative traversal.

    Normalising does alias two spellings of one identifier onto one object name, since
    stripping and NFKC are both many to one, and that aliasing has to be judged against
    what each name is then written by. There are five such writers in this module, not the
    three an earlier statement of this claim named, and they are not all create only:

    * the collection receipt (``ObjectCollectionLedger.record``), create only, refusing a
      differing receipt under a name it already holds;
    * the source run record (``ObjectSourceRuns.record``), create only, refusing as
      existing;
    * the capture registry (``ObjectCaptureRegistry.create``), create only, answering
      ``exists`` for the admission kernel to read back;
    * the capture results store (``ObjectCaptureResults.record``), keyed by the normalised
      attempt id, create only, refusing a differing readback;
    * the day index (``ObjectCollectionLedger.record``), which is a read, append and
      replace under the generation it read, and is the one write here that is not a create.

    The four create only writers fail closed under the aliasing: two spellings reach one
    name and the second is refused rather than overwriting the first, whereas without
    normalising they would write two objects a reader cannot tell apart. That holds of a
    whole object name, and the capture registry's name is built from a composite key, so
    it held there only once the key was judged part by part: applying the rule to the
    joined ``<profile_id>/<result_id>`` stripped the outer edges of the whole string and
    let an interior spelling through, and four spellings of one result id created two
    objects, one of them named with a space in it. ``ObjectCaptureRegistry._name``
    normalises each part of the key now, and ``_verification`` uses the normalised value
    ``_text`` returns rather than discarding it, so the entry payload and the key it is
    stored under carry one spelling. The day index does not refuse, so it is the one place
    the aliasing has to be handled rather than merely tolerated: it holds at most one entry
    per execution id because ``record`` stores the normalised value and compares the
    normalised form of what each stored entry names, which is what keeps a second spelling
    from being appended beside the first, and what lets an entry written before the value
    was normalised be found rather than duplicated. The day index also carries no reader's
    decision on its own account: ``run_rss_now._collection_only`` unions the persisted
    markets it names, so a duplicate entry there is invisible to that reader and would
    only have shown as an object growing once per retry.

    Aliasing is not free even where it fails closed. A caller that hands a non canonical
    spelling reads its own record back under the canonical one, so a field that differs
    only by normalisation is answered as the other caller's record, not as absent. Every
    readback in this module is therefore compared against what the caller asked for, or
    refuses because it cannot be: see ``ObjectCaptureResults.read``.

    The decision still holds at all twenty call sites, and the corrected argument is what
    holds it. Seventeen were here before the readback comparison was added, two are that
    comparison's own, and the twentieth is the day index membership test's, which is why
    an earlier count of fourteen no longer matches anything. Eleven guard a value that
    never becomes an object name at all (the two collection digests, the stored instants
    of a source run and a capture entry, the receipt cutoff, the result id and generation
    of a verification, the execution id a stored day index entry names, the attempt id
    handed to the capture runner, the approver and rule version of the compose binding,
    and the environment), so aliasing cannot arise there and only the bound and the
    character rule apply. The other nine build a name, and for each of them the aliasing is
    answered where the name is written or read: four are create only and refuse a second
    spelling that carries anything different, the day index is deduplicated on the
    normalised value it stores, and every readback under one of these names is compared
    against the identity its caller asked for, or refuses because it cannot be. Nothing
    here is narrowed, because the weaker rule an exception would need is the disagreement
    this closes.
    """
    return bounded_text(value, code)


def _name(value: object, code: str) -> str:
    """One field of a record that goes on to build an object name, so it stays in its prefix."""
    return bounded_name(value, code)


def _aware(value: object, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(code)
    return value


def _unpack(raw: bytes, code: str) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(code) from error
    if not isinstance(value, dict):
        raise ValueError(code)
    return value


def due_cutoff(now: object) -> datetime:
    """The latest closed UTC cutoff at or before ``now``: that day's midnight."""
    current = _aware(now, "clock_invalid").astimezone(UTC)
    return current.replace(hour=0, minute=0, second=0, microsecond=0)


def manifest_digest(manifest: object) -> str:
    """The kernel's own digest of a stage manifest, over the same canonical bytes."""
    if not isinstance(manifest, Mapping):
        raise ValueError("stage_manifest_invalid")
    return sha256(canonical_bytes(manifest)).hexdigest()


def _recorded_id(entry: Mapping[str, object]) -> str | None:
    """The normalised execution id a stored day index entry names, or None if it names none.

    A read widening and nothing else: an entry written under the current rule already
    carries the normalised value and answers the same string. An entry written before it
    carries the raw spelling, and comparing that as stored is what let a second entry be
    appended beside it on every retry.
    """
    try:
        return _name(entry.get("execution_id"), "collection_receipt_invalid")
    except ValueError:
        return None


def _legacy_receipt(stored: bytes, payload: bytes, execution_id: str) -> None:
    """Tolerate a receipt written before the execution id was normalised, or refuse.

    The stored object is the same receipt under the older rule when it differs from the
    payload computed here in one field only, the execution id, and the spelling it carries
    normalises to the one this receipt is named by. Nothing is rewritten: the caller goes
    on to the day index, where the membership test finds the entry the older rule wrote.
    Anything else is a conflict, named for the older rule when the stored id is a second
    spelling of this one and left as the ordinary conflict otherwise.
    """
    try:
        value = _unpack(stored, "collection_receipt_conflict")
    except ValueError:
        raise ValueError("collection_receipt_conflict") from None
    raw = value.get("execution_id")
    if raw == execution_id or _recorded_id(value) != execution_id:
        raise ValueError("collection_receipt_conflict")
    if canonical_bytes({**value, "execution_id": execution_id}) != payload:
        raise ValueError(COLLECTION_RECEIPT_LEGACY_REFUSAL)


class ObjectCollectionLedger:
    """Collection receipts as create only objects, indexed by one object per market day.

    ``operation`` returns the receipt recorded under an execution id or None,
    ``market_day`` the receipts recorded for one ISO date, and ``record`` writes a receipt
    once: the same receipt again is a no op, a differing receipt under the same execution
    id refuses, and a creator that lands between the read and the create is judged by
    the same rule. The day index is appended through the object client's generation
    precondition, so two writers never lose each other's receipt.

    One execution id runs through all three: it names the receipt object, it keys the day
    index entry, and it is a field of the stored receipt. ``record`` normalises it once
    and stores the normalised value, so the three cannot disagree; see the note there.
    """

    def __init__(self, client, *, prefix: str = COLLECTION_PREFIX) -> None:
        self._client = client
        self._prefix = prefix

    def _receipt_name(self, execution_id: str) -> str:
        return f"{self._prefix}/receipts/{execution_id}.json"

    def _day_name(self, trend_date: str) -> str:
        return f"{self._prefix}/days/{trend_date}.json"

    def _day_index(self, trend_date: str) -> tuple[list[dict], int]:
        found = self._client.read(self._day_name(trend_date))
        if found is None:
            return [], 0
        raw, generation = found
        receipts = _unpack(raw, "collection_day_invalid").get("receipts")
        if not isinstance(receipts, list) or any(not isinstance(item, dict) for item in receipts):
            raise ValueError("collection_day_invalid")
        return [dict(item) for item in receipts], generation

    def _create(self, name: str, payload: bytes) -> tuple[bytes, int] | None:
        """Create the receipt object, or hand back what a concurrent creator landed."""
        try:
            self._client.write(name, payload, if_generation_match=0)
        except PreconditionFailed:
            found = self._client.read(name)
            if found is None:
                raise ValueError("collection_receipt_conflict") from None
            return found
        return None

    def operation(self, operation_id: str) -> dict | None:
        name = self._receipt_name(_name(operation_id, "collection_operation_invalid"))
        found = self._client.read(name)
        if found is None:
            return None
        return _unpack(found[0], "collection_receipt_invalid")

    def market_day(self, trend_date: str) -> list[dict]:
        receipts, _generation = self._day_index(_name(trend_date, "collection_day_invalid"))
        return receipts

    def record(self, receipt: Mapping[str, object], *, trend_date: str) -> None:
        recorded = dict(receipt)
        execution_id = _name(recorded.get("execution_id"), "collection_receipt_invalid")
        # The stored payload carries the normalised execution id, not the one handed in.
        # The receipt object's name is built from the normalised value, so a payload
        # carrying the raw one is a record that disagrees with its own key: a reader
        # opening ``receipts/<id>.json`` is answered a receipt naming a different
        # execution id. It also left the day index membership test below comparing a raw
        # value against a normalised one, so an execution id that is not already in NFKC
        # normal form was never found in the index and the identical receipt recorded
        # twice was neither refused nor deduplicated: a second copy was appended, once
        # per retry, and the day index object grew without bound. Normalising here is
        # what makes the comparison compare like with like. The alternative, storing and
        # comparing the raw value, would also have made the comparison consistent, and an
        # earlier statement of this note rejected it for a failure mode it does not have:
        # it was said to hold two index entries under one receipt object, and it does not,
        # because two spellings of one id name one receipt object and the create only
        # comparison on that object refuses the second before the day index is reached.
        # Measured over the canonical spelling and the three non canonical ones, recording
        # the same receipt twice gives one day index entry and two writes under either.
        # Two things do separate them, and neither is the index. The receipt object is
        # named from the normalised value, so a payload carrying the raw one is a record
        # that disagrees with its own key, and the alternative leaves that standing. And
        # over four different spellings of one id the alternative refuses three of them
        # with collection_receipt_conflict while this answers all four as the one receipt
        # they are, so the alternative is the harsher of the two on a live caller and the
        # cheaper of the two on what is already stored, which is the cost below.
        #
        # This one has two costs, and only the first was stated. A caller handing a non
        # canonical spelling reads its receipt back under the canonical one, which
        # ``run_rss_now._collection_only`` refuses as a conflicting duplicate rather than
        # skipping the market: loud and closed, where the duplicate was silent and open.
        # And a receipt already in the bucket, written under a non canonical execution id
        # at any earlier revision, no longer equals the payload computed here, so what was
        # a retry that succeeded while silently duplicating became a hard refusal on pre
        # existing data. The read below tolerates that shape rather than rewriting it: no
        # stored byte moves, and the day index membership test compares the normalised
        # form of what each entry names, so a legacy entry is found instead of being
        # appended beside. That stops a second entry being added beside an existing one;
        # it repairs no index contents already written, which only a rewrite of the
        # objects in the bucket could do, and that is not this module's to decide.
        recorded["execution_id"] = execution_id
        day = _name(trend_date, "collection_day_invalid")
        payload = canonical_bytes(recorded)
        name = self._receipt_name(execution_id)
        found = self._client.read(name)
        if found is None:
            found = self._create(name, payload)
        if found is not None and found[0] != payload:
            _legacy_receipt(found[0], payload, execution_id)
        for _attempt in range(_DAY_WRITE_ATTEMPTS):
            receipts, generation = self._day_index(day)
            if any(_recorded_id(item) == execution_id for item in receipts):
                return
            receipts.append(recorded)
            try:
                self._client.write(
                    self._day_name(day),
                    canonical_bytes({"receipts": receipts}),
                    if_generation_match=generation,
                )
            except PreconditionFailed:
                continue
            return
        raise ValueError("collection_day_contended")


class ObjectSourceRuns:
    """The staging source run relation: one create only object per completed run.

    ``record`` writes the run with its receipt and the two collection instants;
    ``read`` answers the reader shape ``read_completed_source_run`` validates, with an
    absent run carried as ``run: None`` under an ok status.
    """

    def __init__(self, client, *, prefix: str = SOURCE_RUN_PREFIX) -> None:
        self._client = client
        self._prefix = prefix

    def _name(self, run_id: str) -> str:
        return f"{self._prefix}/{_name(run_id, 'source_run_id_invalid')}.json"

    def record(self, run_id: str, run: Mapping[str, object]) -> None:
        name = self._name(run_id)
        if self._client.read(name) is not None:
            raise ValueError("source_run_exists")
        serialized = {
            field: _aware(value, "source_run_invalid").isoformat()
            if field in _INSTANT_FIELDS
            else value
            for field, value in dict(run).items()
        }
        try:
            self._client.write(name, canonical_bytes(serialized), if_generation_match=0)
        except PreconditionFailed as error:
            raise ValueError("source_run_exists") from error

    def read(self, run_id: str) -> dict:
        found = self._client.read(self._name(run_id))
        if found is None:
            return {"status": "ok", "run": None}
        run = _unpack(found[0], "source_run_record_invalid")
        for field in _INSTANT_FIELDS:
            try:
                run[field] = _aware(
                    datetime.fromisoformat(_text(run.get(field), "source_run_record_invalid")),
                    "source_run_record_invalid",
                )
            except ValueError as error:
                raise ValueError("source_run_record_invalid") from error
        return {"status": "ok", "run": run}


def build_provenance(build: object) -> dict[str, str]:
    """The runtime's build facts the exact authority carries: source commit and image digest."""
    if not isinstance(build, Mapping) or set(build) != {"source_sha", "image_uri"}:
        raise ValueError("build_provenance_invalid")
    source_sha, image_uri = build["source_sha"], build["image_uri"]
    if not isinstance(source_sha, str) or _GIT_SHA.fullmatch(source_sha) is None:
        raise ValueError("build_provenance_invalid")
    if not isinstance(image_uri, str) or _IMAGE_DIGEST_URI.fullmatch(image_uri) is None:
        raise ValueError("build_provenance_invalid")
    return {"source_sha": source_sha, "image_uri": image_uri}


def staging_target_refusal(profile: Mapping[str, object]) -> str | None:
    """The refusal when the warehouse the producer would write is not the staging source dataset.

    The dataset is resolved the way the producer resolves it, so an empty environment
    (``trends_v2_dev``), a production dataset or a derived staging dataset all refuse; only
    the dataset the collection profile names for staging, under ``TRENDS_ENV=staging``,
    passes. The resolver is imported here, never at module level.
    """
    from src.utils.bigquery import get_dataset

    if os.environ.get("TRENDS_ENV") != STAGING_ENVIRONMENT:
        return COLLECTION_TARGET_REFUSAL
    target = profile.get("source_dataset")
    if target != STAGING_SOURCE_DATASET or get_dataset() != target:
        return COLLECTION_TARGET_REFUSAL
    return None


def native_collector(ledger, *, profile: Mapping[str, object], build) -> Callable[..., dict]:
    """The producer's collection only terminal, bound in process to the durable ledger.

    Each call builds the exact execution authority of one business attempt: the attempt id
    as the operation id, the runtime's source commit and image digest, and the policy and
    profile digests the collection profile carries (the policy digest is the daily
    profile's source policy digest, checked equal at factory time). The attempt, the build
    and the staging target are checked before the producer module is imported, and the
    same checks are exposed as ``pre_dispatch_refusal`` so the guarded collect stage
    refuses retry safe before its handler reads the ledger. A build the runtime never
    bound refuses ``collection_build_unbound``.
    """
    digests = {
        "policy_sha256": _text(profile.get("policy_sha256"), "collection_profile_invalid"),
        "profile_sha256": _text(profile.get("profile_sha256"), "collection_profile_invalid"),
    }
    facts = None if build is None else build_provenance(build)

    def pre_dispatch_refusal() -> str | None:
        if facts is None:
            return COLLECTION_BUILD_REFUSAL
        return staging_target_refusal(profile)

    def collector(*, stop_after_ingestion: bool, attempt_id: object) -> dict:
        if not isinstance(attempt_id, str) or not attempt_id.strip():
            raise StageRefusal("collection_attempt_invalid")
        refusal = pre_dispatch_refusal()
        if refusal is not None:
            raise StageRefusal(refusal)
        authority = {"execution_id": attempt_id, **facts, **digests}  # type: ignore[dict-item]
        terminal = producer_collector(receipt_ledger=ledger, collection_authority=authority)
        return terminal(stop_after_ingestion=stop_after_ingestion)

    collector.pre_dispatch_refusal = pre_dispatch_refusal  # type: ignore[attr-defined]
    return collector


class IngestReceiptPending(RuntimeError):
    """The dispatched ingest execution recorded no receipt within the wait.

    Its message and ``execution_id`` name the ingest execution; ``retry_safe`` is False.
    """

    def __init__(self, execution_id: str) -> None:
        super().__init__(execution_id)
        self.execution_id = execution_id
        self.retry_safe = False


def _stage_refusal(code: str, *, retry_safe: bool, execution_id: str | None = None):
    """A ``StageRefusal`` that says whether a retry is safe and, once one ran, its execution."""
    error = StageRefusal(code)
    error.retry_safe = retry_safe  # type: ignore[attr-defined]
    if execution_id is not None:
        error.execution_id = execution_id  # type: ignore[attr-defined]
    return error


def _traced(error: BaseException, execution_id: str) -> None:
    """Name the ingest execution on an error raised after it was started."""
    try:
        error.execution_id = execution_id  # type: ignore[attr-defined]
        if not hasattr(error, "retry_safe"):
            error.retry_safe = False  # type: ignore[attr-defined]
    except AttributeError:
        pass


class IngestDispatch:
    """What ``ingest_collector`` dispatches over: the ingest job's runs and its receipt table.

    ``runs`` answers ``run_ingest_job()`` with the Cloud Run operation of one new ingest
    execution (``CloudRunTransport.run_ingest_job``), and ``receipts`` answers
    ``by_execution(execution_id)`` with the stored receipt rows of that execution
    (``ingest_receipts.BigQueryReceiptTable``). ``polls`` reads are made, ``interval``
    seconds apart; either not a positive integer refuses
    ``collection_ingest_poll_budget_invalid``. ``native_ingest_dispatch`` builds the
    runtime's own.
    """

    def __init__(
        self,
        *,
        runs,
        receipts,
        polls: int = INGEST_POLLS,
        interval: int = INGEST_POLL_INTERVAL_SECONDS,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        for value in (polls, interval):
            if type(value) is not int or value < 1:
                raise ValueError("collection_ingest_poll_budget_invalid")
        if sleep is None:
            import time

            sleep = time.sleep
        self.runs = runs
        self.receipts = receipts
        self.polls = polls
        self.interval = interval
        self.sleep = sleep


def _ingest_execution(operation: object) -> str:
    """The ingest execution id a run operation names, or a refusal before any read."""
    metadata = operation.get("metadata") if isinstance(operation, Mapping) else None
    name = operation.get("name") if isinstance(operation, Mapping) else None
    execution = metadata.get("name") if isinstance(metadata, Mapping) else None
    prefix = INGEST_JOB_NAME + "/executions/"
    if (
        not isinstance(name, str)
        or not name.startswith(INGEST_OPERATION_PREFIX)
        or len(name) == len(INGEST_OPERATION_PREFIX)
        or not isinstance(execution, str)
        or not execution.startswith(prefix)
        or INGEST_EXECUTION.fullmatch(execution[len(prefix) :]) is None
    ):
        raise ValueError("collection_ingest_operation_invalid")
    return execution[len(prefix) :]


def _admitted_ingest_receipt(row: object, *, execution_id: str, expected: Mapping) -> dict:
    """The ingest execution's own receipt, if it is this chain's record, else a refusal.

    The row must be the stored record of exactly this execution, its receipt must pass
    the collection receipt validator unchanged, carry the verified manifest authority the
    entry point placed it under, the daily runtime's own source commit and image, the
    daily profile's source policy digest and the entry point's profile digest, and be
    stored under the start day the receipt's closed observation day implies.
    """
    try:
        receipt = read_receipt_row(row)
        fields = dict(receipt)
        contract = fields.pop("contract_version", None)
        fields.pop("complete", None)
        validated = collection_receipt(
            run_id=fields.pop("run_id"),
            execution_id=fields.pop("execution_id"),
            source_sha=fields.pop("source_sha"),
            image_uri=fields.pop("image_uri"),
            policy_sha256=fields.pop("policy_sha256"),
            profile_sha256=fields.pop("profile_sha256"),
            cutoff=fields.pop("cutoff"),
            market_states=fields.pop("market_states"),
            raw_count=fields.pop("raw_rows_persisted"),
            enriched_count=fields.pop("enriched_rows_persisted"),
            funded_close=fields.pop("funded_close"),
            authority_kind=fields.pop("authority_kind", None),
        )
        start_day = (date.fromisoformat(validated["cutoff"]) + timedelta(days=1)).isoformat()
    except (KeyError, TypeError, ValueError) as error:
        raise StageRefusal(INGEST_RECEIPT_REFUSAL) from error
    if (
        fields
        or contract != validated["contract_version"]
        or validated != receipt
        or row["execution_id"] != execution_id
        or validated["execution_id"] != execution_id
        or validated.get("authority_kind") != VERIFIED_AUTHORITY
        or row["trend_date"] != start_day
        or any(validated[field] != value for field, value in expected.items())
    ):
        raise StageRefusal(INGEST_RECEIPT_REFUSAL)
    return validated


def ingest_collection_profile(source_policy_digest: str) -> dict:
    """The profile ``ingest_collector`` checks against: the caller's source policy digest
    and the ingest entry point's own profile digest, read from no process variable."""
    return collection_profile(
        policy_sha256=_text(source_policy_digest, "collection_profile_invalid"),
        profile_sha256=entry_point_profile()["profile_sha256"],
    )


def ingest_collector(
    ledger, *, dispatch: IngestDispatch, profile: Mapping[str, object], build
) -> Callable[..., dict]:
    """Stable entry point: collect by running the ingest job, never in this process.

    The orchestration identity holds ``roles/bigquery.dataViewer`` on
    ``intelligence_42_sources_staging`` and ``roles/run.invoker`` on the ingest job, so it
    collects by starting ``intelligence-42-ingest-staging`` and reading back what that job
    recorded; nothing here writes the source dataset.

    Binding. ``ledger`` is the caller's receipt ledger (``ObjectCollectionLedger`` under
    ``42/daily/collection/``), ``dispatch`` an ``IngestDispatch`` (``native_ingest_dispatch()``
    in the runtime), ``profile`` ``ingest_collection_profile(<source policy digest>)`` and
    ``build`` the caller's own build fact, ``{"source_sha", "image_uri"}``. The receipt's
    ``source_sha`` and ``image_uri`` must equal that build, and the ingest job stamps the
    build it was created from, so the ingest job must be deployed from the same build (the
    same commit and image digest) as the caller; otherwise every run is refused after it
    ran, ``collection_ingest_receipt_mismatch``.

    Stamped digests. The ingest job stamps ``COLLECTION_POLICY_SHA256`` as the policy
    digest its entry point reconciles (``reconciled_policy_digest()``) and
    ``COLLECTION_PROFILE_SHA256`` as ``entry_point_profile()``'s digest, and refuses to
    collect under anything else. A caller profile naming another policy or profile digest
    could never admit what the job writes, so it refuses before any run.

    Request. The returned callable takes exactly
    ``collector(stop_after_ingestion=True, attempt_id=<caller attempt id>)``.

    What it runs. One ``POST run.googleapis.com/v2/.../jobs/intelligence-42-ingest-staging:run``
    with the body ``{}``: no override, so the execution runs the job definition as created
    (``python -m scripts.staging.collect_42_sources`` as the ingest identity, task timeout
    2700 seconds, no retry). Each call runs the job once; the caller keeps an attempt it
    already recorded from calling again (its ledger's ``operation(attempt_id)``).

    Completion. The run operation names the execution
    (``metadata.name = .../executions/intelligence-42-ingest-staging-<5>``). Completion is
    the ingest execution's receipt row in
    ``ogilvy-trends-v2.intelligence_42_sources_staging.collection_receipts``, read by that
    execution id (the run ID) through ``receipts.by_execution``. It reads up to
    ``dispatch.polls`` times, ``dispatch.interval`` seconds apart. An absent table reads
    as no row, and ``ReceiptNotYetReadable`` (the service unavailable, or the read timed
    out) is read again; any other read error propagates. No execution status is read.

    Time budget, by default. The run request is bounded by 30 seconds, each read by 5
    seconds (request and result), and there are 95 reads with 94 sleeps of 30 seconds:
    2820 seconds of sleeps, which outlast the job's 2700 second task timeout, and at most
    30 + 95 x 5 + 2820 = 3325 seconds in all (``INGEST_TIME_BUDGET_SECONDS``), inside the
    daily job's 3600 second task timeout. The caller's own work before the dispatch is
    not in that budget.

    Admission. The row must be the one stored record of that execution: canonical text
    whose sha256 is stored beside it, passing the collection receipt validator unchanged,
    under ``authority_kind`` ``verified_manifest``, with the caller's ``source_sha`` and
    ``image_uri``, the profile's policy and profile digests, and stored under the start day
    one after the receipt's closed ``cutoff``. The admitted receipt, without
    ``authority_kind`` and with ``execution_id`` set to ``attempt_id``, is recorded in
    ``ledger`` under that start day and returned; its ``run_id`` is the ingest run's.

    Refusals, in order. Every ``StageRefusal`` (a ``ValueError``) carries ``retry_safe``.

    Before the job is run, all ``retry_safe`` True, nothing started:
    ``collection_attempt_invalid`` (attempt not text), then the four
    ``pre_dispatch_refusal()`` answers, which it gives without any call:
    ``collection_build_unbound`` (no build), ``collection_target_not_staging`` (this
    process not under ``TRENDS_ENV=staging`` resolving ``intelligence_42_sources_staging``),
    ``collection_ingest_policy_unstamped`` (the profile's policy digest is not the one the
    ingest job stamps), ``collection_ingest_profile_unstamped`` (likewise the profile
    digest); then ``collection_mode_invalid`` (``stop_after_ingestion`` not True).

    At the run request, refused by the API, so no execution exists, ``retry_safe`` True:
    ``collection_ingest_dispatch_forbidden`` (403) and ``collection_ingest_dispatch_refused``
    (any other 4xx). A 5xx or a transport failure (``DailyAuthorityUnavailable`` and the
    like) propagates unchanged: whether an execution started is unknown, so it is not
    retry safe. ``ValueError("collection_ingest_operation_invalid")``: the run answered
    but names no ingest execution; nothing is read and it is not retry safe.

    After the run, ``retry_safe`` False and ``execution_id`` naming the ingest execution,
    so an unknown attempt can be traced to it: ``collection_ingest_receipt_mismatch`` (not
    this caller's record, or two rows); ``IngestReceiptPending`` (a ``RuntimeError``, no
    row within the wait, outcome unknown); and any other error raised while reading or
    recording, including ``ledger.record`` refusals (``collection_receipt_conflict``),
    which propagates with ``execution_id`` set. On success the recorded receipt's
    ``run_id`` is the ingest run's and the ingest table keeps its row under the execution.
    """
    digests = {
        "policy_sha256": _text(profile.get("policy_sha256"), "collection_profile_invalid"),
        "profile_sha256": _text(profile.get("profile_sha256"), "collection_profile_invalid"),
    }
    facts = None if build is None else build_provenance(build)
    stamped: dict[str, str] = {}

    def stamped_refusal() -> str | None:
        if not stamped:
            stamped["policy_sha256"] = reconciled_policy_digest()
            stamped["profile_sha256"] = entry_point_profile()["profile_sha256"]
        if digests["policy_sha256"] != stamped["policy_sha256"]:
            return INGEST_POLICY_REFUSAL
        if digests["profile_sha256"] != stamped["profile_sha256"]:
            return INGEST_PROFILE_REFUSAL
        return None

    def pre_dispatch_refusal() -> str | None:
        if facts is None:
            return COLLECTION_BUILD_REFUSAL
        return staging_target_refusal(profile) or stamped_refusal()

    def poll(execution_id: str, attempt_id: str) -> dict:
        expected = {**facts, **digests}  # type: ignore[dict-item]
        for attempt in range(dispatch.polls):
            if attempt:
                dispatch.sleep(dispatch.interval)
            try:
                rows = dispatch.receipts.by_execution(execution_id)
            except ReceiptNotYetReadable:
                continue
            if not rows:
                continue
            if len(rows) != 1:
                raise _stage_refusal(
                    INGEST_RECEIPT_REFUSAL, retry_safe=False, execution_id=execution_id
                )
            try:
                admitted = _admitted_ingest_receipt(
                    rows[0], execution_id=execution_id, expected=expected
                )
            except StageRefusal as error:
                raise _stage_refusal(
                    str(error), retry_safe=False, execution_id=execution_id
                ) from error
            recorded = daily_ledger_copy(admitted, attempt_id=attempt_id)
            ledger.record(recorded, trend_date=rows[0]["trend_date"])
            return recorded
        raise IngestReceiptPending(execution_id)

    def collector(*, stop_after_ingestion: bool, attempt_id: object) -> dict:
        if not isinstance(attempt_id, str) or not attempt_id.strip():
            raise _stage_refusal("collection_attempt_invalid", retry_safe=True)
        refusal = pre_dispatch_refusal()
        if refusal is not None:
            raise _stage_refusal(refusal, retry_safe=True)
        if stop_after_ingestion is not True:
            raise _stage_refusal("collection_mode_invalid", retry_safe=True)
        from .daily_native_transports import IngestRunRefused

        try:
            operation = dispatch.runs.run_ingest_job()
        except PermissionError as error:
            raise _stage_refusal(INGEST_DISPATCH_REFUSAL, retry_safe=True) from error
        except IngestRunRefused as error:
            raise _stage_refusal(INGEST_RUN_REFUSAL, retry_safe=True) from error
        execution_id = _ingest_execution(operation)
        try:
            return poll(execution_id, attempt_id)
        except Exception as error:
            _traced(error, execution_id)
            raise

    collector.pre_dispatch_refusal = pre_dispatch_refusal  # type: ignore[attr-defined]
    return collector


def _ingest_session():
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _project = google.auth.default(
        scopes=("https://www.googleapis.com/auth/cloud-platform",)
    )
    return AuthorizedSession(credentials)


class _DeferredRuns:
    """The Cloud Run transport over the attached identity, built at the first run."""

    def __init__(self) -> None:
        self._transport = None

    def run_ingest_job(self) -> dict:
        if self._transport is None:
            from .daily_native_transports import CloudRunTransport

            self._transport = CloudRunTransport(
                _ingest_session(), timeout=INGEST_RUN_TIMEOUT_SECONDS
            )
        return self._transport.run_ingest_job()


def native_ingest_dispatch() -> IngestDispatch:
    """The runtime's own dispatch: no credential is read and no client built until it runs.

    Runs go through ``CloudRunTransport.run_ingest_job`` over the attached identity, and
    receipts are read through ``ingest_receipts.native_receipt_table()``, which never
    creates the table.
    """
    return IngestDispatch(runs=_DeferredRuns(), receipts=native_receipt_table())


def bigquery_warehouse(client) -> Callable[[str], list[dict]]:
    """A SQL callable over the injected BigQuery client, in the release script's shape."""

    def warehouse(sql: str) -> list[dict]:
        from scripts.staging import release_open_intelligence_run as release

        return [dict(row) for row in release._query(client, sql)]

    return warehouse


def collection_authority(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """The two digests the producer stamps into its receipt, read where it reads them.

    Both are held to sixty-four hexadecimal characters here, which is what the
    receipt requires of them, so a malformed one refuses at this boundary rather
    than inside the producer after collection has already run.
    """
    values = {}
    variables = environment if environment is not None else os.environ
    for field, variable in (
        ("policy_sha256", COLLECTION_POLICY_VARIABLE),
        ("profile_sha256", COLLECTION_PROFILE_VARIABLE),
    ):
        value = variables.get(variable, "").strip()
        if not value:
            raise ValueError("collection_authority_missing")
        values[field] = value
    for field in ("policy_sha256", "profile_sha256"):
        if _HEX_64.fullmatch(values[field]) is None:
            raise ValueError("collection_authority_invalid")
    return values


def collection_profile(*, policy_sha256: str, profile_sha256: str) -> dict:
    """The staging collection profile the collect stage hands ``collect_staging``."""
    return {
        "contract_version": COLLECTION_PROFILE_CONTRACT,
        "environment": STAGING_ENVIRONMENT,
        "source_dataset": STAGING_SOURCE_DATASET,
        "markets": list(BOOTSTRAP_MARKETS),
        "policy_sha256": policy_sha256,
        "profile_sha256": profile_sha256,
    }


def release_profile_for(profile: Mapping[str, object], *, generation, cutoff: datetime):
    """The live staging release profile of the observation day a midnight cutoff closes."""
    from scripts.staging.release_open_intelligence_run import ReleaseRunProfile

    from src.analysis.open_intelligence.predictions import PredictionRules

    day = cutoff.astimezone(UTC).date() - timedelta(days=1)
    compact = day.isoformat().replace("-", "")
    return ReleaseRunProfile(
        profile_name=f"staging_daily_{compact}",
        run_id=f"run_{compact}_staging_daily_v1",
        source_policy_digest=profile["source_policy_digest"],
        cutoff=day.isoformat(),
        generation_pair=(generation.origin_registry_sha256, generation.resource_manifest_sha256),
        canary_namespace=False,
        prediction_rules=PredictionRules(),
        independence_policy=EXPLICIT_ORIGIN_POLICY,
    )


def _serialize_instants(value: Mapping[str, object]) -> dict:
    return {
        name: item.isoformat() if isinstance(item, datetime) else item
        for name, item in value.items()
    }


def _restore_instants(value: dict, fields: tuple[str, ...], code: str) -> dict:
    for field in fields:
        raw = value.get(field)
        if raw is None:
            continue
        try:
            value[field] = _aware(datetime.fromisoformat(_text(raw, code)), code)
        except ValueError as error:
            raise ValueError(code) from error
    return value


class ObjectCaptureRegistry:
    """The capture registry as create only objects, the writer the admission kernel consumes.

    ``create`` answers the registry's acknowledgement grammar: created when the object
    landed under a zero generation precondition, exists when the precondition refused, and
    ambiguous when the client raised anything else, so the kernel reads the exact object
    back before any retry. ``read`` answers the stored entry with its instants restored, or
    None.
    """

    def __init__(self, client, *, prefix: str = CAPTURE_PREFIX) -> None:
        self._client = client
        self._prefix = prefix

    def _name(self, key: str) -> str:
        # The key is composite, ``<profile_id>/<result_id>``, and that is the only shape
        # its one caller builds. Judging the joined string stripped only the outer edges of
        # the whole of it, so an interior spelling reached the object name unchanged and
        # four spellings of one result id created two objects a reader cannot tell apart,
        # which is the outcome the rule exists to prevent. The two parts are judged on
        # their own account: for a key already in normal form the name is the same byte for
        # byte, a part that is empty or would walk out of the prefix refuses, and the pair
        # is what bounds the length the single judgement used to bound.
        parts = key.split("/") if isinstance(key, str) else []
        if len(parts) != 2:
            raise ValueError("capture_key_invalid")
        judged = "/".join(_name(part, "capture_key_invalid") for part in parts)
        return f"{self._prefix}/registry/{judged}.json"

    def create(self, key: str, entry: Mapping[str, object]) -> dict:
        name = self._name(key)
        payload = canonical_bytes(_serialize_instants(entry))
        try:
            self._client.write(name, payload, if_generation_match=0)
        except PreconditionFailed:
            return {"status": "exists"}
        except Exception:
            return {"status": "ambiguous"}
        return {"status": "created"}

    def read(self, key: str) -> dict | None:
        found = self._client.read(self._name(key))
        if found is None:
            return None
        entry = _unpack(found[0], "capture_entry_record_invalid")
        return _restore_instants(
            entry, CAPTURE_ENTRY_INSTANT_FIELDS, "capture_entry_record_invalid"
        )


class ObjectCaptureResults:
    """The runner's own result per business attempt, create only; the readback ``verified`` answers.

    The stored object carries the attempt it was produced for beside the readback fields,
    and ``read`` answers only when that attempt is the one being asked about. Without it
    the readback was uncomparable: its field set carries a result id, digests, a state and
    two instants and no attempt identity at all, so whatever object sat under the name was
    answered as this attempt's result. The name is the normalised attempt id, and
    normalising is many to one, so two attempt ids that differ only by normalisation share
    one name; the capture handler treats any non None readback as proof that a native
    result already exists for the attempt, reconciles it without snapshotting again and
    registers a capture entry whose operation id is this attempt while the result id and
    the digests are another attempt's. That fails open, which is the one thing a readback
    must not do. The sibling paths are the pattern: a completed source run is read back and
    refused when its receipt does not match, and the capture registry entry is read back
    and compared against the entry that was admitted.

    A stored object without the attempt field cannot be compared and is refused rather
    than trusted. It is not the same answer as a record this module cannot parse: the
    field set an earlier revision wrote is a shape this store already holds, so it is
    read and answered with ``CAPTURE_RESULT_LEGACY_REFUSAL``, and both attempt refusals
    are stage refusals so the handler records them by their own code.
    """

    def __init__(self, client, *, prefix: str = CAPTURE_PREFIX) -> None:
        self._client = client
        self._prefix = prefix

    def _name(self, attempt_id: str) -> str:
        # Both callers normalise the attempt before reaching here, so this guard cannot
        # fire from either of them and removing it is an equivalent mutation. It is kept
        # deliberately: the name this builds may not leave its prefix whatever calls it,
        # and the alternative is a private method whose contract is its callers' habit.
        return f"{self._prefix}/results/{_name(attempt_id, 'capture_attempt_invalid')}.json"

    def record(self, attempt_id: str, readback: Mapping[str, object]) -> None:
        attempt = _name(attempt_id, "capture_attempt_invalid")
        if set(readback) != CAPTURE_READBACK_FIELDS:
            raise ValueError("capture_result_record_invalid")
        name = self._name(attempt)
        payload = canonical_bytes(
            {**_serialize_instants(readback), CAPTURE_RESULT_ATTEMPT_FIELD: attempt}
        )
        found = self._client.read(name)
        if found is None:
            try:
                self._client.write(name, payload, if_generation_match=0)
            except PreconditionFailed:
                found = self._client.read(name)
                if found is None:
                    raise ValueError("capture_result_conflict") from None
            else:
                return
        if found[0] != payload:
            raise ValueError("capture_result_conflict")

    def read_stored(self, attempt_id: str) -> dict | None:
        """The readback reader: the stored mapping as it is, any shape; never used to act."""
        found = self._client.read(self._name(_name(attempt_id, "capture_attempt_invalid")))
        if found is None:
            return None
        return _unpack(found[0], "capture_result_record_invalid")

    def read(self, attempt_id: str) -> dict | None:
        attempt = _name(attempt_id, "capture_attempt_invalid")
        found = self._client.read(self._name(attempt))
        if found is None:
            return None
        values = _unpack(found[0], "capture_result_record_invalid")
        # A stored result that does not name its attempt cannot be compared against the
        # attempt being asked about, so it is refused rather than answered. The field sets
        # earlier revisions wrote are exactly the readback set, with or without the capture
        # coverage, and those objects are in the bucket already: they are read here rather
        # than called unparseable, and answered with a condition of their own, so the stage
        # can tell them from a corrupt record and from another attempt's result. Widening
        # what can be read changes no stored byte.
        if set(values) in (CAPTURE_READBACK_FIELDS, CAPTURE_PRE_COVERAGE_READBACK_FIELDS):
            raise StageRefusal(CAPTURE_RESULT_LEGACY_REFUSAL)
        # A result recorded before the coverage travelled with it is a readable object of
        # this seam, not a corrupt one: the writer is create only and compares bytes, so an
        # attempt whose object predates the coverage could otherwise never be reconciled.
        if set(values) not in (
            CAPTURE_READBACK_FIELDS | {CAPTURE_RESULT_ATTEMPT_FIELD},
            CAPTURE_PRE_COVERAGE_READBACK_FIELDS | {CAPTURE_RESULT_ATTEMPT_FIELD},
        ):
            raise ValueError("capture_result_record_invalid")
        if values.pop(CAPTURE_RESULT_ATTEMPT_FIELD) != attempt:
            raise StageRefusal(CAPTURE_RESULT_ATTEMPT_REFUSAL)
        return _restore_instants(values, CAPTURE_INSTANT_FIELDS, "capture_result_record_invalid")


def _receipt_cutoff(run: object) -> date:
    """The closed observation day the admitted collect receipt names, never the clock's day."""
    receipt = run.get("receipt") if isinstance(run, Mapping) else None
    value = receipt.get("cutoff") if isinstance(receipt, Mapping) else None
    try:
        return date.fromisoformat(_text(value, "capture_run_invalid"))
    except ValueError as error:
        raise ValueError("capture_run_invalid") from error


def _verification(value: object) -> dict:
    if not isinstance(value, Mapping) or set(value) != CAPTURE_VERIFICATION_FIELDS:
        raise ValueError("capture_verification_invalid")
    checked = {field: value[field] for field in CAPTURE_VERIFICATION_FIELDS}
    for field in ("result_id", "generation"):
        # The normalised value is what is kept. Discarding it let the raw spelling flow on
        # into the capture entry payload and into the registry key built from it, which is
        # the record that disagrees with its own key this module refuses elsewhere.
        checked[field] = _text(checked[field], "capture_verification_invalid")
    for field in ("image_digest", "schema_digest", "source_digest"):
        if not isinstance(checked[field], str) or _HEX_64.fullmatch(checked[field]) is None:
            raise ValueError("capture_verification_invalid")
    return checked


class NativeCapture:
    """The capture stage client over the snapshot capture runner and the capture registry.

    ``snapshot`` runs the bound runner once for the attempt, under the execution authority
    the daily authority issued for it and the v2 capture origin the handler resolved, for
    the closed observation day the admitted collect receipt names (a catch up cutoff
    snapshots its own day, not the clock's), and records the readback the handler admits
    from the runner's own result: the compact payload's snapshot digest, captured instant,
    source instant and scope, the status, and the verification the runner read from the
    execution result record, the execution authority's image and the stored artifact's
    object generation. ``verified`` answers that readback or None, so a second invocation
    of the same attempt reconciles without a second runner call.
    """

    def __init__(self, *, object_client, warehouse, runner=None, prefix: str = CAPTURE_PREFIX):
        self.writer = ObjectCaptureRegistry(object_client, prefix=prefix)
        self._results = ObjectCaptureResults(object_client, prefix=prefix)
        self._warehouse = warehouse
        self._runner = runner

    def _bound_runner(self):
        return self._runner if self._runner is not None else DEFAULT_CAPTURE_RUNNER

    def pre_dispatch_refusal(self) -> str | None:
        return None if callable(self._bound_runner()) else CAPTURE_RUNNER_REFUSAL

    def snapshot(self, run, *, snapshot_as_of, attempt_id, origin, authority) -> tuple[dict, str]:
        runner = self._bound_runner()
        if not callable(runner):
            raise StageRefusal(CAPTURE_RUNNER_REFUSAL)
        issued_execution(authority)
        instant = _aware(snapshot_as_of, "capture_instant_invalid")
        cutoff = _receipt_cutoff(run)
        if instant.astimezone(UTC).date() <= cutoff:
            raise ValueError("capture_cutoff_invalid")
        outcome = runner(
            run,
            cutoff=cutoff,
            snapshot_as_of=instant,
            attempt_id=_name(attempt_id, "capture_attempt_invalid"),
            origin=origin,
            client=self._warehouse,
            authority=authority,
        )
        if not isinstance(outcome, tuple) or len(outcome) != 3:
            raise ValueError("capture_runner_result_invalid")
        payload, status, verification = outcome
        if not isinstance(payload, Mapping) or status not in {"succeeded", "failed"}:
            raise ValueError("capture_runner_result_invalid")
        verified = _verification(verification)
        readback = {
            "result_id": verified["result_id"],
            "content_digest": payload.get("snapshot_digest"),
            "image_digest": verified["image_digest"],
            "generation": verified["generation"],
            "completion_state": status,
            "capture_available_at": payload.get("captured_at"),
            "snapshot_as_of": payload.get("source_as_of"),
            "scope": payload.get("client_scope_id"),
            "schema_digest": verified["schema_digest"],
            "source_digest": verified["source_digest"],
            # The coverage the admission needs: the market scope the capture ran for and
            # the creation record of every snapshot table it attempted, as the result wrote
            # them, so a later reconciliation admits the same coverage rather than none.
            "market_scope": payload.get("market_scope"),
            "creation_records": payload.get("creation_records"),
        }
        self._results.record(attempt_id, readback)
        return dict(payload), status

    def verified(self, attempt_id: str) -> dict | None:
        return self._results.read(attempt_id)


def issued_execution(authority: object) -> tuple[object, object]:
    """The issued authority object and its consumption a capture receipt carries.

    The daily authority issues them under ``execution``: the objects the reservation path
    answered for the capture attempt, the exact objects the runner body presents to the
    engine's consumed execution check, for the capture stage under mode ``new_consume``.
    Anything else refuses ``capture_authority_unbound``, a stage refusal the handler
    records retry safe before entry.
    """
    if (
        not isinstance(authority, Mapping)
        or authority.get("authority") is None
        or authority.get("consumption") is None
        or authority.get("stage") != "capture"
        or authority.get("mode") != CAPTURE_ORIGIN_MODE
    ):
        raise StageRefusal(CAPTURE_AUTHORITY_REFUSAL)
    return authority["authority"], authority["consumption"]


def native_capture_execution(runtime_clients=None) -> Callable[..., dict]:
    """The runner seam's execution over the runtime's own capture objects.

    ``execution(attempt_id, origin, authority)`` reads the five input artifacts the
    issued authority's approval names, by digest, from the source capture objects the
    capture script's runtime identity path opens (``runtime_clients`` answers the query
    client and the objects; the script's own is the default, and it validates the runtime
    identity first), and answers them with the objects. Nothing is opened until a capture
    attempt dispatches.
    """

    def execution(attempt_id, origin, authority) -> dict:
        from scripts.staging import capture_protected_production_snapshot as script

        issued, _consumption = issued_execution(authority)
        digests = script._manifest_artifact_digests(getattr(issued, "approval", None))
        clients = script._runtime_clients if runtime_clients is None else runtime_clients
        _query_client, objects = clients()
        artifacts = {
            name: objects.read_input(name, digests[name], timeout=CAPTURE_ARTIFACT_TIMEOUT)
            for name in sorted(script._INPUTS)
        }
        return {"artifacts": artifacts, "objects": objects}

    return execution


def snapshot_capture_runner(execution) -> Callable[..., tuple]:
    """The runner seam over the snapshot capture runner's validated body.

    The runner receives, beside the run and the instants, the execution authority the
    receipt carries (the issued authority object and its consumption) and the v2 origin;
    ``execution(attempt_id, origin, authority)`` answers the capture execution of the
    attempt as a mapping of the five input artifacts and the capture objects. Each call
    validates the inputs at the snapshot instant, runs ``_run_operation`` once under that
    authority, records the result through the execution result ledger and answers the
    compact payload, the status and the verification: the result id the ledger issued, the
    authority's image digest, the stored artifact's generation, the digest over the plan's
    source schema digests and the digest of the completed run's receipt.
    """
    if not callable(execution):
        raise ValueError("capture_execution_invalid")

    def runner(run, *, cutoff, snapshot_as_of, attempt_id, origin, client, authority) -> tuple:
        from scripts.staging import capture_protected_production_snapshot as script

        from src.analysis.open_intelligence import execution_approval

        issued, consumption = issued_execution(authority)
        bound = execution(attempt_id, origin, authority)
        if not isinstance(bound, Mapping) or set(bound) != {"artifacts", "objects"}:
            raise ValueError("capture_execution_invalid")
        inputs = script._validate_inputs(bound["artifacts"], cutoff, CAPTURE_MODE, snapshot_as_of)
        payload, status = script._run_operation(
            bound["artifacts"],
            cutoff,
            CAPTURE_MODE,
            authority=issued,
            consumption=consumption,
            client=client,
            objects=bound["objects"],
            now=snapshot_as_of,
        )
        canonical = canonical_bytes(payload).decode("utf-8")
        authority = issued
        result = execution_approval._record_execution_result(
            authority,
            consumption,
            authority.execution_name + CAPTURE_RESULT_SUFFIX,
            canonical,
            sha256(canonical.encode("utf-8")).hexdigest(),
            status,
        )
        stored = payload.get("stored_artifact")
        generation = stored.get("generation") if isinstance(stored, Mapping) else None
        image = authority.image_uri
        verification = {
            "result_id": result.result_id,
            "image_digest": image.rsplit("sha256:", 1)[-1] if isinstance(image, str) else None,
            "generation": None if generation is None else str(generation),
            "schema_digest": sha256(
                canonical_bytes(
                    [statement.source_schema_digest for statement in inputs["plan"].statements]
                )
            ).hexdigest(),
            "source_digest": receipt_source_digest(run["receipt"]),
        }
        return payload, status, verification

    return runner


# The runner seam bound by default: the native capture execution over the capture
# script's own runtime identity path. A client constructed with its own runner keeps it.
DEFAULT_CAPTURE_RUNNER = snapshot_capture_runner(native_capture_execution())


def daily_readiness_rules(cutoff: datetime) -> ReadinessRules:
    """The readiness rules of the observation day a midnight cutoff closes."""
    closed = cutoff.astimezone(UTC).date() - timedelta(days=1)
    return ReadinessRules(
        current_cutoff=datetime(closed.year, closed.month, closed.day, tzinfo=UTC),
        minimum_geo_confidence=DAILY_READINESS_GEO_FLOOR,
    )


def compose_clients(
    binding: object, *, warehouse, release_profile, cutoff, build, now, instant, products=None
) -> dict:
    """The composer and persist clients of one daily compose stage over the runtime's binding.

    ``binding`` is the only thing this function does not derive for itself, and nothing
    here creates it: the reader that resolves an admitted capture entry to its source
    window, and the principal, instant and rule version of the approval that admits the
    daily composition rules. A binding short of exact refuses ``compose_binding_invalid``
    and binds nothing, so a half filled binding can never dispatch a paid composition.

    Everything else is the runtime's own: the client scope resolved from the packaged
    scope configuration over the release profile's run id and the bootstrap markets, the
    readiness rules of the closed observation day, the deterministic lexical similarity
    provider the pipeline and the replay both default to, the release profile's own rule
    bundle, the wiring's own clock, and the run receipt facts read from the composer's own
    record of the run it composed. The run receipt names the source commit the image was
    built from, so a runtime that bound no build fact has none to name and refuses
    ``compose_binding_unbuilt``.

    ``instant`` is the wiring clock's reading this build was made at and ``cutoff`` is the
    cutoff derived from it, which this function checks rather than assumes, since both
    arrive as independent arguments and the approval comparand is the cutoff.

    An approval dated later than the cutoff refuses ``compose_approval_not_yet_granted``:
    the approval admits a composition of the observations the cutoff closes, so it cannot
    postdate them. The comparison is inclusive, so an approval at the close of its own
    window binds, which is the shape of every approval this repository records. An approval
    earlier than ``COMPOSE_APPROVAL_NOT_BEFORE`` refuses ``compose_approval_not_credible``:
    the epoch is not an approval, it is an unset field.
    """
    from src.analysis.open_intelligence.daily_composer import (
        build_native_composer,
        build_native_persist,
        composition_receipt_inputs,
        daily_rule_bundle,
    )
    from src.analysis.open_intelligence.pipeline import TARGET_DATASET
    from src.contracts.open_intelligence import resolve_client_scope

    if not isinstance(binding, Mapping) or set(binding) != set(COMPOSE_BINDING_FIELDS):
        raise ValueError(COMPOSE_BINDING_REFUSAL)
    # Kept here although ``NativeComposer`` refuses a non callable reader too, and under a
    # code of its own so that keeping it is observable: the binding layer refuses a binding
    # short of exact before it builds anything.
    if not callable(binding["source_window"]):
        raise ValueError(COMPOSE_SOURCE_WINDOW_REFUSAL)
    try:
        approved_by = _text(binding["approved_by"], COMPOSE_BINDING_REFUSAL)
        rule_version = _text(binding["rule_version"], COMPOSE_BINDING_REFUSAL)
        approved_at = _aware(binding["approved_at"], COMPOSE_BINDING_REFUSAL)
    except ValueError as error:
        raise ValueError(COMPOSE_BINDING_REFUSAL) from error
    closed = _aware(cutoff, COMPOSE_CUTOFF_REFUSAL)
    if closed != due_cutoff(_aware(instant, "clock_invalid")):
        raise ValueError(COMPOSE_CUTOFF_REFUSAL)
    if approved_at > closed:
        raise ValueError(COMPOSE_APPROVAL_REFUSAL)
    if approved_at < COMPOSE_APPROVAL_NOT_BEFORE:
        raise ValueError(COMPOSE_APPROVAL_STALE_REFUSAL)
    if build is None:
        raise ValueError(COMPOSE_BUILD_REFUSAL)
    facts = build_provenance(build)
    composer = build_native_composer(
        warehouse=warehouse,
        scope=resolve_client_scope(
            run_id=release_profile.run_id, market_scope=tuple(BOOTSTRAP_MARKETS)
        ),
        source_window_from_entry=binding["source_window"],
        readiness_rules=daily_readiness_rules(cutoff),
        semantic_provider=_lexical_semantic_provider(),
        now=now,
    )
    persist = build_native_persist(
        client=warehouse,
        dataset=TARGET_DATASET,
        rule_bundle=daily_rule_bundle(
            release_profile,
            approved_by=approved_by,
            approved_at=approved_at,
            rule_version=rule_version,
        ),
        run_receipt_inputs=composition_receipt_inputs(composer, source_sha=facts["source_sha"]),
        authority_receipt_from_run=None if products is None else products.authority_receipt,
    )
    return {"composer": composer, "persist": persist}


def _lexical_semantic_provider():
    """The deterministic lexical provider the pipeline and the replay both default to."""
    from scripts.staging.replay_open_intelligence import LexicalSimilarityProviderV2

    return LexicalSimilarityProviderV2()


def native_compose_binding():
    """The native compose binding, which no reviewed approval record supplies yet.

    A binding needs independently certified packaged rules and the principal, instant and
    rule version of the approval that admits them. No such approval record is packaged, so
    this resolves to None in every case and the guard refuses the compose stage before
    dispatch. It never raises: it is resolved while the whole daily cycle's handlers are
    built, and a refusal here would abort collect and capture along with compose.
    """
    from scripts.staging import replay_open_intelligence as replay

    from .daily_composer import COMPOSITION_RULES_PATH

    try:
        replay.load_composition_rules_v3(
            replay.ROOT / COMPOSITION_RULES_PATH, require_certified=True
        )
    except Exception:
        return None
    # Certified rules alone admit nothing: the approval record is still unavailable.
    return None


def build_native_stage_clients(
    *,
    engine,
    profile,
    environment,
    object_client,
    warehouse,
    now,
    build=None,
    capture_runner=None,
    compose_binding=None,
) -> dict[str, object]:
    """Bind the stage handlers' clients over the runtime's engine, object client and warehouse.

    Refuses a daily profile whose source policy digest differs from the policy digest the
    collection authority carries, since the collect stage would refuse every receipt.
    ``build`` is the runtime's own build fact, a mapping of ``source_sha`` and
    ``image_uri``, validated here; left None, the collect stage refuses before dispatch.
    ``capture_runner`` is the capture runner seam; left None the module default applies,
    the native seam over the capture script's runtime identity path.
    ``compose_binding`` is the compose seam; left None, as every caller leaves it while no
    native authorization for a paid composition exists, the composer and persist clients
    stay unbound and the guard refuses the compose stage before dispatch. There is no
    module default for it, deliberately: a composition is dispatched only where a caller
    hands over the binding of ``compose_clients``.
    """
    checked = validate_daily_profile(profile)
    facts = None if build is None else build_provenance(build)
    _text(environment, "environment_invalid")
    if not callable(now):
        raise ValueError("clock_invalid")
    store_module = getattr(engine, "daily_store", None)
    if store_module is None or not callable(getattr(store_module, "DailyStore", None)):
        raise ValueError("engine_invalid")
    authority = collection_authority()
    if authority["policy_sha256"] != checked["source_policy_digest"]:
        raise ValueError("collection_policy_mismatch")
    generation = active_generation()
    instant = now()
    cutoff = due_cutoff(instant)
    ledger = ObjectCollectionLedger(object_client)
    profile_client = collection_profile(**authority)
    release_profile = release_profile_for(checked, generation=generation, cutoff=cutoff)
    from .daily_dynamic_meter import DynamicMeter
    from .daily_product_boundary import native_product_io_factory
    from .daily_products import DailyProducts

    dynamic_meter = DynamicMeter(warehouse)
    products = DailyProducts(
        objects=object_client,
        build=facts,
        now=now,
        io_factory=native_product_io_factory(warehouse=warehouse, meter=dynamic_meter),
        run_id=release_profile.run_id,
        dynamic_meter=dynamic_meter,
    )
    return {
        "clock": now,
        "store": store_module.DailyStore(object_client, owner=STORE_OWNER, now=now),
        "collection_profile": profile_client,
        "collector": native_collector(ledger, profile=profile_client, build=facts),
        "collection_ledger": ledger,
        "source_runs": ObjectSourceRuns(object_client),
        "generation": generation,
        "capture": NativeCapture(
            object_client=object_client, warehouse=warehouse, runner=capture_runner
        ),
        **(
            {name: UnboundClient(name, attributes) for name, attributes in UNBOUND_CLIENTS.items()}
            if compose_binding is None
            else compose_clients(
                compose_binding,
                warehouse=dynamic_meter,
                release_profile=release_profile,
                cutoff=cutoff,
                build=build,
                now=now,
                instant=instant,
                products=products,
            )
        ),
        "warehouse": bigquery_warehouse(warehouse),
        "certifier": NativeCertifier(
            object_client=object_client,
            warehouse=warehouse,
            release_profile=release_profile,
            generation=generation,
            readiness_rules=daily_readiness_rules(cutoff),
        ),
        "release_profile": release_profile,
        "products": products,
        "telemetry": BoundaryTelemetry(DisabledDispositionSink()),
    }


def _binding(client: object) -> str:
    if isinstance(client, Mapping):
        return str(client.get("contract_version", "mapping"))
    target = client if isinstance(client, type) or hasattr(client, "__qualname__") else type(client)
    return f"{target.__module__}.{target.__qualname__}"


def describe_stage_clients(clients: Mapping[str, object]) -> dict[str, dict]:
    """Per client: bound or not, the binding's name, and the refusal code it carries.

    An unbound client carries its ``client_unbound`` code; a bound client that would
    refuse before dispatch carries that refusal (the collector's unbound build or non
    staging target, a capture client constructed without any runner), so the plan shows
    where a deployed run stops; every other client carries None.
    """
    described = {}
    for name in CLIENT_FIELDS:
        client = clients[name]
        if isinstance(client, UnboundClient):
            described[name] = {"bound": False, "binding": "unbound", "code": client.code}
            continue
        check = getattr(client, "pre_dispatch_refusal", None)
        code = check() if callable(check) else None
        described[name] = {"bound": True, "binding": _binding(client), "code": code}
    return described


def _refused_before_dispatch(stage: str, manifest: object, code: str) -> dict:
    return {
        "state": "failed",
        "input_digest": manifest_digest(manifest),
        "output_digest": "",
        "result_reference": f"{stage}_refused:{code}",
        "retry_safe": True,
    }


def _pre_dispatch_checks(stage: str, clients: Mapping[str, object]) -> list[Callable]:
    """The refusal checks of the clients whose paid call the stage dispatches."""
    checks = [
        check
        for name in PRE_DISPATCH_CLIENTS.get(stage, ())
        if callable(check := getattr(clients[name], "pre_dispatch_refusal", None))
    ]
    if stage == "collect" and not checks:
        profile = clients["collection_profile"]
        checks.append(lambda: staging_target_refusal(profile))  # type: ignore[arg-type]
    return checks


def _guard_stage(stage: str, handler: Callable, clients: Mapping[str, object]) -> Callable:
    """Refuse the stage before dispatch when one of its clients refuses."""
    checks = _pre_dispatch_checks(stage, clients)
    if not checks:
        return handler

    def guarded(*, manifest, authority_receipt):
        for check in checks:
            code = check()
            if code is not None:
                return _refused_before_dispatch(stage, manifest, code)
        return handler(manifest=manifest, authority_receipt=authority_receipt)

    guarded.__name__ = stage
    if hasattr(handler, "validate_reuse"):
        guarded.validate_reuse = handler.validate_reuse
    return guarded


def guard_unbound_stages(handlers: Mapping[str, Callable], clients: Mapping[str, object]) -> dict:
    """Refuse, before dispatch, every stage that would reach an unbound client.

    A stage whose clients are bound still refuses before dispatch when one of them carries
    a pre dispatch refusal: the collector's unbound build or non staging target, and a
    capture client constructed without any runner, by the same retry safe shape.
    """
    guarded = {}
    for stage, handler in handlers.items():
        unbound = next(
            (
                clients[name]
                for name in STAGE_CLIENTS[stage]
                if isinstance(clients[name], UnboundClient)
            ),
            None,
        )
        if unbound is None:
            guarded[stage] = _guard_stage(stage, handler, clients)
            continue

        def refuse(*, manifest, authority_receipt, _stage=stage, _code=unbound.code):
            return _refused_before_dispatch(_stage, manifest, _code)

        refuse.__name__ = stage
        if stage == "compose":

            def refuse_reuse(*, manifest, result, _code=unbound.code):
                raise StageRefusal(_code)

            refuse.validate_reuse = refuse_reuse
        guarded[stage] = refuse
    return guarded


def guarded_stage_handlers(clients: Mapping[str, object], *, profile) -> dict:
    """The bound stage handlers, guarded so unbound clients refuse before any native call."""
    from .daily_products import DailyProducts

    if not isinstance(clients.get("products"), DailyProducts):
        raise ValueError("products_native_binding_missing")
    return guard_unbound_stages(build_stage_handlers(clients, profile=profile), clients)


__all__ = [
    "CAPTURE_PREFIX",
    "CAPTURE_RESULT_ATTEMPT_REFUSAL",
    "CAPTURE_RESULT_LEGACY_REFUSAL",
    "CAPTURE_RUNNER_REFUSAL",
    "COLLECTION_BUILD_REFUSAL",
    "COLLECTION_PREFIX",
    "COLLECTION_RECEIPT_LEGACY_REFUSAL",
    "COLLECTION_TARGET_REFUSAL",
    "COMPOSE_APPROVAL_NOT_BEFORE",
    "COMPOSE_APPROVAL_REFUSAL",
    "COMPOSE_APPROVAL_STALE_REFUSAL",
    "COMPOSE_BINDING_FIELDS",
    "COMPOSE_BINDING_REFUSAL",
    "COMPOSE_BUILD_REFUSAL",
    "COMPOSE_CUTOFF_REFUSAL",
    "COMPOSE_SOURCE_WINDOW_REFUSAL",
    "DEFAULT_CAPTURE_RUNNER",
    "INGEST_POLLS",
    "INGEST_POLL_INTERVAL_SECONDS",
    "INGEST_RUN_TIMEOUT_SECONDS",
    "INGEST_TIME_BUDGET_SECONDS",
    "SOURCE_RUN_PREFIX",
    "STAGE_CLIENTS",
    "UNBOUND_CLIENTS",
    "IngestDispatch",
    "IngestReceiptPending",
    "NativeCapture",
    "ObjectCaptureRegistry",
    "ObjectCaptureResults",
    "ObjectCollectionLedger",
    "ObjectSourceRuns",
    "UnboundClient",
    "bigquery_warehouse",
    "build_native_stage_clients",
    "build_provenance",
    "collection_authority",
    "collection_profile",
    "compose_clients",
    "daily_readiness_rules",
    "describe_stage_clients",
    "due_cutoff",
    "guard_unbound_stages",
    "guarded_stage_handlers",
    "ingest_collection_profile",
    "ingest_collector",
    "issued_execution",
    "manifest_digest",
    "native_capture_execution",
    "native_collector",
    "native_ingest_dispatch",
    "release_profile_for",
    "snapshot_capture_runner",
    "staging_target_refusal",
]
