"""Create every 42 staging identity and grant its roles, once, by adding only.

Albert runs this in his own terminal with his own gcloud user login:

    py -3.13 core/setup/bootstrap.py                  dry run, prints the plan and changes nothing
    py -3.13 core/setup/bootstrap.py --apply          creates what is missing, adds missing bindings, reads back
    py -3.13 core/setup/bootstrap.py --apply --iap-group group:NAME

docs/full-42/SETUP.md is the source of truth. The script only adds. It never removes or replaces a
binding, never sets a whole policy through gcloud, never edits a dataset access list wholesale, never
deletes anything, never reads or adds a secret value and touches project ogilvy-trends-v2 only. The BigQuery
connection has no add command, so its grant is a read, merge and write that sends the fetched etag and
refuses to send if any existing member would be lost. The one in-place change is the staging provider's
attribute condition, patched alone and only when the new condition narrows the live one.
"""
import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

import requests
from google.api_core.exceptions import GoogleAPICallError, NotFound
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

PROJECT = "ogilvy-trends-v2"
REGION = "us-central1"
BQ_LOCATION = "US"

CORE = "intelligence_42_core"
AGENT_DATASET = "intelligence_42_agent"
NEW_DATASETS = (CORE, AGENT_DATASET)
LEDGER_REASON = "live calls write the ledger, ASK_DAILY enforced"
AGENT_TABLES = {
    "credit_ledger": LEDGER_REASON,
    "raw_responses": LEDGER_REASON,
    "post_observations": LEDGER_REASON,
    "suppressions": "staff add people to the suppression list from the app",
}
LEGACY_DATASETS = ("trends_v2_dev", "trends_v2_staging", "trends_v2_staging_funded", "intelligence_42_sources_staging")
LEGACY_JOBS = ("trends_v2_dev", "trends_v2_staging")

BUCKET = "ogilvy-trends-v2-f42-media-staging"
SOFT_DELETE_DAYS = 30
CONNECTION = "f42-vertex"
CONNECTION_URL = f"https://bigqueryconnection.googleapis.com/v1/projects/{PROJECT}/locations/{BQ_LOCATION.lower()}/connections"
REPO = "intelligence-42"
SECRET = "SOCIALCRAWL_OGILVY_API_KEY"
UI_PASSCODE = "UI_PASSCODE"  # container only; Albert adds the value himself, this script never adds or reads a version
CHANNEL_WEBHOOK_URL = "CHANNEL_WEBHOOK_URL"  # container only, as UI_PASSCODE; Albert adds the webhook address himself
# Albert creates this secret and its value (the Gmail app password for the alert digest); this script never creates
# it, and plans its one grant only once it exists.
GMAIL_APP_PASSWORD = "GMAIL_APP_PASSWORD"

GITHUB_REPO = "jhbanalytics-pixel/42-Ogilvy-Intelligence"
POOL = "f42-github"
PROVIDER = "f42-github-staging"
ISSUER = "https://token.actions.githubusercontent.com"
ATTRIBUTE_MAPPING = "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref"
GITHUB_REF = "refs/heads/full-42"
# Only a push to full-42 of this repository, the one trigger of .github/workflows/staging.yml, can sign in.
ATTRIBUTE_CONDITION = (f"assertion.repository == '{GITHUB_REPO}' && assertion.ref == '{GITHUB_REF}'"
                       " && assertion.event_name == 'push'")
PROVIDER_URL = (f"https://iam.googleapis.com/v1/projects/{PROJECT}/locations/global/"
                f"workloadIdentityPools/{POOL}/providers/{PROVIDER}")

APIS = tuple(f"{name}.googleapis.com" for name in (
    "run", "cloudscheduler", "bigquery", "bigqueryconnection", "bigquerystorage", "aiplatform", "secretmanager",
    "artifactregistry", "cloudbuild", "iamcredentials", "sts", "iap", "monitoring", "logging", "iam",
    "serviceusage",
))

RUNTIME = ("f42-collector", "f42-enricher", "f42-agent", "f42-brief", "f42-web", "f42-scheduler")
ACCOUNTS = RUNTIME + ("f42-deployer", "f42-builder")

IMPERSONATE_ENV = "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT"
GRANT_RETRIES = 6
GRANT_WAIT_SECONDS = 10

# Never granted. The only roles with "admin" in the name are the three SETUP.md names.
FORBIDDEN_ROLES = {"roles/owner", "roles/editor"}
ADMIN_ALLOWED = {"roles/bigquery.connectionAdmin", "roles/cloudscheduler.admin", "roles/storage.objectAdmin"}

EDIT = "roles/bigquery.dataEditor"
VIEW = "roles/bigquery.dataViewer"
INVOKER = "roles/run.invoker"
BASIC_DATASET_ROLES = {"WRITER": EDIT, "READER": VIEW, "OWNER": "roles/bigquery.dataOwner"}


class CloudError(Exception):
    """A gcloud, REST or BigQuery call failed; the message is the error's last line."""


@dataclass(frozen=True)
class Binding:
    identity: str
    member: str | None
    role: str
    kind: str
    name: str
    reason: str


@dataclass
class Row:
    status: str
    identity: str
    role: str
    resource: str
    reason: str
    binding: Binding | None = None
    action: object = None


@dataclass
class State:
    account: str
    number: str
    apis: set
    accounts: set
    datasets: dict
    tables: set
    bucket: bool
    secrets: set
    connection: dict | None
    pool: bool
    provider: bool
    condition: str | None = None
    policies: dict = field(default_factory=dict)


def sa_email(account_id):
    return f"{account_id}@{PROJECT}.iam.gserviceaccount.com"


def iap_agent(number):
    return f"serviceAccount:service-{number}@gcp-sa-iap.iam.gserviceaccount.com"


def github_principal(number, attribute="repository", value=GITHUB_REPO):
    return (f"principalSet://iam.googleapis.com/projects/{number}/locations/global/"
            f"workloadIdentityPools/{POOL}/attribute.{attribute}/{value}")


# Held until GitHub Actions is set up and the default Cloud Build account is checked (Albert, 29 September 2026).
# Bootstrap plans and applies none of these, nor the workloadIdentityUser binding for the full-42 principal set.
# Setting HELD_GRANTS_ON = True switches all of them on.
HELD_GRANTS_ON = False
DEPLOYER_BUILD_GRANTS_HELD = (
    ("roles/cloudbuild.builds.editor", ("project", PROJECT), "creates the jobs image build"),
    ("roles/serviceusage.serviceUsageConsumer", ("project", PROJECT), "creates the jobs image build"),
    ("roles/iam.serviceAccountUser", ("sa", sa_email("f42-deployer")), "the build runs as f42-deployer"),
    ("roles/storage.objectCreator", ("bucket", BUCKET), "uploads build source under build-source/, cannot overwrite"),
)


def condition_narrows(live):
    """True when the new condition only adds clauses to the live one, so it can only shrink who signs in."""
    return not live or ATTRIBUTE_CONDITION.startswith(live + " && ")


def desired_bindings(state, iap_group):
    """Every binding from the SETUP.md Identities table, plus the few extras marked as such."""
    out = []
    project, secret, bucket = ("project", PROJECT), ("secret", SECRET), ("bucket", BUCKET)
    repo, conn = ("repo", REPO), ("connection", CONNECTION)

    def add(identity, member, role, resource, reason):
        out.append(Binding(identity, member, role, resource[0], resource[1], reason))

    def grant(account, role, resource, reason):
        add(account, "serviceAccount:" + sa_email(account), role, resource, reason)

    def datasets(account, role, names, reason):
        for name in names:
            grant(account, role, ("dataset", name), reason)

    def bq_basics(account):
        grant(account, "roles/bigquery.jobUser", project, "runs queries")
        grant(account, "roles/bigquery.readSessionUser", project, "BigQuery Storage reads")

    def run_next(account):
        grant(account, INVOKER, project, "starts the next job")
        grant(account, "roles/run.jobsExecutorWithOverrides", project, "starts the next job")

    grant_later = "beyond SETUP.md table: "

    a = "f42-collector"
    datasets(a, EDIT, NEW_DATASETS, "writes posts and runs rows")
    bq_basics(a)
    run_next(a)
    grant(a, "roles/secretmanager.secretAccessor", secret, "SocialCrawl key, this secret only")
    datasets(a, VIEW, LEGACY_JOBS, "legacy reads")

    a = "f42-enricher"
    datasets(a, EDIT, NEW_DATASETS, "writes enrichment and runs rows")
    bq_basics(a)
    run_next(a)
    grant(a, "roles/aiplatform.user", project, "model calls")
    grant(a, "roles/bigquery.connectionUser", conn, "remote embedding model")
    grant(a, "roles/storage.objectAdmin", bucket, "writes clips and keyframes")
    grant(a, "roles/secretmanager.secretAccessor", secret, "transcripts")

    a = "f42-agent"
    grant(a, VIEW, ("dataset", CORE), "reads core; writes only the four tables below")
    for table, reason in AGENT_TABLES.items():
        grant(a, EDIT, ("table", f"{CORE}.{table}"), reason)
    grant(a, EDIT, ("dataset", AGENT_DATASET), "runs, findings, live results")
    bq_basics(a)
    grant(a, "roles/bigquery.connectionUser", conn, "remote embedding model")
    grant(a, "roles/aiplatform.user", project, "model calls")
    grant(a, "roles/secretmanager.secretAccessor", secret, "live calls")
    grant(a, "roles/secretmanager.secretAccessor", ("secret", CHANNEL_WEBHOOK_URL),
          "daily team channel post from f42-scheduled-asks")
    grant(a, "roles/storage.objectViewer", bucket, "reads clips")
    datasets(a, VIEW, LEGACY_JOBS, grant_later + "task 1.16 reads legacy rows")
    if GMAIL_APP_PASSWORD in state.secrets:
        grant(a, "roles/secretmanager.secretAccessor", ("secret", GMAIL_APP_PASSWORD),
              grant_later + "the daily alert email digest, f42-digest")

    a = "f42-brief"
    datasets(a, EDIT, NEW_DATASETS, "detection, briefs, learning")
    bq_basics(a)
    grant(a, "roles/bigquery.connectionUser", conn, "remote embedding model")
    grant(a, "roles/aiplatform.user", project, "model calls")
    grant(a, "roles/secretmanager.secretAccessor", secret, "confirmation searches")
    run_next(a)
    datasets(a, VIEW, LEGACY_JOBS, grant_later + "task 1.7 reads legacy rows")

    a = "f42-web"
    grant(a, INVOKER, project, "app and API service")
    datasets(a, VIEW, NEW_DATASETS, "reads for the app")
    bq_basics(a)
    grant(a, "roles/storage.objectViewer", bucket, "serves clips")
    grant(a, "roles/secretmanager.secretAccessor", ("secret", UI_PASSCODE), grant_later + "app passcode gate, lane L4 request")

    a = "f42-scheduler"
    grant(a, INVOKER, project, "Cloud Scheduler and job chaining")
    grant(a, "roles/run.jobsExecutorWithOverrides", project, "Cloud Scheduler and job chaining")

    a = "f42-deployer"
    grant(a, "roles/run.developer", project, "deploys staging")
    for runtime in RUNTIME:
        grant(a, "roles/iam.serviceAccountUser", ("sa", sa_email(runtime)), "deploys as this runtime account")
    grant(a, "roles/artifactregistry.writer", repo, "pushes images")
    grant(a, "roles/cloudscheduler.jobRunner", project, "runs Scheduler jobs")
    add("GitHub " + GITHUB_REPO, github_principal(state.number), "roles/iam.workloadIdentityUser",
        ("sa", sa_email(a)), "this repository; the staging provider admits only pushes to full-42")
    if HELD_GRANTS_ON:
        add("GitHub " + GITHUB_REF, github_principal(state.number, "ref", GITHUB_REF), "roles/iam.workloadIdentityUser",
            ("sa", sa_email(a)), "pushes to full-42 through the staging provider only")
        for role, resource, reason in DEPLOYER_BUILD_GRANTS_HELD:
            grant(a, role, resource, reason)
    grant(a, "roles/logging.logWriter", project, grant_later + "a Cloud Build run as a user managed account writes its log")
    grant(a, "roles/storage.objectViewer", bucket, grant_later + "builds stage source under build-source/")

    a = "f42-builder"
    datasets(a, EDIT, NEW_DATASETS, "builds the new datasets")
    datasets(a, VIEW, LEGACY_DATASETS, "legacy reads")
    bq_basics(a)
    grant(a, "roles/bigquery.connectionAdmin", conn, "creates the remote embedding model, this connection only")
    grant(a, "roles/run.developer", project, "deploys staging until Actions exists")
    for target in RUNTIME + ("f42-deployer",):
        grant(a, "roles/iam.serviceAccountUser", ("sa", sa_email(target)), "deploys as this account")
    grant(a, "roles/cloudbuild.builds.editor", project, "builds images as f42-deployer")
    grant(a, "roles/serviceusage.serviceUsageConsumer", project, "builds images as f42-deployer")
    grant(a, "roles/artifactregistry.writer", repo, "pushes images")
    grant(a, "roles/cloudscheduler.admin", project, "manages Scheduler jobs")
    grant(a, "roles/storage.objectAdmin", bucket, "media bucket")
    grant(a, "roles/aiplatform.user", project, "model calls")
    grant(a, "roles/logging.viewer", project, "reads job logs")
    grant(a, "roles/monitoring.editor", project, "alert policies")
    add("user:" + state.account, "user:" + state.account, "roles/iam.serviceAccountTokenCreator",
        ("sa", sa_email(a)), "Albert impersonates the builder")

    conn_sa = (state.connection or {}).get("service_account")
    add("f42-vertex connection", "serviceAccount:" + (conn_sa or "(connection account, known once f42-vertex exists)"),
        "roles/aiplatform.user", project, "remote embedding model calls Vertex")
    add("IAP service agent", iap_agent(state.number), INVOKER, project, "IAP reaches Cloud Run")
    add(iap_group or "Ogilvy group (pass --iap-group group:NAME)", iap_group,
        "roles/iap.httpsResourceAccessor", project, "IAP-secured Web App User")
    return out


def check_roles(bindings):
    for b in bindings:
        if b.role in FORBIDDEN_ROLES or ("admin" in b.role.lower() and b.role not in ADMIN_ALLOWED):
            raise ValueError(f"refusing to grant {b.role} to {b.identity}")


def resource_label(kind, name):
    return {
        "project": f"project {name}",
        "dataset": f"dataset {name}",
        "table": f"table {name}",
        "secret": f"secret {name}",
        "bucket": f"bucket gs://{name}",
        "repo": f"repo {name} ({REGION})",
        "sa": f"account {name.split('@')[0]}",
        "connection": f"connection {name} ({BQ_LOCATION})",
    }[kind]


def resource_exists(state, kind, name):
    return {
        "project": True,
        "secret": name in state.secrets,
        "repo": True,
        "sa": name in state.accounts,
        "dataset": state.datasets.get(name) is not None,
        "table": name in state.tables,
        "bucket": state.bucket,
        "connection": state.connection is not None,
    }[kind]


def gather(cloud, account):
    pool = cloud.pool_exists()
    condition = cloud.provider_condition() if pool else None
    state = State(
        account=account,
        number=cloud.project_number(),
        apis=cloud.enabled_apis(),
        accounts=cloud.service_accounts(),
        datasets={name: cloud.dataset_location(name) for name in NEW_DATASETS + LEGACY_DATASETS},
        tables={f"{CORE}.{t}" for t in AGENT_TABLES if cloud.table_exists(CORE, t)},
        bucket=cloud.bucket_exists(BUCKET),
        secrets=cloud.secret_names(),
        connection=cloud.connection(),
        pool=pool,
        provider=condition is not None,
        condition=condition,
    )
    for kind, name in sorted({(b.kind, b.name) for b in desired_bindings(state, None)}):
        if resource_exists(state, kind, name):
            state.policies[(kind, name)] = cloud.policy(kind, name)
    return state


def binding_status(b, state):
    if b.member is None:
        return "WAITING"
    if b.kind == "table" and b.name not in state.tables:
        return "PENDING"
    if b.kind == "dataset" and b.name in LEGACY_DATASETS and state.datasets.get(b.name) is None:
        return "SKIPPED"
    if (b.role, b.member) in state.policies.get((b.kind, b.name), set()):
        return "EXISTS"
    return "GRANT"


def binding_rows(state, iap_group, readback=False):
    rows = []
    for b in desired_bindings(state, iap_group):
        status = binding_status(b, state)
        if readback:
            status = {"EXISTS": "PRESENT", "GRANT": "MISSING"}.get(status, status)
        rows.append(Row(status, b.identity, b.role, resource_label(b.kind, b.name), b.reason, binding=b))
    return rows


def resource_rows(state, cloud):
    rows = []

    def row(exists, what, resource, reason, action):
        rows.append(Row("EXISTS" if exists else "CREATE", "-", what, resource, reason, action=None if exists else action))

    missing = [api for api in APIS if api not in state.apis]
    row(not missing, "APIs", ", ".join(missing) or f"all {len(APIS)} enabled", "SETUP.md list plus iam and serviceusage",
        lambda: cloud.enable_apis(missing))
    for account in ACCOUNTS:
        row(sa_email(account) in state.accounts, "service account", account, "SETUP.md Identities",
            lambda account=account: cloud.create_service_account(account))
    iap_bound = (INVOKER, iap_agent(state.number)) in state.policies.get(("project", PROJECT), set())
    row(iap_bound, "IAP service agent", iap_agent(state.number).split(":")[1], "generated before its run.invoker grant",
        cloud.generate_iap_identity)
    for name in NEW_DATASETS:
        location = state.datasets.get(name)
        note = "" if location in (None, BQ_LOCATION) else f", expected {BQ_LOCATION}"
        row(location is not None, "dataset", f"{name} ({location or BQ_LOCATION}{note})", "no expiry",
            lambda name=name: cloud.create_dataset(name))
    row(state.bucket, "bucket", f"gs://{BUCKET} ({REGION})",
        f"uniform access, public access prevention, soft delete {SOFT_DELETE_DAYS} days, no lifecycle",
        lambda: cloud.create_bucket(BUCKET))
    row(UI_PASSCODE in state.secrets, "secret", f"{UI_PASSCODE} (automatic replication, no version)",
        "beyond SETUP.md table: app passcode gate, lane L4 request; Albert sets the value",
        lambda: cloud.create_secret(UI_PASSCODE))
    row(CHANNEL_WEBHOOK_URL in state.secrets, "secret", f"{CHANNEL_WEBHOOK_URL} (automatic replication, no version)",
        "team channel webhook for f42-scheduled-asks, f42-agent only; Albert sets the value",
        lambda: cloud.create_secret(CHANNEL_WEBHOOK_URL))
    row(state.connection is not None, "connection", f"{CONNECTION} (CLOUD_RESOURCE, {BQ_LOCATION})", "remote embedding model",
        cloud.create_connection)
    row(state.pool, "WIF pool", POOL, "GitHub Actions deploys", cloud.create_pool)
    row(state.provider, "OIDC provider", PROVIDER, "accepts pushes to full-42 of this repository only", cloud.create_provider)
    if state.provider and state.condition != ATTRIBUTE_CONDITION:
        if condition_narrows(state.condition):
            rows.append(Row("UPDATE", "-", "OIDC provider condition", PROVIDER, "narrows who can sign in as f42-deployer",
                            action=cloud.update_provider_condition))
        else:
            rows.append(Row("DIFFERS", "-", "OIDC provider condition", PROVIDER,
                            "new condition does not only narrow the live one; Albert changes it by hand"))
    return rows


def print_condition(state):
    if state.provider and state.condition != ATTRIBUTE_CONDITION:
        print(f"\nOIDC provider {PROVIDER} condition\n  before: {state.condition}\n  after:  {ATTRIBUTE_CONDITION}")


def print_rows(title, rows):
    print(f"\n{title}")
    print(f"{'STATUS':<8} {'IDENTITY':<30} {'ROLE':<40} {'RESOURCE':<50} REASON")
    for r in rows:
        print(f"{r.status:<8} {r.identity:<30} {r.role:<40} {r.resource:<50} {r.reason}")


def grant_with_retry(cloud, b):
    for attempt in range(GRANT_RETRIES + 1):
        try:
            cloud.add_binding(b.kind, b.name, b.role, b.member)
            return
        except CloudError as e:
            if attempt == GRANT_RETRIES or "does not exist" not in str(e).lower():
                raise
            print(f"  waiting {GRANT_WAIT_SECONDS}s for the new account to reach IAM ({attempt + 1}/{GRANT_RETRIES})")
            time.sleep(GRANT_WAIT_SECONDS)


def preflight(cloud):
    """Return (refusal reason, account). The script must run as Albert's own user login."""
    if os.environ.get(IMPERSONATE_ENV):
        return f"{IMPERSONATE_ENV} is set; run from a terminal without it so gcloud acts as your own login", None
    try:
        if cloud.impersonation():
            return "gcloud config auth/impersonate_service_account is set; unset it so gcloud acts as your own login", None
        account = cloud.account()
    except (CloudError, OSError) as e:
        return f"cannot read the gcloud login: {e}", None
    if not account:
        return "no active gcloud account; run gcloud auth login with your own user first", None
    if account.endswith(".gserviceaccount.com"):
        return f"the active gcloud account {account} is a service account; switch to your own user login", None
    return None, account


def main(argv=None, cloud=None):
    parser = argparse.ArgumentParser(description="Add-only IAM bootstrap for 42 staging (docs/full-42/SETUP.md).")
    parser.add_argument("--apply", action="store_true", help="make the changes; without it this is a dry run")
    parser.add_argument("--iap-group", help="group:NAME (or domain:NAME) that gets IAP-secured Web App User")
    args = parser.parse_args(argv)

    if args.iap_group and not args.iap_group.startswith(("group:", "domain:")):
        print("Refusing: --iap-group must look like group:NAME or domain:NAME")
        return 2
    cloud = cloud or RealCloud()
    reason, account = preflight(cloud)
    if reason:
        print(f"Refusing: {reason}")
        return 2
    print(f"Acting as {account} on project {PROJECT} ({'apply' if args.apply else 'dry run'})")

    step = "reading state"
    try:
        state = gather(cloud, account)
        check_roles(desired_bindings(state, args.iap_group))
        resources = resource_rows(state, cloud)
        print_rows("Plan", resources + binding_rows(state, args.iap_group))
        print_condition(state)
        if GMAIL_APP_PASSWORD not in state.secrets:
            print(f"\n{GMAIL_APP_PASSWORD} does not exist yet, so its secretAccessor grant to f42-agent is not planned. "
                  "Albert creates the secret and adds the app password, then re-runs with --apply.")
        if not args.apply:
            print("\nDry run: nothing changed. Run again with --apply to make the CREATE, UPDATE and GRANT rows.")
            return 0

        print("\nApplying")
        for row in resources:
            if row.status in ("CREATE", "UPDATE"):
                step = f"CREATE {row.role} {row.resource}"
                print(step)
                row.action()
        step = "reading state after creates"
        state = gather(cloud, account)
        for row in binding_rows(state, args.iap_group):
            if row.status == "GRANT":
                b = row.binding
                step = f"GRANT {b.role} to {b.member} on {row.resource}"
                print(step)
                grant_with_retry(cloud, b)
        step = "reading back"
        state = gather(cloud, account)
    except CloudError as e:
        print(f"FAILED: {step}: {e}")
        return 1

    rows = binding_rows(state, args.iap_group, readback=True)
    print_rows("Readback", rows)
    counts = Counter(r.status for r in rows)
    print(f"\n{len(rows)} expected bindings: {counts['PRESENT']} present, {counts['MISSING']} missing, "
          f"{counts['PENDING']} pending, {counts['SKIPPED']} skipped, {counts['WAITING']} waiting")
    condition_ok = state.condition == ATTRIBUTE_CONDITION
    print(f"OIDC provider condition: {'matches' if condition_ok else 'DIFFERS'}")
    print_condition(state)
    if counts["WAITING"]:
        print("Re-run with --apply --iap-group group:NAME once the Ogilvy group is named.")
    print("Next: the smoke job per identity (SETUP.md, The bootstrap script); bootstrap is done only when each passes.")
    print("After task 1.2 creates the core tables, re-run with --apply to add the PENDING table grants.")
    return 0 if counts["MISSING"] == 0 and condition_ok else 1


def bucket_body(name):
    """The media bucket: uniform access, public access prevention, 30-day soft delete, no lifecycle rules."""
    return {
        "name": name,
        "location": REGION.upper(),
        "iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True}, "publicAccessPrevention": "enforced"},
        "softDeletePolicy": {"retentionDurationSeconds": str(SOFT_DELETE_DAYS * 86400)},
    }


def policy_pairs(policy):
    """(role, member) pairs of the unconditional bindings; a conditional or expiring grant does not count."""
    return {(b["role"], m) for b in (policy or {}).get("bindings", []) if not b.get("condition") for m in b.get("members", [])}


def merge_policy(old, role, member):
    """Return a copy of old with member added to role. Keeps every other binding and the etag."""
    new = copy.deepcopy(old)
    bindings = new.setdefault("bindings", [])
    for b in bindings:
        if b.get("role") == role and not b.get("condition"):
            members = b.setdefault("members", [])
            if member not in members:
                members.append(member)
            return new
    bindings.append({"role": role, "members": [member]})
    return new


def check_keeps(old, new):
    """Raise unless every (role, member, condition) in old is still in new and the etag is unchanged."""
    def triples(policy):
        return {(b.get("role"), m, json.dumps(b.get("condition"), sort_keys=True))
                for b in policy.get("bindings", []) for m in b.get("members", [])}
    lost = triples(old) - triples(new)
    if lost:
        raise ValueError(f"policy change would drop {len(lost)} existing member(s); nothing sent")
    if old.get("etag") != new.get("etag"):
        raise ValueError("policy change lost the fetched etag; nothing sent")


class Cloud:
    """The only way plan, apply and readback touch Google Cloud. RealCloud implements it; tests use a fake."""

    # reads
    def account(self): raise NotImplementedError
    def impersonation(self): raise NotImplementedError
    def project_number(self): raise NotImplementedError
    def enabled_apis(self): raise NotImplementedError
    def service_accounts(self): raise NotImplementedError
    def dataset_location(self, name): raise NotImplementedError
    def table_exists(self, dataset, table): raise NotImplementedError
    def bucket_exists(self, name): raise NotImplementedError
    def secret_names(self): raise NotImplementedError
    def connection(self): raise NotImplementedError
    def pool_exists(self): raise NotImplementedError
    def provider_condition(self): raise NotImplementedError
    def policy(self, kind, name): raise NotImplementedError

    # writes, all additive
    def enable_apis(self, apis): raise NotImplementedError
    def create_service_account(self, account_id): raise NotImplementedError
    def create_dataset(self, name): raise NotImplementedError
    def create_bucket(self, name): raise NotImplementedError
    def create_secret(self, name): raise NotImplementedError
    def create_connection(self): raise NotImplementedError
    def create_pool(self): raise NotImplementedError
    def create_provider(self): raise NotImplementedError
    def update_provider_condition(self): raise NotImplementedError
    def generate_iap_identity(self): raise NotImplementedError
    def add_binding(self, kind, name, role, member): raise NotImplementedError


class RealCloud(Cloud):
    """gcloud for most calls; BigQuery and REST with the same gcloud user's token, held in memory only."""

    def __init__(self, gcloud=None):
        self.gcloud = gcloud or shutil.which("gcloud") or "gcloud"
        self._token = None
        self._client = None

    def _run(self, *args):
        # stdin closed: a gcloud prompt fails at once instead of waiting silently for an answer.
        proc = subprocess.run([self.gcloud, *args], stdin=subprocess.DEVNULL, capture_output=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            lines = [line.strip() for line in (proc.stderr or "").splitlines() if line.strip()]
            raise CloudError(lines[-1] if lines else f"gcloud {args[0]} exited with {proc.returncode}")
        return proc.stdout or ""

    def _json(self, *args):
        out = self._run(*args, "--format=json").strip()
        return json.loads(out) if out else None

    def _credentials(self):
        if self._token is None:
            self._token = self._run("auth", "print-access-token").strip()
        return Credentials(token=self._token)

    def _bq(self):
        if self._client is None:
            self._client = bigquery.Client(project=PROJECT, credentials=self._credentials())
        return self._client

    def _query(self, sql, location):
        try:
            self._bq().query(sql, location=location).result()
        except GoogleAPICallError as e:
            raise CloudError(str(e).strip().splitlines()[-1]) from None

    def _rest(self, method, url, body=None):
        self._credentials()
        headers = {"Authorization": "Bearer " + self._token, "x-goog-user-project": PROJECT}
        try:
            resp = requests.request(method, url, headers=headers, json=body, timeout=60)
        except requests.exceptions.RequestException as e:
            # Type and host only: the exception text is never shown, so no header can leak.
            raise CloudError(f"network error ({type(e).__name__}) calling {url.split('/')[2]}") from None
        if method == "GET" and resp.status_code == 404:
            return None
        try:
            data = json.loads(resp.text) if resp.text else {}
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            message = ((data.get("error") or {}).get("message") or "").strip() if isinstance(data, dict) else ""
            raise CloudError(f"HTTP {resp.status_code}: {message.splitlines()[-1] if message else 'no message'}")
        return data

    # reads

    def account(self):
        active = self._json("auth", "list", "--filter=status:ACTIVE") or []
        return active[0].get("account", "") if active else ""

    def impersonation(self):
        value = self._run("config", "get", "auth/impersonate_service_account").strip()
        return "" if value in ("", "(unset)") else value

    def project_number(self):
        return str(self._json("projects", "describe", PROJECT)["projectNumber"])

    def enabled_apis(self):
        services = self._json("services", "list", "--enabled", f"--project={PROJECT}") or []
        return {(s.get("config") or {}).get("name") or s["name"].split("/")[-1] for s in services}

    def service_accounts(self):
        return {a["email"] for a in self._json("iam", "service-accounts", "list", f"--project={PROJECT}") or []}

    def dataset_location(self, name):
        try:
            return self._bq().get_dataset(f"{PROJECT}.{name}").location
        except NotFound:
            return None

    def table_exists(self, dataset, table):
        try:
            self._bq().get_table(f"{PROJECT}.{dataset}.{table}")
            return True
        except NotFound:
            return False

    def bucket_exists(self, name):
        buckets = self._json("storage", "buckets", "list", f"--project={PROJECT}") or []
        return any(b.get("name") == name or b.get("storage_url", "").rstrip("/") == f"gs://{name}" for b in buckets)

    def secret_names(self):
        # Secret metadata only; values are never read.
        return {s["name"].split("/")[-1] for s in self._json("secrets", "list", f"--project={PROJECT}") or []}

    def connection(self):
        data = self._rest("GET", f"{CONNECTION_URL}/{CONNECTION}")
        if data is None:
            return None
        return {"service_account": (data.get("cloudResource") or {}).get("serviceAccountId", "")}

    def pool_exists(self):
        pools = self._json("iam", "workload-identity-pools", "list", "--location=global", f"--project={PROJECT}") or []
        return any(p["name"].endswith("/" + POOL) and p.get("state") != "DELETED" for p in pools)

    def provider_condition(self):
        """The live attribute condition, "" when it has none, None when the provider does not exist."""
        providers = self._json("iam", "workload-identity-pools", "providers", "list", f"--workload-identity-pool={POOL}",
                               "--location=global", f"--project={PROJECT}") or []
        for p in providers:
            if p["name"].endswith("/" + PROVIDER) and p.get("state") != "DELETED":
                return p.get("attributeCondition", "")
        return None

    def _connection_policy(self):
        return self._rest("POST", f"{CONNECTION_URL}/{CONNECTION}:getIamPolicy",
                          {"options": {"requestedPolicyVersion": 3}}) or {}

    def policy(self, kind, name):
        p = f"--project={PROJECT}"
        if kind == "project":
            return policy_pairs(self._json("projects", "get-iam-policy", PROJECT))
        if kind == "sa":
            return policy_pairs(self._json("iam", "service-accounts", "get-iam-policy", name, p))
        if kind == "secret":
            return policy_pairs(self._json("secrets", "get-iam-policy", name, p))
        if kind == "bucket":
            return policy_pairs(self._json("storage", "buckets", "get-iam-policy", f"gs://{name}", p))
        if kind == "repo":
            return policy_pairs(self._json("artifacts", "repositories", "get-iam-policy", name, f"--location={REGION}", p))
        if kind == "connection":
            return policy_pairs(self._connection_policy())
        if kind == "table":
            return policy_pairs({"bindings": list(self._bq().get_iam_policy(f"{PROJECT}.{name}").bindings)})
        if kind == "dataset":
            pairs = set()
            for entry in self._bq().get_dataset(f"{PROJECT}.{name}").access_entries:
                role = BASIC_DATASET_ROLES.get(entry.role, entry.role)
                if not role:
                    continue
                if entry.entity_type == "userByEmail":
                    kind_prefix = "serviceAccount:" if entry.entity_id.endswith(".gserviceaccount.com") else "user:"
                    pairs.add((role, kind_prefix + entry.entity_id))
                elif entry.entity_type == "groupByEmail":
                    pairs.add((role, "group:" + entry.entity_id))
                elif entry.entity_type == "iamMember":
                    pairs.add((role, entry.entity_id))
            return pairs
        raise ValueError(kind)

    # writes, all additive

    def enable_apis(self, apis):
        self._run("services", "enable", *apis, f"--project={PROJECT}", "--format=none")

    def create_service_account(self, account_id):
        self._run("iam", "service-accounts", "create", account_id, f"--display-name={account_id}",
                  f"--project={PROJECT}", "--format=none")

    def create_dataset(self, name):
        # No expiry option on purpose: expiry is deletion.
        self._query(f'CREATE SCHEMA IF NOT EXISTS `{PROJECT}.{name}` OPTIONS(location="{BQ_LOCATION}")', BQ_LOCATION)

    def create_bucket(self, name):
        # Storage JSON API rather than gcloud, so no command line ever carries a delete word, even the soft one.
        self._rest("POST", f"https://storage.googleapis.com/storage/v1/b?project={PROJECT}", bucket_body(name))

    def create_secret(self, name):
        # The empty container only. No version is ever added here.
        self._run("secrets", "create", name, "--replication-policy=automatic", f"--project={PROJECT}", "--format=none")

    def create_connection(self):
        self._rest("POST", f"{CONNECTION_URL}?connectionId={CONNECTION}", {"cloudResource": {}})

    def create_pool(self):
        self._run("iam", "workload-identity-pools", "create", POOL, "--location=global", f"--display-name={POOL}",
                  f"--project={PROJECT}", "--format=none")

    def create_provider(self):
        self._run("iam", "workload-identity-pools", "providers", "create-oidc", PROVIDER, "--location=global",
                  f"--workload-identity-pool={POOL}", f"--issuer-uri={ISSUER}", f"--attribute-mapping={ATTRIBUTE_MAPPING}",
                  f"--attribute-condition={ATTRIBUTE_CONDITION}", f"--project={PROJECT}", "--format=none")

    def update_provider_condition(self):
        # One PATCH with an update mask of the condition alone; pool, provider, issuer and mapping stay as they are.
        self._rest("PATCH", PROVIDER_URL + "?updateMask=attributeCondition", {"attributeCondition": ATTRIBUTE_CONDITION})

    def generate_iap_identity(self):
        self._rest("POST", f"https://serviceusage.googleapis.com/v1beta1/projects/{PROJECT}/services/"
                           "iap.googleapis.com:generateServiceIdentity", {})

    def add_binding(self, kind, name, role, member):
        m, r, c, p = f"--member={member}", f"--role={role}", "--condition=None", f"--project={PROJECT}"
        if kind == "project":
            self._run("projects", "add-iam-policy-binding", PROJECT, m, r, c, "--format=none")
        elif kind == "sa":
            self._run("iam", "service-accounts", "add-iam-policy-binding", name, m, r, c, p, "--format=none")
        elif kind == "secret":
            self._run("secrets", "add-iam-policy-binding", name, m, r, c, p, "--format=none")
        elif kind == "bucket":
            self._run("storage", "buckets", "add-iam-policy-binding", f"gs://{name}", m, r, c, p, "--format=none")
        elif kind == "repo":
            self._run("artifacts", "repositories", "add-iam-policy-binding", name, f"--location={REGION}", m, r, c, p,
                      "--format=none")
        elif kind == "dataset":
            location = self.dataset_location(name) or BQ_LOCATION
            self._query(f'GRANT `{role}` ON SCHEMA `{PROJECT}.{name}` TO "{member}"', location)
        elif kind == "table":
            location = self.dataset_location(name.split(".")[0]) or BQ_LOCATION
            self._query(f'GRANT `{role}` ON TABLE `{PROJECT}.{name}` TO "{member}"', location)
        elif kind == "connection":
            old = self._connection_policy()
            if not old.get("etag"):
                raise CloudError("connection policy came back without an etag; nothing sent")
            new = merge_policy(old, role, member)
            check_keeps(old, new)
            self._rest("POST", f"{CONNECTION_URL}/{CONNECTION}:setIamPolicy", {"policy": new})
        else:
            raise ValueError(kind)


if __name__ == "__main__":
    sys.exit(main())
