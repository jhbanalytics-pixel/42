"""Bind one staging release candidate to its native readbacks (U06, E04).

A candidate is the set of identities a tester or an assessor is actually looking
at: the repository commit, the engine, app and ops trees, both images and the
builds that pushed them, the Cloud Run revision and its traffic, the question
queue, the active policy and deployment, the served assets, the design package,
and the daily captures and released runs the product answers from. This module
writes one receipt that names each of those, says which check proved it and
refuses to call the candidate bound while any one is unproven.

It proves nothing by reading the network. Every native value comes from a
receipt an existing tool already wrote on the owner's machine:

``release.py verify`` with the published ReleaseIndex is the native readback of
the commit, images, trees, contracts, pinned authority objects, served assets,
service, revision, traffic, queue and ledger. This module does not repeat that
validation. It runs the same closed ReleaseIndex grammar over the index bytes,
requires the verify receipt to name those exact bytes and the plan it checked,
to be verified and recent, and to record that it read every reference the index
names, and then reads the bound values out of it.

``release.py resume`` is the only way the queue runs. A running queue is bound
only through a running_verified resume receipt for the same binding whose resume
plan names the apply result this verify receipt reconciled, and which ran after
that route and before the verify read; the verify receipt may then differ from
a release only in the queue not being paused.

The design package is read from a clean checkout at the index commit whose app
tree inventory hashes to the index's app tree digest, using the release index producer's own git blob
reader, so the vendored archive is the one the image was built from.

Capture entries and quality release records are the owner's readbacks of the
capture registry and the release record table. For each required capture profile the
registry's own selection picks the latest eligible capture at the binding time. They prove the identity and
completion of the named captures and released runs, not that a given answer was
served from them; answer level provenance is proved by the question receipts.

A missing or failing input leaves its subject unproven with the reason; it never
drops out of the receipt. ``compare`` reports which subjects changed between two
receipts, because a later change invalidates only the certification receipts
bound to the subjects it touched.
"""

import argparse
import ast
import hashlib
import io
import json
import re
import sys
import tarfile
import traceback
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]
for _entry in (str(_ROOT / "engine"), str(_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from ops.deploy import release, release_index  # noqa: E402
from ops.deploy.release import Refusal, _require, _sha256  # noqa: E402
from src.analysis.open_intelligence import capture_registry  # noqa: E402
from src.analysis.open_intelligence.capture_registry import (  # noqa: E402
    CAPTURE_MARKETS,
)
from src.analysis.open_intelligence.live_quality import (  # noqa: E402
    QUALITY_RELEASE_FIELDS,
)
from src.analysis.open_intelligence.brain_contract import canonical_bytes  # noqa: E402
from scripts.staging import read_ingest_run  # noqa: E402
from src.analysis.open_intelligence.ingest_receipts import (  # noqa: E402
    daily_ledger_copy,
    receipt_source_digest,
)
from src.analysis.open_intelligence.staging_source_profile import (  # noqa: E402
    staging_profile_id,
)

CONTRACT = "42_candidate_binding_v1"
GUIDE_CONTRACT = "42_candidate_guide_section_v1"
DEFAULT_MAX_AGE_MINUTES = 60
# The release record contract the release script writes; a test pins it to the
# script's own constant, which cannot be imported here without the BigQuery SDK.
RELEASE_RECORD_CONTRACT = "open_intelligence_quality_release_v2"
INGEST_READBACK_CONTRACT = read_ingest_run.RECEIPT_VERSION
# The top level fields the readback tool writes on a real read; a test pins them.
INGEST_READBACK_FIELDS = frozenset(
    {
        "contract_version",
        "mode",
        "execution_id",
        "cutoff",
        "expected",
        "tables",
        "maximum_bytes_billed",
        "receipt_row",
        "raw_content",
        "state",
        "reasons",
        "queries",
        "dry_run_bytes_total",
        "bytes_billed_total",
    }
)
_INGEST_BUILD_FIELDS = ("policy_sha256", "profile_sha256", "source_sha", "image_uri")
DESIGN_PACKAGE = "ogilvy-intelligence-design-system"
FRONTEND = "app/frontend"
SUBJECTS = (
    "verify_receipt",
    "repository_commit",
    "trees",
    "images",
    "service_revision",
    "traffic",
    "queue",
    "policy_deployment",
    "source_binding",
    "served_assets",
    "design_package",
    "collection",
    "captures",
    "released_runs",
)
# Subjects whose values make up the candidate identity. The verify receipt is
# how the others were proved, not part of what was proved.
IDENTITY_SUBJECTS = SUBJECTS[1:]
MANDATORY_JOURNEYS = (
    ("ask", "Ask a question and read the answer with its claims and receipts"),
    ("follow_up", "Ask a follow up on the same answer"),
    ("challenge", "Challenge a claim and see the evidence that supports or limits it"),
    ("insufficient", "Ask something outside the covered sources and see the gap"),
    ("source_inspection", "Open a cited source: record, excerpt, date, link, market"),
    ("history", "Ask about an earlier dated window"),
    ("next_day", "Ask again the next day on newly collected data"),
    ("investigation", "Open an investigation, reopen it later"),
    ("review", "Review a draft brief"),
    ("export", "Export one approved brief"),
    ("forecast_outcome", "Read a forecast and its outcome state"),
    ("client_report", "Open the BSA client view"),
    ("response", "Start a response workflow from a finding"),
)
FEEDBACK_FIELDS = (
    "Journey",
    "Question or action, in your words",
    "What you expected",
    "What you got",
    "Did you open a cited source (yes or no)",
    "Useful (1 to 5)",
    "Anything wrong, missing or confusing",
)
_RUN_ID = re.compile(r"[a-z0-9_][a-z0-9_-]{0,127}\Z")
_DIGEST_FIELDS = tuple(
    field for field in QUALITY_RELEASE_FIELDS if field.endswith(("_digest", "_sha256"))
)
_SUBJECT_DRIFT = frozenset({"service", "revision", "traffic", "queue", "ledger"})
_CAPTURE_INSTANTS = (
    "observation_window_end",
    "collection_started_at",
    "collection_completed_at",
    "snapshot_as_of",
    "capture_available_at",
    "expires_at",
)


# Rows


def _bound(value, check, evidence=None):
    """``value`` is candidate identity; ``evidence`` records what was read but
    changes with ordinary use (the ledger generation moves with every admitted
    question), so it stays out of the identity and out of ``compare``."""
    row = {"state": "bound", "value": value, "check": check}
    if evidence is not None:
        row["evidence"] = evidence
    return row


def _unproven(reason, value=None):
    return {"state": "unproven", "value": value, "reason": reason}


def _instant(value):
    try:
        return release._parse_time(value, "instant_invalid")
    except Refusal:
        return None


# Inputs


def _load(path, code):
    value, raw = release._read_json(path, code)
    _require(type(value) is dict, code)
    return value, raw


def _load_optional_list(path, code):
    if path is None:
        return None
    value, _ = release._read_json(path, code)
    _require(type(value) is list, code)
    return value


# Verify receipt


def _verify_row(verify, index, index_raw, plan_sha256, now, max_age):
    """The verify receipt must be a verified or queue only read of this index
    and plan, taken inside the freshness window."""
    record = verify.get("release_index")
    if verify.get("contract_version") != release.RECEIPT_CONTRACT or (
        verify.get("kind") != "verify"
    ):
        return _unproven("verify_receipt_invalid")
    if verify.get("plan_sha256") != plan_sha256:
        return _unproven("verify_receipt_plan_mismatch")
    if type(record) is not dict or verify.get("release_index_sha256") != _sha256(
        index_raw
    ):
        return _unproven("verify_receipt_index_mismatch")
    if record.get("sha256") != _sha256(index_raw) or record.get("state") != "verified":
        return _unproven("verify_receipt_index_unproven")
    if record.get("references_read") != release.release_index_references(index):
        return _unproven("verify_receipt_references_incomplete")
    checked_at = _instant(verify.get("checked_at"))
    if checked_at is None:
        return _unproven("verify_receipt_invalid")
    if checked_at > now:
        return _unproven("verify_receipt_from_future")
    if now - checked_at > max_age:
        return _unproven("verify_receipt_stale")
    drift = verify.get("drift")
    if type(drift) is not list:
        return _unproven("verify_receipt_invalid")
    # Drift on a subject this receipt binds leaves that subject unproven below.
    # Any other drift, an unproven apply operation for one, voids the receipt.
    if any(item not in _SUBJECT_DRIFT for item, _ in _drift_items(verify)) or len(
        _drift_items(verify)
    ) != len(drift):
        return _unproven("verify_drift")
    return _bound(
        {
            "checked_at": release._stamp(checked_at),
            "release_index_sha256": record["sha256"],
            "references_read": len(record["references_read"]),
        },
        "release.py verify receipt with the release index",
    )


def _drift_items(verify):
    return [
        (item.get("item"), item.get("error"))
        for item in verify.get("drift") or []
        if type(item) is dict
    ]


# Queue


def _queue_row(verify, verify_plan, resume, resume_plan):
    native = (verify.get("native") or {}).get("queue") or {}
    state = native.get("state")
    drift = _drift_items(verify)
    binding = verify_plan["binding"]
    queue_drift = [entry for entry in drift if entry[0] == "queue"]
    if native.get("name") != release.QUEUE_API_NAME:
        return _unproven("queue_name_mismatch", {"state": state})
    if state == "PAUSED" and not queue_drift:
        return _bound(
            {"state": "PAUSED", "name": native.get("name")},
            "release.py verify queue readback",
        )
    if state != "RUNNING":
        return _unproven("queue_state_unverified", {"state": state})
    if queue_drift != [("queue", "queue_not_paused")]:
        return _unproven("verify_drift", {"state": state})
    if resume is None or resume_plan is None:
        return _unproven("resume_receipt_not_supplied", {"state": state})
    if (
        resume.get("contract_version") != release.RECEIPT_CONTRACT
        or resume.get("kind") != "resume"
        or resume.get("state") != "running_verified"
    ):
        return _unproven("resume_receipt_not_running_verified", {"state": state})
    plan, plan_sha256 = resume_plan
    if resume.get("plan_sha256") != plan_sha256:
        return _unproven("resume_receipt_plan_mismatch", {"state": state})
    if resume.get("binding") != binding or plan.get("binding") != binding:
        return _unproven("resume_receipt_binding_mismatch", {"state": state})
    # The resume plan names the route receipt it resumed after; it must be the
    # apply result this verify receipt reconciled, not an earlier route of the
    # same binding.
    route = plan.get("route") or {}
    if route.get("sha256") != verify.get("result_sha256"):
        return _unproven("resume_receipt_route_mismatch", {"state": state})
    started = _instant(resume.get("started_at"))
    finished = _instant(resume.get("finished_at"))
    routed = _instant(route.get("finished_at"))
    checked = _instant(verify.get("checked_at"))
    if None in (started, finished, routed, checked) or not (
        routed <= started <= finished <= checked
    ):
        return _unproven("resume_receipt_out_of_order", {"state": state})
    return _bound(
        {"state": "RUNNING", "name": native.get("name")},
        "release.py resume running_verified receipt chained to the verified apply "
        "result, and verify queue readback",
        {"resumed_at": release._stamp(finished)},
    )


# Service, revision, traffic


def _service_rows(verify, plan, binding, tag, index, drift):
    native = verify.get("native") or {}
    revision = native.get("revision") or {}
    expected_image = plan["expected"]["image"]
    names = {
        binding["revision_name"],
        release.SERVICE_API_NAME + "/revisions/" + binding["revision_name"],
    }
    if any(item == "revision" for item, _ in drift):
        revision_row = _unproven("verify_drift")
    elif revision.get("name") not in names:
        revision_row = _unproven("revision_name_mismatch")
    elif (
        revision.get("image") != expected_image or expected_image != index["app_image"]
    ):
        revision_row = _unproven("revision_image_mismatch")
    else:
        revision_row = _bound(
            {
                "revision_name": binding["revision_name"],
                "image": revision["image"],
                "service_account": revision.get("service_account"),
            },
            "release.py verify revision readback",
        )
    service = native.get("service") or {}
    statuses = service.get("trafficStatuses") or []
    targets = release.traffic_targets(binding, tag)
    observed = [
        {key: item.get(key) for key in targets[0]}
        for item in statuses
        if type(item) is dict
    ]
    if any(item in ("service", "traffic") for item, _ in drift):
        traffic_row = _unproven("verify_drift")
    elif service.get("traffic") != targets or observed != targets:
        traffic_row = _unproven("traffic_unverified")
    else:
        traffic_row = _bound(
            {
                "revision_name": binding["revision_name"],
                "percent": 100,
                "tag": tag,
                "url": release.asset_origin(binding["canonical_service_audience"], tag),
            },
            "release.py verify service traffic readback",
        )
    return revision_row, traffic_row


# Design package


def _design_package_row(repo, index):
    if repo is None:
        return _unproven("checkout_not_supplied")
    try:
        root = Path(repo).resolve()
        head = release_index._git(root, "rev-parse", "HEAD").decode("ascii").strip()
        release_index._require_checkout(root, index["commit"])
        entries = release_index._tree_entries(root, head)
        paths = release_index._tree_paths(entries, release_index.TREES["app_tree"])
        manifest_path = FRONTEND + "/package.json"
        blobs = release_index._blobs(root, entries, paths)
        inventory = release_index.inventory_bytes(blobs, paths)
    except Refusal as error:
        return _unproven(str(error))
    if _sha256(inventory) != index["app_tree_sha256"]:
        return _unproven("checkout_app_tree_mismatch")
    try:
        package = json.loads(blobs[manifest_path].decode("utf-8"))
        dependency = package["dependencies"][DESIGN_PACKAGE]
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError):
        return _unproven("design_package_dependency_missing")
    prefix = "file:vendor/"
    match = isinstance(dependency, str) and re.fullmatch(
        re.escape(prefix + DESIGN_PACKAGE + "-") + r"(\d+\.\d+\.\d+)\.tgz", dependency
    )
    if not match:
        return _unproven("design_package_dependency_invalid")
    archive_path = FRONTEND + "/" + dependency[len("file:") :]
    sidecar_path = archive_path[: -len(".tgz")] + ".sha256"
    if archive_path not in blobs or sidecar_path not in blobs:
        return _unproven("design_package_archive_missing")
    archive = blobs[archive_path]
    archive_sha256 = _sha256(archive)
    pinned = blobs[sidecar_path].decode("ascii", "replace").strip().lower()
    if pinned != archive_sha256:
        return _unproven("design_package_archive_digest_mismatch")
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            member = bundle.extractfile("package/dist/manifest.json")
            _require(member is not None, "design_package_manifest_missing")
            manifest = json.loads(member.read().decode("utf-8"))
    except (KeyError, OSError, tarfile.TarError, UnicodeError, json.JSONDecodeError):
        return _unproven("design_package_manifest_missing")
    except Refusal as error:
        return _unproven(str(error))
    identity = manifest.get("package") if type(manifest) is dict else None
    if (
        type(identity) is not dict
        or identity.get("name") != DESIGN_PACKAGE
        or identity.get("version") != match.group(1)
    ):
        return _unproven("design_package_version_mismatch")
    return _bound(
        {
            "name": DESIGN_PACKAGE,
            "version": identity["version"],
            "contract_version": manifest.get("contractVersion"),
            "source_commit": manifest.get("sourceCommit"),
            "archive_sha256": archive_sha256,
            "checkout_commit": head,
        },
        "clean checkout app tree equals the index app tree; vendored archive "
        "equals its pinned digest and names this version",
    )


# Captures and released runs


def _capture_diagnostics(parsed, *, scope, now):
    """Per entry classification by the registry's own rules, computed without
    raising so a conflict or a malformed entry can still be shown."""
    diagnostics = []
    for index, entry in enumerate(parsed):
        checked, reason = capture_registry._classify(entry, now=now, scope=scope)
        diagnostics.append(
            {
                "index": index,
                "profile_id": None if checked is None else checked["profile_id"],
                "generation": None if checked is None else checked["generation"],
                "reason": reason,
            }
        )
    return diagnostics


def _captures_row(
    entries,
    *,
    scope,
    profiles,
    now,
    collection=None,
    collection_receipt=None,
    collection_attempt=None,
):
    """Run the registry's own selection over the unedited readback at the
    binding time for each required profile, and bind the capture it selects.

    The required profiles come from the approved capture profile, not from the
    readback, so an old expired day or another scope's profile in the same
    readback is recorded as a diagnostic and does not block the binding. An
    entry that fails the registry grammar anywhere in the readback, or two
    entries of one profile that disagree on the profile binding, leave the
    subject unproven: the readback itself is then not trustworthy.

    A bound collection adds the capture of its own cutoff under its own source
    policy, named by the function the daily capture stage registers it under, and
    that capture must carry the collection's full policy digest. Its source digest must
    be the digest of the daily ledger copy of the bound receipt under the given collect
    attempt, rebuilt with the engine's own functions, so a capture that closed another
    execution or attempt is refused. The scope is always supplied: the daily capture
    plan names it and no copy is pinned here."""
    if entries is None:
        return _unproven("captures_not_supplied")
    if not entries:
        return _unproven("captures_empty")
    if scope is None:
        return _unproven("capture_scope_not_supplied")
    if type(profiles) not in (tuple, list):
        return _unproven("capture_profiles_invalid")
    if any(
        not isinstance(profile, str) or not profile or profile != profile.strip()
        for profile in profiles
    ):
        return _unproven("capture_profiles_invalid")
    derived = None
    if collection is not None:
        try:
            derived = staging_profile_id(
                date.fromisoformat(collection["cutoff"]), collection["policy_sha256"]
            )
        except (KeyError, TypeError, ValueError):
            return _unproven("capture_collection_profile_invalid")
        if collection_attempt is None:
            return _unproven("collection_attempt_not_supplied")
        if (
            not isinstance(collection_attempt, str)
            or not collection_attempt
            or collection_attempt != collection_attempt.strip()
        ):
            return _unproven("collection_attempt_invalid")
        try:
            closed = receipt_source_digest(
                daily_ledger_copy(collection_receipt, attempt_id=collection_attempt)
            )
        except (AttributeError, TypeError, ValueError):
            return _unproven("capture_collection_profile_invalid")
    required = sorted({*profiles, *([derived] if derived else [])})
    if not required:
        return _unproven("capture_profiles_not_supplied")
    parsed = []
    for entry in entries:
        copy_ = dict(entry) if type(entry) is dict else entry
        if type(copy_) is dict:
            for field in _CAPTURE_INSTANTS:
                instant = _instant(copy_.get(field))
                if instant is not None:
                    copy_[field] = instant
        parsed.append(copy_)
    diagnostics = _capture_diagnostics(parsed, scope=scope, now=now)
    if any(item["profile_id"] is None for item in diagnostics):
        return _unproven("capture_readback_malformed", diagnostics)
    selected = []
    for profile_id in required:
        try:
            choice = capture_registry.explain_capture_selection(
                parsed, now=now, scope=scope, profile_id=profile_id
            )["selected"]
        except ValueError as error:
            return _unproven(
                "capture_" + str(error).removeprefix("capture_"), diagnostics
            )
        if choice is None:
            return _unproven("capture_none_eligible:" + profile_id, diagnostics)
        if (
            profile_id == derived
            and choice["policy_digest"] != collection["policy_sha256"]
        ):
            return _unproven("capture_collection_policy_mismatch", diagnostics)
        if profile_id == derived and choice["source_digest"] != closed:
            return _unproven("capture_collection_receipt_mismatch", diagnostics)
        selected.append(choice)
    bound = [
        {
            "profile_id": checked["profile_id"],
            "profile_version": checked["profile_version"],
            "scope": checked["scope"],
            "operation_id": checked["operation_id"],
            "result_id": checked["result_id"],
            "generation": checked["generation"],
            "content_digest": checked["content_digest"],
            "source_digest": checked["source_digest"],
            "markets": list(checked["markets"]),
            "observation_window_end": release._stamp(checked["observation_window_end"]),
            "capture_available_at": release._stamp(checked["capture_available_at"]),
        }
        for checked in selected
    ]
    return _bound(
        sorted(
            bound, key=lambda item: (item["observation_window_end"], item["profile_id"])
        ),
        "capture registry readback, latest eligible capture of each required "
        "profile selected by the registry's own selection rules at the binding time",
        {
            "scope": scope,
            "required_profiles": required,
            "collection_profile": derived,
            "collection_attempt_id": collection_attempt if derived else None,
            "diagnostics": diagnostics,
        },
    )


def _released_runs_row(records, *, now):
    if records is None:
        return _unproven("release_records_not_supplied")
    if not records:
        return _unproven("release_records_empty")
    runs = []
    for record in records:
        if type(record) is not dict or set(record) != set(QUALITY_RELEASE_FIELDS):
            return _unproven("release_record_invalid")
        if not isinstance(record["run_id"], str) or not _RUN_ID.fullmatch(
            record["run_id"]
        ):
            return _unproven("release_record_invalid")
        if record["release_contract_version"] != RELEASE_RECORD_CONTRACT:
            return _unproven("release_record_contract_mismatch")
        if any(not release._hex64(record[field]) for field in _DIGEST_FIELDS):
            return _unproven("release_record_invalid")
        released_at = _instant(record["released_at"])
        if released_at is None:
            return _unproven("release_record_invalid")
        if released_at > now:
            return _unproven("release_record_from_future")
        runs.append(
            {
                "run_id": record["run_id"],
                "run_receipt_digest": record["run_receipt_digest"],
                "released_at": release._stamp(released_at),
            }
        )
    if len({run["run_id"] for run in runs}) != len(runs):
        return _unproven("release_record_duplicate")
    return _bound(
        sorted(runs, key=lambda item: (item["released_at"], item["run_id"])),
        "quality release record readback, one row per released run",
    )


def _json_text(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def _tool_instant(value):
    """A time as the readback tool spells it, UTC with a +00:00 offset, else None."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.astimezone(UTC).isoformat() != value:
        return None
    return parsed


def _ingest_groups(groups, window):
    """The raw groups as the readback tool wrote them: one dict per market and source
    in market and source order, a positive integer count and a collection time inside
    the run's window."""
    _require(
        groups == sorted(groups, key=lambda group: (group["market"], group["source"])),
        "ingest_groups_invalid",
    )
    for group in groups:
        _require(
            type(group) is dict
            and isinstance(group["source"], str)
            and type(group["row_count"]) is int
            and group["row_count"] > 0,
            "ingest_groups_invalid",
        )
        at = _tool_instant(group["last_collected_at"])
        _require(
            at is not None and window["window_start"] <= at < window["window_end"],
            "ingest_groups_invalid",
        )
    return groups


def _recheck_ingest(readback):
    """Run the readback tool's own checks again over what the readback recorded.

    The stored row is rebuilt from the parsed receipt, whose canonical text must hash
    to the digest the warehouse computed, so a receipt field edited after the read
    fails here and every identity value is one that digest covers. The tool's pinned
    policy and profile, its build validation and the daily stage's admission then
    run on that row, and the raw groups are held to the receipt again. Returns the
    rebuilt receipt, or refuses with a named error.
    """
    row, raw = readback["receipt_row"], readback["raw_content"]
    receipt, expected = row["receipt"], readback["expected"]
    _require(set(readback) == INGEST_READBACK_FIELDS, "ingest_readback_fields")
    _require(
        readback["tables"]
        == {
            "receipts": read_ingest_run.RECEIPT_TABLE_ID,
            "raw_content": read_ingest_run.RAW_TABLE_ID,
        },
        "ingest_tables_mismatch",
    )
    build = read_ingest_run._build(
        {"source_sha": expected["source_sha"], "image_uri": expected["image_uri"]}
    )
    cutoff = date.fromisoformat(readback["cutoff"])
    _require(cutoff.isoformat() == readback["cutoff"], "ingest_cutoff_invalid")
    payload = canonical_bytes(receipt).decode("utf-8")
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    rebuilt = {
        "execution_id": row["execution_id"],
        "trend_date": row["trend_date"],
        "receipt_json": payload,
        "receipt_sha256": digest,
        "computed_sha256": digest,
        "recorded_at": row["recorded_at"],
    }
    summary, reasons, parsed = read_ingest_run.check_receipt_rows(
        [rebuilt], execution_id=readback["execution_id"], cutoff=cutoff, build=build
    )
    _require(not reasons, "ingest_receipt_refused")
    # Compared as JSON text, not with ==, so a 1 or a 3.0 the tool never writes does
    # not pass for True or 3.
    _require(
        _json_text(summary) == _json_text({**row, "receipt": parsed}),
        "ingest_receipt_summary",
    )
    start = datetime.combine(cutoff + timedelta(days=1), time.min, UTC)
    window = {
        "window_start": start,
        "window_end": start + timedelta(days=read_ingest_run.RAW_WINDOW_DAYS),
    }
    _require(
        expected
        == {
            "trend_date": start.date().isoformat(),
            "authority_kind": read_ingest_run.VERIFIED_AUTHORITY,
            "policy_sha256": read_ingest_run.EXPECTED_POLICY_SHA256,
            "profile_sha256": read_ingest_run.EXPECTED_PROFILE_SHA256,
            **build,
        },
        "ingest_expected_mismatch",
    )
    groups = _ingest_groups(raw["groups"], window)
    recomputed, raw_reasons = read_ingest_run.check_raw_groups(groups, parsed, window)
    _require(
        not raw_reasons and _json_text(recomputed) == _json_text(raw),
        "ingest_raw_refused",
    )
    return parsed


def _collection_row(readback, *, now):
    if readback is None:
        return _unproven("ingest_readback_not_supplied")
    if readback.get("contract_version") != INGEST_READBACK_CONTRACT:
        return _unproven("ingest_readback_contract_mismatch")
    if readback.get("mode") != "read":
        return _unproven("ingest_readback_not_read")
    if readback.get("state") != "verified" or readback.get("reasons") != []:
        return _unproven("ingest_readback_not_verified")
    row = readback.get("receipt_row")
    if type(row) is dict and row.get("admission_scope") == "digests_only":
        return _unproven("ingest_build_not_bound")
    try:
        receipt = _recheck_ingest(readback)
    except (
        Refusal,
        read_ingest_run.ReadRefused,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ):
        return _unproven("ingest_readback_inconsistent")
    raw = readback["raw_content"]
    recorded_at = _tool_instant(row["recorded_at"])
    collected = [_instant(group["last_collected_at"]) for group in raw["groups"]]
    if recorded_at is None:
        return _unproven("ingest_readback_inconsistent")
    if recorded_at > now or max(collected) > now:
        return _unproven("ingest_readback_from_future")
    return _bound(
        {
            "execution_id": receipt["execution_id"],
            "run_id": receipt["run_id"],
            "cutoff": receipt["cutoff"],
            "receipt_sha256": row["receipt_sha256"],
            **{field: receipt[field] for field in _INGEST_BUILD_FIELDS},
            "markets": sorted(receipt["market_states"]),
            "raw_rows": receipt["raw_rows_persisted"],
        },
        "owner ingest readback, checked again by the readback tool's own checks: the "
        "execution's one collection receipt under the pinned policy and profile, "
        "admitted by the daily stage with its build, and the run's raw rows",
        {
            "recorded_at": release._stamp(recorded_at),
            "last_collected_at": release._stamp(max(collected)),
            "last_collected_by_market": {
                market: release._stamp(_instant(value))
                for market, value in sorted(raw["last_collected_by_market"].items())
            },
        },
    )


# Kernel


def bind(
    *,
    index_path,
    plan_path,
    verify_path,
    now,
    max_age=timedelta(minutes=DEFAULT_MAX_AGE_MINUTES),
    resume_path=None,
    resume_plan_path=None,
    repo=None,
    captures_path=None,
    capture_scope=None,
    capture_profiles=(),
    release_records_path=None,
    ingest_readback_path=None,
    collection_attempt_id=None,
):
    """Return the candidate binding receipt. Malformed inputs refuse; a check
    that fails leaves its subject unproven with the reason."""
    _require(isinstance(now, datetime) and now.tzinfo is not None, "clock_invalid")
    index, index_raw = _load(index_path, "release_index_invalid")
    release.validate_release_index_shape(index)
    plan, plan_sha256 = release._load_plan(plan_path, "release")
    verify, verify_raw = _load(verify_path, "verify_receipt_invalid")
    resume = None if resume_path is None else _load(resume_path, "resume_invalid")[0]
    resume_plan = (
        None
        if resume_plan_path is None
        else release._load_plan(resume_plan_path, "resume")
    )
    captures = _load_optional_list(captures_path, "captures_invalid")
    records = _load_optional_list(release_records_path, "release_records_invalid")
    readback = (
        None
        if ingest_readback_path is None
        else _load(ingest_readback_path, "ingest_readback_invalid")[0]
    )
    binding = plan["binding"]
    tag = plan["expected"]["tag"]
    rows = {}
    rows["verify_receipt"] = _verify_row(
        verify, index, index_raw, plan_sha256, now, max_age
    )
    verified = rows["verify_receipt"]["state"] == "bound"
    drift = _drift_items(verify)

    def from_verify(row):
        return row if verified else _unproven("verify_receipt_unproven")

    # The binding comparisons against the plan below repeat what a genuine
    # verify receipt already required before it recorded the index as
    # verified; they stay so a hand made receipt cannot carry a foreign index.
    if index["commit"] != binding["lp_commit"]:
        rows["repository_commit"] = _unproven("commit_binding_mismatch")
    else:
        rows["repository_commit"] = from_verify(
            _bound(
                {"repository": index["repository"], "commit": index["commit"]},
                "release index commit read against the build that pushed the app image",
            )
        )
    rows["trees"] = from_verify(
        _bound(
            {
                "engine_tree_sha256": index["engine_tree_sha256"],
                "app_tree_sha256": index["app_tree_sha256"],
                "ops_tree_sha256": index["ops_tree_sha256"],
                "resource_manifest_sha256": index["resource_manifest_sha256"],
                "contract_digests": dict(sorted(index["contract_digests"].items())),
            },
            "pinned tree inventories read back and hashed by release.py verify",
        )
    )
    if index["app_image"] != plan["expected"]["image"]:
        rows["images"] = _unproven("app_image_binding_mismatch")
    else:
        rows["images"] = from_verify(
            _bound(
                {
                    "app_image": index["app_image"],
                    "engine_image": index["engine_image"],
                    "app_build": index["app_build"],
                    "engine_build": index["engine_build"],
                },
                "registry manifests and build receipts read by release.py verify",
            )
        )
    revision_row, traffic_row = _service_rows(verify, plan, binding, tag, index, drift)
    rows["service_revision"] = from_verify(revision_row)
    rows["traffic"] = from_verify(traffic_row)
    rows["queue"] = from_verify(_queue_row(verify, plan, resume, resume_plan))
    ledger = (verify.get("native") or {}).get("ledger") or {}
    if (
        index["policy_digest"] != binding["policy_digest"]
        or index["deployment_digest"] != binding["deployment_digest"]
    ):
        rows["policy_deployment"] = _unproven("policy_binding_mismatch")
    elif any(item == "ledger" for item, _ in drift) or (
        ledger.get("active_deployment_digest") != binding["deployment_digest"]
    ):
        rows["policy_deployment"] = _unproven("ledger_not_active")
    else:
        rows["policy_deployment"] = from_verify(
            _bound(
                {
                    "policy_digest": index["policy_digest"],
                    "deployment_digest": index["deployment_digest"],
                },
                "policy and deployment objects hashed and ledger active binding read "
                "by release.py verify",
                {"ledger_generation": ledger.get("generation")},
            )
        )
    rows["source_binding"] = from_verify(
        _bound(
            {
                "evidence_mode": index["evidence_mode"],
                "source_binding": dict(sorted(index["source_binding"].items())),
                "raw_authority_objects": sorted(
                    (
                        dict(sorted(item.items()))
                        for item in index["raw_authority_objects"]
                    ),
                    key=lambda item: item["name"],
                ),
            },
            "pinned source binding and authority object copies hashed by "
            "release.py verify",
        )
    )
    if not index["served_assets"]:
        rows["served_assets"] = _unproven("served_assets_empty")
    else:
        rows["served_assets"] = from_verify(
            _bound(
                {
                    "origin": release.asset_origin(
                        binding["canonical_service_audience"], tag
                    ),
                    "assets": dict(sorted(index["served_assets"].items())),
                },
                "each asset fetched from the tagged revision and hashed by "
                "release.py verify",
            )
        )
    rows["design_package"] = _design_package_row(repo, index)
    rows["collection"] = _collection_row(readback, now=now)
    collection = rows["collection"]
    bound_collection = collection["state"] == "bound"
    rows["captures"] = _captures_row(
        captures,
        scope=capture_scope,
        profiles=capture_profiles,
        now=now,
        collection=collection["value"] if bound_collection else None,
        collection_receipt=_recheck_ingest(readback) if bound_collection else None,
        collection_attempt=collection_attempt_id,
    )
    rows["released_runs"] = _released_runs_row(records, now=now)
    unproven = [name for name in SUBJECTS if rows[name]["state"] != "bound"]
    identity = {name: rows[name]["value"] for name in IDENTITY_SUBJECTS}
    return {
        "contract_version": CONTRACT,
        "state": "bound" if not unproven else "unproven",
        "candidate_id": release.canonical_digest(identity) if not unproven else None,
        "bound_at": release._stamp(now),
        "max_age_minutes": max_age.total_seconds() / 60,
        "evidence_mode": index["evidence_mode"],
        "inputs": {
            "release_index_sha256": _sha256(index_raw),
            "plan_sha256": plan_sha256,
            "verify_receipt_sha256": _sha256(verify_raw),
        },
        "subjects": rows,
        "unproven": unproven,
    }


def changed_subjects(previous, current):
    """Identity subjects whose bound value differs between two receipts. A
    subject unproven in either receipt counts as changed."""
    for receipt in (previous, current):
        _require(
            type(receipt) is dict and receipt.get("contract_version") == CONTRACT,
            "binding_receipt_invalid",
        )
    changed = []
    for name in IDENTITY_SUBJECTS:
        before = previous["subjects"].get(name) or {}
        after = current["subjects"].get(name) or {}
        if (
            before.get("state") != "bound"
            or after.get("state") != "bound"
            or before.get("value") != after.get("value")
        ):
            changed.append(name)
    return changed


# Guide section


def render_guide_section(receipt):
    """The candidate section of Albert's staging guide. Every candidate value
    is copied from the binding receipt; nothing is filled from memory."""
    _require(
        type(receipt) is dict and receipt.get("contract_version") == CONTRACT,
        "binding_receipt_invalid",
    )
    subjects = receipt["subjects"]
    ready = receipt["state"] == "bound"

    def value(name):
        row = subjects[name]
        return row["value"] if row["state"] == "bound" else None

    lines = [f"<!-- {GUIDE_CONTRACT} -->", "## The candidate you are testing", ""]
    if ready:
        lines.append(
            "Ready for you to test. Staging was read back at "
            f"{subjects['verify_receipt']['value']['checked_at']}; candidate "
            f"`{receipt['candidate_id']}`."
        )
    else:
        lines.append(
            "Not ready for testing. These checks did not pass: "
            + ", ".join(receipt["unproven"])
            + ". Values shown are the ones that did pass."
        )
    traffic = value("traffic")
    revision = value("service_revision")
    commit = value("repository_commit")
    design = value("design_package")
    lines += ["", "### Where", ""]
    lines.append(f"URL: {traffic['url']}" if traffic else "URL: unproven")
    lines.append(
        f"Revision: `{revision['revision_name']}`" if revision else "Revision: unproven"
    )
    lines.append(f"Commit: `{commit['commit']}`" if commit else "Commit: unproven")
    lines.append(
        f"Design package: {design['name']} {design['version']}"
        if design
        else "Design package: unproven"
    )
    lines += ["", "### Source dates", ""]
    captures = value("captures")
    if captures:
        for capture in captures:
            lines.append(
                f"Capture {capture['profile_id']}: data up to "
                f"{capture['observation_window_end']}, available from "
                f"{capture['capture_available_at']}, markets "
                f"{', '.join(capture['markets'])}."
            )
    else:
        lines.append("No capture identity was proven for this candidate.")
    runs = value("released_runs")
    if runs:
        for run in runs:
            lines.append(f"Released run {run['run_id']} at {run['released_at']}.")
    else:
        lines.append("No released run was proven for this candidate.")
    collection = value("collection")
    if collection:
        last = subjects["collection"]["evidence"]["last_collected_at"]
        lines.append(
            f"Collection for {collection['cutoff']}: run {collection['run_id']}, "
            f"{collection['raw_rows']} raw rows in {', '.join(collection['markets'])}, "
            f"last collected at {last}."
        )
    else:
        lines.append("No ingest collection was proven for this candidate.")
    lines += ["", "### What to test", ""]
    lines += [
        f"{number}. {text}" for number, (_, text) in enumerate(MANDATORY_JOURNEYS, 1)
    ]
    lines += ["", "### Known limits", ""]
    limits = []
    if captures:
        covered = {market for capture in captures for market in capture["markets"]}
        missing = [market for market in CAPTURE_MARKETS if market not in covered]
        if missing:
            limits.append(
                "No capture covers these markets: " + ", ".join(missing) + "."
            )
    limits += [
        f"{name} is unproven: {subjects[name]['reason']}."
        for name in receipt["unproven"]
    ]
    lines += limits or ["None recorded by the binding checks."]
    lines += ["", "### Feedback", "", "Copy this for each thing you try:", ""]
    lines += [f"{field}: " for field in FEEDBACK_FIELDS]
    return "\n".join(lines) + "\n"


# CLI


def release_record_contract_in_script():
    """The release record contract the release script declares, read from its
    source so the pin above can be tested without importing BigQuery."""
    script = _ROOT / "engine/scripts/staging/release_open_intelligence_run.py"
    tree = ast.parse(script.read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and getattr(node.targets[0], "id", None) == "RELEASE_CONTRACT_VERSION"
        ):
            return ast.literal_eval(node.value)
    return None


def build_parser():
    parser = argparse.ArgumentParser(
        prog="bind_candidate.py", description=__doc__.splitlines()[0]
    )
    commands = parser.add_subparsers(dest="command", required=True)
    bind_command = commands.add_parser(
        "bind", help="write the candidate binding receipt from native readbacks"
    )
    bind_command.add_argument("--release-index", required=True)
    bind_command.add_argument("--plan", required=True, help="release plan verified")
    bind_command.add_argument(
        "--verify", required=True, help="release.py verify receipt with the index"
    )
    bind_command.add_argument(
        "--resume", default=None, help="running_verified resume receipt, if resumed"
    )
    bind_command.add_argument(
        "--resume-plan", default=None, help="the resume plan that receipt ran"
    )
    bind_command.add_argument(
        "--repo", default=None, help="clean checkout whose app tree is the candidate's"
    )
    bind_command.add_argument("--captures", default=None, help="capture entries JSON")
    bind_command.add_argument(
        "--capture-scope",
        default=None,
        help="scope the captures must carry, as the daily capture plan names it",
    )
    bind_command.add_argument(
        "--capture-profile",
        action="append",
        default=None,
        help="further required capture profile id from the approved profile; repeatable",
    )
    bind_command.add_argument(
        "--release-records", default=None, help="quality release records JSON"
    )
    bind_command.add_argument(
        "--ingest-readback", default=None, help="read_ingest_run.py receipt"
    )
    bind_command.add_argument(
        "--collection-attempt-id",
        default=None,
        help="the daily collect stage's attempt id the collection ledger keys the "
        "ingest receipt under",
    )
    bind_command.add_argument(
        "--max-age-minutes", type=int, default=DEFAULT_MAX_AGE_MINUTES
    )
    bind_command.add_argument("--output", required=True, help="write-once receipt")
    guide = commands.add_parser("guide", help="render the guide's candidate section")
    guide.add_argument("--binding", required=True)
    guide.add_argument("--output", required=True, help="write-once markdown")
    compare = commands.add_parser(
        "compare", help="list identity subjects changed between two receipts"
    )
    compare.add_argument("--previous", required=True)
    compare.add_argument("--current", required=True)
    return parser


def execute(argv, *, now=None) -> int:
    args = build_parser().parse_args(argv)
    output = getattr(args, "output", None)
    try:
        if args.command == "bind":
            release._require_absent(args.output)
            _require(1 <= args.max_age_minutes <= 1440, "max_age_invalid")
            receipt = bind(
                index_path=args.release_index,
                plan_path=args.plan,
                verify_path=args.verify,
                resume_path=args.resume,
                resume_plan_path=args.resume_plan,
                repo=args.repo,
                captures_path=args.captures,
                capture_scope=args.capture_scope,
                capture_profiles=tuple(args.capture_profile or ()),
                release_records_path=args.release_records,
                ingest_readback_path=args.ingest_readback,
                collection_attempt_id=args.collection_attempt_id,
                now=now or datetime.now(UTC),
                max_age=timedelta(minutes=args.max_age_minutes),
            )
            release._write_once(args.output, receipt)
            code = 0 if receipt["state"] == "bound" else 1
            summary = {
                "state": receipt["state"],
                "candidate_id": receipt["candidate_id"],
                "unproven": receipt["unproven"],
                "output": str(args.output),
            }
        elif args.command == "guide":
            release._require_absent(args.output)
            receipt, _ = _load(args.binding, "binding_receipt_invalid")
            text = render_guide_section(receipt)
            with open(args.output, "x", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
            code = 0 if receipt["state"] == "bound" else 1
            summary = {"state": receipt["state"], "output": str(args.output)}
        else:
            previous, _ = _load(args.previous, "binding_receipt_invalid")
            current, _ = _load(args.current, "binding_receipt_invalid")
            changed = changed_subjects(previous, current)
            code = 0 if not changed else 1
            summary = {
                "state": "unchanged" if not changed else "changed",
                "changed": changed,
            }
    except ValueError as error:
        summary = {"state": "refused", "error": str(error), "output": str(output)}
        code = 1
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        summary = {
            "state": "refused",
            "error": f"{type(error).__name__}: {error}",
            "output": str(output),
        }
        code = 1
    print(json.dumps(summary, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(execute(sys.argv[1:]))
