"""Owner run words for the approved provisioning delta's non routine bindings.

The iam-v2 words of the execution store migration grant the delta's routine
authorizations and routine level bindings, and runtime_jobs renders the job
bindings it creates jobs with. Nothing else applies the rest of the delta: the
dataset grants, the project level job user and log writer rows, the Cloud Run,
Artifact Registry, Cloud Tasks and service account rows, the conditional bucket
rows and the version pinned secret row. These three words do, for exactly the
rows of the approved amendment d delta and nothing else.

iam-delta-plan reads every targeted policy and diffs it against the rows.
iam-delta-dry-run also renders the exact write requests (dataset access patch,
setIamPolicy, bucket IAM put) and sends none. iam-delta-apply sends only the
additions, each guarded by the etag it read, never removes a member, then reads
every policy again and fails closed unless every row it could reach is present
and every binding that was there before still is. A second apply adds nothing.

The delta is pinned by the digest of its bytes and validated against the pinned
resource manifest before any row is selected. A row the delta leaves under an
unresolved topic is refused, and so is a project level row outside the two
classes the delta approves at project level (job user under the manifest
precedent label, the build identity's log writer). Routine rows stay with the
iam-v2 words and are only counted here. Conditions are written verbatim; IAM
needs a title on a conditional binding and the delta carries none, so the title
is derived from the expression and a readback matches on the expression alone.

Every URL is built from a pinned endpoint and an identifier that matched its own
grammar, never by concatenating a document's resource name onto an endpoint.
The write guard runs where the request is built, so the dry run and the apply
send path go through the same check. It permits only rows of the pinned delta,
loaded here from the pinned bytes, never a row set a caller hands it, and it
refuses a policy whose version drops below what was read or a conditional
policy written below version 3. An apply that could not reach every targeted
resource emits its receipt and exits non zero.
"""

import argparse
import copy
import functools
import hashlib
import http.client
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from ops.deploy.iam_delta import validate_iam_delta
from ops.deploy.resource_guard import load_resource_manifest
from ops.deploy.runtime_native_adapter import NativeError, no_redirect_transport

ROOT = Path(__file__).resolve().parents[2]
# ops/deploy/iam_delta_v1.json as approved in amendment d (approved 2026-09-20), with
# the resource manifest it was approved against. Both files have since moved on to
# amendment e, so these words read the approved amendment d bytes retained under
# engine/configs and never the amendment e rows.
_RETAINED = ROOT / "engine" / "configs" / "open_intelligence"
DELTA_PATH = _RETAINED / "iam_delta_amendment_d.json"
MANIFEST_PATH = _RETAINED / "resource_manifest_amendment_d.json"
DELTA_SHA256 = "1d2e8ead5a7c17c51b53ca640ed4c295294aef204ce4abeee9f03c7e822d5e3e"
MANIFEST_SHA256 = "ee809a4e81dec5242ea71ddd703990c5e0a9e237613e11135e069be0ea51ad96"
CONTRACT_VERSION = "42_iam_delta_bindings_v1"
WORDS = {
    "iam-delta-plan": "plan",
    "iam-delta-dry-run": "dry_run",
    "iam-delta-apply": "apply",
}
STATES = ("owner-gcloud", "adc")
# main's exit code for an apply that ran cleanly but could not reach every row.
EXIT_INCOMPLETE = 3

PROJECT = "ogilvy-trends-v2"
PROJECT_NUMBER = "590353929363"
REGION = "us-central1"
_MAX_DELTA_BYTES = 4 * 1024 * 1024

BIGQUERY_ENDPOINT = (
    f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}/datasets/"
)
PROJECT_ENDPOINT = f"https://cloudresourcemanager.googleapis.com/v1/projects/{PROJECT}"
RUN_ENDPOINT = f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/"
REGISTRY_ENDPOINT = (
    f"https://artifactregistry.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/"
)
TASKS_ENDPOINT = (
    f"https://cloudtasks.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/"
)
ACCOUNT_ENDPOINT = f"https://iam.googleapis.com/v1/projects/{PROJECT}/serviceAccounts/"
STORAGE_ENDPOINT = "https://storage.googleapis.com/storage/v1/b/"
SECRET_ENDPOINT = f"https://secretmanager.googleapis.com/v1/projects/{PROJECT}/secrets/"
_POLICY_V3_QUERY = "?options.requestedPolicyVersion=3"
_POLICY_V3_BODY = {"options": {"requestedPolicyVersion": 3}}

_LOWER_63 = r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
_ACCOUNT = (
    rf"[a-z][a-z0-9-]{{4,28}}[a-z0-9]@{re.escape(PROJECT)}\.iam\.gserviceaccount\.com"
)
_P = re.escape(PROJECT)
_L = re.escape(REGION)
_GRAMMARS = (
    (
        "bigquery_dataset",
        re.compile(
            rf"//bigquery\.googleapis\.com/projects/{_P}/datasets/([A-Za-z_][A-Za-z0-9_]{{0,1023}})"
        ),
    ),
    (
        "project",
        re.compile(rf"//cloudresourcemanager\.googleapis\.com/projects/{_P}()"),
    ),
    (
        "run",
        re.compile(
            rf"//run\.googleapis\.com/projects/{_P}/locations/{_L}/((?:jobs|services)/{_LOWER_63})"
        ),
    ),
    (
        "artifactregistry",
        re.compile(
            rf"//artifactregistry\.googleapis\.com/projects/{_P}/locations/{_L}/"
            r"(repositories/[a-z][a-z0-9-]{2,61}[a-z0-9])"
        ),
    ),
    (
        "cloudtasks",
        re.compile(
            rf"//cloudtasks\.googleapis\.com/projects/{_P}/locations/{_L}/(queues/{_LOWER_63})"
        ),
    ),
    (
        "service_account",
        re.compile(
            rf"//iam\.googleapis\.com/projects/{_P}/serviceAccounts/({_ACCOUNT})"
        ),
    ),
    (
        "bucket",
        re.compile(
            r"//storage\.googleapis\.com/projects/_/buckets/([a-z0-9][a-z0-9._-]{1,61}[a-z0-9])"
        ),
    ),
    (
        "secret_version",
        re.compile(
            rf"//secretmanager\.googleapis\.com/projects/{_P}/secrets/([A-Za-z0-9_-]{{1,255}})"
            r"/versions/([1-9][0-9]{0,8})"
        ),
    ),
    (
        "secret",
        re.compile(
            rf"//secretmanager\.googleapis\.com/projects/{_P}/secrets/([A-Za-z0-9_-]{{1,255}})"
        ),
    ),
)
_ROUTINE = re.compile(
    rf"//bigquery\.googleapis\.com/projects/{_P}/datasets/[A-Za-z_][A-Za-z0-9_]*/routines/[A-Za-z_][A-Za-z0-9_]*"
)
_MEMBER = re.compile(rf"serviceAccount:({_ACCOUNT})")
_ROLE = re.compile(
    rf"roles/[A-Za-z][A-Za-z0-9.]*|projects/{_P}/roles/[A-Za-z][A-Za-z0-9_.]*"
)
# Kinds whose rows this tool writes. A secret container row would grant every
# version, and nothing in the delta approves one, so only version rows are served.
_SUPPORTED = frozenset(
    {
        "artifactregistry",
        "bigquery_dataset",
        "bucket",
        "cloudtasks",
        "project",
        "run",
        "secret_version",
        "service_account",
    }
)
_CONDITIONED = frozenset({"bucket", "secret_version"})
_DATASET_ROLES = {
    "OWNER": "roles/bigquery.dataOwner",
    "WRITER": "roles/bigquery.dataEditor",
    "READER": "roles/bigquery.dataViewer",
}
_DATASET_LEGACY = {role: legacy for legacy, role in _DATASET_ROLES.items()}

_JOB_USER = "roles/bigquery.jobUser"
_LOG_WRITER = "roles/logging.logWriter"
_BUILDS_VIEWER = "roles/cloudbuild.builds.viewer"
_SERVICE = f"//run.googleapis.com/projects/{PROJECT}/locations/{REGION}/services/listening-post-staging"
_SCHEDULER_ACCOUNT = (
    f"//iam.googleapis.com/projects/{PROJECT}/serviceAccounts/"
    f"intelligence-42-scheduler@{PROJECT}.iam.gserviceaccount.com"
)
_APPROVALS_BUCKET = "//storage.googleapis.com/projects/_/buckets/ogilvy-trends-v2-execution-approvals-staging"
_DEPLOYMENT = frozenset({"build", "deploy"})


def _scheduler_row(row, kind):
    return (
        row["resource"].startswith("//cloudscheduler.googleapis.com/")
        or row["role"].startswith("roles/cloudscheduler.")
        or (
            row["role"] == "roles/iam.serviceAccountUser"
            and row["resource"] == _SCHEDULER_ACCOUNT
        )
    )


# Each unresolved topic of the delta, as the rows it would cover. The topics carry
# no resource or role of their own, so the mapping is pinned here; a delta naming a
# topic this table does not hold is refused whole rather than guessed at. Where a
# topic names members, only those members' rows are caught.
_UNRESOLVED = {
    "cloud_scheduler_job_iam": _scheduler_row,
    "execution_approvals_bucket": lambda row, kind: (
        row["resource"] == _APPROVALS_BUCKET
    ),
    "human_approver": lambda row, kind: (
        re.search(
            r"/routines/sp_(approve_open_intelligence_execution|disable_open_intelligence_execution_approval)_v2$",
            row["resource"],
        )
        is not None
    ),
    "ingestion_source_credentials": lambda row, kind: (
        kind in {"secret", "secret_version"}
    ),
    "cloudbuild_builds_viewer": lambda row, kind: row["role"] == _BUILDS_VIEWER,
    "price_policy_custom_run_role": lambda row, kind: (
        row["role"] == "roles/run.developer" and row["resource"] == _SERVICE
    ),
}
_PROJECT_LEVEL_UNRESOLVED_ROLES = {"cloudbuild_builds_viewer": _BUILDS_VIEWER}


def _refuse(code):
    raise ValueError(code)


def parse_resource(resource):
    """The kind and identifier of one delta resource name, or a refusal."""
    if not isinstance(resource, str) or not resource.isascii():
        _refuse("resource_name_invalid")
    for kind, grammar in _GRAMMARS:
        match = grammar.fullmatch(resource)
        if match is None:
            continue
        if kind == "bucket" and ".." in match.group(1):
            _refuse("resource_name_invalid")
        return kind, match.groups()
    _refuse("resource_name_invalid")


def _kind_or_none(resource):
    # A row of a kind with no grammar here is refused below, not fatal: it may
    # still belong to an unresolved topic, and the receipt should say which.
    try:
        return parse_resource(resource)[0]
    except ValueError:
        return None


def target_of(resource):
    """The resource whose policy holds the row: a secret version's secret."""
    kind, groups = parse_resource(resource)
    if kind == "secret_version":
        return f"//secretmanager.googleapis.com/projects/{PROJECT}/secrets/{groups[0]}"
    return resource


def _secret_condition(resource):
    _kind, (secret, version) = parse_resource(resource)
    return (
        "resource.type == 'secretmanager.googleapis.com/SecretVersion' && "
        f"resource.name == 'projects/{PROJECT_NUMBER}/secrets/{secret}/versions/{version}'"
    )


def condition_title(expression):
    return (
        "42 iam delta v1 " + hashlib.sha256(expression.encode("utf-8")).hexdigest()[:16]
    )


def _condition(row):
    if row.condition is None:
        return None
    return {"title": condition_title(row.condition), "expression": row.condition}


@dataclass(frozen=True)
class Row:
    member: str
    role: str
    resource: str
    condition: str | None
    kind: str
    target: str

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Targets:
    delta_sha256: str
    manifest_sha256: str
    members: tuple
    rows: tuple
    refused: list
    project_level: list
    project_level_unresolved: list
    routine_rows: int


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _refuse("iam_delta_bindings_delta_invalid")
        value[key] = item
    return value


def load_manifest(path=MANIFEST_PATH):
    return load_resource_manifest(path, expected_sha256=MANIFEST_SHA256)


def _member_name(value):
    if isinstance(value, str) and _MEMBER.fullmatch(value):
        return value
    if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", value):
        return f"serviceAccount:{value}@{PROJECT}.iam.gserviceaccount.com"
    _refuse("iam_delta_bindings_member_unknown")


def _identities(manifest):
    keys = {}
    for key, name in manifest["identities"].items():
        keys.setdefault("serviceAccount:" + name.rsplit("/", 1)[1], set()).add(key)
    return keys


def _project_basis(row, identity_keys):
    if row["condition"] is not None:
        return None
    if (
        row["role"] == _JOB_USER
        and row["purpose"].startswith("manifest_precedent:")
        and not identity_keys & _DEPLOYMENT
    ):
        return "manifest_precedent"
    if row["role"] == _LOG_WRITER and identity_keys == {"build"}:
        return "build_project_log_writer"
    return None


def select_targets(delta, manifest, digest, members=None, *, validate=True):
    """The delta rows this tool may grant, and every row it refuses and why."""
    if validate:
        validate_iam_delta(delta, manifest)
    approval = delta.get("approval") if type(delta) is dict else None
    if type(approval) is not dict or approval.get("state") != "approved":
        _refuse("iam_delta_bindings_delta_unapproved")
    if approval.get("applied") is not False:
        _refuse("iam_delta_bindings_delta_invalid")
    if (delta.get("project"), delta.get("project_number"), delta.get("region")) != (
        PROJECT,
        PROJECT_NUMBER,
        REGION,
    ):
        _refuse("iam_delta_bindings_delta_invalid")
    unresolved = {}
    for entry in delta.get("unresolved") or []:
        if entry.get("topic") not in _UNRESOLVED:
            _refuse("iam_delta_bindings_unresolved_unmapped")
        unresolved[entry["topic"]] = frozenset(entry.get("members") or ())
    rows = delta.get("bindings")
    if type(rows) is not list:
        _refuse("iam_delta_bindings_delta_invalid")
    bound = {row.get("member") for row in rows if type(row) is dict}
    selected = None
    if members:
        selected = {_member_name(value) for value in members}
        if not selected <= bound:
            _refuse("iam_delta_bindings_member_unknown")
    forbidden = {entry.get("resource") for entry in delta.get("must_not_grant") or []}
    identities = _identities(manifest)
    targets, refused, project_level = [], [], []
    routine_rows = 0
    seen = set()
    for row in rows:
        if type(row) is not dict or set(row) != {
            "condition",
            "member",
            "purpose",
            "resource",
            "role",
        }:
            _refuse("iam_delta_bindings_delta_invalid")
        if selected is not None and row["member"] not in selected:
            continue
        resource = row["resource"]
        report = {
            "condition": row["condition"],
            "member": row["member"],
            "resource": resource,
            "role": row["role"],
        }
        # Every must_not_grant entry names a routine, so this runs before the
        # routine rows are set aside for the iam-v2 words.
        if resource in forbidden:
            refused.append({**report, "reason": "must_not_grant"})
            continue
        if isinstance(resource, str) and _ROUTINE.fullmatch(resource):
            routine_rows += 1
            continue
        if not isinstance(row["role"], str) or not _ROLE.fullmatch(row["role"]):
            _refuse("iam_delta_bindings_delta_invalid")
        if not _MEMBER.fullmatch(str(row["member"])) or "app" in identities.get(
            row["member"], {"app"}
        ):
            _refuse("iam_delta_bindings_member_invalid")
        kind = _kind_or_none(resource)
        topic = next(
            (
                name
                for name, members_ in sorted(unresolved.items())
                if _UNRESOLVED[name](row, kind)
                and (not members_ or row["member"] in members_)
            ),
            None,
        )
        if topic is not None:
            refused.append({**report, "reason": "unresolved:" + topic})
            continue
        if kind == "project":
            basis = _project_basis(row, identities[row["member"]])
            project_level.append(
                {
                    "member": row["member"],
                    "role": row["role"],
                    "approved": basis is not None,
                    "basis": basis,
                }
            )
            if basis is None:
                refused.append({**report, "reason": "project_level_unapproved"})
                continue
        if kind not in _SUPPORTED:
            refused.append({**report, "reason": "kind_unsupported"})
            continue
        condition = row["condition"]
        if kind in _CONDITIONED:
            if (
                not isinstance(condition, str)
                or not condition
                or condition != condition.strip()
            ):
                _refuse("iam_delta_bindings_condition_invalid")
            if kind == "secret_version" and condition != _secret_condition(resource):
                _refuse("iam_delta_bindings_condition_invalid")
        elif condition is not None:
            _refuse("iam_delta_bindings_condition_invalid")
        key = (row["member"], row["role"], resource, condition)
        if key in seen:
            _refuse("iam_delta_bindings_delta_invalid")
        seen.add(key)
        targets.append(
            Row(
                member=row["member"],
                role=row["role"],
                resource=resource,
                condition=condition,
                kind=kind,
                target=target_of(resource),
            )
        )
    unresolved_project = []
    for topic, role in sorted(_PROJECT_LEVEL_UNRESOLVED_ROLES.items()):
        if topic in unresolved:
            names = sorted(
                m for m in unresolved[topic] if selected is None or m in selected
            )
            if names:
                unresolved_project.append(
                    {"members": names, "role": role, "topic": topic}
                )
    return Targets(
        delta_sha256=digest,
        manifest_sha256=MANIFEST_SHA256,
        members=tuple(sorted(selected or bound)),
        rows=tuple(targets),
        refused=refused,
        project_level=project_level,
        project_level_unresolved=unresolved_project,
        routine_rows=routine_rows,
    )


def load_targets(members=None, *, delta_path=DELTA_PATH, manifest_path=MANIFEST_PATH):
    try:
        raw = Path(delta_path).read_bytes()
    except OSError:
        _refuse("iam_delta_bindings_delta_invalid")
    if len(raw) > _MAX_DELTA_BYTES:
        _refuse("iam_delta_bindings_delta_invalid")
    # The digest is recomputed from the very bytes that are parsed.
    digest = hashlib.sha256(raw).hexdigest()
    if digest != DELTA_SHA256:
        _refuse("iam_delta_bindings_delta_digest_mismatch")
    try:
        delta = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: _refuse("iam_delta_bindings_delta_invalid"),
        )
    except (UnicodeError, json.JSONDecodeError):
        _refuse("iam_delta_bindings_delta_invalid")
    return select_targets(delta, load_manifest(manifest_path), digest, members)


@functools.cache
def _pinned_rows():
    """Every row the pinned delta lets this tool grant, from the pinned bytes."""
    return frozenset(load_targets().rows)


# Policies


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _dataset_member(entry):
    if "userByEmail" in entry:
        email = str(entry["userByEmail"])
        prefix = (
            "serviceAccount:" if email.endswith(".gserviceaccount.com") else "user:"
        )
        return prefix + email
    if "iamMember" in entry:
        return str(entry["iamMember"])
    for key, prefix in (
        ("groupByEmail", "group:"),
        ("domain", "domain:"),
        ("specialGroup", "specialGroup:"),
    ):
        if key in entry:
            return prefix + str(entry[key])
    return "entry:" + _canonical(
        {k: v for k, v in entry.items() if k not in {"role", "condition"}}
    )


def _rows_of(kind, payload):
    """(role, member, condition expression, condition json) for every grant held."""
    rows = []
    if kind == "bigquery_dataset":
        for entry in payload["access"]:
            if type(entry) is not dict:
                _refuse("iam_delta_bindings_readback_invalid")
            role = entry.get("role")
            role = _DATASET_ROLES.get(role, role)
            condition = entry.get("condition")
            expression = (
                condition.get("expression") if type(condition) is dict else None
            )
            rows.append(
                (str(role), _dataset_member(entry), expression, _canonical(condition))
            )
        return rows
    for binding in payload.get("bindings") or []:
        if type(binding) is not dict or type(binding.get("members")) is not list:
            _refuse("iam_delta_bindings_readback_invalid")
        condition = binding.get("condition")
        if condition is not None and (
            type(condition) is not dict or not condition.get("expression")
        ):
            _refuse("iam_delta_bindings_readback_invalid")
        expression = condition["expression"] if condition is not None else None
        for member in binding["members"]:
            rows.append(
                (
                    str(binding.get("role")),
                    str(member),
                    expression,
                    _canonical(condition),
                )
            )
    return rows


def _held(kind, payload, row):
    return any(
        (role, member, expression) == (row.role, row.member, row.condition)
        for role, member, expression, _exact in _rows_of(kind, payload)
    )


def _checked(kind, payload):
    if type(payload) is not dict:
        _refuse("iam_delta_bindings_readback_invalid")
    etag = payload.get("etag")
    if not isinstance(etag, str) or not etag:
        _refuse("iam_delta_bindings_etag_missing")
    if kind == "bigquery_dataset":
        if type(payload.get("access")) is not list:
            _refuse("iam_delta_bindings_readback_invalid")
    elif type(payload.get("bindings", [])) is not list:
        _refuse("iam_delta_bindings_readback_invalid")
    _rows_of(kind, payload)
    return payload


def _dataset_entry(row):
    email = _MEMBER.fullmatch(row.member).group(1)
    return {"role": _DATASET_LEGACY.get(row.role, row.role), "userByEmail": email}


def _revised(kind, before, additions):
    """The add only policy: every binding kept as read, each addition appended."""
    revised = copy.deepcopy(before)
    if kind == "bigquery_dataset":
        return {
            "access": [*revised["access"], *(_dataset_entry(row) for row in additions)]
        }
    bindings = revised.get("bindings") or []
    for row in additions:
        condition = _condition(row)
        match = next(
            (
                item
                for item in bindings
                if item.get("role") == row.role and item.get("condition") == condition
            ),
            None,
        )
        if match is None:
            item = {"role": row.role, "members": [row.member]}
            if condition is not None:
                item["condition"] = condition
            bindings.append(item)
        elif row.member not in match["members"]:
            match["members"].append(row.member)
    revised["bindings"] = bindings
    if any("condition" in item for item in bindings):
        revised["version"] = 3
    return revised


def _version(policy):
    version = policy.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        _refuse("iam_delta_bindings_write_refused")
    return version


def _guard_write(kind, target, before, additions, allowed):
    """The send path check: only targeted, missing rows of this resource, add only.

    The permitted rows are the pinned delta's own; a caller's allowed rows can
    only narrow them.
    """
    permitted = _pinned_rows() & set(allowed)
    for row in additions:
        if not isinstance(row, Row) or row not in permitted or row.target != target:
            _refuse("iam_delta_bindings_write_refused")
        if _held(kind, before, row):
            _refuse("iam_delta_bindings_write_refused")
    if not additions:
        _refuse("iam_delta_bindings_write_refused")
    after = _revised(kind, before, additions)
    view = {**before, **after}
    kept = set(_rows_of(kind, before))
    now = set(_rows_of(kind, view))
    if not kept <= now:
        _refuse("iam_delta_bindings_write_refused")
    added = {(role, member, expression) for role, member, expression, _ in now - kept}
    if added != {(row.role, row.member, row.condition) for row in additions}:
        _refuse("iam_delta_bindings_write_refused")
    if kind != "bigquery_dataset":
        version = _version(after)
        if version < _version(before):
            _refuse("iam_delta_bindings_write_refused")
        conditional = any(
            type(item) is dict and item.get("condition") is not None
            for item in after.get("bindings") or []
        )
        if conditional and version != 3:
            _refuse("iam_delta_bindings_write_refused")
    return after


def build_request(call, **kwargs):
    """The exact request one call sends; the dry run renders these."""
    kind, groups = parse_resource(kwargs["resource"])
    if kind == "secret_version":
        kind, groups = "secret", groups[:1]
    ident = groups[0]
    if call == "get_policy":
        if kind == "bigquery_dataset":
            return {
                "call": call,
                "method": "GET",
                "url": BIGQUERY_ENDPOINT + ident + "?accessPolicyVersion=3",
            }
        if kind == "project":
            return {
                "call": call,
                "method": "POST",
                "url": PROJECT_ENDPOINT + ":getIamPolicy",
                "json": _POLICY_V3_BODY,
            }
        if kind == "cloudtasks":
            return {
                "call": call,
                "method": "POST",
                "url": TASKS_ENDPOINT + ident + ":getIamPolicy",
                "json": _POLICY_V3_BODY,
            }
        if kind == "service_account":
            return {
                "call": call,
                "method": "POST",
                "url": ACCOUNT_ENDPOINT + ident + ":getIamPolicy" + _POLICY_V3_QUERY,
            }
        if kind == "bucket":
            return {
                "call": call,
                "method": "GET",
                "url": STORAGE_ENDPOINT
                + ident
                + "/iam?optionsRequestedPolicyVersion=3",
            }
        base = {
            "run": RUN_ENDPOINT,
            "artifactregistry": REGISTRY_ENDPOINT,
            "secret": SECRET_ENDPOINT,
        }.get(kind)
        if base is None:
            _refuse("resource_kind_unsupported")
        return {
            "call": call,
            "method": "GET",
            "url": base + ident + ":getIamPolicy" + _POLICY_V3_QUERY,
        }
    if call == "set_policy":
        if "allowed" not in kwargs:
            _refuse("iam_delta_bindings_write_refused")
        target = target_of(kwargs["resource"])
        policy = _guard_write(
            "bigquery_dataset" if kind == "bigquery_dataset" else "policy",
            target,
            kwargs["before"],
            list(kwargs["additions"]),
            kwargs["allowed"],
        )
        if kind == "bigquery_dataset":
            return {
                "call": call,
                "method": "PATCH",
                "url": BIGQUERY_ENDPOINT + ident + "?accessPolicyVersion=3",
                "headers": {"If-Match": kwargs["before"]["etag"]},
                "json": policy,
            }
        if kind == "bucket":
            return {
                "call": call,
                "method": "PUT",
                "url": STORAGE_ENDPOINT + ident + "/iam",
                "json": policy,
            }
        base = {
            "project": PROJECT_ENDPOINT,
            "run": RUN_ENDPOINT + ident,
            "artifactregistry": REGISTRY_ENDPOINT + ident,
            "cloudtasks": TASKS_ENDPOINT + ident,
            "service_account": ACCOUNT_ENDPOINT + ident,
            "secret": SECRET_ENDPOINT + ident,
        }.get(kind)
        if base is None:
            _refuse("resource_kind_unsupported")
        return {
            "call": call,
            "method": "POST",
            "url": base + ":setIamPolicy",
            "json": {"policy": policy},
        }
    _refuse("unknown_call")


_WRITE_CALLS = frozenset({"set_policy"})


class Adapter:
    """One injectable transport; writes only when built for the apply word."""

    def __init__(self, state, *, transport=None, token_source=None, writes=False):
        if state not in STATES:
            _refuse("adapter_state_invalid")
        self.state = state
        self.transport = transport or no_redirect_transport
        self.writes = bool(writes)
        self.record = []
        self._token_source = token_source

    def token(self):
        if self._token_source is None:
            from ops.deploy.release_native_adapter import NativeAdapter as _Credentials

            self._token_source = _Credentials(self.state).token
        return self._token_source()

    def render(self, call, **kwargs):
        request = build_request(call, **kwargs)
        rendered = {key: request[key] for key in ("call", "method", "url")}
        if "headers" in request:
            rendered["headers"] = dict(request["headers"])
        if "json" in request:
            rendered["body"] = request["json"]
        return rendered

    def perform(self, call, **kwargs):
        if call in _WRITE_CALLS and not self.writes:
            _refuse("iam_delta_bindings_write_not_allowed")
        request = build_request(call, **kwargs)
        headers = {
            "Authorization": "Bearer " + self.token(),
            "Accept": "application/json",
        }
        headers.update(request.get("headers") or {})
        data = None
        entry = {"call": call, "method": request["method"], "url": request["url"]}
        if "json" in request:
            data = json.dumps(
                request["json"], sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
            entry["request_sha256"] = hashlib.sha256(data).hexdigest()
        # Recorded before it is sent, so a request whose answer never arrives is named.
        self.record.append(entry)
        response = self.transport(
            {
                "method": request["method"],
                "url": request["url"],
                "headers": headers,
                "body": data,
            }
        )
        status = _status(call, response, request["url"])
        entry["status"] = status
        return status, _payload(call, status, response)

    def get(self, call, **kwargs):
        status, payload = self.perform(call, **kwargs)
        if status == 404:
            return None
        if status != 200:
            raise NativeError(call, status, payload)
        return payload

    def write(self, call, **kwargs):
        status, payload = self.perform(call, **kwargs)
        if status != 200:
            raise NativeError(call, status, payload)
        return payload


def _status(call, response, url):
    if type(response) is not dict or "status" not in response:
        raise NativeError(call, None, "transport_response_invalid")
    status = response["status"]
    if isinstance(status, bool) or not isinstance(status, int):
        raise NativeError(call, None, "transport_response_invalid")
    if 300 <= status < 400:
        raise NativeError(call, status, "redirect_refused")
    arrived = response.get("url")
    if arrived is not None and arrived != url:
        raise NativeError(call, status, "redirect_refused")
    return status


def _payload(call, status, response):
    body = bytes(response.get("body") or b"")
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise NativeError(call, status, "response_not_json") from None
    if type(payload) is not dict:
        raise NativeError(call, status, "response_not_json")
    return payload


# Words


class WriteRefusal(ValueError):
    """A refusal after the write phase began; applied names what already landed."""

    def __init__(self, code, applied):
        super().__init__(code)
        self.applied = copy.deepcopy(applied)


# A truncated body (http.client.IncompleteRead) is an HTTPException, not an
# OSError; it may follow a write that landed, so it is caught like one.
_TRANSPORT_ERRORS = (NativeError, OSError, ValueError, http.client.HTTPException)


def _kind_of(target):
    kind, _groups = parse_resource(target)
    return "bigquery_dataset" if kind == "bigquery_dataset" else "policy"


def _read(adapter, targets):
    state = {}
    for target in sorted({row.target for row in targets.rows}):
        payload = adapter.get("get_policy", resource=target)
        state[target] = None if payload is None else _checked(_kind_of(target), payload)
    return state


def _split(targets, state):
    present, missing, absent = [], [], []
    for row in targets.rows:
        payload = state[row.target]
        if payload is None:
            absent.append(row)
        elif _held(_kind_of(row.target), payload, row):
            present.append(row)
        else:
            missing.append(row)
    return present, missing, absent


def _additions(missing):
    grouped = {}
    for row in missing:
        grouped.setdefault(row.target, []).append(row)
    return sorted(grouped.items())


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _readback_digest(state):
    return _digest(
        {
            target: None
            if payload is None
            else sorted(list(item) for item in _rows_of(_kind_of(target), payload))
            for target, payload in sorted(state.items())
        }
    )


def _broader(targets, state):
    found = []
    for row in targets.rows:
        payload = state[row.target]
        if row.condition is None or payload is None:
            continue
        for role, member, expression, _exact in _rows_of(_kind_of(row.target), payload):
            if (role, member, expression) == (row.role, row.member, None):
                found.append(
                    {
                        "member": row.member,
                        "resource": row.resource,
                        "role": row.role,
                        "held": "unconditional",
                    }
                )
                break
    return found


def _listed(rows):
    return sorted((row.as_dict() for row in rows), key=_canonical)


def run(mode, targets, adapter):
    if mode not in {"plan", "dry_run", "apply"}:
        _refuse("iam_delta_bindings_mode_invalid")
    if mode != "apply" and adapter.writes:
        _refuse("iam_delta_bindings_write_not_allowed")
    before = _read(adapter, targets)
    present, missing, absent = _split(targets, before)
    receipt = {
        "contract_version": CONTRACT_VERSION,
        "mode": mode,
        "delta_sha256": targets.delta_sha256,
        "resource_manifest_sha256": targets.manifest_sha256,
        "members": list(targets.members),
        "routine_rows_left_to_iam_v2": targets.routine_rows,
        "refused": list(targets.refused),
        "project_level": list(targets.project_level),
        "project_level_unresolved": list(targets.project_level_unresolved),
        "target_sha256": _digest(_listed(targets.rows)),
        "conditions": sorted(
            (
                {"expression": row.condition, "title": condition_title(row.condition)}
                for row in {row for row in targets.rows if row.condition is not None}
            ),
            key=_canonical,
        ),
        "broader_grants": _broader(targets, before),
    }
    if mode == "plan":
        receipt["rows"] = {
            "target": len(targets.rows),
            "present": _listed(present),
            "missing": _listed(missing),
            "absent": _listed(absent),
        }
        receipt["readback_sha256"] = _readback_digest(before)
        return receipt
    writes = _additions(missing)
    rendered = [
        adapter.render(
            "set_policy",
            resource=target,
            before=before[target],
            additions=rows,
            allowed=targets.rows,
        )
        for target, rows in writes
    ]
    receipt["writes_planned"] = len(rendered)
    receipt["planned_payload_sha256"] = _digest(rendered)
    if mode == "dry_run":
        receipt["requests"] = rendered
        receipt["rows"] = {
            "target": len(targets.rows),
            "present": len(present),
            "missing": _listed(missing),
            "absent": _listed(absent),
        }
        receipt["readback_sha256"] = _readback_digest(before)
        return receipt
    applied = {"writes": [], "failed_at": None}
    for target, rows in writes:
        try:
            adapter.write(
                "set_policy",
                resource=target,
                before=before[target],
                additions=rows,
                allowed=targets.rows,
            )
        except _TRANSPORT_ERRORS as error:
            applied["failed_at"] = target
            raise WriteRefusal("iam_delta_bindings_write_refused", applied) from error
        applied["writes"].append(target)
    applied["failed_at"] = "readback"
    # The proof is a fresh read of every target, never the answer to a write.
    try:
        after = _read(adapter, targets)
        still_present, still_missing, still_absent = _split(targets, after)
        kept = all(
            after[target] is not None
            and set(_rows_of(_kind_of(target), payload))
            <= set(_rows_of(_kind_of(target), after[target]))
            for target, payload in before.items()
            if payload is not None
        )
    except _TRANSPORT_ERRORS as error:
        raise WriteRefusal("iam_delta_bindings_readback_mismatch", applied) from error
    reachable = {row for row in targets.rows if before[row.target] is not None}
    if not kept or reachable & set(still_missing):
        raise WriteRefusal("iam_delta_bindings_readback_mismatch", applied)
    applied["failed_at"] = None
    receipt["applied"] = applied
    receipt["rows"] = {
        "target": len(targets.rows),
        "present_before": len(present),
        "added": len(missing),
        "present_after": len(still_present),
        "absent": _listed(still_absent),
    }
    receipt["complete"] = len(still_present) == len(targets.rows)
    receipt["readback_sha256"] = _readback_digest(after)
    return receipt


def _parser():
    parser = argparse.ArgumentParser(prog="iam_delta_bindings")
    parser.add_argument("word", choices=sorted(WORDS))
    parser.add_argument("--member", action="append", default=None)
    parser.add_argument("--adapter-state", choices=STATES, default="owner-gcloud")
    return parser


def _emit(stream, payload):
    stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def main(argv=None, *, adapter_factory=None, out=None) -> int:
    arguments = _parser().parse_args(sys.argv[1:] if argv is None else list(argv))
    stream = out or sys.stdout
    mode = WORDS[arguments.word]
    build = adapter_factory or (lambda state, writes: Adapter(state, writes=writes))
    try:
        targets = load_targets(arguments.member)
        receipt = run(mode, targets, build(arguments.adapter_state, mode == "apply"))
    except WriteRefusal as error:
        _emit(stream, {"error": str(error), "applied": error.applied})
        return 1
    except _TRANSPORT_ERRORS as error:
        _emit(stream, {"error": str(error) or type(error).__name__})
        return 1
    _emit(stream, receipt)
    if mode == "apply" and receipt["complete"] is not True:
        return EXIT_INCOMPLETE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
